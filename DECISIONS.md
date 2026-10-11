# DECISIONS.md

1. **Indexes created by `init_db()`**:
   - `employees`: unique `emp_code`, `(department, joined_on)`, and `joined_on`.
   - `attendance_logs`: unique `(emp_code, date)`, `(date DESC, emp_code ASC)` for sorted pagination, `(date, late_minutes, emp_code)` for leaderboard queries, `(status, date DESC)`, and `(emp_code, punch_in DESC)` for punch-outs.
   - *Rejected*: `{ work_hours: 1 }` due to write overhead while analytics queries filter primarily by date.

2. **Punch-in race**:
   The unique index on `(emp_code, date)` guarantees WiredTiger accepts only one insert. The losing concurrent request raises `DuplicateKeyError`, caught and returned as 409 Conflict.

3. **Leaderboard ties**:
   We use MongoDB’s `$rank` window function (`rank <= limit`). Tied employees share ranks (e.g., 1, 2, 2, 4), sorted by `total_late_minutes` descending then `emp_code` ascending, correctly allowing returned results to exceed `limit`.

4. **Headcount**:
   Aggregation starts from `employees` (filtering `joined_on <= period_end`), joining `attendance_logs` with `preserveNullAndEmptyArrays: true`. Employees with zero logs are retained and counted in headcount.

5. **100x scale change**:
   Maintain incremental rollups on punch-out and regularization into pre-aggregated daily/monthly summary collections, eliminating full-collection aggregations.
