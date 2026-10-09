# Employee Attendance & Analytics API

FastAPI + MongoDB backend for the HROne engineering assignment.

## How to run

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set environment variables (or create a .env file)
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"

# 3. Start the server
uvicorn app.main:app --port 8000
```

The app is ready when `GET /health` returns `{"status": "ok"}` (within ~5 seconds).

## Environment variables

| Variable    | Description                          | Example                              |
|-------------|--------------------------------------|--------------------------------------|
| `MONGO_URI` | MongoDB connection string            | `mongodb://localhost:27017`          |
| `MONGO_DB`  | Database name                        | `attendance_db`                      |

Real environment variables always override `.env`.

## Indexes

All indexes are created automatically at startup (idempotent). No manual setup needed.

## Loading sample data

```bash
python sample_seed.py
```

## Notes

- All application code is in `app/main.py`.
- Instants in the API are epoch milliseconds (integers). BSON datetimes are converted at the boundary.
- Attendance dates and shift times are IST (UTC+05:30).
- See `REVIEW.md` for the starter-code defect analysis and `DECISIONS.md` for design decisions.
