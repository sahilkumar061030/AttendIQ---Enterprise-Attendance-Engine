# DECISIONS.md

1. **Indexes.**
I created unique indexes on `employees.emp_code` and `attendance_logs.(emp_code, date)` to enforce business key uniqueness and prevent race conditions. I added `attendance_logs.(date, emp_code)` to serve paginated listing, `attendance_logs.(emp_code, punch_in)` for open punch-out lookups, `attendance_logs.(date, late_minutes)` for leaderboard filtering, and `employees.(department, joined_on)` for department trend lookups. I considered indexing `status` alone, but rejected it due to low cardinality and poor selectivity.

2. **Punch-in race.**
Both requests pass validation and attempt `insert_one()`. Because of the unique compound index on `(emp_code, date)`, MongoDB atomically accepts the first write and rejects the second with `DuplicateKeyError`. My code intercepts this error and immediately returns HTTP 409 Conflict. No check-then-insert gap exists.

3. **Ties.**
Using MongoDB’s `$rank` window operator, tied employees share the same rank and subsequent ranks are skipped. Because `limit` is applied after ranking (`rank <= limit`), all tied employees at the cutoff are returned. If two employees tie at rank 10, both are included (yielding 11 rows), deterministically ordered by `total_late_minutes` descending, then `emp_code` ascending.

4. **Headcount.**
My department summary pipeline starts from the `employees` collection filtered by `joined_on <= period_end`, rather than starting from logs. It joins `attendance_logs` using `$lookup` and unwinds with `preserveNullAndEmptyArrays: true`. Employees with zero logs still contribute to the `$addToSet: "$emp_code"` department headcount.

5. **One thing you would change for 100x data.**
I would maintain pre-aggregated daily department summaries and monthly employee rollups via change streams or during punch-out/regularization writes, avoiding multi-collection aggregation pipelines across millions of raw documents.
