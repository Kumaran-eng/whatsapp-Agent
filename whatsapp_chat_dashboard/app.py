import hashlib
import hmac
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import time as _time

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel, Field
from order_agent import answer_with_order_tools
from memory import (
    initialize_memory_tables,
    build_full_memory_context,
    generate_episode_summary,
    store_episode,
    store_preference,
)
from audit import (
    initialize_audit_table,
    log_incoming_message,
    log_outgoing_reply,
    log_manual_send,
    log_tool_call,
    log_memory_event,
    log_error,
    log_human_handoff,
    get_audit_logs,
    export_audit_to_excel,
)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "").strip()
APP_SECRET = os.getenv("APP_SECRET", "").strip()
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN", "").strip()
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "").strip()
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v25.0").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()
ORDER_API_URL = os.getenv("ORDER_API_URL", "http://127.0.0.1:8000").rstrip("/")
ORDER_API_KEY = os.getenv("ORDER_API_KEY", "").strip()
FESTIVE_API_URL = os.getenv("FESTIVE_API_URL", "http://127.0.0.1:8002").rstrip("/")
DB_PATH = ROOT / "messages.db"

app = FastAPI(title="WhatsApp Chat Dashboard", docs_url=None, redoc_url=None)


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
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wa_message_id TEXT UNIQUE,
                phone TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('incoming','outgoing')),
                text TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_messages_phone_id ON messages(phone, id)")

        # --- Customer Memory Table ---
        db.execute("""
            CREATE TABLE IF NOT EXISTS customer_memory (
                phone TEXT PRIMARY KEY,
                last_order_id TEXT,
                customer_name TEXT,
                notes TEXT,
                updated_at TEXT NOT NULL
            )
        """)

        db.commit()
    finally:
        db.close()


@app.on_event("startup")
def startup():
    initialize_db()
    initialize_memory_tables()  # Create semantic_preferences + episodic_summaries tables
    initialize_audit_table()    # Create audit_log table


@app.middleware("http")
async def keep_dashboard_local(request: Request, call_next):
    # The public tunnel is only for Meta's webhook. Keep chat data and send controls local.
    if request.url.path != "/webhook":
        host = request.client.host if request.client else ""
        if host not in {"127.0.0.1", "::1", "localhost", "testclient"} or request.headers.get("x-forwarded-for"):
            return PlainTextResponse("Dashboard is available on this computer only.", status_code=403)
    return await call_next(request)


@app.get("/")
def home():
    return FileResponse(ROOT / "index.html")


@app.get("/api/status")
async def status():
    return {
        "server": "running",
        "whatsapp_token_set": bool(WHATSAPP_TOKEN),
        "phone_number_id_set": bool(PHONE_NUMBER_ID),
        "verify_token_set": bool(VERIFY_TOKEN),
        "app_secret_set": bool(APP_SECRET),
        "gemini_api_key_set": bool(GEMINI_API_KEY),
        "gemini_model": GEMINI_MODEL,
    }


@app.get("/api/conversations")
def conversations():
    db = connect()
    try:
        rows = db.execute("""
            SELECT m.phone, m.text AS last_message, m.direction, m.status, m.created_at
            FROM messages m
            JOIN (SELECT phone, MAX(id) AS last_id FROM messages GROUP BY phone) latest
              ON latest.last_id = m.id
            ORDER BY m.id DESC
        """).fetchall()
        return {"conversations": [dict(row) for row in rows]}
    finally:
        db.close()


def normalize_phone(value: str) -> str:
    phone = re.sub(r"[^0-9]", "", value)
    if not 8 <= len(phone) <= 15:
        raise HTTPException(status_code=400, detail="Enter a phone number with country code, 8–15 digits.")
    return phone


@app.get("/api/messages/{phone}")
def get_messages(phone: str, after: int = 0):
    phone = normalize_phone(phone)
    db = connect()
    try:
        rows = db.execute(
            "SELECT id, wa_message_id, phone, direction, text, status, created_at FROM messages WHERE phone=? AND id>? ORDER BY id",
            (phone, after),
        ).fetchall()
        return {"messages": [dict(row) for row in rows]}
    finally:
        db.close()


class SendMessage(BaseModel):
    to: str = Field(min_length=8, max_length=24)
    text: str = Field(min_length=1, max_length=4000)


def send_whatsapp_message(phone: str, text: str):
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        raise RuntimeError("Set WHATSAPP_TOKEN and PHONE_NUMBER_ID in .env")
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": phone,
        "type": "text",
        "text": {"preview_url": False, "body": text[:4000]},
    }
    response = httpx.post(url, headers=headers, json=payload, timeout=25)
    try:
        data = response.json()
    except ValueError:
        data = {}
    if not response.is_success:
        error = data.get("error", {})
        raise RuntimeError(f"Meta error {error.get('code', 'unknown')}: {error.get('message', 'Message rejected')}")
    sent = data.get("messages", [])
    return sent[0].get("id") if sent else None


def save_outgoing(phone: str, text: str, wa_id: str | None):
    db = connect()
    try:
        db.execute(
            "INSERT OR IGNORE INTO messages(wa_message_id, phone, direction, text, status, created_at) VALUES (?, ?, 'outgoing', ?, 'accepted', ?)",
            (wa_id, phone, text, now()),
        )
        db.commit()
    finally:
        db.close()


@app.post("/api/send")
def send_message(request: SendMessage):
    phone = normalize_phone(request.to)
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")
    try:
        wa_id = send_whatsapp_message(phone, text)
        save_outgoing(phone, text, wa_id)
        log_manual_send(phone, text)  # Audit
        return {"ok": True, "detail": "Meta accepted the message for delivery.", "wa_message_id": wa_id}
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"Could not connect to Meta ({type(exc).__name__}).")
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


def valid_signature(body: bytes, signature: str) -> bool:
    if not APP_SECRET or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@app.get("/webhook")
async def verify_webhook(request: Request):
    query = request.query_params
    if not VERIFY_TOKEN:
        raise HTTPException(status_code=503, detail="VERIFY_TOKEN is missing from .env")
    if query.get("hub.mode") == "subscribe" and hmac.compare_digest(query.get("hub.verify_token", ""), VERIFY_TOKEN):
        return PlainTextResponse(query.get("hub.challenge", ""))
    raise HTTPException(status_code=403, detail="Webhook verification failed; check VERIFY_TOKEN")


def save_incoming(phone: str, text: str, wa_id: str | None) -> bool:
    db = connect()
    try:
        cursor = db.execute(
            "INSERT OR IGNORE INTO messages(wa_message_id, phone, direction, text, status, created_at) VALUES (?, ?, 'incoming', ?, 'received', ?)",
            (wa_id, phone, text, now()),
        )
        db.commit()
        return cursor.rowcount == 1
    finally:
        db.close()


# =============================================
# CUSTOMER MEMORY LAYER
# =============================================

ORDER_ID_PATTERN = re.compile(r"\bORD[\s\-]?\d{3,}\b", re.IGNORECASE)
HUMAN_SUPPORT_PATTERNS = (
    re.compile(r"\b(?:human(?: support)?|real person|live (?:agent|person)|representative|supervisor|operator)\b", re.IGNORECASE),
    re.compile(r"\b(?:talk|speak|chat|connect|transfer|escalate|hand\s?off)\b.{0,50}\b(?:human|person|agent|representative|supervisor|someone|customer support|customer service|support team)\b", re.IGNORECASE),
    re.compile(r"\b(?:need|want|request|get me)\b.{0,35}\b(?:a\s+)?(?:human|person|agent|representative|supervisor|someone|customer support|customer service|support team)\b", re.IGNORECASE),
)


def get_customer_memory(phone: str) -> dict:
    """Load saved context for this customer."""
    db = connect()
    try:
        row = db.execute(
            "SELECT last_order_id, customer_name, notes FROM customer_memory WHERE phone=?",
            (phone,),
        ).fetchone()
        if row:
            return {
                "last_order_id": row["last_order_id"],
                "customer_name": row["customer_name"],
                "notes": row["notes"],
            }
        return {}
    finally:
        db.close()


def save_customer_memory(phone: str, order_id: str = None, customer_name: str = None, notes: str = None):
    """Save/update context for this customer."""
    db = connect()
    try:
        existing = db.execute("SELECT * FROM customer_memory WHERE phone=?", (phone,)).fetchone()
        if existing:
            updates = []
            params = []
            if order_id:
                updates.append("last_order_id=?")
                params.append(order_id)
            if customer_name:
                updates.append("customer_name=?")
                params.append(customer_name)
            if notes:
                updates.append("notes=?")
                params.append(notes)
            if updates:
                updates.append("updated_at=?")
                params.append(now())
                params.append(phone)
                db.execute(f"UPDATE customer_memory SET {', '.join(updates)} WHERE phone=?", params)
        else:
            db.execute(
                "INSERT INTO customer_memory(phone, last_order_id, customer_name, notes, updated_at) VALUES (?, ?, ?, ?, ?)",
                (phone, order_id, customer_name, notes, now()),
            )
        db.commit()
    finally:
        db.close()


def extract_order_ids(text: str) -> list[str]:
    """Extract order IDs like ORD101, ORD 102, etc. from text."""
    matches = ORDER_ID_PATTERN.findall(text)
    # Normalize: remove spaces/dashes, uppercase
    return [re.sub(r"[\s\-]", "", m).upper() for m in matches]


def requests_human_support(text: str) -> bool:
    """Recognize explicit requests to speak with a human support representative."""
    if re.search(
        r"\b(?:don't|dont|do not|never)\s+(?:want|need|request|send|connect|transfer|speak|talk)\b.{0,50}\b(?:human|person|agent|representative|supervisor|support)\b",
        text or "",
        re.IGNORECASE,
    ):
        return False
    return any(pattern.search(text or "") for pattern in HUMAN_SUPPORT_PATTERNS)


def conversation_for_model(phone: str) -> list[dict]:
    """Build conversation history with customer memory context injected."""
    db = connect()
    try:
        rows = db.execute(
            "SELECT direction,text FROM messages WHERE phone=? ORDER BY id DESC LIMIT 20",
            (phone,),
        ).fetchall()
        rows = list(reversed(rows))
        messages = [
            {"role": "user" if row["direction"] == "incoming" else "assistant", "content": row["text"]}
            for row in rows
        ]
        return messages
    finally:
        db.close()


def conversation_for_handoff(phone: str, limit: int = 100) -> list[dict]:
    """Return a recent, timestamped transcript for the support inbox."""
    db = connect()
    try:
        rows = db.execute(
            "SELECT direction, text, created_at FROM messages WHERE phone=? ORDER BY id DESC LIMIT ?",
            (phone, limit),
        ).fetchall()
        return [
            {
                "role": "user" if row["direction"] == "incoming" else "assistant",
                "content": row["text"][:4000],
                "created_at": row["created_at"],
            }
            for row in reversed(rows)
        ]
    finally:
        db.close()


def fallback_handoff_summary(transcript: list[dict]) -> str:
    """Build a useful summary if Gemini is unavailable."""
    customer_messages = [
        message["content"].strip()
        for message in transcript
        if message["role"] == "user" and message["content"].strip()
    ]
    recent = customer_messages[-5:]
    if not recent:
        return "The customer requested human support. No customer messages were available to summarize."
    excerpts = " | ".join(message[:500] for message in recent)
    return f"The customer requested human support. Recent customer messages: {excerpts}"[:4000]


async def create_human_support_escalation(phone: str, history: list[dict]) -> dict:
    """Summarize the recent conversation and send it to the Festive Agent inbox."""
    transcript = conversation_for_handoff(phone)
    summary = ""
    try:
        episode = await generate_episode_summary(history)
        summary = (episode.get("summary") or "").strip()
    except Exception as exc:
        print(f"HUMAN HANDOFF: Gemini summary unavailable ({type(exc).__name__}).", flush=True)

    if not summary:
        summary = fallback_handoff_summary(transcript)
    elif "human support" not in summary.lower():
        summary = f"Customer requested human support. {summary}"

    conversation_text = " ".join(message["content"] for message in transcript)
    order_ids = extract_order_ids(conversation_text)
    memory = get_customer_memory(phone)
    order_id = order_ids[-1] if order_ids else memory.get("last_order_id")
    payload = {
        "phone": phone,
        "order_id": order_id,
        "summary": summary[:4000],
        "transcript": transcript,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(
            f"{FESTIVE_API_URL}/api/human-support/escalations",
            json=payload,
        )
        response.raise_for_status()
        return response.json()


def build_memory_context(phone: str) -> str:
    """Build a context string from customer memory to inject into the agent."""
    memory = get_customer_memory(phone)
    if not memory:
        return ""

    parts = []
    if memory.get("last_order_id"):
        parts.append(f"Last known order ID: {memory['last_order_id']}")
    if memory.get("customer_name"):
        parts.append(f"Customer name: {memory['customer_name']}")
    if memory.get("notes"):
        parts.append(f"Notes: {memory['notes']}")

    if parts:
        return "CUSTOMER CONTEXT (from previous conversations):\n" + "\n".join(parts)
    return ""


async def generate_and_send_reply(phone: str):
    """Generate one Gemini reply and send it back to the WhatsApp sender."""
    t0 = _time.monotonic()
    try:
        if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
            raise RuntimeError("Missing WHATSAPP_TOKEN or PHONE_NUMBER_ID in .env")

        history = conversation_for_model(phone)

        latest_customer_message = next(
            (message["content"] for message in reversed(history) if message["role"] == "user"),
            "",
        )
        if requests_human_support(latest_customer_message):
            try:
                escalation = await create_human_support_escalation(phone, history)
                reply = "I’ve sent a summary of our conversation to the human support team. A team member will follow up with you here."
                handoff_order_ids = extract_order_ids(" ".join(m["content"] for m in history))
                log_human_handoff(
                    phone=phone,
                    order_id=handoff_order_ids[-1] if handoff_order_ids else get_customer_memory(phone).get("last_order_id"),
                    summary=f"Escalation #{escalation.get('id', '')} sent to the Festive Agent inbox.",
                )
                print(f"HUMAN HANDOFF created for {phone} (escalation #{escalation.get('id', 'unknown')}).", flush=True)
            except Exception as exc:
                log_error(phone, "HUMAN_HANDOFF", f"{type(exc).__name__}: {exc}")
                reply = "I couldn’t connect you to the human support inbox just now. Please try again shortly."
                print(f"HUMAN HANDOFF failed for {phone}: {type(exc).__name__}: {exc}", flush=True)

            sent_id = send_whatsapp_message(phone, reply)
            save_outgoing(phone, reply, sent_id)
            log_outgoing_reply(phone, reply, int((_time.monotonic() - t0) * 1000))
            return

        # Get the current user query for semantic search
        current_query = ""
        for m in reversed(history):
            if m["role"] == "user":
                current_query = m["content"]
                break

        # Build FULL memory context (key-value + semantic preferences + episodic summaries)
        memory_context = build_full_memory_context(phone, current_query)

        # Pass memory context to the agent — now returns (reply, tool_calls_list)
        reply, tool_calls = await answer_with_order_tools(history, memory_context)
        reply = reply.strip()
        if not reply:
            raise RuntimeError("LangGraph returned an empty reply")

        duration_ms = int((_time.monotonic() - t0) * 1000)

        # ── Audit: log every tool call ──
        for tc in tool_calls:
            log_tool_call(
                phone=phone,
                tool_name=tc.get("tool_name", ""),
                tool_input=tc.get("tool_input", ""),
                tool_output=tc.get("tool_output", ""),
                order_id=tc.get("order_id", ""),
            )

        # Extract and save any order IDs from the conversation
        all_text = " ".join(m["content"] for m in history[-4:]) + " " + reply
        order_ids = extract_order_ids(all_text)
        if order_ids:
            save_customer_memory(phone, order_id=order_ids[-1])

        sent_id = send_whatsapp_message(phone, reply)
        save_outgoing(phone, reply, sent_id)

        # ── Audit: log the bot reply ──
        log_outgoing_reply(phone, reply, duration_ms)

        print(f"AUTO-REPLY sent to {phone} using {GEMINI_MODEL} ({duration_ms}ms)", flush=True)

        # --- Save Episodic + Semantic Memory (after reply, non-blocking) ---
        try:
            full_history = history + [{"role": "assistant", "content": reply}]
            episode = await generate_episode_summary(full_history)

            if episode.get("summary"):
                store_episode(phone, episode["summary"], episode.get("key_facts", []))
                log_memory_event(phone, "episodic", episode["summary"][:300])

            for pref in episode.get("preferences", []):
                if pref and isinstance(pref, str) and len(pref.strip()) > 3:
                    store_preference(phone, pref.strip())
                    log_memory_event(phone, "semantic", pref.strip()[:300])
        except Exception as mem_err:
            print(f"MEMORY: Post-reply memory save failed (non-fatal): {mem_err}", flush=True)

    except httpx.ConnectError:
        log_error(phone, "BOT_REPLY", "Gemini API is unavailable")
        print(f"AUTO-REPLY failed for {phone}: Gemini API is unavailable.", flush=True)
    except httpx.TimeoutException:
        log_error(phone, "BOT_REPLY", "Gemini API timed out")
        print(f"AUTO-REPLY failed for {phone}: Gemini API timed out.", flush=True)
    except httpx.HTTPStatusError as exc:
        detail = "Gemini or Meta returned an HTTP error."
        try:
            detail = exc.response.json().get("error", detail)
        except Exception:
            pass
        log_error(phone, "BOT_REPLY", str(detail))
        print(f"AUTO-REPLY failed for {phone}: {detail}", flush=True)
    except Exception as exc:
        log_error(phone, "BOT_REPLY", f"{type(exc).__name__}: {exc}")
        print(f"AUTO-REPLY failed for {phone}: {type(exc).__name__}: {exc}", flush=True)



def update_delivery_status(wa_id: str, status: str):
    db = connect()
    try:
        db.execute("UPDATE messages SET status=? WHERE wa_message_id=?", (status, wa_id))
        db.commit()
    finally:
        db.close()


@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    if not valid_signature(body, request.headers.get("X-Hub-Signature-256", "")):
        print("Webhook rejected: invalid signature. Check APP_SECRET.", flush=True)
        raise HTTPException(status_code=403, detail="Invalid signature; check APP_SECRET")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                phone = message.get("from")
                kind = message.get("type", "unknown")
                if not phone:
                    continue
                is_text = kind == "text"
                text = message.get("text", {}).get("body", "") if is_text else f"[{kind} message received]"
                inserted = save_incoming(phone, text, message.get("id"))
                print(f"INCOMING WhatsApp from {phone}: {text}", flush=True)

                # ── Audit: log incoming message ──
                if inserted:
                    log_incoming_message(phone, text)

                # Extract order IDs from incoming message and save to memory
                if is_text:
                    order_ids = extract_order_ids(text)
                    if order_ids:
                        save_customer_memory(phone, order_id=order_ids[-1])

                # Avoid duplicate replies if Meta retries delivery of the same webhook.
                if inserted and is_text:
                    background_tasks.add_task(generate_and_send_reply, phone)
            for item in value.get("statuses", []):
                wa_id = item.get("id")
                if wa_id:
                    update_delivery_status(wa_id, item.get("status", "unknown"))

    return {"status": "received"}


# =============================================
# AUDIT LOG API
# =============================================

@app.get("/api/audit")
def api_audit_logs(phone: str = Query(None), action: str = Query(None), limit: int = Query(500)):
    """Get audit logs as JSON (for the dashboard)."""
    logs = get_audit_logs(phone=phone, action=action, limit=limit)
    return {"audit_logs": logs, "total": len(logs)}


@app.get("/api/audit/download")
def download_audit_excel(phone: str = Query(None)):
    """Download audit logs as an Excel spreadsheet."""
    try:
        xlsx_bytes = export_audit_to_excel(phone=phone)
        filename = f"audit_log_{phone}.xlsx" if phone else "audit_log_all.xlsx"
        return Response(
            content=xlsx_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Export failed: {exc}")
