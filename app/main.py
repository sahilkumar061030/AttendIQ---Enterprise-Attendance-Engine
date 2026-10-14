"""
Employee Attendance & Analytics API (FastAPI + MongoDB)
Single-file production implementation conforming to openapi.yaml and business rules R1-R10.
Run: uvicorn app.main:app --port 8000
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import os
import re
from typing import Any, Dict, List, Literal, Optional

from bson import ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

# Real environment variables take priority over .env
load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

# R1: Shift times and attendance dates are IST (UTC+05:30)
IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Helper Functions & Calculations
# --------------------------------------------------------------------------- #

def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """R8: Rounds float using half-up arithmetic (rates to 4 decimals, numbers to 2)."""
    if val is None:
        return None
    d = Decimal(str(val))
    q = Decimal("10") ** -decimals
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    """Converts a BSON or Python datetime to epoch milliseconds integer."""
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        return int(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def epoch_ms_to_utc_dt(ms: Optional[int]) -> Optional[datetime]:
    """R1: Truncates instant to whole seconds and returns UTC timezone-aware datetime."""
    if ms is None:
        return None
    sec = ms // 1000  # Truncate sub-second milliseconds to whole seconds
    return datetime.fromtimestamp(sec, tz=timezone.utc)


def now_utc_sec() -> datetime:
    """R1: Returns current time truncated to whole seconds in UTC."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def compute_attendance_date(ts_utc: datetime, shift_start: str, shift_end: str) -> str:
    """R1: Determines attendance date in IST, handling overnight shifts (shift_end <= shift_start)."""
    ts_ist = ts_utc.astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    is_overnight = (end_h, end_m) <= (start_h, start_m)

    # R1: For overnight shifts, punch-in earlier than shift_end belongs to the previous day's shift
    if is_overnight and (ts_ist.hour, ts_ist.minute, ts_ist.second) < (end_h, end_m, 0):
        return (ts_ist.date() - timedelta(days=1)).isoformat()
    return ts_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, att_date_str: str) -> int:
    """R2: Grace period 10 mins. Late if strictly > 10:00 after shift_start, floor minutes from shift_start."""
    punch_in_ist = punch_in_utc.astimezone(IST)
    start_h, start_m = map(int, shift_start.split(":"))
    att_d = date.fromisoformat(att_date_str)
    shift_start_dt = datetime(att_d.year, att_d.month, att_d.day, start_h, start_m, 0, tzinfo=IST)
    diff_sec = int((punch_in_ist - shift_start_dt).total_seconds())
    # R2: Strictly more than 10 minutes (600 seconds)
    if diff_sec > 600:
        return diff_sec // 60
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: Work hours = (punch_out - punch_in) in seconds / 3600, rounded 2 decimals half-up."""
    diff_sec = Decimal(int((punch_out_utc - punch_in_utc).total_seconds()))
    hours = (diff_sec / Decimal("3600")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out_utc: datetime, shift_start: str, shift_end: str, att_date_str: str) -> int:
    """R3: Floor minutes from shift_end to punch_out, counted only if >= 30, else 0."""
    att_d = date.fromisoformat(att_date_str)
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))
    is_overnight = (end_h, end_m) <= (start_h, start_m)
    # Overnight shift end falls on the next calendar day
    shift_end_date = att_d + timedelta(days=1) if is_overnight else att_d
    shift_end_dt = datetime(shift_end_date.year, shift_end_date.month, shift_end_date.day, end_h, end_m, 0, tzinfo=IST)
    punch_out_ist = punch_out_utc.astimezone(IST)
    diff_sec = int((punch_out_ist - shift_end_dt).total_seconds())
    ot_mins = diff_sec // 60
    return ot_mins if ot_mins >= 30 else 0


def serialize_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Formats employee MongoDB document for API response (converts created_at, strips _id)."""
    if not doc:
        return doc
    d = dict(doc)
    d.pop("_id", None)
    d["created_at"] = dt_to_epoch_ms(d.get("created_at"))
    return d


def serialize_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Formats attendance MongoDB document for API response per openapi schema."""
    if not doc:
        return doc
    d = dict(doc)
    d.pop("_id", None)
    d.pop("id", None)
    d["punch_in"] = dt_to_epoch_ms(d.get("punch_in"))
    d["punch_out"] = dt_to_epoch_ms(d.get("punch_out"))
    d["half_day"] = bool(d.get("half_day", False))
    d["late_minutes"] = int(d.get("late_minutes", 0))
    d["overtime_minutes"] = int(d.get("overtime_minutes", 0))

    history_list = []
    for h in (d.get("history") or []):
        entry = dict(h)
        entry["at"] = dt_to_epoch_ms(entry.get("at"))
        raw_changes = entry.get("changes") or {}
        changes = {}
        for k, v in raw_changes.items():
            ch = dict(v)
            if k in ("punch_in", "punch_out"):
                ch["from"] = dt_to_epoch_ms(ch.get("from"))
                ch["to"] = dt_to_epoch_ms(ch.get("to"))
            changes[k] = ch
        entry["changes"] = changes
        history_list.append(entry)
    d["history"] = history_list
    return d


def json_safe(obj: Any) -> Any:
    """Recursively converts BSON datatypes to standard JSON types for explain responses."""
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(x) for x in obj]
    if isinstance(obj, datetime):
        return dt_to_epoch_ms(obj)
    if isinstance(obj, ObjectId):
        return str(obj)
    if isinstance(obj, Decimal):
        return float(obj)
    return obj


# --------------------------------------------------------------------------- #
# Database Indexes (Idempotent Startup)
# --------------------------------------------------------------------------- #

def init_indexes():
    """Creates all required database indexes idempotently at application startup."""
    # employees: emp_code must be globally unique
    db.employees.create_index([("emp_code", 1)], unique=True, name="idx_employees_emp_code_unique")
    # employees: backs department filtering and sorted queries
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="idx_employees_dept_emp_code")
    # employees: backs headcount eligibility filtering by joined_on
    db.employees.create_index([("joined_on", 1)], name="idx_employees_joined_on")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="idx_employees_dept_joined")

    # attendance_logs: unique compound index prevents duplicate punch-ins per employee per date (race-safe)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="idx_att_emp_date_unique")
    # attendance_logs: backs default list sorting by date desc, then emp_code asc
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="idx_att_date_emp")
    # attendance_logs: backs open punch-out lookup for most recent record
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="idx_att_emp_punch_in")
    # attendance_logs: backs late leaderboard aggregation range scans
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)], name="idx_att_date_late")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan context manager ensuring indexes are initialized on startup."""
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Pydantic Request / Validation Models
# --------------------------------------------------------------------------- #

class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_shift_times(self):
        # Shift start must differ from shift end
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    punch_out: Optional[int] = Field(default=None, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health", tags=["system"])
def health():
    """Readiness probe: returns 200 only if MongoDB answers a ping; else 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/employees", status_code=201, tags=["employees"])
def create_employee(body: EmployeeCreate):
    """Creates an employee document. Enforced by unique index on emp_code; returns 409 on duplicate."""
    doc = body.model_dump()
    # Server clock creation time truncated to whole seconds
    doc["created_at"] = now_utc_sec()

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return serialize_employee_doc(doc)


@app.get("/employees", tags=["employees"])
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """R10: Lists employees sorted by emp_code ascending with pagination; total reflects filters."""
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee_doc(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201, tags=["attendance"])
def punch_in(body: PunchInRequest):
    """R1, R2: Records punch-in; race-safe via unique compound index (emp_code, date)."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    # R1: Truncate instant to whole seconds
    if body.punched_at is not None:
        ts_utc = epoch_ms_to_utc_dt(body.punched_at)
    else:
        ts_utc = now_utc_sec()

    # R1: Compute IST attendance date taking overnight shifts into account
    att_date = compute_attendance_date(ts_utc, emp["shift_start"], emp["shift_end"])

    # R2: Compute late minutes from shift start with 10-minute grace period
    late_minutes = compute_late_minutes(ts_utc, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": ts_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Already punched in for this date")

    return serialize_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=200, tags=["attendance"])
def punch_out(body: PunchOutRequest):
    """R3-R5: Closes most recent open punch-in. Atomic update handles concurrent punch-outs."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    # R1: Truncate instant to whole seconds
    if body.punched_at is not None:
        punch_out_utc = epoch_ms_to_utc_dt(body.punched_at)
    else:
        punch_out_utc = now_utc_sec()

    # Find the employee's most recent record whose punch_in is at or before punch_out
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(status_code=404, detail="No punch-in found for this employee")

    # Check if record is already punched out
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    # Validate timing: punch_out must be strictly after punch_in and within 24 hours
    duration_sec = int((punch_out_utc - punch_in_utc).total_seconds())
    if duration_sec <= 0:
        raise HTTPException(status_code=422, detail="punched_at must be strictly after punch_in")
    if duration_sec > 86400:
        raise HTTPException(status_code=422, detail="punched_at cannot exceed 24 hours after punch_in")

    # R4: Work hours rounded half-up
    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    # R5: Half day if work_hours < 4.50
    half_day = work_hours < 4.50
    # R3: Overtime floor minutes >= 30
    overtime_minutes = compute_overtime(punch_out_utc, emp["shift_start"], emp["shift_end"], record["date"])

    # Atomic conditional update prevents concurrent punch-out race conditions
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Record was already punched out concurrently")

    return serialize_attendance_doc(updated)


@app.get("/attendance", tags=["attendance"])
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """R10: Lists attendance records sorted by date desc, then emp_code asc using index."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_attendance_doc(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{date}", tags=["attendance"])
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    """Regularizes (corrects) one day's record, recomputing derived fields and appending to history."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    # Setting ABSENT or LEAVE clears both punch times
    target_status = body.status if body.status is not None else record["status"]
    if target_status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(status_code=422, detail="Cannot supply punch times when status is ABSENT or LEAVE")

    if target_status in ("ABSENT", "LEAVE"):
        new_punch_in_dt = None
        new_punch_out_dt = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status requires a punch_in
        if body.punch_in is not None:
            new_punch_in_dt = epoch_ms_to_utc_dt(body.punch_in)
        else:
            new_punch_in_dt = record.get("punch_in")

        if new_punch_in_dt is None:
            raise HTTPException(status_code=422, detail="Presence status requires punch_in")

        if new_punch_in_dt.tzinfo is None:
            new_punch_in_dt = new_punch_in_dt.replace(tzinfo=timezone.utc)

        # R1: punch_in must stay on the record's attendance date
        computed_date = compute_attendance_date(new_punch_in_dt, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(status_code=422, detail="punch_in must stay on the record's attendance date")

        if body.punch_out is not None:
            new_punch_out_dt = epoch_ms_to_utc_dt(body.punch_out)
        else:
            new_punch_out_dt = record.get("punch_out")

        if new_punch_out_dt is not None and new_punch_out_dt.tzinfo is None:
            new_punch_out_dt = new_punch_out_dt.replace(tzinfo=timezone.utc)

        # Recompute derived fields R2-R5
        new_late_minutes = compute_late_minutes(new_punch_in_dt, emp["shift_start"], date)

        if new_punch_out_dt is not None:
            duration_sec = int((new_punch_out_dt - new_punch_in_dt).total_seconds())
            if duration_sec <= 0:
                raise HTTPException(status_code=422, detail="punch_out must be strictly after punch_in")
            if duration_sec > 86400:
                raise HTTPException(status_code=422, detail="punch_out cannot exceed 24 hours after punch_in")

            new_work_hours = compute_work_hours(new_punch_in_dt, new_punch_out_dt)
            new_half_day = new_work_hours < 4.50
            new_overtime_minutes = compute_overtime(new_punch_out_dt, emp["shift_start"], emp["shift_end"], date)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Normalize existing record fields for exact change comparison
    old_status = record.get("status")
    old_punch_in = record.get("punch_in")
    if old_punch_in is not None and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)

    old_punch_out = record.get("punch_out")
    if old_punch_out is not None and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    old_work_hours = record.get("work_hours")
    old_late_minutes = int(record.get("late_minutes", 0))
    old_overtime_minutes = int(record.get("overtime_minutes", 0))
    old_half_day = bool(record.get("half_day", False))

    changes: Dict[str, Any] = {}
    if old_status != target_status:
        changes["status"] = {"from": old_status, "to": target_status}
    if old_punch_in != new_punch_in_dt:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in_dt}
    if old_punch_out != new_punch_out_dt:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out_dt}
    if old_work_hours != new_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if old_late_minutes != new_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if old_overtime_minutes != new_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if old_half_day != new_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    # Request that changes nothing is rejected with 422
    if len(changes) == 0:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    history_entry = {
        "at": now_utc_sec(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Concurrency control: match exact history length to prevent lost updates
    curr_history_len = len(record.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, curr_history_len]},
        },
        {
            "$set": {
                "status": target_status,
                "punch_in": new_punch_in_dt,
                "punch_out": new_punch_out_dt,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {
                "history": history_entry
            },
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent regularize modification conflict")

    return serialize_attendance_doc(updated)


@app.get("/analytics/employees/{emp_code}/monthly", tags=["analytics"])
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """R5-R8: Monthly attendance summary for one employee via aggregation."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    # R7: Working days are Mon-Fri counted from joined_on for mid-month joiners
    joined_d = date.fromisoformat(emp["joined_on"])
    month_start_d = date.fromisoformat(start_date)
    month_end_d = date.fromisoformat(end_date)
    eff_start = max(month_start_d, joined_d)

    if eff_start > month_end_d:
        working_days = 0
    else:
        working_days = sum(
            1 for day_idx in range((month_end_d - eff_start).days + 1)
            if (eff_start + timedelta(days=day_idx)).weekday() < 5
        )

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_date, "$lte": end_date},
            }
        },
        {
            "$addFields": {
                "dayOfWeek": {
                    "$isoDayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                }
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                # R7: present_days counts only Mon-Fri records (dayOfWeek <= 5)
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$lte": ["$dayOfWeek", 5]},
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        data = agg_res[0]
        present_days = float(data.get("present_days", 0.0))
        leave_days = int(data.get("leave_days", 0))
        late_count = int(data.get("late_count", 0))
        total_late_minutes = int(data.get("total_late_minutes", 0))
        total_overtime_minutes = int(data.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    # R8: attendance_pct rounded 2 decimals half-up, or null when working_days is 0
    if working_days > 0:
        pct = (Decimal(str(present_days)) / Decimal(str(working_days)) * Decimal(100)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        attendance_pct = float(pct)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", tags=["analytics"])
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """R9: Department summary via cross-collection aggregation; counts headcount with zero logs."""
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    # R9: Start from employees whose joined_on <= end of the month
    match_stage: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
    if department:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", start_date]},
                                    {"$lte": ["$date", end_date]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        # Preserves employees who have zero attendance records in the month
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$addFields": {
                "logDayOfWeek": {
                    "$cond": [
                        {"$ne": ["$logs", None]},
                        {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        None,
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees_set": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$lte": ["$logDayOfWeek", 5]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                ]
                            },
                            {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "valid_work_hours": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": ["$logs", None]},
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            "$$REMOVE",
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$ne": ["$logs", None]}, {"$gt": ["$logs.late_minutes", 0]}]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$cond": [
                            {"$ne": ["$logs", None]},
                            {"$ifNull": ["$logs.late_minutes", 0]},
                            0,
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$ne": ["$logs", None]}, {"$eq": ["$logs.status", "LEAVE"]}]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [{"$ne": ["$logs", None]}, {"$eq": ["$logs.status", "ON_DUTY"]}]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employees_set"},
                "present_days": "$present_days",
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$valid_work_hours"}, 0]},
                        {"$avg": "$valid_work_hours"},
                        None,
                    ]
                },
                "late_count": "$late_count",
                "total_late_minutes": "$total_late_minutes",
                "leave_count": "$leave_count",
                "on_duty_count": "$on_duty_count",
            }
        },
        {"$sort": {"department": 1}},
    ]

    results = list(db.employees.aggregate(pipeline))
    items = []
    for r in results:
        # Omit departments whose headcount is 0 for the month
        if r.get("headcount", 0) == 0:
            continue
        items.append({
            "department": r["department"],
            "headcount": int(r["headcount"]),
            "present_days": round_half_up(r["present_days"], 2),
            "avg_work_hours": round_half_up(r["avg_work_hours"], 2),
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", tags=["analytics"])
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Ranks employees by late minutes; standard competition ranking (1,2,2,4), limit after ranking."""
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    start_date = f"{month}-01"
    end_date = f"{month}-{num_days:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": start_date, "$lte": end_date},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        # Competition ranking: ties share rank, next rank is skipped
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        # limit applies after ranking: include all ties at cutoff rank <= limit
        {"$match": {"rank": {"$lte": limit}}},
        # Deterministic ordering: total_late_minutes desc, then emp_code asc
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": "$rank",
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": "$total_late_minutes",
                "late_count": "$late_count",
            }
        },
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", tags=["analytics"])
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    """Daily attendance trend with database gap filling ($densify) and 7-day moving average."""
    dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))
    if not dept_emps:
        raise HTTPException(status_code=404, detail="Department not found")

    from_d = date.fromisoformat(from_date)
    to_d = date.fromisoformat(to_date)
    if to_d < from_d:
        raise HTTPException(status_code=422, detail="to date cannot be earlier than from date")
    if (to_d - from_d).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Range cannot exceed 92 days")

    dept_codes = [e["emp_code"] for e in dept_emps]

    # Boundaries for $densify (UTC datetime)
    from_dt_utc = datetime(from_d.year, from_d.month, from_d.day, tzinfo=timezone.utc)
    to_plus_one_dt_utc = datetime(to_d.year, to_d.month, to_d.day, tzinfo=timezone.utc) + timedelta(days=1)

    pipeline: List[Dict[str, Any]] = [
        # Match attendance records for department employees in the range
        {
            "$match": {
                "emp_code": {"$in": dept_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                },
            }
        },
        {
            "$project": {
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1,
            }
        },
        # Union with a single seed document ensures $densify works even with 0 logs
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "date_dt": {"$literal": from_dt_utc},
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        # Gap-filling: densify across all days in the range
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt_utc, to_plus_one_dt_utc],
                },
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "day_of_week": {"$isoDayOfWeek": "$date_dt"},
            }
        },
        # Deduplicate days
        {
            "$group": {
                "_id": "$date",
                "present_count": {"$max": "$present_count"},
                "late_count": {"$max": "$late_count"},
                "day_of_week": {"$first": "$day_of_week"},
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "date": "$_id",
                "is_working_day": {"$lte": ["$day_of_week", 5]},
                "present_count": "$present_count",
                "late_count": "$late_count",
            }
        },
        # Lookup headcount of department employees with joined_on <= that day (R9)
        {
            "$lookup": {
                "from": "employees",
                "let": {"curr_date": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$curr_date"]},
                                ]
                            }
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "hc",
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$ifNull": [{"$arrayElemAt": ["$hc.count", 0]}, 0]
                }
            }
        },
        # Attendance rate on working days with headcount > 0; otherwise null
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_working_day", True]},
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        # 7-day moving average over current and up to 6 preceding rows in range
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {"$sort": {"date": 1}},
    ]

    results = list(db.attendance_logs.aggregate(pipeline))
    items = []
    for r in results:
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": round_half_up(r["present_count"], 2),
            "late_count": int(r["late_count"]),
            "attendance_rate": round_half_up(r.get("attendance_rate"), 4),
            "moving_avg_7d": round_half_up(r.get("moving_avg_7d"), 4),
        })

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", tags=["admin"])
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = None,
    from_date: Optional[str] = Query(default=None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(default=None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """Runs MongoDB explain with executionStats verbosity for the named endpoint's query/pipeline."""
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from > date_to")
        q: Dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        if status:
            q["status"] = status
        skip = (page - 1) * page_size
        explain_doc = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_safe(explain_doc)}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": start_date, "$lte": end_date},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                }
            },
        ]
        explain_doc = db.command(
            "aggregate",
            "attendance_logs",
            pipeline=pipeline,
            explain=True,
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_safe(explain_doc)}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        end_date = f"{month}-{num_days:02d}"
        match_stage: Dict[str, Any] = {"joined_on": {"$lte": end_date}}
        if department:
            match_stage["department"] = department
        pipeline = [
            {"$match": match_stage},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "logs",
                }
            },
        ]
        explain_doc = db.command(
            "aggregate",
            "employees",
            pipeline=pipeline,
            explain=True,
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": json_safe(explain_doc)}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        year, m = map(int, month.split("-"))
        num_days = calendar.monthrange(year, m)[1]
        start_date = f"{month}-01"
        end_date = f"{month}-{num_days:02d}"
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": start_date, "$lte": end_date},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                }
            },
        ]
        explain_doc = db.command(
            "aggregate",
            "attendance_logs",
            pipeline=pipeline,
            explain=True,
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_safe(explain_doc)}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": from_date, "$lte": to_date},
                }
            },
            {
                "$group": {
                    "_id": "$date",
                    "total": {"$sum": 1},
                }
            },
        ]
        explain_doc = db.command(
            "aggregate",
            "attendance_logs",
            pipeline=pipeline,
            explain=True,
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": json_safe(explain_doc)}

    else:
        raise HTTPException(status_code=422, detail=f"Unknown endpoint '{endpoint}'")
