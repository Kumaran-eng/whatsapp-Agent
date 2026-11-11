import sqlite3
import os
import json
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

DB_PATH = ROOT / "holidays.db"

app = FastAPI(title="Festive Update Agent", docs_url=None, redoc_url=None)


def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def connect():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def initialize_db():
    db = connect()
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS holidays (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                festival_name TEXT NOT NULL,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_holidays_dates ON holidays(start_date, end_date)")
        db.execute("""
            CREATE TABLE IF NOT EXISTS human_support_escalations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                order_id TEXT,
                summary TEXT NOT NULL,
                transcript TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_read INTEGER NOT NULL DEFAULT 0
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_support_created ON human_support_escalations(created_at)")
        db.commit()
    finally:
        db.close()


@app.on_event("startup")
def startup():
    initialize_db()


@app.middleware("http")
async def keep_support_inbox_local(request: Request, call_next):
    # Escalation records contain customer phone numbers and conversation text.
    if request.url.path.startswith("/api/human-support/"):
        host = request.client.host if request.client else ""
        if host not in {"127.0.0.1", "::1", "localhost", "testclient"} or request.headers.get("x-forwarded-for"):
            return PlainTextResponse("Human support inbox is available on this computer only.", status_code=403)
    return await call_next(request)


@app.get("/")
def home():
    return FileResponse(ROOT / "index.html")


# --- API Endpoints ---

@app.get("/api/holidays")
def list_holidays():
    db = connect()
    try:
        rows = db.execute(
            "SELECT id, festival_name, start_date, end_date, created_at FROM holidays ORDER BY start_date"
        ).fetchall()
        return {"holidays": [dict(row) for row in rows]}
    finally:
        db.close()


class HolidayCreate(BaseModel):
    festival_name: str = Field(min_length=1, max_length=100)
    start_date: str = Field(min_length=10, max_length=10)  # YYYY-MM-DD
    end_date: str = Field(min_length=10, max_length=10)


@app.post("/api/holidays")
def add_holiday(holiday: HolidayCreate):
    # Validate dates
    try:
        start = date.fromisoformat(holiday.start_date)
        end = date.fromisoformat(holiday.end_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must be in YYYY-MM-DD format.")
    if end < start:
        raise HTTPException(status_code=400, detail="End date cannot be before start date.")

    db = connect()
    try:
        cursor = db.execute(
            "INSERT INTO holidays(festival_name, start_date, end_date, created_at) VALUES (?, ?, ?, ?)",
            (holiday.festival_name.strip(), holiday.start_date, holiday.end_date, now()),
        )
        db.commit()
        return {
            "ok": True,
            "id": cursor.lastrowid,
            "detail": f"{holiday.festival_name} holiday added ({holiday.start_date} to {holiday.end_date}).",
        }
    finally:
        db.close()


@app.delete("/api/holidays/{holiday_id}")
def delete_holiday(holiday_id: int):
    db = connect()
    try:
        cursor = db.execute("DELETE FROM holidays WHERE id=?", (holiday_id,))
        db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Holiday not found.")
        return {"ok": True, "detail": "Holiday deleted."}
    finally:
        db.close()


@app.get("/api/holidays/active")
def active_holidays():
    """Return holidays that overlap with today — used by the main agent."""
    today = date.today().isoformat()
    db = connect()
    try:
        rows = db.execute(
            "SELECT id, festival_name, start_date, end_date FROM holidays WHERE start_date <= ? AND end_date >= ? ORDER BY start_date",
            (today, today),
        ).fetchall()
        return {"active": [dict(row) for row in rows], "today": today}
    finally:
        db.close()


@app.get("/api/holidays/upcoming")
def upcoming_holidays():
    """Return holidays in the next 15 days — used by the main agent."""
    today = date.today().isoformat()
    db = connect()
    try:
        rows = db.execute(
            "SELECT id, festival_name, start_date, end_date FROM holidays WHERE end_date >= ? ORDER BY start_date LIMIT 10",
            (today,),
        ).fetchall()
        return {"holidays": [dict(row) for row in rows], "today": today}
    finally:
        db.close()


# --- Human support inbox ---

class SupportMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)
    created_at: str = Field(default="", max_length=64)


class SupportEscalationCreate(BaseModel):
    phone: str = Field(min_length=8, max_length=15, pattern=r"^[0-9]{8,15}$")
    order_id: str | None = Field(default=None, max_length=64)
    summary: str = Field(min_length=1, max_length=4000)
    transcript: list[SupportMessage] = Field(min_length=1, max_length=100)


@app.post("/api/human-support/escalations", status_code=201)
def create_human_support_escalation(escalation: SupportEscalationCreate):
    transcript_json = json.dumps(
        [message.model_dump() for message in escalation.transcript],
        ensure_ascii=False,
    )
    db = connect()
    try:
        cursor = db.execute(
            """INSERT INTO human_support_escalations
               (phone, order_id, summary, transcript, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (escalation.phone, escalation.order_id, escalation.summary, transcript_json, now()),
        )
        db.commit()
        return {"ok": True, "id": cursor.lastrowid, "detail": "Human support request received."}
    finally:
        db.close()


@app.get("/api/human-support/escalations")
def list_human_support_escalations():
    db = connect()
    try:
        rows = db.execute(
            """SELECT id, phone, order_id, summary, created_at, is_read
               FROM human_support_escalations ORDER BY id DESC LIMIT 100"""
        ).fetchall()
        unread = db.execute(
            "SELECT COUNT(*) AS count FROM human_support_escalations WHERE is_read=0"
        ).fetchone()["count"]
        return {
            "escalations": [dict(row) for row in rows],
            "unread_count": unread,
        }
    finally:
        db.close()


@app.get("/api/human-support/escalations/{escalation_id}")
def get_human_support_escalation(escalation_id: int):
    db = connect()
    try:
        row = db.execute(
            """SELECT id, phone, order_id, summary, transcript, created_at, is_read
               FROM human_support_escalations WHERE id=?""",
            (escalation_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Human support request not found.")
        result = dict(row)
        try:
            result["transcript"] = json.loads(result["transcript"])
        except (json.JSONDecodeError, TypeError):
            result["transcript"] = []
        return result
    finally:
        db.close()


@app.post("/api/human-support/escalations/{escalation_id}/read")
def mark_human_support_escalation_read(escalation_id: int):
    db = connect()
    try:
        cursor = db.execute(
            "UPDATE human_support_escalations SET is_read=1 WHERE id=?",
            (escalation_id,),
        )
        db.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Human support request not found.")
        return {"ok": True}
    finally:
        db.close()
