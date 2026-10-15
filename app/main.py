"""HROne Employee Attendance & Analytics API - completed implementation."""
import os
import re
from datetime import date as Date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.environ.get("MONGO_URI")
MONGO_DB = os.environ.get("MONGO_DB")
if not MONGO_URI or not MONGO_DB:
    raise RuntimeError("MONGO_URI and MONGO_DB environment variables are required")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]

employees = db.employees
attendance_logs = db.attendance_logs

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
IST_NAME = "Asia/Kolkata"
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = PRESENCE_STATUSES | {"ABSENT", "LEAVE"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
EMP_RE = re.compile(r"^EMP\d{4,6}$")

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Validation / conversion helpers
# --------------------------------------------------------------------------- #
def validate_date_string(value: str) -> str:
    if not DATE_RE.fullmatch(value):
        raise ValueError("date must be YYYY-MM-DD")
    try:
        Date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid calendar date") from exc
    return value


def validate_month(value: str) -> str:
    if not MONTH_RE.fullmatch(value):
        raise ValueError("month must be YYYY-MM")
    return value


def validate_time_string(value: str) -> str:
    if not TIME_RE.fullmatch(value):
        raise ValueError("time must be HH:MM")
    return value


def validate_epoch_ms(value: int | None) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise ValueError("timestamp must be epoch milliseconds")
    return value


def epoch_ms_to_datetime(value: int) -> datetime:
    # Store UTC BSON datetime, truncated to whole seconds as required by R1.
    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(microsecond=0).replace(tzinfo=None)


def datetime_to_epoch_ms(value: datetime | None) -> int | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def now_utc_truncated() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def ist_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(IST)


def shift_datetime(attendance_date: str, shift_time: str, overnight_next_day: bool = False) -> datetime:
    d = Date.fromisoformat(attendance_date)
    if overnight_next_day:
        d += timedelta(days=1)
    h, m = map(int, shift_time.split(":"))
    return datetime(d.year, d.month, d.day, h, m, tzinfo=IST)


def shift_is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start


def attendance_date_for_punch(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = ist_datetime(punch_in)
    d = local.date()
    if shift_is_overnight(shift_start, shift_end) and local.strftime("%H:%M:%S") < f"{shift_end}:00":
        d -= timedelta(days=1)
    return d.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:
    start = shift_datetime(attendance_date, shift_start)
    minutes = int((punch_in.replace(tzinfo=UTC) - start.astimezone(UTC)).total_seconds() // 60)
    return minutes if (punch_in.replace(tzinfo=UTC) - start.astimezone(UTC)).total_seconds() > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    hours = Decimal(str((punch_out - punch_in).total_seconds())) / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, attendance_date: str) -> int:
    overnight = shift_is_overnight(shift_start, shift_end)
    end = shift_datetime(attendance_date, shift_end, overnight_next_day=overnight)
    delta_seconds = (punch_out.replace(tzinfo=UTC) - end.astimezone(UTC)).total_seconds()
    minutes = int(delta_seconds // 60)
    return minutes if minutes >= 30 else 0


def compute_derived(emp: dict, attendance_date: str, punch_in: datetime | None, punch_out: datetime | None) -> dict:
    if punch_in is None:
        return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(punch_in, emp["shift_start"], attendance_date)
    if punch_out is None:
        return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    work_hours = compute_work_hours(punch_in, punch_out)
    return {
        "late_minutes": late,
        "work_hours": work_hours,
        "overtime_minutes": compute_overtime(
            punch_out, emp["shift_start"], emp["shift_end"], attendance_date
        ),
        "half_day": work_hours < 4.50,
    }


def api_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [api_history(h) for h in doc.get("history", [])],
    }


def api_history(entry: dict) -> dict:
    changes = {}
    for key, change in entry.get("changes", {}).items():
        from_value = change.get("from")
        to_value = change.get("to")
        if key in {"punch_in", "punch_out"}:
            from_value = datetime_to_epoch_ms(from_value)
            to_value = datetime_to_epoch_ms(to_value)
        changes[key] = {"from": from_value, "to": to_value}
    return {
        "at": datetime_to_epoch_ms(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": changes,
    }


def api_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc["created_at"]),
    }


def round_half_up(value, places: int = 2):
    if value is None:
        return None
    quantum = Decimal("1") if places == 0 else Decimal("1") / (Decimal(10) ** places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def month_bounds(month: str) -> tuple[str, str]:
    year, mon = map(int, month.split("-"))
    start = Date(year, mon, 1)
    end = Date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1) - timedelta(days=1)
    return start.isoformat(), end.isoformat()


def date_to_mongo(value: str) -> datetime:
    d = Date.fromisoformat(value)
    return datetime(d.year, d.month, d.day)


def validate_date_range(date_from: str | None, date_to: str | None):
    if date_from:
        validate_date_string(date_from)
    if date_to:
        validate_date_string(date_to)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")


# --------------------------------------------------------------------------- #
# Pydantic models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, v):
        return validate_date_string(v)

    @model_validator(mode="after")
    def distinct_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, v):
        return validate_epoch_ms(v)

    @field_validator("status")
    @classmethod
    def valid_status(cls, v):
        if v not in PRESENCE_STATUSES:
            raise ValueError("status must be PRESENT, WFH or ON_DUTY")
        return v


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)

    @field_validator("punched_at")
    @classmethod
    def valid_timestamp(cls, v):
        return validate_epoch_ms(v)


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def valid_status(cls, v):
        if v is not None and v not in ALL_STATUSES:
            raise ValueError("invalid status")
        return v

    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_timestamp(cls, v):
        return validate_epoch_ms(v)


# --------------------------------------------------------------------------- #
# MongoDB startup / indexes
# --------------------------------------------------------------------------- #
def ensure_indexes():
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="ux_emp_code")

    employees.create_index(
        [("department", ASCENDING), ("joined_on", ASCENDING)],
        name="ix_department_joined"
    )

    attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="ux_attendance_emp_date",
    )

    attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", DESCENDING), ("status", ASCENDING)],
        name="ix_attendance_employee_date_status",
    )

    attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_emp"
    )

    attendance_logs.create_index(
        [("date", ASCENDING), ("status", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_status_emp"
    )

    attendance_logs.create_index(
        [("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_late_emp"
    )


@app.on_event("startup")
def startup():
    ensure_indexes()


# --------------------------------------------------------------------------- #
# Basic endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc_truncated()
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return api_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department is not None else {}
    total = employees.count_documents(q)
    cursor = employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [api_employee(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# --------------------------------------------------------------------------- #
# Attendance endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else now_utc_truncated()
    attendance_date = attendance_date_for_punch(ts.replace(tzinfo=UTC), emp["shift_start"], emp["shift_end"])
    derived = compute_derived(emp, attendance_date, ts, None)
    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": derived["late_minutes"],
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return api_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    ts = epoch_ms_to_datetime(body.punched_at) if body.punched_at is not None else now_utc_truncated()
    # Find and claim the most recent open punch-in atomically. The unique update
    # filter makes two simultaneous callers contend on the same document.
    candidate = attendance_logs.find_one_and_update(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts},
            "punch_out": None,
        },
        {"$set": {"punch_out": ts}},
        sort=[("punch_in", DESCENDING)],
        return_document=ReturnDocument.AFTER,
    )
    if candidate is None:
        # If another simultaneous request already closed a record at exactly this
        # instant, this request is the loser. Otherwise there is no punch-in.
        if attendance_logs.find_one({"emp_code": body.emp_code, "punch_out": ts}, {"_id": 1}):
            raise HTTPException(409, "attendance record is already punched out")
        raise HTTPException(404, "no punch-in found")

    punch_in_dt = candidate["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=UTC)
    ts_aware = ts.replace(tzinfo=UTC)
    delta = (ts_aware - punch_in_dt).total_seconds()
    if delta <= 0 or delta > 24 * 3600:
        # Undo the claim only if the same document still contains our timestamp.
        attendance_logs.update_one(
            {"_id": candidate["_id"], "punch_out": ts}, {"$set": {"punch_out": None}}
        )
        if delta <= 0:
            raise HTTPException(422, "punched_at must be after punch_in")
        raise HTTPException(422, "punch_out cannot be more than 24 hours after punch_in")

    # Re-read the employee's shift and compute stored derived values.
    derived = compute_derived(emp, candidate["date"], punch_in_dt.replace(tzinfo=None), ts)
    attendance_logs.update_one(
        {"_id": candidate["_id"], "punch_out": ts},
        {"$set": {
            "work_hours": derived["work_hours"],
            "late_minutes": derived["late_minutes"],
            "overtime_minutes": derived["overtime_minutes"],
            "half_day": derived["half_day"],
        }},
    )
    candidate["punch_out"] = ts
    candidate.update(derived)
    return api_attendance(candidate)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    validate_date_range(date_from, date_to)
    if status is not None and status not in ALL_STATUSES:
        raise HTTPException(422, "invalid status")
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    total = attendance_logs.count_documents(q)
    cursor = (
        attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [api_attendance(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    validate_date_string(date)
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    current = attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if current is None:
        raise HTTPException(404, "attendance record not found")

    old_status = current["status"]
    old_in = current.get("punch_in")
    old_out = current.get("punch_out")
    new_status = body.status if body.status is not None else old_status

    if new_status in {"ABSENT", "LEAVE"}:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT or LEAVE cannot include punch times")
        new_in = None
        new_out = None
    else:
        new_in = epoch_ms_to_datetime(body.punch_in) if body.punch_in is not None else old_in
        new_out = epoch_ms_to_datetime(body.punch_out) if body.punch_out is not None else old_out
        if new_in is None:
            raise HTTPException(422, "presence status requires punch_in")
        # R1: punch-in must belong to this attendance date.
        calculated_date = attendance_date_for_punch(new_in, emp["shift_start"], emp["shift_end"])
        if calculated_date != date:
            raise HTTPException(422, "punch_in must belong to the attendance date")
        if new_out is not None:
            if new_out <= new_in or (new_out - new_in).total_seconds() > 24 * 3600:
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    derived = compute_derived(emp, date, new_in, new_out)
    old_values = {
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "work_hours": current.get("work_hours"),
        "late_minutes": current.get("late_minutes", 0),
        "overtime_minutes": current.get("overtime_minutes", 0),
        "half_day": current.get("half_day", False),
    }
    new_values = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        **derived,
    }
    changes = {}
    for key in old_values:
        if old_values[key] != new_values[key]:
            changes[key] = {"from": old_values[key], "to": new_values[key]}
    if not changes:
        raise HTTPException(422, "request does not change the attendance record")

    history_entry = {
        "at": now_utc_truncated(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    # Optimistic concurrency: only update the version represented by the fields
    # read above. This prevents concurrent PATCH requests from losing history.
    filter_doc = {
        "_id": current["_id"],
        "status": old_status,
        "punch_in": old_in,
        "punch_out": old_out,
        "$or": [
            {"history": current.get("history", [])},
            {"history": {"$exists": False}} if "history" not in current else {"history": current.get("history", [])},
        ],
    }
    update_doc = {
        "$set": {
            "status": new_status,
            "punch_in": new_in,
            "punch_out": new_out,
            **derived,
        },
        "$push": {"history": history_entry},
    }
    result = attendance_logs.update_one(filter_doc, update_doc)
    if result.modified_count != 1:
        raise HTTPException(409, "attendance record changed concurrently")

    updated = attendance_logs.find_one({"_id": current["_id"]})
    return api_attendance(updated)


# --------------------------------------------------------------------------- #
# Aggregation helpers
# --------------------------------------------------------------------------- #
def employee_monthly_pipeline(emp_code: str, month: str):
    start, end = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code}},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", start]},
                    {"$lte": ["$date", end]},
                ]}}},
                {"$project": {
                    "_id": 0, "date": 1, "status": 1, "half_day": {"$ifNull": ["$half_day", False]},
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                    "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                }},
            ],
            "as": "logs",
        }},
        {"$project": {
            "emp_code": 1,
            "joined_on": 1,
            "logs": 1,
            "period_start": {"$cond": [
                {"$gt": ["$joined_on", start]}, "$joined_on", start
            ]},
            "period_end": {"$literal": end},
        }},
        {"$set": {
            "days": {
                "$range": [
                    0,
                    {"$add": [{"$dateDiff": {
                        "startDate": {"$dateFromString": {"dateString": "$period_start"}},
                        "endDate": {"$dateFromString": {"dateString": "$period_end"}},
                        "unit": "day",
                    }}, 1]},
                ]
            }
        }},
        {"$set": {
            "working_days": {
                "$size": {
                    "$filter": {
                        "input": "$days",
                        "as": "offset",
                        "cond": {"$in": [
                            {"$dayOfWeek": {"$dateAdd": {
                                "startDate": {"$dateFromString": {"dateString": "$period_start"}},
                                "unit": "day", "amount": "$$offset"
                            }}}, [2, 3, 4, 5, 6]
                        ]}
                    }
                }
            }
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": {"emp_code": "$emp_code", "month": month},
            "working_days": {"$first": "$working_days"},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", start]}}}}, [2, 3, 4, 5, 6]]},
                ]},
                {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0
            ]}},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$logs.overtime_minutes", 0]}},
        }},
        {"$project": {
            "_id": 0, "emp_code": "$_id.emp_code", "month": "$_id.month",
            "working_days": 1, "present_days": 1, "leave_days": 1,
            "late_count": 1, "total_late_minutes": 1, "total_overtime_minutes": 1,
            "attendance_pct": {"$cond": [
                {"$gt": ["$working_days", 0]},
                {"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 100]},
                None,
            ]},
        }},
    ]


def department_summary_pipeline(month: str, department: str | None = None):
    start, end = month_bounds(month)
    employee_match = {"joined_on": {"$lte": end}}
    if department is not None:
        employee_match["department"] = department
    return [
        {"$match": employee_match},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"code": "$emp_code"},
            "pipeline": [
                {"$match": {"$expr": {"$and": [
                    {"$eq": ["$emp_code", "$$code"]},
                    {"$gte": ["$date", start]},
                    {"$lte": ["$date", end]},
                ]}}},
                {"$project": {
                    "_id": 0, "date": 1, "status": 1, "half_day": {"$ifNull": ["$half_day", False]},
                    "late_minutes": {"$ifNull": ["$late_minutes", 0]}, "work_hours": 1,
                }},
            ],
            "as": "logs",
        }},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": {"department": "$department", "emp_code": "$emp_code"},
            "headcount": {"$first": 1},
            "present_days": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": {"$ifNull": ["$logs.date", start]}}}}, [2,3,4,5,6]]}
                ]},
                {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]}, 0
            ]}},
            "work_hours_sum": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne": ["$logs.work_hours", None]}
                ]}, "$logs.work_hours", 0
            ]}},
            "work_hours_count": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne": ["$logs.work_hours", None]}
                ]}, 1, 0
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}},
        }},
        {"$group": {
            "_id": "$_id.department",
            "headcount": {"$sum": "$headcount"},
            "present_days": {"$sum": "$present_days"},
            "work_hours_sum": {"$sum": "$work_hours_sum"},
            "work_hours_count": {"$sum": "$work_hours_count"},
            "late_count": {"$sum": "$late_count"},
            "total_late_minutes": {"$sum": "$total_late_minutes"},
            "leave_count": {"$sum": "$leave_count"},
            "on_duty_count": {"$sum": "$on_duty_count"},
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "avg_work_hours": {"$cond": [
                {"$gt": ["$work_hours_count", 0]},
                {"$divide": ["$work_hours_sum", "$work_hours_count"]}, None
            ]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1
        }},
        {"$sort": {"department": ASCENDING}},
    ]


def late_leaderboard_pipeline(month: str, limit: int, department: str | None = None):
    start, end = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": start, "$lte": end}, "late_minutes": {"$gt": 0}}},
        {"$lookup": {
            "from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "employee"
        }},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$group": {
            "_id": "$emp_code", "name": {"$first": "$employee.name"},
            "department": {"$first": "$employee.department"},
            "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1},
        }},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": DESCENDING},
            "output": {"rank": {"$rank": {}}}
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": DESCENDING, "_id": ASCENDING}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": 1, "department": 1,
            "total_late_minutes": 1, "late_count": 1
        }},
    ]
    return pipeline


def department_trend_pipeline(department: str, from_date: str, to_date: str):
    from_dt = date_to_mongo(from_date)
    to_dt_exclusive = date_to_mongo(to_date) + timedelta(days=1)

    return [
        {
            "$match": {
                "date": {
                    "$gte": from_date,
                    "$lte": to_date,
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        {
            "$match": {
                "employee.department": department
            }
        },
        {
            "$project": {
                "date": {
                    "$dateFromString": {
                        "dateString": "$date"
                    }
                },
                "present_value": {
                    "$cond": [
                        {
                            "$in": [
                                "$status",
                                ["PRESENT", "WFH", "ON_DUTY"],
                            ]
                        },
                        {
                            "$cond": [
                                {"$ifNull": ["$half_day", False]},
                                0.5,
                                1,
                            ]
                        },
                        0,
                    ]
                },
                "late_value": {
                    "$cond": [
                        {
                            "$gt": [
                                {"$ifNull": ["$late_minutes", 0]},
                                0,
                            ]
                        },
                        1,
                        0,
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {"$sum": "$present_value"},
                "late_count": {"$sum": "$late_value"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },

        # Create the complete date range from the requested start/end dates.
        # This avoids $documents, which is not supported by the local
        # MongoDB aggregation context.
        {
            "$densify": {
                "field": "date",
                "partitionByFields": [],
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_exclusive],
                },
            }
        },

        {
            "$set": {
                "present_count": {
                    "$ifNull": ["$present_count", 0]
                },
                "late_count": {
                    "$ifNull": ["$late_count", 0]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {
                    "day_string": {
                        "$dateToString": {
                            "date": "$date",
                            "format": "%Y-%m-%d",
                        }
                    }
                },
                "pipeline": [
                    {
                        "$match": {
                            "department": department
                        }
                    },
                    {
                        "$match": {
                            "$expr": {
                                "$lte": [
                                    "$joined_on",
                                    "$$day_string",
                                ]
                            }
                        }
                    },
                    {
                        "$count": "count"
                    },
                ],
                "as": "hc",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$hc.count", 0]},
                        0,
                    ]
                },
                "is_working_day": {
                    "$in": [
                        {"$dayOfWeek": "$date"},
                        [2, 3, 4, 5, 6],
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate_raw": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                "$present_count",
                                "$headcount",
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {
                    "date": ASCENDING
                },
                "output": {
                    "moving_avg_raw": {
                        "$avg": "$attendance_rate_raw",
                        "window": {
                            "documents": [-6, 0]
                        },
                    }
                },
            }
        },
        {
            "$match": {
                "date": {
                    "$gte": from_dt,
                    "$lt": to_dt_exclusive,
                }
            }
        },
        {
            "$sort": {
                "date": ASCENDING
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateToString": {
                        "date": "$date",
                        "format": "%Y-%m-%d",
                    }
                },
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": "$attendance_rate_raw",
                "moving_avg_7d": "$moving_avg_raw",
            }
        },
    ]

# --------------------------------------------------------------------------- #
# Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    validate_month(month)
    if employees.find_one({"emp_code": emp_code}, {"_id": 1}) is None:
        raise HTTPException(404, "employee not found")
    pipeline = employee_monthly_pipeline(emp_code, month)
    result = next(iter(employees.aggregate(pipeline)), None)
    if result is None:
        raise HTTPException(404, "employee not found")
    result["attendance_pct"] = round_half_up(result.get("attendance_pct"), 4)
    result["present_days"] = round_half_up(result.get("present_days", 0), 2)
    return result


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    validate_month(month)
    pipeline = department_summary_pipeline(month, department)
    items = list(employees.aggregate(pipeline))
    for item in items:
        item["present_days"] = round_half_up(item.get("present_days", 0), 2)
        item["avg_work_hours"] = round_half_up(item.get("avg_work_hours"), 2)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    validate_month(month)
    pipeline = late_leaderboard_pipeline(month, limit, department)
    items = list(attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(alias="from"), to: str = Query(...)):
    validate_date_string(from_)
    validate_date_string(to)
    if to < from_:
        raise HTTPException(422, "to must not be before from")
    if (Date.fromisoformat(to) - Date.fromisoformat(from_)).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    if employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    pipeline = department_trend_pipeline(department, from_, to)
    items = list(attendance_logs.aggregate(pipeline))
    for item in items:
        item["attendance_rate"] = round_half_up(item.get("attendance_rate"), 4)
        item["moving_avg_7d"] = round_half_up(item.get("moving_avg_7d"), 4)
        item["present_count"] = round_half_up(item.get("present_count", 0), 2)
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Explain endpoint
# --------------------------------------------------------------------------- #
def explain_aggregate(collection, pipeline):
    return db.command(
        "explain",
        {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}},
        verbosity="executionStats",
    )


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    allowed = {"attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"}
    if endpoint not in allowed:
        raise HTTPException(422, "invalid endpoint")

    if endpoint == "attendance_list":
        validate_date_range(date_from, date_to)
        if status is not None and status not in ALL_STATUSES:
            raise HTTPException(422, "invalid status")
        q = {}
        if emp_code is not None:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to: q["date"]["$lte"] = date_to
        if status is not None: q["status"] = status
        # find().explain with the exact filters/sort/pagination.
        explanation = attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size).explain("executionStats")
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explanation}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        validate_month(month)
        pipeline = employee_monthly_pipeline(emp_code, month)
        explanation = explain_aggregate(employees, pipeline)
        return {"endpoint": endpoint, "collection": "employees", "explain": explanation}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        validate_month(month)
        pipeline = department_summary_pipeline(month, department)
        explanation = explain_aggregate(employees, pipeline)
        return {"endpoint": endpoint, "collection": "employees", "explain": explanation}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        validate_month(month)
        pipeline = late_leaderboard_pipeline(month, limit, department)
        explanation = explain_aggregate(attendance_logs, pipeline)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explanation}

    if not department or not from_ or not to:
        raise HTTPException(422, "department, from and to are required")
    validate_date_string(from_)
    validate_date_string(to)
    if to < from_ or (Date.fromisoformat(to) - Date.fromisoformat(from_)).days + 1 > 92:
        raise HTTPException(422, "invalid trend date range")
    pipeline = department_trend_pipeline(department, from_, to)
    explanation = explain_aggregate(attendance_logs, pipeline)
    return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explanation}
