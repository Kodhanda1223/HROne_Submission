# Employee Attendance & Analytics API

FastAPI + MongoDB implementation for the HROne Software Engineer Trainee assignment.

## Run

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

Set `MONGO_URI` and `MONGO_DB` as environment variables. A local `.env` is supported for development, but it must not be committed.

The service creates required MongoDB indexes at startup. MongoDB 6.0+ is required.

## Implemented

- Employee creation/listing with validation, uniqueness and pagination
- Race-safe punch-in and punch-out
- Attendance regularization with an append-only audit history
- Employee monthly analytics
- Department summary analytics
- Late-comer leaderboard with competition ranking and ties
- Department daily trend with MongoDB gap filling and 7-day moving average
- MongoDB execution-plan explain endpoint
- IST attendance-date rules, overnight shifts and required rounding rules

## Start command

```bash
uvicorn app.main:app --port 8000
```

No secrets, `.env`, Dockerfile or data dumps are included in the submission.
