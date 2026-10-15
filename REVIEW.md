# Starter Code Review

| # | Where | What is wrong | How you'd notice it | How it was fixed |
|---|---|---|---|---|
| 1 | `health()` | Always returns 200 without checking MongoDB readiness. | Stop MongoDB and call `/health`. | Ping MongoDB and return 503 when the ping fails. |
| 2 | `create_employee()` | Duplicate protection uses check-then-insert, so concurrent creates can both pass the check. | Send parallel POSTs with the same `emp_code`. | Added a unique MongoDB index and handle `DuplicateKeyError` as 409. |
| 3 | `create_employee()` | `created_at` is stored/returned as a normal Python datetime instead of the required epoch-millisecond API value. | Inspect the response and stored type. | Store BSON datetime internally and convert it to epoch milliseconds at the API boundary. |
| 4 | `list_employees()` | Pagination skips the first page because it uses `page * page_size`. | Request page 1 and observe the first page is skipped. | Use `(page - 1) * page_size`. |
| 5 | `list_employees()` | `total` ignores the department filter. | Request `?department=Engineering` and compare total with the filtered result count. | Count using the same filter query. |
| 6 | `list_employees()` | Results are not sorted by `emp_code` ascending as required. | Seed employees out of order and call GET `/employees`. | Add an indexed `emp_code` ascending sort. |
| 7 | `list_employees()` | Query parameters have no contract-level validation for page/page_size. | Try page 0 or page_size > 100. | Use FastAPI constrained query parameters. |
| 8 | `punch_in()` | Unknown employees are not rejected before using the employee document. | Punch in with an unknown `emp_code`; the starter can fail instead of returning 404. | Return 404 when the employee does not exist. |
| 9 | `punch_in()` | Timestamps are converted using the machine's local timezone and are not truncated to whole seconds. | Use an epoch timestamp with milliseconds on a machine in a non-UTC timezone. | Treat API instants as UTC epoch milliseconds and truncate to whole seconds. |
| 10 | `punch_in()` | Attendance date does not implement the IST/overnight R1 rules. | Test a 22:00-06:00 employee with a punch-in shortly after midnight. | Derive the attendance date using IST and the overnight-shift rule. |
| 11 | `punch_in()` | The duplicate-record check is a race-prone check-then-insert. | Send simultaneous punch-ins for the same employee/date. | Enforce a unique `(emp_code, date)` index and translate the losing insert to 409. |
| 12 | `punch_in()` | Request status is not validated against PRESENT/WFH/ON_DUTY. | Send ABSENT or LEAVE to the punch-in endpoint. | Validate the presence-status enum. |
| 13 | `compute_late_minutes()` | The helper is not safe for the assignment's timezone/date model and its inputs do not explicitly anchor the shift to the attendance date. | Test late calculation around midnight/overnight shifts. | Calculate the shift start from the attendance date in IST and apply the strict 10-minute rule. |
| 14 | `compute_work_hours()` | Python `round()` uses binary floating-point/banker's rounding, but R4 requires decimal half-up rounding. | Use a duration exactly at a half-cent boundary. | Use `Decimal(...).quantize(..., ROUND_HALF_UP)`. |
| 15 | `compute_overtime()` | The starter always places shift end on the same calendar date, so overnight overtime is wrong. | Punch out after 06:00 for a 22:00-06:00 employee. | Put overnight shift end on the following calendar day. |
| 16 | `list_attendance()` | It loads the entire filtered collection into Python and sorts/slices there. | Seed ~100k records and inspect memory/time or the explain plan. | Push filtering, sorting and pagination into MongoDB and add indexes. |
| 17 | `list_attendance()` | `total` is computed from the materialized Python list instead of the filtered MongoDB count. | Use a large dataset and a filtered request. | Use `count_documents()` with the exact filter. |
| 18 | `list_attendance()` | MongoDB `_id` is exposed indirectly as an `id` field, which violates the contract. | Inspect the JSON response. | Return the natural key fields only; `_id` never appears in the API. |
| 19 | `list_attendance()` | Stored `history`, `half_day`, `late_minutes`, and `overtime_minutes` may be missing in seeded data, but the response must provide the contract defaults. | Call the endpoint on the provided seed document for EMP0002. | Normalize missing audit/derived fields when serializing. |
| 20 | Missing endpoints | Punch-out, regularization, all four analytics endpoints, and `/admin/explain/{endpoint}` are absent. | Compare the starter routes with `openapi.yaml`. | Implemented every endpoint in the contract. |

| 21 | `list_attendance()` | It accepts invalid date strings, reversed date ranges, invalid statuses, and out-of-range page/page_size values instead of following the contract's 422 validation rules. | Try malformed `date_from`, `date_from > date_to`, an unknown status, page 0, or page_size 101. | Added explicit date/status validation and constrained pagination parameters. |
| 22 | `EmployeeIn` / employee creation | The starter accepts invalid `emp_code`, email, shift-time and joined-date values that the OpenAPI schema constrains. | POST malformed employee data such as `emp_code: "x"` or `joined_on: "not-a-date"`. | Added Pydantic constraints and calendar/shift validation. |
| 23 | `compute_overtime()` | It returns any positive overtime, but R3 requires overtime only when the duration from shift end is at least 30 whole minutes. | Punch out 10 or 29 minutes after shift end. | Apply the 30-minute threshold after flooring to whole minutes. |

### Inspected and intentionally kept

- `PRESENT`, `WFH`, and `ON_DUTY` are treated as presence statuses; `ABSENT` and `LEAVE` are not created by punch-in.
- The natural attendance key remains `(emp_code, date)`; MongoDB `_id` is kept internal.
- Analytics read the stored derived values instead of recomputing them from punch timestamps, as required by the data model.
- The provided helper idea of centralizing late/work/overtime calculations was retained, but the helpers were corrected for the assignment's exact rules.
