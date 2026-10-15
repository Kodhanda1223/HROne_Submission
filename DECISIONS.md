# Engineering Decisions

1. **Indexes.** I created a unique `employees.emp_code` index for identity and concurrent duplicate protection, `employees(department, joined_on)` for headcount lookups, and attendance indexes for employee/date, date/employee, date/status, and date/late-minute access. I rejected a status-only index because the main queries are driven by dates or employee/date ranges.

2. **Punch-in race.** The API calculates the attendance date and attempts one insert. A unique `(emp_code, date)` index is the synchronization point: one concurrent insert gets 201; duplicate inserts get `DuplicateKeyError`, translated to 409. There is no check-then-insert dependency.

3. **Ties.** The leaderboard groups late minutes per employee, then uses MongoDB `$rank`. Equal totals share a rank and the cutoff is applied after ranking, so ties at the cutoff are included. `emp_code` provides deterministic ordering.

4. **Headcount.** Department summary starts from employees joined by period end, including employees with no logs. It groups by employee before department, so each employee counts once and zero-log employees remain in headcount.

5. **100x data.** I would introduce pre-aggregated daily/monthly reporting collections, cache repeated reporting ranges, and isolate analytics from transactional traffic with a dedicated read workload/read replica.
