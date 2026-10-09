# REVIEW.md — Part A: Starter Code Review

## Defects found and fixed

### 1. `/health` always returns 200 — never 503
**Location:** `health()` function  
**What's wrong:** No MongoDB ping is attempted; the endpoint always returns `{"status": "ok"}` even when the database is unreachable.  
**How to notice:** Stop MongoDB and call `GET /health` — still gets 200.  
**Fix:** Added `client.admin.command("ping")` inside a try/except; returns 503 on failure.

---

### 2. `POST /employees` — race condition on duplicate `emp_code`
**Location:** `create_employee()`  
**What's wrong:** Uses check-then-insert (`find_one` then `insert_one`). Two concurrent requests for the same `emp_code` can both pass the check and both receive 201.  
**How to notice:** Send two simultaneous `POST /employees` requests with the same `emp_code`.  
**Fix:** Removed the `find_one` check; rely on a unique index on `emp_code` and catch `DuplicateKeyError` → 409.

---

### 3. `POST /employees` — `created_at` returned as Python datetime, not epoch ms
**Location:** `create_employee()`  
**What's wrong:** `datetime.now()` is stored and returned as a Python `datetime` object. The contract requires `created_at` to be epoch milliseconds (integer) in the response.  
**How to notice:** Inspect the response body — `created_at` is a datetime string, not an integer.  
**Fix:** Store as UTC datetime; serialize to epoch ms at the API boundary with `dt_to_ms()`.

---

### 4. `GET /employees` — wrong `skip` calculation
**Location:** `list_employees()`  
**What's wrong:** `skip = page * page_size` skips one extra page. Page 1 should skip 0 records, but skips 20.  
**How to notice:** Request page 1 — first record is missing.  
**Fix:** Changed to `skip = (page - 1) * page_size`.

---

### 5. `GET /employees` — `total` ignores department filter
**Location:** `list_employees()`  
**What's wrong:** `count_documents({})` counts the whole collection regardless of the `department` filter.  
**How to notice:** Filter by a department with 2 employees in a 10-employee database — `total` returns 10.  
**Fix:** Changed to `count_documents(q)`.

---

### 6. `GET /employees` — results not sorted by `emp_code`
**Location:** `list_employees()`  
**What's wrong:** No `.sort()` call; MongoDB returns documents in natural order.  
**How to notice:** Insert employees out of order; listing them returns them unsorted.  
**Fix:** Added `.sort("emp_code", ASCENDING)`.

---

### 7. `POST /attendance/punch-in` — no 404 for unknown employee
**Location:** `punch_in()`  
**What's wrong:** `find_one` result is never checked for `None`. If the employee does not exist, `emp["shift_start"]` raises `TypeError` → 500.  
**How to notice:** Punch in with an `emp_code` that doesn't exist.  
**Fix:** Added `if emp is None: raise HTTPException(404, ...)`.

---

### 8. `POST /attendance/punch-in` — race condition on duplicate punch-in
**Location:** `punch_in()`  
**What's wrong:** Same check-then-insert pattern as the employee endpoint; concurrent same-day punch-ins can both succeed.  
**How to notice:** Send two simultaneous punch-in requests for the same employee and date.  
**Fix:** Unique compound index on `(emp_code, date)` + catch `DuplicateKeyError` → 409.

---

### 9. `POST /attendance/punch-in` — wrong timezone for attendance date (R1)
**Location:** `punch_in()`  
**What's wrong:** `datetime.fromtimestamp(ms/1000)` uses the server's local timezone. The attendance date must be the IST calendar date of the punch. On a non-IST server this produces the wrong `date`.  
**How to notice:** Run on a UTC server; punch in at 23:00 UTC (00:30 IST next day) — wrong date stored.  
**Fix:** Convert epoch ms → UTC datetime → IST datetime; derive `date` in IST.

---

### 10. `POST /attendance/punch-in` — no epoch-ms range validation
**Location:** `punch_in()` and `PunchInIn` model  
**What's wrong:** Any integer is accepted for `punched_at`, including second-range values like `1700000000`.  
**How to notice:** Send `punched_at: 1700000000` (seconds, not ms) — accepted without error.  
**Fix:** Added validator: reject if `v < 100_000_000_000` or `v > 4_102_444_800_000` with 422.

---

### 11. `compute_late_minutes` — wrong late detection at exactly 10:00 boundary (R2)
**Location:** `compute_late_minutes()`  
**What's wrong:** `int(total_seconds / 60)` truncates first, then checks `> 10`. For a punch at 09:40:01 (601 seconds late), `int(601/60) = 10`, and `10 > 10` is `False` → returns 0 instead of 10.  
**How to notice:** Punch in at exactly 09:40:01 with shift 09:30 — `late_minutes` should be 10 but is 0.  
**Fix:** Check `elapsed_seconds > 600` (strict seconds), then return `int(elapsed_seconds // 60)`.

---

### 12. `compute_overtime` — wrong for overnight shifts; no 30-minute minimum (R3)
**Location:** `compute_overtime()`  
**What's wrong:** `datetime.fromisoformat(date_str).replace(hour=...)` creates a naive datetime without IST offset, making the shift-end instant wrong. Also, there is no `>= 30` check — any overtime is returned.  
**How to notice:** For a night shift (22:00–06:00), overtime is calculated against the wrong day.  
**Fix:** Build shift-end in IST (handle overnight +1 day); add `return minutes if minutes >= 30 else 0`.

---

### 13. `compute_work_hours` — uses Python `round()` (banker's rounding) instead of half-up (R4)
**Location:** `compute_work_hours()`  
**What's wrong:** `round(x, 2)` uses IEEE 754 banker's rounding. `round(0.5250..., 2)` may round to `0.52` instead of `0.53`.  
**How to notice:** Work hours with `.5` in the third decimal may round incorrectly.  
**Fix:** Use `Decimal(str(seconds)) / Decimal("3600")` quantized with `ROUND_HALF_UP`.

---

### 14. `GET /attendance` — entire collection loaded into memory
**Location:** `list_attendance()`  
**What's wrong:** `list(db.attendance_logs.find(q))` fetches all matching documents, then sorts in Python. At 100k records this is a full collection scan with OOM risk.  
**How to notice:** Query on a 100k dataset — slow response and high memory use.  
**Fix:** Use MongoDB cursor `.sort().skip().limit()` backed by a compound index on `(date desc, emp_code asc)`.

---

### 15. `GET /attendance` — `date_from > date_to` not validated → should be 422
**Location:** `list_attendance()`  
**What's wrong:** No validation; the query silently returns zero results.  
**Fix:** Added `if date_from and date_to and date_from > date_to: raise HTTPException(422, ...)`.

---

### 16. `GET /attendance` — `_id` leaked as `id` in response
**Location:** `list_attendance()`  
**What's wrong:** `d["id"] = str(d.pop("_id"))` exposes the internal ObjectId as `id`. The contract says `_id` never appears in a response and there is no `id` field in `AttendanceRecord`.  
**Fix:** Project `{"_id": 0}` in the query; removed the `id` assignment.

---

### 17. `POST /attendance/punch-in` — `status` field accepts any string
**Location:** `PunchInIn` model  
**What's wrong:** No validation; a client can send `status: "ABSENT"` which is illegal at punch-in.  
**Fix:** Added validator: only `PRESENT`, `WFH`, `ON_DUTY` accepted; else 422.

---

## Items examined and considered fine

- `load_dotenv()` placement is correct — real env vars already override `.env` by default in python-dotenv.
- Using `Optional[int]` for `punched_at` (default to now) is correct per the contract.
- The helper function structure (separate compute functions) is a good pattern worth keeping.
