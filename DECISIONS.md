# DECISIONS.md

1. **Indexes.**
   - On `employees`: Unique `{ emp_code: 1 }` guarantees employee uniqueness under concurrent creations. Compound `{ department: 1, joined_on: 1 }` speeds up department filtering and headcount evaluations.
   - On `attendance_logs`: Unique compound `{ emp_code: 1, date: 1 }` guarantees one record per employee-day and supports natural key lookups. `{ date: -1, emp_code: 1 }` backs the attendance list endpoint's sort order (`date` desc, `emp_code` asc) avoiding in-memory sorting. `{ date: 1, late_minutes: 1, emp_code: 1 }` optimizes monthly leaderboard queries. `{ emp_code: 1, punch_in: -1 }` serves punch-out resolution.
   - *Considered and rejected*: An index on `{ work_hours: 1 }`, because analytics filter primarily by date ranges and department, making a dedicated work-hours index wasteful overhead.

2. **Punch-in race.**
   Two concurrent punch-ins calculate identical IST attendance dates and attempt `insert_one`. Due to the unique compound index on `(emp_code, date)`, MongoDB's WiredTiger engine accepts only the first insert (returning 201). The second fails with `DuplicateKeyError`, which our exception handler catches and translates cleanly to 409 Conflict.

3. **Ties.**
   We rank using MongoDB's `$rank` window function (competition ranking: 1, 2, 2, 4). The `limit` filter (`rank <= limit`) applies strictly *after* ranking, and results are ordered by `total_late_minutes` descending then `emp_code` ascending. Both tied employees are returned, which correctly allows the returned array to exceed `limit` rows.

4. **Headcount.**
   The aggregation begins on `employees` (filtering `joined_on <= month_end`). It performs a `$lookup` on `attendance_logs` and an `$unwind` with `preserveNullAndEmptyArrays: true`. Employees with zero logs are retained, ensuring their headcount contribution is counted.

5. **One thing you would change for 100x data.**
   Precompute and maintain daily and monthly rollups incrementally upon punch-out and regularization, querying pre-aggregated tables rather than scanning multi-million raw attendance logs on the fly.
