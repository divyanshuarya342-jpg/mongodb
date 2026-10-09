# DECISIONS.md — Part C: Design Decisions

## 1. Indexes created and why

I created six indexes. On `employees`: a unique index on `emp_code` (enforces uniqueness race-safely for concurrent creates and serves every employee lookup); a compound index on `(department, joined_on)` (serves R9 headcount queries that filter by department and compare `joined_on` to a date). On `attendance_logs`: a unique compound index on `(emp_code, date)` (the natural key — prevents duplicate punch-ins concurrently without a check-then-insert); a compound index on `(date DESC, emp_code ASC)` (matches the sort order of `GET /attendance`, making it an index-only scan for list queries at 100k docs); an index on `(emp_code, punch_in)` (used by punch-out to find the most recent open record for an employee); and a compound index on `(date, status, emp_code)` (covers analytics aggregations that filter on date range and status).

## 2. What happens when two identical punch-ins arrive at the same instant

Both requests reach `insert_one` with the same `(emp_code, date)`. MongoDB's unique compound index on those two fields serialises the writes at the storage layer. Exactly one insert succeeds and receives a 201 response. The other gets a `DuplicateKeyError` from PyMongo, which the endpoint catches and converts to a 409. There is no check-then-insert race window.

## 3. What the leaderboard returns for a tie at the cutoff

The pipeline uses `$setWindowFields` with `$rank`, which implements standard competition ranking: tied employees share a rank and the next rank is skipped (1, 2, 2, 4). The `limit` filter keeps every employee whose `rank <= limit`, so if two employees tie at rank 10 when `limit=10` both are returned, giving 11 rows. This is correct per the contract: "the response can therefore contain more than `limit` rows when employees tie at a rank <= `limit`."

## 4. How the department summary counts employees with no logs

The pipeline starts from the `employees` collection (not `attendance_logs`), filtered to `joined_on <= month_end`. A `$lookup` left-joins attendance records for the month. `$unwind` with `preserveNullAndEmpty: true` keeps employees who have no matching logs as a single document with a null `logs` field. The subsequent `$group` counts each distinct `emp_code` for the headcount, while only summing stats from non-null log entries. This ensures zero-log employees are counted in headcount but contribute nothing to `present_days`, `late_count`, etc.

## 5. What I would change for 100× the data (~10M records)

At 10M attendance records I would: (1) shard `attendance_logs` on `{emp_code: 1, date: 1}` so reads and writes distribute across shards; (2) add a partial index on `(emp_code, punch_in)` filtered to `{punch_out: null}` so the punch-out lookup only scans open records; (3) pre-aggregate monthly summaries into a separate `monthly_stats` collection via a change-stream consumer or a nightly job, so analytics queries read a small pre-computed collection instead of scanning millions of logs; (4) consider Atlas Search or a time-series collection for the trend endpoint, since `$densify` over a large range on a big collection can be expensive.
