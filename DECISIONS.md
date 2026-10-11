# DECISIONS.md

1. Indexes
In init_db, I indexed employees on emp_code (unique) for fast lookups and unique creation, (department, joined_on) for department queries, and joined_on for headcount. On attendance_logs, I added (emp_code, date) unique for punch-in safety, (date desc, emp_code asc) for the attendance list, (date, late_minutes, emp_code) for the late leaderboard, (status, date desc) for status filters, and (emp_code, punch_in desc) for punch-out. I skipped work_hours because no query filters on it.

2. Punch-in race
Both requests try inserting the same employee and date. The unique index on (emp_code, date) lets only one succeed. The second gets a DuplicateKeyError, which I catch and return as 409 Conflict.

3. Ties
I used MongoDB's $rank window function so tied employees share rank numbers. Filtering for rank <= limit after ranking lets tied 10th-place employees both return.

4. Headcount
I aggregated from employees joined with attendance_logs using preserveNullAndEmptyArrays true. This keeps and counts employees with no logs.

5. One thing you would change for 100x data
The department trend aggregation would slow down first from daily lookups. I would precompute daily totals into a summary collection during punch-out.
