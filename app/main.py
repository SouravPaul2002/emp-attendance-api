"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (read from environment, .env as fallback)
"""
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Literal, Optional, Union

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB = os.getenv("MONGO_DB")

if not MONGO_URI:
    raise RuntimeError("MONGO_URI environment variable is required")
if not MONGO_DB:
    raise RuntimeError("MONGO_DB environment variable is required")

client = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=5000,
    tz_aware=True,
    tzinfo=timezone.utc,
)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_db():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True)
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index([("joined_on", ASCENDING)])

    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING)])
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Helpers & Business Logic
# --------------------------------------------------------------------------- #
def validate_date(date_str: Optional[str]) -> Optional[date]:
    if not isinstance(date_str, str):
        return None
    try:
        return date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid calendar date: {date_str}")


def to_utc_dt(epoch_ms: int) -> datetime:
    epoch_s = epoch_ms // 1000
    return datetime.fromtimestamp(epoch_s, tz=timezone.utc)


def to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def round_half_up(val: Optional[Union[float, Decimal]], decimals: int) -> Optional[float]:
    if val is None:
        return None
    fmt = "0." + "0" * decimals if decimals > 0 else "0"
    return float(Decimal(str(val)).quantize(Decimal(fmt), rounding=ROUND_HALF_UP))


def get_attendance_date(punch_in_utc: datetime, shift_start_str: str, shift_end_str: str) -> str:
    punch_in_ist = punch_in_utc.astimezone(IST)
    cal_date = punch_in_ist.date()
    punch_time = punch_in_ist.time()

    h_s, m_s = map(int, shift_start_str.split(":"))
    h_e, m_e = map(int, shift_end_str.split(":"))
    start_t = time(h_s, m_s)
    end_t = time(h_e, m_e)

    is_overnight = end_t <= start_t
    if is_overnight and punch_time < end_t:
        return (cal_date - timedelta(days=1)).isoformat()
    return cal_date.isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start_str: str, date_str: str) -> int:
    att_date = date.fromisoformat(date_str)
    h_s, m_s = map(int, shift_start_str.split(":"))
    shift_start_ist = datetime.combine(att_date, time(h_s, m_s), tzinfo=IST)
    punch_in_ist = punch_in_utc.astimezone(IST).replace(microsecond=0)

    diff_seconds = int((punch_in_ist - shift_start_ist).total_seconds())
    if diff_seconds <= 600:
        return 0
    return diff_seconds // 60


def compute_overtime(punch_out_utc: datetime, shift_start_str: str, shift_end_str: str, date_str: str) -> int:
    att_date = date.fromisoformat(date_str)
    h_s, m_s = map(int, shift_start_str.split(":"))
    h_e, m_e = map(int, shift_end_str.split(":"))
    start_t = time(h_s, m_s)
    end_t = time(h_e, m_e)

    is_overnight = end_t <= start_t
    shift_end_date = att_date + timedelta(days=1) if is_overnight else att_date
    shift_end_ist = datetime.combine(shift_end_date, end_t, tzinfo=IST)
    punch_out_ist = punch_out_utc.astimezone(IST).replace(microsecond=0)

    diff_seconds = int((punch_out_ist - shift_end_ist).total_seconds())
    if diff_seconds < 0:
        return 0
    minutes = diff_seconds // 60
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> Optional[float]:
    diff_seconds = int((punch_out_utc.replace(microsecond=0) - punch_in_utc.replace(microsecond=0)).total_seconds())
    if diff_seconds < 0:
        return 0.0
    hours_dec = Decimal(diff_seconds) / Decimal(3600)
    return float(hours_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return bool(work_hours < 4.50)


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc.get("created_at")),
    }


def serialize_attendance_record(doc: dict) -> dict:
    hist = []
    for h in doc.get("history", []):
        c_map = {}
        for k, v in h.get("changes", {}).items():
            from_v = v.get("from")
            to_v = v.get("to")
            if k in ("punch_in", "punch_out"):
                if isinstance(from_v, datetime):
                    from_v = to_epoch_ms(from_v)
                if isinstance(to_v, datetime):
                    to_v = to_epoch_ms(to_v)
            c_map[k] = {"from": from_v, "to": to_v}
        hist.append({
            "at": to_epoch_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": c_map
        })
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": hist
    }


# --------------------------------------------------------------------------- #
# Request Schemas
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")

    @model_validator(mode="after")
    def validate_fields(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError(f"Invalid calendar date for joined_on: {self.joined_on}")
        return self


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(None, strict=True, ge=100000000000, le=4102444800000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(None, strict=True, ge=100000000000, le=4102444800000)


class RegularizeRequest(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(None, strict=True, ge=100000000000, le=4102444800000)
    punch_out: Optional[StrictInt] = Field(None, strict=True, ge=100000000000, le=4102444800000)
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# System Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


# --------------------------------------------------------------------------- #
# Employee Endpoints
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    validate_date(body.joined_on)
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    doc = body.model_dump()
    doc["created_at"] = now_utc
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee(e) for e in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance Endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_dt = to_utc_dt(body.punched_at)
    else:
        punch_in_dt = datetime.now(timezone.utc).replace(microsecond=0)

    date_str = get_attendance_date(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_dt, emp["shift_start"], date_str)

    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return serialize_attendance_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_dt = to_utc_dt(body.punched_at)
    else:
        punch_out_dt = datetime.now(timezone.utc).replace(microsecond=0)

    rec = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punch_out_dt, "$ne": None},
        },
        sort=[("punch_in", DESCENDING)]
    )
    if not rec:
        raise HTTPException(status_code=404, detail="No matching punch-in found")

    if rec.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    punch_in_dt = rec["punch_in"]
    if punch_in_dt.tzinfo is None:
        punch_in_dt = punch_in_dt.replace(tzinfo=timezone.utc)

    diff_seconds = int((punch_out_dt - punch_in_dt).total_seconds())
    if diff_seconds <= 0:
        raise HTTPException(status_code=422, detail="punch_out must be strictly greater than punch_in")
    if diff_seconds > 86400:
        raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime = compute_overtime(punch_out_dt, emp["shift_start"], emp["shift_end"], rec["date"])
    half_day = compute_half_day(work_hours)

    punch_filter = {
        "emp_code": body.emp_code,
        "date": rec["date"],
        "$or": [{"punch_out": None}, {"punch_out": {"$exists": False}}],
        "punch_in": rec["punch_in"],
        "status": rec.get("status"),
    }

    updated = db.attendance_logs.find_one_and_update(
        punch_filter,
        {
            "$set": {
                "punch_out": punch_out_dt,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Record is already punched out or was concurrently modified")

    return serialize_attendance_record(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        validate_date(date_from)
    if date_to:
        validate_date(date_to)

    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be less than or equal to date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(skip).limit(page_size)
    items = [serialize_attendance_record(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...
):
    validate_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=404, detail="Attendance record not found")

    # Status ABSENT or LEAVE clears both punch times; supplying punch_in or punch_out is 422
    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(status_code=422, detail="Cannot supply punch_in or punch_out when setting ABSENT or LEAVE")

    final_status = body.status if body.status is not None else rec["status"]

    if final_status in ("ABSENT", "LEAVE"):
        final_punch_in = None
        final_punch_out = None
    else:
        if body.punch_in is not None:
            final_punch_in = to_utc_dt(body.punch_in)
        else:
            final_punch_in = rec.get("punch_in")
            if final_punch_in is not None and final_punch_in.tzinfo is None:
                final_punch_in = final_punch_in.replace(tzinfo=timezone.utc)

        if final_punch_in is None:
            raise HTTPException(status_code=422, detail="A presence status requires a punch_in")

        if body.punch_out is not None:
            final_punch_out = to_utc_dt(body.punch_out)
        else:
            final_punch_out = rec.get("punch_out")
            if final_punch_out is not None and final_punch_out.tzinfo is None:
                final_punch_out = final_punch_out.replace(tzinfo=timezone.utc)

    # punch_in must stay on the record's attendance date (R1)
    if final_punch_in is not None:
        computed_date = get_attendance_date(final_punch_in, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(status_code=422, detail="punch_in does not belong to the record's attendance date")

    # punch_out must be after punch_in and within 24 hours
    if final_punch_out is not None and final_punch_in is not None:
        diff_sec = int((final_punch_out - final_punch_in).total_seconds())
        if diff_sec <= 0:
            raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
        if diff_sec > 86400:
            raise HTTPException(status_code=422, detail="punch_out must be within 24 hours of punch_in")

    # Recompute derived fields
    if final_status in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(final_punch_in, emp["shift_start"], date)
        if final_punch_out is not None:
            new_work_hours = compute_work_hours(final_punch_in, final_punch_out)
            new_overtime_minutes = compute_overtime(final_punch_out, emp["shift_start"], emp["shift_end"], date)
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Check for actual changes
    field_changes = {}

    old_status = rec.get("status")
    if old_status != final_status:
        field_changes["status"] = {"from": old_status, "to": final_status}

    old_pi = rec.get("punch_in")
    if old_pi is not None and old_pi.tzinfo is None:
        old_pi = old_pi.replace(tzinfo=timezone.utc)
    if old_pi != final_punch_in:
        field_changes["punch_in"] = {"from": old_pi, "to": final_punch_in}

    old_po = rec.get("punch_out")
    if old_po is not None and old_po.tzinfo is None:
        old_po = old_po.replace(tzinfo=timezone.utc)
    if old_po != final_punch_out:
        field_changes["punch_out"] = {"from": old_po, "to": final_punch_out}

    old_wh = rec.get("work_hours")
    if old_wh != new_work_hours:
        field_changes["work_hours"] = {"from": old_wh, "to": new_work_hours}

    old_lm = rec.get("late_minutes", 0)
    if old_lm != new_late_minutes:
        field_changes["late_minutes"] = {"from": old_lm, "to": new_late_minutes}

    old_ot = rec.get("overtime_minutes", 0)
    if old_ot != new_overtime_minutes:
        field_changes["overtime_minutes"] = {"from": old_ot, "to": new_overtime_minutes}

    old_hd = rec.get("half_day", False)
    if old_hd != new_half_day:
        field_changes["half_day"] = {"from": old_hd, "to": new_half_day}

    if not field_changes:
        raise HTTPException(status_code=422, detail="Request changes nothing")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": field_changes,
    }

    set_update = {
        "status": final_status,
        "punch_in": final_punch_in,
        "punch_out": final_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }

    filter_query = {
        "emp_code": emp_code,
        "date": date,
        "status": rec.get("status"),
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
    }

    if "history" in rec and rec["history"]:
        filter_query["history"] = rec["history"]
    else:
        filter_query["$or"] = [
            {"history": {"$exists": False}},
            {"history": []}
        ]

    updated = db.attendance_logs.find_one_and_update(
        filter_query,
        {
            "$set": set_update,
            "$push": {"history": history_entry}
        },
        return_document=ReturnDocument.AFTER
    )
    if not updated:
        raise HTTPException(status_code=409, detail="Concurrent edit conflict")

    return serialize_attendance_record(updated)


# --------------------------------------------------------------------------- #
# Analytics Endpoints
# --------------------------------------------------------------------------- #
def get_month_days(month: str) -> int:
    y, m = map(int, month.split("-"))
    if m == 12:
        next_m = date(y + 1, 1, 1)
    else:
        next_m = date(y, m + 1, 1)
    return (next_m - timedelta(days=1)).day


def count_working_days(month: str, joined_on_str: str) -> int:
    y, m = map(int, month.split("-"))
    last_day = get_month_days(month)
    month_start = date(y, m, 1)
    month_end = date(y, m, last_day)

    joined_date = date.fromisoformat(joined_on_str)
    start_date = max(month_start, joined_date)
    if start_date > month_end:
        return 0

    total_days = (month_end - start_date).days + 1
    return sum(1 for i in range(total_days) if (start_date + timedelta(days=i)).weekday() < 5)


def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    last_d = get_month_days(month)
    start_str = f"{month}-01"
    end_str = f"{month}-{last_d:02d}"
    return [
        {"$match": {
            "emp_code": emp_code,
            "date": {"$gte": start_str, "$lte": end_str}
        }},
        {"$addFields": {
            "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
            "is_present_status": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
        }},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_present_status",
                                {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]}
                            ]
                        },
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                        0.0
                    ]
                }
            },
            "leave_days": {
                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
            },
            "total_late_minutes": {
                "$sum": {"$ifNull": ["$late_minutes", 0]}
            },
            "total_overtime_minutes": {
                "$sum": {"$ifNull": ["$overtime_minutes", 0]}
            }
        }}
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    w_days = count_working_days(month, emp["joined_on"])
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    res = list(db.attendance_logs.aggregate(pipeline))

    if res:
        doc = res[0]
        present_days = float(doc["present_days"])
        leave_days = int(doc["leave_days"])
        late_count = int(doc["late_count"])
        total_late = int(doc["total_late_minutes"])
        total_ot = int(doc["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if w_days > 0:
        pct_dec = (Decimal(str(present_days)) / Decimal(str(w_days))) * Decimal(100)
        attendance_pct = round_half_up(pct_dec, 2)
    else:
        attendance_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": w_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct
    }


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    last_d = get_month_days(month)
    start_str = f"{month}-01"
    end_str = f"{month}-{last_d:02d}"

    match_emp: Dict[str, Any] = {"joined_on": {"$lte": end_str}}
    if department:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {"$lookup": {
            "from": "attendance_logs",
            "let": {"emp": "$emp_code"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$emp_code", "$$emp"]},
                            {"$gte": ["$date", start_str]},
                            {"$lte": ["$date", end_str]}
                        ]
                    }
                }},
                {"$addFields": {
                    "day_of_week": {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                    "is_present_status": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
                }}
            ],
            "as": "logs"
        }},
        {"$unwind": {
            "path": "$logs",
            "preserveNullAndEmptyArrays": True
        }},
        {"$group": {
            "_id": "$department",
            "employee_set": {"$addToSet": "$emp_code"},
            "present_days": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$logs.is_present_status", True]},
                                {"$in": ["$logs.day_of_week", [2, 3, 4, 5, 6]]}
                            ]
                        },
                        {"$cond": [{"$eq": ["$logs.half_day", True]}, 0.5, 1.0]},
                        0.0
                    ]
                }
            },
            "work_hours_sum": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$logs.is_present_status", True]},
                                {"$ne": ["$logs.work_hours", None]}
                            ]
                        },
                        "$logs.work_hours",
                        0.0
                    ]
                }
            },
            "work_hours_count": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$logs.is_present_status", True]},
                                {"$ne": ["$logs.work_hours", None]}
                            ]
                        },
                        1,
                        0
                    ]
                }
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": ["$logs.late_minutes", 0]}, 1, 0]}
            },
            "total_late_minutes": {
                "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
            },
            "leave_count": {
                "$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}
            },
            "on_duty_count": {
                "$sum": {"$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]}
            }
        }},
        {"$project": {
            "_id": 0,
            "department": "$_id",
            "headcount": {"$size": "$employee_set"},
            "present_days": 1,
            "work_hours_sum": 1,
            "work_hours_count": 1,
            "late_count": 1,
            "total_late_minutes": 1,
            "leave_count": 1,
            "on_duty_count": 1
        }},
        {"$sort": {"department": 1}}
    ]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None
):
    pipeline = build_department_summary_pipeline(month, department)
    results = list(db.employees.aggregate(pipeline))

    items = []
    for r in results:
        count = r["work_hours_count"]
        if count > 0:
            avg_wh = round_half_up(Decimal(str(r["work_hours_sum"])) / Decimal(str(count)), 2)
        else:
            avg_wh = None

        items.append({
            "department": r["department"],
            "headcount": r["headcount"],
            "present_days": float(r["present_days"]),
            "avg_work_hours": avg_wh,
            "late_count": int(r["late_count"]),
            "total_late_minutes": int(r["total_late_minutes"]),
            "leave_count": int(r["leave_count"]),
            "on_duty_count": int(r["on_duty_count"]),
        })

    return {"month": month, "items": items}


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> List[Dict[str, Any]]:
    last_d = get_month_days(month)
    start_str = f"{month}-01"
    end_str = f"{month}-{last_d:02d}"

    pipeline: List[Dict[str, Any]] = [
        {"$match": {
            "date": {"$gte": start_str, "$lte": end_str},
            "late_minutes": {"$gt": 0}
        }},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": "$late_minutes"},
            "late_count": {"$sum": 1}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "_id",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {"$project": {
            "_id": 0,
            "emp_code": "$_id",
            "name": "$emp.name",
            "department": "$emp.department",
            "total_late_minutes": 1,
            "late_count": 1
        }},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {
                "rank": {"$rank": {}}
            }
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ])
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> List[Dict[str, Any]]:
    from_dt = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
    to_dt = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc) + timedelta(days=1)

    return [
        {"$match": {
            "date": {"$gte": from_date, "$lte": to_date}
        }},
        {"$lookup": {
            "from": "employees",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "as": "emp"
        }},
        {"$unwind": "$emp"},
        {"$match": {
            "emp.department": department
        }},
        {"$unionWith": {
            "pipeline": [
                {"$documents": [{"date": from_date, "is_dummy": True}]}
            ]
        }},
        {"$addFields": {
            "dt": {"$dateFromString": {"dateString": "$date"}}
        }},
        {"$densify": {
            "field": "dt",
            "range": {
                "step": 1,
                "unit": "day",
                "bounds": [from_dt, to_dt]
            }
        }},
        {"$addFields": {
            "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$dt"}},
            "is_dummy": {"$ifNull": ["$is_dummy", False]}
        }},
        {"$group": {
            "_id": "$date_str",
            "dt": {"$first": "$dt"},
            "present_count": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_dummy", False]},
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}
                            ]
                        },
                        {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                        0.0
                    ]
                }
            },
            "late_count": {
                "$sum": {
                    "$cond": [
                        {
                            "$and": [
                                {"$eq": ["$is_dummy", False]},
                                {"$gt": ["$late_minutes", 0]}
                            ]
                        },
                        1,
                        0
                    ]
                }
            }
        }},
        {"$lookup": {
            "from": "employees",
            "let": {"cur_date": "$_id"},
            "pipeline": [
                {"$match": {
                    "$expr": {
                        "$and": [
                            {"$eq": ["$department", department]},
                            {"$lte": ["$joined_on", "$$cur_date"]}
                        ]
                    }
                }},
                {"$count": "c"}
            ],
            "as": "hc"
        }},
        {"$project": {
            "_id": 0,
            "date": "$_id",
            "dt": 1,
            "day_of_week": {"$dayOfWeek": "$dt"},
            "present_count": 1,
            "late_count": 1,
            "headcount": {"$ifNull": [{"$first": "$hc.c"}, 0]}
        }},
        {"$addFields": {
            "is_working_day": {"$in": ["$day_of_week", [2, 3, 4, 5, 6]]}
        }},
        {"$addFields": {
            "attendance_rate": {
                "$cond": [
                    {
                        "$and": [
                            {"$eq": ["$is_working_day", True]},
                            {"$gt": ["$headcount", 0]}
                        ]
                    },
                    {"$divide": ["$present_count", "$headcount"]},
                    None
                ]
            }
        }},
        {"$setWindowFields": {
            "sortBy": {"date": 1},
            "output": {
                "moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {
                        "documents": [-6, "current"]
                    }
                }
            }
        }},
        {"$project": {"dt": 0, "day_of_week": 0}},
        {"$sort": {"date": 1}}
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")
):
    d_from = validate_date(from_date)
    d_to = validate_date(to_date)
    if not d_from or not d_to:
        raise HTTPException(status_code=422, detail="Valid 'from' and 'to' calendar dates are required")

    if to_date < from_date:
        raise HTTPException(status_code=422, detail="'to' must be greater than or equal to 'from'")

    if (d_to - d_from).days + 1 > 92:
        raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")

    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    raw_items = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for r in raw_items:
        att_rate = round_half_up(r.get("attendance_rate"), 4)
        m_avg = round_half_up(r.get("moving_avg_7d"), 4)
        items.append({
            "date": r["date"],
            "is_working_day": bool(r["is_working_day"]),
            "headcount": int(r["headcount"]),
            "present_count": float(r["present_count"]),
            "late_count": int(r["late_count"]),
            "attendance_rate": att_rate,
            "moving_avg_7d": m_avg
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    if date_from:
        validate_date(date_from)
    if date_to:
        validate_date(date_to)
    if from_date:
        validate_date(from_date)
    if to_date:
        validate_date(to_date)

    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to
        skip = (page - 1) * page_size
        find_cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size
        }
        res = db.command("explain", find_cmd, verbosity="executionStats")
        clean_exp = json.loads(json_util.dumps(res))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_exp}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {}
        }
        res = db.command("explain", cmd, verbosity="executionStats")
        clean_exp = json.loads(json_util.dumps(res))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_exp}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = build_department_summary_pipeline(month, department)
        cmd = {
            "aggregate": "employees",
            "pipeline": pipeline,
            "cursor": {}
        }
        res = db.command("explain", cmd, verbosity="executionStats")
        clean_exp = json.loads(json_util.dumps(res))
        return {"endpoint": endpoint, "collection": "employees", "explain": clean_exp}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = build_late_leaderboard_pipeline(month, limit, department)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {}
        }
        res = db.command("explain", cmd, verbosity="executionStats")
        clean_exp = json.loads(json_util.dumps(res))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_exp}

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        if to_date < from_date:
            raise HTTPException(status_code=422, detail="'to' must be >= 'from'")
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        cmd = {
            "aggregate": "attendance_logs",
            "pipeline": pipeline,
            "cursor": {}
        }
        res = db.command("explain", cmd, verbosity="executionStats")
        clean_exp = json.loads(json_util.dumps(res))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": clean_exp}

    raise HTTPException(status_code=422, detail="Unsupported endpoint")
