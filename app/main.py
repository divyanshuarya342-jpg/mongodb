"""
Employee Attendance & Analytics API
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB  (a local .env is loaded for convenience;
      real environment variables always win)
"""

import os
import re
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv()  # .env is convenient locally; real env vars already override it

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB  = os.environ.get("MONGO_DB",  "attendance_db")

client = MongoClient(MONGO_URI)
db     = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))

# Epoch-ms bounds from openapi.yaml EpochMillis schema
EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ABSENT_STATUSES   = {"ABSENT", "LEAVE"}
ALL_STATUSES      = PRESENCE_STATUSES | ABSENT_STATUSES

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# ---------------------------------------------------------------------------
# Indexes — created at startup, idempotent
# ---------------------------------------------------------------------------

@app.on_event("startup")
def create_indexes() -> None:
    # employees
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, background=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], background=True)

    # attendance_logs
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, background=True
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], background=True
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", ASCENDING)], background=True
    )
    db.attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)], background=True
    )


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def ms_to_dt(ms: int) -> datetime:
    """Epoch ms (truncated to whole seconds) → UTC-aware datetime."""
    return datetime.fromtimestamp(ms // 1000, tz=timezone.utc)


def dt_to_ms(dt: datetime) -> int:
    """Datetime (naive = UTC) → epoch milliseconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()) * 1000


def to_utc(dt: datetime) -> datetime:
    """Ensure datetime is UTC-aware (PyMongo returns naive UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def to_ist(dt: datetime) -> datetime:
    return to_utc(dt).astimezone(IST)


def now_utc() -> datetime:
    """Current UTC time, microseconds stripped (whole-second precision)."""
    return datetime.now(tz=timezone.utc).replace(microsecond=0)


# ---------------------------------------------------------------------------
# Business rules R1–R5
# ---------------------------------------------------------------------------

def attendance_date_for_punch(punch_ist: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: return YYYY-MM-DD attendance date in IST.
    Overnight shift (shift_end <= shift_start): a punch-in whose IST clock time
    is strictly earlier than shift_end belongs to the *previous* day's shift.
    """
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    overnight = (eh * 60 + em) <= (sh * 60 + sm)
    if overnight:
        # punch_ist.time() is naive but carries the correct IST clock time
        punch_time_mins = punch_ist.hour * 60 + punch_ist.minute
        shift_end_mins  = eh * 60 + em
        if punch_time_mins < shift_end_mins:
            return (punch_ist.date() - timedelta(days=1)).isoformat()
    return punch_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """
    R2: late only if punch-in > 10 min 0 sec after shift_start.
    late_minutes = floor(elapsed_seconds / 60) — measured from shift_start, not grace end.
    Examples (shift 09:30):
        09:40:00 → elapsed=600s  → 600 <= 600 → 0
        09:40:01 → elapsed=601s  → 601  > 600 → floor(601/60)=10
        10:15:59 → elapsed=2759s → floor(2759/60)=45
    """
    h, m = map(int, shift_start.split(":"))
    shift_start_utc = (
        datetime.fromisoformat(attendance_date)
        .replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
        .astimezone(timezone.utc)
    )
    pi = to_utc(punch_in_utc)
    elapsed = (pi - shift_start_utc).total_seconds()
    if elapsed <= 600:
        return 0
    return int(elapsed // 60)


def compute_overtime_minutes(punch_out_utc: datetime, shift_end: str,
                              attendance_date: str, shift_start: str) -> int:
    """
    R3: floor(minutes from shift_end to punch_out), only if >= 30; else 0.
    For overnight shifts shift_end is on the NEXT calendar day.
    """
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))
    overnight = (eh * 60 + em) <= (sh * 60 + sm)
    base = datetime.fromisoformat(attendance_date).replace(
        hour=eh, minute=em, second=0, microsecond=0, tzinfo=IST
    )
    if overnight:
        base = base + timedelta(days=1)
    shift_end_utc = base.astimezone(timezone.utc)
    po = to_utc(punch_out_utc)
    over = (po - shift_end_utc).total_seconds()
    if over < 0:
        return 0
    mins = int(over // 60)
    return mins if mins >= 30 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) seconds / 3600, rounded 2 decimals HALF-UP."""
    seconds = Decimal(str(int((to_utc(punch_out_utc) - to_utc(punch_in_utc)).total_seconds())))
    return float((seconds / Decimal("3600")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day when rounded work_hours < 4.50."""
    return work_hours < 4.50


def round2(v: float) -> float:
    return float(Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round4(v: float) -> float:
    return float(Decimal(str(v)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def serialize_record(doc: dict) -> dict:
    """Raw MongoDB attendance_logs doc → API-shape dict."""
    doc.pop("_id", None)

    def _ms(v):
        return dt_to_ms(v) if isinstance(v, datetime) else v

    doc["punch_in"]  = _ms(doc.get("punch_in"))
    doc["punch_out"] = _ms(doc.get("punch_out"))
    doc.setdefault("late_minutes",     0)
    doc.setdefault("overtime_minutes", 0)
    doc.setdefault("half_day",         False)
    doc.setdefault("history",          [])

    for entry in doc["history"]:
        if isinstance(entry.get("at"), datetime):
            entry["at"] = dt_to_ms(entry["at"])
        for field in ("punch_in", "punch_out"):
            ch = entry.get("changes", {}).get(field)
            if ch:
                if isinstance(ch.get("from"), datetime):
                    ch["from"] = dt_to_ms(ch["from"])
                if isinstance(ch.get("to"), datetime):
                    ch["to"] = dt_to_ms(ch["to"])
    return doc


def serialize_employee(doc: dict) -> dict:
    doc.pop("_id", None)
    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = dt_to_ms(doc["created_at"])
    return doc


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class EmployeeCreate(BaseModel):
    emp_code:    str
    name:        str
    email:       str
    department:  str
    shift_start: str = "09:30"
    shift_end:   str = "18:30"
    joined_on:   str

    @field_validator("emp_code")
    @classmethod
    def v_emp_code(cls, v: str) -> str:
        if not re.match(r"^EMP\d{4,6}$", v):
            raise ValueError("emp_code must be EMP followed by 4-6 digits")
        return v

    @field_validator("name")
    @classmethod
    def v_name(cls, v: str) -> str:
        if not 1 <= len(v) <= 100:
            raise ValueError("name must be 1-100 chars")
        return v

    @field_validator("email")
    @classmethod
    def v_email(cls, v: str) -> str:
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v) or len(v) > 120:
            raise ValueError("invalid email")
        return v

    @field_validator("department")
    @classmethod
    def v_dept(cls, v: str) -> str:
        if not 1 <= len(v) <= 50:
            raise ValueError("department must be 1-50 chars")
        return v

    @field_validator("shift_start", "shift_end")
    @classmethod
    def v_shift(cls, v: str) -> str:
        if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v):
            raise ValueError("shift time must be HH:MM (24-hour)")
        return v

    @field_validator("joined_on")
    @classmethod
    def v_joined(cls, v: str) -> str:
        datetime.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def v_shifts_differ(self) -> "EmployeeCreate":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


def _validate_epoch_ms(v):
    if v is None:
        return v
    if not isinstance(v, int) or isinstance(v, bool):
        raise ValueError("must be an integer epoch milliseconds value")
    if not (EPOCH_MS_MIN <= v <= EPOCH_MS_MAX):
        raise ValueError(f"epoch ms out of range [{EPOCH_MS_MIN}, {EPOCH_MS_MAX}]")
    return v


class PunchInRequest(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None
    status:     str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def v_ts(cls, v): return _validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def v_status(cls, v: str) -> str:
        if v not in PRESENCE_STATUSES:
            raise ValueError(f"status must be one of {sorted(PRESENCE_STATUSES)}")
        return v


class PunchOutRequest(BaseModel):
    emp_code:   str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def v_ts(cls, v): return _validate_epoch_ms(v)


class RegularizeRequest(BaseModel):
    status:         Optional[str] = None
    punch_in:       Optional[int] = None
    punch_out:      Optional[int] = None
    reason:         str
    regularized_by: str

    @field_validator("punch_in", "punch_out")
    @classmethod
    def v_ts(cls, v): return _validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def v_status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError("invalid status")
        return v

    @field_validator("reason")
    @classmethod
    def v_reason(cls, v: str) -> str:
        if not 5 <= len(v) <= 200:
            raise ValueError("reason must be 5-200 chars")
        return v

    @field_validator("regularized_by")
    @classmethod
    def v_by(cls, v: str) -> str:
        if not 1 <= len(v) <= 50:
            raise ValueError("regularized_by must be 1-50 chars")
        return v


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        return JSONResponse(status_code=503, content={"detail": "MongoDB unavailable"})


# ---------------------------------------------------------------------------
# /employees
# ---------------------------------------------------------------------------

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page:       int = Query(default=1,  ge=1),
    page_size:  int = Query(default=20, ge=1, le=100),
):
    q: dict = {}
    if department:
        q["department"] = department
    total = db.employees.count_documents(q)
    skip  = (page - 1) * page_size
    items = [
        serialize_employee(d)
        for d in db.employees.find(q, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    ]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---------------------------------------------------------------------------
# /attendance/punch-in
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, f"Employee {body.emp_code!r} not found")

    punch_utc = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    punch_ist = to_ist(punch_utc)

    att_date = attendance_date_for_punch(punch_ist, emp["shift_start"], emp["shift_end"])
    late     = compute_late_minutes(punch_utc, emp["shift_start"], att_date)

    doc = {
        "emp_code":         body.emp_code,
        "date":             att_date,
        "status":           body.status,
        "punch_in":         punch_utc,
        "punch_out":        None,
        "work_hours":       None,
        "late_minutes":     late,
        "overtime_minutes": 0,
        "half_day":         False,
        "history":          [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, f"Already punched in for {att_date}")

    return serialize_record(doc)


# ---------------------------------------------------------------------------
# /attendance/punch-out
# ---------------------------------------------------------------------------

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, f"Employee {body.emp_code!r} not found")

    punch_utc = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # Most recent open record with punch_in <= punched_at
    record = db.attendance_logs.find_one(
        {
            "emp_code":  body.emp_code,
            "punch_in":  {"$lte": punch_utc},
            "punch_out": None,
            "status":    {"$in": list(PRESENCE_STATUSES)},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if record is None:
        # Already punched out → 409; otherwise → 404
        closed = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$lte": punch_utc},
                "status":   {"$in": list(PRESENCE_STATUSES)},
            },
            sort=[("punch_in", DESCENDING)],
        )
        if closed and closed.get("punch_out") is not None:
            raise HTTPException(409, "Record already punched out")
        raise HTTPException(404, "No open punch-in record found")

    pi_utc = to_utc(record["punch_in"])
    diff   = (punch_utc - pi_utc).total_seconds()
    if diff <= 0:
        raise HTTPException(422, "punched_at must be after punch_in")
    if diff > 86400:
        raise HTTPException(422, "punched_at must be within 24 hours of punch_in")

    wh   = compute_work_hours(pi_utc, punch_utc)
    ot   = compute_overtime_minutes(punch_utc, emp["shift_end"], record["date"], emp["shift_start"])
    half = compute_half_day(wh)

    # Race-safe: guard punch_out=None so only one concurrent winner
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {
            "punch_out":        punch_utc,
            "work_hours":       wh,
            "overtime_minutes": ot,
            "half_day":         half,
        }},
        return_document=True,
    )
    if updated is None:
        raise HTTPException(409, "Record already punched out (concurrent request)")

    return serialize_record(updated)


# ---------------------------------------------------------------------------
# GET /attendance
# ---------------------------------------------------------------------------

@app.get("/attendance")
def list_attendance(
    emp_code:  Optional[str] = None,
    date_from: Optional[str] = None,
    date_to:   Optional[str] = None,
    status:    Optional[str] = None,
    page:      int = Query(default=1,  ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    if status and status not in ALL_STATUSES:
        raise HTTPException(422, f"Invalid status: {status}")

    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from: q["date"]["$gte"] = date_from
        if date_to:   q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip  = (page - 1) * page_size
    docs  = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items":     [serialize_record(d) for d in docs],
        "total":     total,
        "page":      page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------------
# PATCH /attendance/{emp_code}/{date}
# ---------------------------------------------------------------------------

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, f"Employee {emp_code!r} not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if record is None:
        raise HTTPException(404, f"No attendance record for {emp_code} on {date}")

    new_status    = body.status   if body.status    is not None else record["status"]
    new_punch_in  = ms_to_dt(body.punch_in)  if body.punch_in  is not None else record.get("punch_in")
    new_punch_out = ms_to_dt(body.punch_out) if body.punch_out is not None else record.get("punch_out")

    # ABSENT/LEAVE must not have punch times
    if new_status in ABSENT_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "punch_in/punch_out must not be supplied with ABSENT or LEAVE")
        new_punch_in  = None
        new_punch_out = None

    # Presence requires punch_in
    if new_status in PRESENCE_STATUSES and new_punch_in is None:
        raise HTTPException(422, "punch_in is required for presence status")

    if new_punch_in is not None:
        new_punch_in = to_utc(new_punch_in)
        # R1: punch_in must stay on the record's attendance date
        pi_ist = to_ist(new_punch_in)
        if attendance_date_for_punch(pi_ist, emp["shift_start"], emp["shift_end"]) != date:
            raise HTTPException(422, "punch_in IST date does not match the record's attendance date")

    if new_punch_out is not None:
        new_punch_out = to_utc(new_punch_out)
        if new_punch_in is None:
            raise HTTPException(422, "punch_out requires punch_in")
        diff = (new_punch_out - new_punch_in).total_seconds()
        if diff <= 0:
            raise HTTPException(422, "punch_out must be after punch_in")
        if diff > 86400:
            raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # Recompute derived fields
    if new_status in PRESENCE_STATUSES:
        new_late = compute_late_minutes(new_punch_in, emp["shift_start"], date)
        if new_punch_out is not None:
            new_wh   = compute_work_hours(new_punch_in, new_punch_out)
            new_ot   = compute_overtime_minutes(new_punch_out, emp["shift_end"], date, emp["shift_start"])
            new_half = compute_half_day(new_wh)
        else:
            new_wh, new_ot, new_half = None, 0, False
    else:
        new_late, new_wh, new_ot, new_half = 0, None, 0, False

    # Gather old values (normalised)
    old_pi  = to_utc(record["punch_in"])  if record.get("punch_in")  is not None else None
    old_po  = to_utc(record["punch_out"]) if record.get("punch_out") is not None else None
    old_wh  = record.get("work_hours")
    old_lat = record.get("late_minutes",     0)
    old_ot  = record.get("overtime_minutes", 0)
    old_hd  = record.get("half_day",         False)
    old_st  = record["status"]

    # Build changes dict — only fields that actually changed
    changes: dict = {}
    if old_st  != new_status:    changes["status"]           = {"from": old_st,  "to": new_status}
    if old_pi  != new_punch_in:  changes["punch_in"]         = {
        "from": dt_to_ms(old_pi)       if old_pi       is not None else None,
        "to":   dt_to_ms(new_punch_in) if new_punch_in is not None else None,
    }
    if old_po  != new_punch_out: changes["punch_out"]        = {
        "from": dt_to_ms(old_po)        if old_po        is not None else None,
        "to":   dt_to_ms(new_punch_out) if new_punch_out is not None else None,
    }
    if old_wh  != new_wh:        changes["work_hours"]       = {"from": old_wh,  "to": new_wh}
    if old_lat != new_late:      changes["late_minutes"]     = {"from": old_lat, "to": new_late}
    if old_ot  != new_ot:        changes["overtime_minutes"] = {"from": old_ot,  "to": new_ot}
    if old_hd  != new_half:      changes["half_day"]         = {"from": old_hd,  "to": new_half}

    if not changes:
        raise HTTPException(422, "Request changes nothing")

    # Build DB-side history entry (punch times stored as BSON datetimes)
    db_changes: dict = {}
    for k, v in changes.items():
        if k in ("punch_in", "punch_out"):
            db_changes[k] = {
                "from": ms_to_dt(v["from"]) if v["from"] is not None else None,
                "to":   ms_to_dt(v["to"])   if v["to"]   is not None else None,
            }
        else:
            db_changes[k] = v

    history_entry_db = {
        "at":      now_utc(),
        "by":      body.regularized_by,
        "reason":  body.reason,
        "changes": db_changes,
    }

    # Optimistic concurrency: match current history length so concurrent patches fail
    expected_history_len = len(record.get("history") or [])

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id":   record["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, expected_history_len]},
        },
        {
            "$set": {
                "status":           new_status,
                "punch_in":         new_punch_in,
                "punch_out":        new_punch_out,
                "work_hours":       new_wh,
                "late_minutes":     new_late,
                "overtime_minutes": new_ot,
                "half_day":         new_half,
            },
            "$push": {"history": history_entry_db},
        },
        return_document=True,
    )
    if updated is None:
        raise HTTPException(409, "Concurrent modification — please retry")

    return serialize_record(updated)


# ---------------------------------------------------------------------------
# GET /analytics/employees/{emp_code}/monthly
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month:    str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, f"Employee {emp_code!r} not found")

    year, mon = map(int, month.split("-"))
    last_day  = monthrange(year, mon)[1]
    m_start   = f"{year:04d}-{mon:02d}-01"
    m_end     = f"{year:04d}-{mon:02d}-{last_day:02d}"

    # R7: working days from max(joined_on, month_start) to month_end
    count_from    = max(emp["joined_on"], m_start)
    working_days  = _count_working_days(count_from, m_end)

    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": m_start, "$lte": m_end}}},
        {"$addFields": {
            "is_weekday": {"$in": [
                {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                [2, 3, 4, 5, 6],
            ]},
            "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            "lm": {"$ifNull": ["$late_minutes",     0]},
            "om": {"$ifNull": ["$overtime_minutes", 0]},
            "hd": {"$ifNull": ["$half_day",         False]},
        }},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": ["$is_presence", "$is_weekday"]},
                    {"$cond": ["$hd", 0.5, 1.0]},
                    0.0,
                ]
            }},
            "leave_days":             {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count":             {"$sum": {"$cond": [{"$gt": ["$lm", 0]}, 1, 0]}},
            "total_late_minutes":     {"$sum": "$lm"},
            "total_overtime_minutes": {"$sum": "$om"},
        }},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))
    if rows:
        r = rows[0]
        present_days           = r["present_days"]
        leave_days             = int(r["leave_days"])
        late_count             = int(r["late_count"])
        total_late_minutes     = int(r["total_late_minutes"])
        total_overtime_minutes = int(r["total_overtime_minutes"])
    else:
        present_days = leave_days = late_count = total_late_minutes = total_overtime_minutes = 0

    present_days   = round2(present_days)
    attendance_pct = round2(present_days / working_days * 100) if working_days > 0 else None

    return {
        "emp_code":               emp_code,
        "month":                  month,
        "working_days":           working_days,
        "present_days":           present_days,
        "leave_days":             leave_days,
        "late_count":             late_count,
        "total_late_minutes":     total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct":         attendance_pct,
    }


# ---------------------------------------------------------------------------
# GET /analytics/departments/summary
# ---------------------------------------------------------------------------

@app.get("/analytics/departments/summary")
def department_summary(
    month:      str           = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    year, mon = map(int, month.split("-"))
    last_day  = monthrange(year, mon)[1]
    m_start   = f"{year:04d}-{mon:02d}-01"
    m_end     = f"{year:04d}-{mon:02d}-{last_day:02d}"

    emp_match: dict = {"joined_on": {"$lte": m_end}}
    if department:
        emp_match["department"] = department

    pipeline = [
        # Start from employees so zero-log staff are counted (R9)
        {"$match": emp_match},
        {"$lookup": {
            "from":     "attendance_logs",
            "let":      {"ec": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {
                    "$and": [
                        {"$eq":  ["$emp_code", "$$ec"]},
                        {"$gte": ["$date",     m_start]},
                        {"$lte": ["$date",     m_end]},
                    ]
                }}},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmpty": True}},
        {"$group": {
            "_id":      "$department",
            "headcount": {"$addToSet": "$emp_code"},
            "present_days": {"$sum": {
                "$cond": [
                    {"$and": [
                        {"$ne":  [{"$ifNull": ["$logs", None]}, None]},
                        {"$in":  [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$in":  [
                            {"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", "2000-01-01"]}}}},
                            [2, 3, 4, 5, 6],
                        ]},
                    ]},
                    {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1.0]},
                    0.0,
                ]
            }},
            # Collect work_hours only from presence records that have a non-null value
            "wh_list": {"$push": {
                "$cond": [
                    {"$and": [
                        {"$ne":  [{"$ifNull": ["$logs", None]}, None]},
                        {"$in":  [{"$ifNull": ["$logs.status", ""]}, ["PRESENT", "WFH", "ON_DUTY"]]},
                        {"$ne":  [{"$ifNull": ["$logs.work_hours", None]}, None]},
                    ]},
                    "$logs.work_hours",
                    "$$REMOVE",
                ]
            }},
            "late_count":         {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes",    0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count":        {"$sum": {"$cond": [{"$eq": [{"$ifNull": ["$logs.status", ""]}, "LEAVE"]},    1, 0]}},
            "on_duty_count":      {"$sum": {"$cond": [{"$eq": [{"$ifNull": ["$logs.status", ""]}, "ON_DUTY"]},  1, 0]}},
        }},
        {"$project": {
            "_id": 0,
            "department":    "$_id",
            "headcount":     {"$size": "$headcount"},
            "present_days":  1,
            "avg_work_hours": {
                "$cond": [
                    {"$gt": [{"$size": "$wh_list"}, 0]},
                    {"$avg": "$wh_list"},
                    None,
                ]
            },
            "late_count":         1,
            "total_late_minutes": 1,
            "leave_count":        1,
            "on_duty_count":      1,
        }},
        {"$match": {"headcount": {"$gt": 0}}},
        {"$sort":  {"department": ASCENDING}},
    ]

    rows  = list(db.employees.aggregate(pipeline))
    items = [
        {
            "department":         r["department"],
            "headcount":          r["headcount"],
            "present_days":       round2(r["present_days"]),
            "avg_work_hours":     round2(r["avg_work_hours"]) if r["avg_work_hours"] is not None else None,
            "late_count":         r["late_count"],
            "total_late_minutes": r["total_late_minutes"],
            "leave_count":        r["leave_count"],
            "on_duty_count":      r["on_duty_count"],
        }
        for r in rows
    ]
    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# GET /analytics/leaderboard/late
# ---------------------------------------------------------------------------

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month:      str           = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit:      int           = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    year, mon = map(int, month.split("-"))
    last_day  = monthrange(year, mon)[1]
    m_start   = f"{year:04d}-{mon:02d}-01"
    m_end     = f"{year:04d}-{mon:02d}-{last_day:02d}"

    emp_match: dict = {}
    if department:
        emp_match["department"] = department

    pipeline = [
        {"$match": emp_match},
        {"$lookup": {
            "from":     "attendance_logs",
            "let":      {"ec": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {
                    "$and": [
                        {"$eq":  ["$emp_code", "$$ec"]},
                        {"$gte": ["$date",     m_start]},
                        {"$lte": ["$date",     m_end]},
                        {"$gt":  [{"$ifNull": ["$late_minutes", 0]}, 0]},
                    ]
                }}},
                {"$project": {"_id": 0, "late_minutes": {"$ifNull": ["$late_minutes", 0]}}},
            ],
            "as": "late_logs",
        }},
        # Keep only employees that were late at least once
        {"$match": {"late_logs.0": {"$exists": True}}},
        {"$project": {
            "_id":                0,
            "emp_code":           1,
            "name":               1,
            "department":         1,
            "total_late_minutes": {"$sum": "$late_logs.late_minutes"},
            "late_count":         {"$size": "$late_logs"},
        }},
        {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
        # Standard competition ranking: ties share rank, next rank skipped
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1, "emp_code": 1},
            "output": {"rank": {"$rank": {}}},
        }},
        # Apply limit AFTER ranking — so tied rows at the cutoff are all returned
        {"$match": {"rank": {"$lte": limit}}},
    ]

    rows  = list(db.employees.aggregate(pipeline))
    items = [
        {
            "rank":               r["rank"],
            "emp_code":           r["emp_code"],
            "name":               r["name"],
            "department":         r["department"],
            "total_late_minutes": r["total_late_minutes"],
            "late_count":         r["late_count"],
        }
        for r in rows
    ]
    return {"month": month, "items": items}


# ---------------------------------------------------------------------------
# GET /analytics/departments/{department}/trend
# ---------------------------------------------------------------------------

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date:  str = Query(..., alias="from"),
    to_date:    str = Query(..., alias="to"),
):
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, f"Department {department!r} not found")

    try:
        fd = datetime.fromisoformat(from_date).date()
        td = datetime.fromisoformat(to_date).date()
    except ValueError:
        raise HTTPException(422, "Invalid date format; use YYYY-MM-DD")

    if to_date < from_date:
        raise HTTPException(422, "'to' must not be before 'from'")
    if (td - fd).days > 92:
        raise HTTPException(422, "Date range must not exceed 92 days")

    # $densify requires literal datetime bounds
    from_dt = datetime(fd.year, fd.month, fd.day, tzinfo=timezone.utc)
    to_dt   = datetime(td.year, td.month, td.day, tzinfo=timezone.utc) + timedelta(days=1)

    pipeline = _trend_pipeline(department, from_date, to_date, from_dt, to_dt)
    raw_rows = list(db.employees.aggregate(pipeline))

    # Post-process: apply 4-decimal rounding to rates (MongoDB $avg may return high precision)
    items = []
    for r in raw_rows:
        ar  = r.get("attendance_rate")
        ma  = r.get("moving_avg_7d")
        items.append({
            "date":           r["date"],
            "is_working_day": r["is_working_day"],
            "headcount":      r["headcount"],
            "present_count":  r["present_count"],
            "late_count":     r["late_count"],
            "attendance_rate": round4(ar) if ar is not None else None,
            "moving_avg_7d":   round4(ma) if ma is not None else None,
        })
    return {"department": department, "items": items}


def _trend_pipeline(department: str, from_date: str, to_date: str,
                    from_dt: datetime, to_dt: datetime) -> list:
    """
    Full trend pipeline:
    1. Aggregate per-day present/late counts from attendance_logs (dept-filtered).
    2. $densify to produce one doc per calendar day.
    3. Per-day headcount via $lookup.
    4. attendance_rate (working days with headcount>0 only).
    5. 7-day moving average via $setWindowFields.
    """
    return [
        # ── 1. Filter logs in range that belong to this department
        {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
        {"$lookup": {
            "from":         "employees",
            "localField":   "emp_code",
            "foreignField": "emp_code",
            "as":           "emp_info",
        }},
        {"$match": {"emp_info.department": department}},
        {"$addFields": {
            "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            "hd": {"$ifNull": ["$half_day",     False]},
            "lm": {"$ifNull": ["$late_minutes", 0]},
        }},

        # ── 2. Group by date
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {
                "$cond": [
                    "$is_presence",
                    {"$cond": ["$hd", 0.5, 1.0]},
                    0.0,
                ]
            }},
            "late_count": {"$sum": {"$cond": [{"$gt": ["$lm", 0]}, 1, 0]}},
        }},

        # ── 3. Convert string date → datetime for $densify
        {"$addFields": {
            "date_dt": {"$dateFromString": {"dateString": "$_id"}},
        }},

        # ── 4. Gap-fill: one doc per calendar day in [from, to]
        {"$densify": {
            "field": "date_dt",
            "range": {
                "step":   1,
                "unit":   "day",
                "bounds": [from_dt, to_dt],  # Python datetime literals — valid for $densify
            },
        }},

        # ── 5. Restore / default fields after densify inserts skeleton docs
        {"$addFields": {
            "date":           {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
            "present_count":  {"$ifNull": ["$present_count", 0.0]},
            "late_count":     {"$ifNull": ["$late_count",    0]},
            "is_working_day": {"$in": [{"$dayOfWeek": "$date_dt"}, [2, 3, 4, 5, 6]]},
        }},

        # ── 6. Headcount per day: employees in dept with joined_on <= date string
        {"$lookup": {
            "from":     "employees",
            "let":      {"day_str": "$date"},
            "pipeline": [
                {"$match": {
                    "department": department,
                    "$expr": {"$lte": ["$joined_on", "$$day_str"]},
                }},
                {"$count": "n"},
            ],
            "as": "hc_result",
        }},
        {"$addFields": {
            "headcount": {
                "$cond": [
                    {"$gt": [{"$size": "$hc_result"}, 0]},
                    {"$arrayElemAt": ["$hc_result.n", 0]},
                    0,
                ]
            },
        }},

        # ── 7. attendance_rate: working days with headcount > 0 only
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    {"$divide": ["$present_count", "$headcount"]},
                    None,
                ]
            },
        }},

        # ── 8. Sort for window function
        {"$sort": {"date": ASCENDING}},

        # ── 9. 7-day moving average over non-null attendance_rate values
        #       documents window [-6, 0] = current row + up to 6 preceding rows
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]},
                }
            },
        }},

        # ── 10. Final shape
        {"$project": {
            "_id":            0,
            "date":           1,
            "is_working_day": 1,
            "headcount":      1,
            "present_count":  1,
            "late_count":     1,
            "attendance_rate": 1,
            "moving_avg_7d":   1,
        }},
    ]


# ---------------------------------------------------------------------------
# GET /admin/explain/{endpoint}
# ---------------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint:   str,
    emp_code:   Optional[str] = None,
    month:      Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit:      int           = Query(default=10, ge=1, le=50),
    date_from:  Optional[str] = None,
    date_to:    Optional[str] = None,
    status:     Optional[str] = None,
    from_date:  Optional[str] = Query(default=None, alias="from"),
    to_date:    Optional[str] = Query(default=None, alias="to"),
    page:       int           = Query(default=1,  ge=1),
    page_size:  int           = Query(default=20, ge=1, le=100),
):
    valid = {"attendance_list", "employee_monthly", "department_summary",
             "late_leaderboard", "department_trend"}
    if endpoint not in valid:
        raise HTTPException(422, f"endpoint must be one of {sorted(valid)}")

    def _agg_explain(collection, pipeline: list) -> dict:
        """Run explain(executionStats) on an aggregation pipeline."""
        return db.command(
            "explain",
            {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )

    # ── attendance_list
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not be after date_to")
        q: dict = {}
        if emp_code:  q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to:   q["date"]["$lte"] = date_to
        if status:    q["status"] = status
        skip = (page - 1) * page_size
        explain_out = db.command(
            "explain",
            {
                "find":       "attendance_logs",
                "filter":     q,
                "sort":       {"date": -1, "emp_code": 1},
                "skip":       skip,
                "limit":      page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_out}

    # ── employee_monthly
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        year, mon = map(int, month.split("-"))
        last_day  = monthrange(year, mon)[1]
        m_start   = f"{year:04d}-{mon:02d}-01"
        m_end     = f"{year:04d}-{mon:02d}-{last_day:02d}"
        pipeline  = [{"$match": {"emp_code": emp_code, "date": {"$gte": m_start, "$lte": m_end}}}]
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": _agg_explain("attendance_logs", pipeline)}

    # ── department_summary
    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        year, mon = map(int, month.split("-"))
        last_day  = monthrange(year, mon)[1]
        m_end     = f"{year:04d}-{mon:02d}-{last_day:02d}"
        emp_match: dict = {"joined_on": {"$lte": m_end}}
        if department:
            emp_match["department"] = department
        pipeline = [{"$match": emp_match}]
        return {"endpoint": endpoint, "collection": "employees",
                "explain": _agg_explain("employees", pipeline)}

    # ── late_leaderboard
    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        year, mon = map(int, month.split("-"))
        last_day  = monthrange(year, mon)[1]
        m_start   = f"{year:04d}-{mon:02d}-01"
        m_end     = f"{year:04d}-{mon:02d}-{last_day:02d}"
        emp_match = {}
        if department:
            emp_match["department"] = department
        pipeline = [
            {"$match": emp_match},
            {"$lookup": {
                "from":     "attendance_logs",
                "let":      {"ec": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {
                        "$and": [
                            {"$eq":  ["$emp_code", "$$ec"]},
                            {"$gte": ["$date",     m_start]},
                            {"$lte": ["$date",     m_end]},
                            {"$gt":  [{"$ifNull": ["$late_minutes", 0]}, 0]},
                        ]
                    }}},
                ],
                "as": "late_logs",
            }},
            {"$match": {"late_logs.0": {"$exists": True}}},
        ]
        return {"endpoint": endpoint, "collection": "employees",
                "explain": _agg_explain("employees", pipeline)}

    # ── department_trend
    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from, and to are required")
        try:
            fd = datetime.fromisoformat(from_date).date()
            td_d = datetime.fromisoformat(to_date).date()
        except ValueError:
            raise HTTPException(422, "Invalid date format")
        if to_date < from_date:
            raise HTTPException(422, "'to' must not be before 'from'")
        if (td_d - fd).days > 92:
            raise HTTPException(422, "Range exceeds 92 days")
        from_dt = datetime(fd.year,   fd.month,   fd.day,   tzinfo=timezone.utc)
        to_dt   = datetime(td_d.year, td_d.month, td_d.day, tzinfo=timezone.utc) + timedelta(days=1)
        pipeline = _trend_pipeline(department, from_date, to_date, from_dt, to_dt)
        return {"endpoint": endpoint, "collection": "employees",
                "explain": _agg_explain("employees", pipeline)}

    raise HTTPException(422, "Unknown endpoint")


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _count_working_days(date_from_str: str, date_to_str: str) -> int:
    """Count Mon–Fri days between two YYYY-MM-DD strings, inclusive."""
    start = datetime.fromisoformat(date_from_str).date()
    end   = datetime.fromisoformat(date_to_str).date()
    if start > end:
        return 0
    # Fast formula: total days - weekends
    total  = (end - start).days + 1
    # Number of full weeks
    full_w = total // 7
    extra  = total % 7
    # weekdays in the partial week
    start_wd = start.weekday()  # 0=Mon
    extra_wd = sum(1 for i in range(extra) if (start_wd + i) % 7 < 5)
    return full_w * 5 + extra_wd
