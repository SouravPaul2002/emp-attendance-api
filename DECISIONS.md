# DECISIONS.md

1. Indexes
In init_db, I indexed employees on emp_code (unique), (department, joined_on), and joined_on. On attendance_logs, I added (emp_code, date) as unique, (date desc, emp_code asc), (date, late_minutes, emp_code), (status, date desc), and (emp_code, punch_in desc). I skipped indexing work_hours because no query filters by it.

2. Punch-in race
Both requests try to insert a record for the same employee and date. The unique index on (emp_code, date) lets only the first insert succeed. The second gets a DuplicateKeyError, which my code catches and returns as 409 Conflict.

3. Ties
I used MongoDB's $rank window function so tied employees get the same rank. Because I filter for rank <= limit after ranking, two people can share 10th place and the endpoint returns 11 items.

4. Headcount
I started the aggregation on employees filtered by joined_on. Then I did a $lookup into attendance_logs with preserveNullAndEmptyArrays set to true so employees without logs are still counted.

5. One thing you would change for 100x data
The department trend endpoint would slow down first because it runs daily lookups across the range. I would precompute daily totals during punch-out and store them in a summary collection.
