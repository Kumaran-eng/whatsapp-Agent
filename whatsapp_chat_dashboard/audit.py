"""
Audit Log — Tracks every action in the system
================================================
Logs: incoming messages, outgoing replies, tool calls, memory operations.
Exports to Excel (.xlsx) on demand.
"""

import io
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "messages.db"


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _connect():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def initialize_audit_table():
    """Create the audit_log table if it doesn't exist."""
    db = _connect()
    try:
        db.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                phone TEXT,
                action TEXT NOT NULL,
                direction TEXT,
                order_id TEXT,
                tool_name TEXT,
                tool_input TEXT,
                tool_output TEXT,
                message_text TEXT,
                reply_text TEXT,
                memory_type TEXT,
                memory_data TEXT,
                duration_ms INTEGER,
                status TEXT DEFAULT 'success',
                error TEXT
            )
        """)
        db.execute("CREATE INDEX IF NOT EXISTS idx_audit_phone ON audit_log(phone)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action)")
        db.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(timestamp)")
        db.commit()
    finally:
        db.close()


# =============================================
# LOG FUNCTIONS
# =============================================

def log_incoming_message(phone: str, text: str):
    """Log an incoming WhatsApp message."""
    db = _connect()
    try:
        db.execute(
            "INSERT INTO audit_log(timestamp, phone, action, direction, message_text) VALUES (?,?,?,?,?)",
            (_now(), phone, "INCOMING_MESSAGE", "incoming", text),
        )
        db.commit()
    except Exception as e:
        print(f"AUDIT: log_incoming_message failed: {e}", flush=True)
    finally:
        db.close()


def log_outgoing_reply(phone: str, reply_text: str, duration_ms: int = None):
    """Log an outgoing bot reply."""
    db = _connect()
    try:
        db.execute(
            "INSERT INTO audit_log(timestamp, phone, action, direction, reply_text, duration_ms) VALUES (?,?,?,?,?,?)",
            (_now(), phone, "BOT_REPLY", "outgoing", reply_text, duration_ms),
        )
        db.commit()
    except Exception as e:
        print(f"AUDIT: log_outgoing_reply failed: {e}", flush=True)
    finally:
        db.close()


def log_manual_send(phone: str, text: str):
    """Log a message sent manually from the dashboard."""
    db = _connect()
    try:
        db.execute(
            "INSERT INTO audit_log(timestamp, phone, action, direction, message_text) VALUES (?,?,?,?,?)",
            (_now(), phone, "MANUAL_SEND", "outgoing", text),
        )
        db.commit()
    except Exception as e:
        print(f"AUDIT: log_manual_send failed: {e}", flush=True)
    finally:
        db.close()


def log_tool_call(phone: str, tool_name: str, tool_input: str, tool_output: str, order_id: str = None):
    """Log a tool call made by the LangGraph agent."""
    db = _connect()
    try:
        db.execute(
            "INSERT INTO audit_log(timestamp, phone, action, tool_name, tool_input, tool_output, order_id) VALUES (?,?,?,?,?,?,?)",
            (_now(), phone, "TOOL_CALL", tool_name, tool_input, tool_output, order_id),
        )
        db.commit()
    except Exception as e:
        print(f"AUDIT: log_tool_call failed: {e}", flush=True)
    finally:
        db.close()


def log_memory_event(phone: str, memory_type: str, memory_data: str):
    """Log a memory save/load event."""
    db = _connect()
    try:
        db.execute(
            "INSERT INTO audit_log(timestamp, phone, action, memory_type, memory_data) VALUES (?,?,?,?,?)",
            (_now(), phone, "MEMORY_SAVE", memory_type, memory_data),
        )
        db.commit()
    except Exception as e:
        print(f"AUDIT: log_memory_event failed: {e}", flush=True)
    finally:
        db.close()


def log_human_handoff(phone: str, order_id: str = None, summary: str = ""):
    """Log a customer request delivered to the human support inbox."""
    db = _connect()
    try:
        db.execute(
            """INSERT INTO audit_log(timestamp, phone, action, order_id, tool_name, tool_output)
               VALUES (?,?,?,?,?,?)""",
            (_now(), phone, "HUMAN_HANDOFF", order_id, "festive_support_inbox", summary),
        )
        db.commit()
    except Exception as e:
        print(f"AUDIT: log_human_handoff failed: {e}", flush=True)
    finally:
        db.close()


def log_error(phone: str, action: str, error: str):
    """Log an error."""
    db = _connect()
    try:
        db.execute(
            "INSERT INTO audit_log(timestamp, phone, action, status, error) VALUES (?,?,?,?,?)",
            (_now(), phone, action, "error", error),
        )
        db.commit()
    except Exception as e:
        print(f"AUDIT: log_error failed: {e}", flush=True)
    finally:
        db.close()


# =============================================
# QUERY LOGS
# =============================================

def get_audit_logs(phone: str = None, action: str = None, limit: int = 500) -> list[dict]:
    """Get audit logs with optional filters."""
    db = _connect()
    try:
        query = "SELECT * FROM audit_log WHERE 1=1"
        params = []
        if phone:
            query += " AND phone=?"
            params.append(phone)
        if action:
            query += " AND action=?"
            params.append(action)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = db.execute(query, params).fetchall()
        return [dict(r) for r in reversed(rows)]  # Chronological order
    finally:
        db.close()


# =============================================
# EXCEL EXPORT
# =============================================

def _truncate(text, max_len=500):
    """Truncate text for Excel cells."""
    if not text:
        return ""
    text = str(text)
    return text[:max_len] + "..." if len(text) > max_len else text


def export_audit_to_excel(phone: str = None) -> bytes:
    """Export audit logs to an Excel file and return as bytes."""
    logs = get_audit_logs(phone=phone, limit=5000)

    wb = Workbook()

    # ── Sheet 1: Full Audit Log ──
    ws1 = wb.active
    ws1.title = "Audit Log"

    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="087F5B", end_color="087F5B", fill_type="solid")
    thin_border = Border(
        left=Side(style="thin", color="D0D0D0"),
        right=Side(style="thin", color="D0D0D0"),
        top=Side(style="thin", color="D0D0D0"),
        bottom=Side(style="thin", color="D0D0D0"),
    )
    wrap_align = Alignment(wrap_text=True, vertical="top")

    headers = [
        "ID", "Timestamp", "Phone", "Action", "Direction",
        "Order ID", "Tool Name", "Tool Input", "Tool Output",
        "Customer Message", "Bot Reply", "Memory Type", "Memory Data",
        "Duration (ms)", "Status", "Error",
    ]

    for col_idx, header in enumerate(headers, 1):
        cell = ws1.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = thin_border
        cell.alignment = Alignment(horizontal="center", vertical="center")

    # Color mapping for action types
    action_colors = {
        "INCOMING_MESSAGE": PatternFill(start_color="E3F2FD", end_color="E3F2FD", fill_type="solid"),  # Blue
        "BOT_REPLY": PatternFill(start_color="E8F5E9", end_color="E8F5E9", fill_type="solid"),  # Green
        "TOOL_CALL": PatternFill(start_color="FFF3E0", end_color="FFF3E0", fill_type="solid"),  # Orange
        "MANUAL_SEND": PatternFill(start_color="F3E5F5", end_color="F3E5F5", fill_type="solid"),  # Purple
        "MEMORY_SAVE": PatternFill(start_color="FFF9C4", end_color="FFF9C4", fill_type="solid"),  # Yellow
    }

    for row_idx, log in enumerate(logs, 2):
        values = [
            log.get("id"),
            log.get("timestamp", ""),
            log.get("phone", ""),
            log.get("action", ""),
            log.get("direction", ""),
            log.get("order_id", ""),
            log.get("tool_name", ""),
            _truncate(log.get("tool_input", "")),
            _truncate(log.get("tool_output", "")),
            _truncate(log.get("message_text", "")),
            _truncate(log.get("reply_text", "")),
            log.get("memory_type", ""),
            _truncate(log.get("memory_data", "")),
            log.get("duration_ms"),
            log.get("status", ""),
            log.get("error", ""),
        ]
        row_fill = action_colors.get(log.get("action"), None)
        for col_idx, val in enumerate(values, 1):
            cell = ws1.cell(row=row_idx, column=col_idx, value=val if val else "")
            cell.border = thin_border
            cell.alignment = wrap_align
            if row_fill:
                cell.fill = row_fill

    # Column widths
    col_widths = [6, 22, 16, 18, 10, 12, 16, 30, 30, 40, 40, 14, 30, 12, 10, 30]
    for i, w in enumerate(col_widths, 1):
        ws1.column_dimensions[get_column_letter(i)].width = w

    ws1.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(logs) + 1}"
    ws1.freeze_panes = "A2"

    # ── Sheet 2: Conversations (grouped by phone) ──
    ws2 = wb.create_sheet("Conversations")
    conv_headers = ["Timestamp", "Phone", "Direction", "Message"]
    for col_idx, header in enumerate(conv_headers, 1):
        cell = ws2.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = thin_border

    conv_logs = [l for l in logs if l.get("action") in ("INCOMING_MESSAGE", "BOT_REPLY", "MANUAL_SEND")]
    for row_idx, log in enumerate(conv_logs, 2):
        action = log.get("action", "")
        text = log.get("message_text") or log.get("reply_text") or ""
        direction = "Customer →" if action == "INCOMING_MESSAGE" else "← Bot" if action == "BOT_REPLY" else "← Manual"
        values = [log.get("timestamp", ""), log.get("phone", ""), direction, _truncate(text, 1000)]
        row_fill = action_colors.get(action, None)
        for col_idx, val in enumerate(values, 1):
            cell = ws2.cell(row=row_idx, column=col_idx, value=val)
            cell.border = thin_border
            cell.alignment = wrap_align
            if row_fill:
                cell.fill = row_fill

    ws2.column_dimensions["A"].width = 22
    ws2.column_dimensions["B"].width = 16
    ws2.column_dimensions["C"].width = 14
    ws2.column_dimensions["D"].width = 80
    ws2.auto_filter.ref = f"A1:D{len(conv_logs) + 1}"
    ws2.freeze_panes = "A2"

    # ── Sheet 3: Tool Calls ──
    ws3 = wb.create_sheet("Tool Calls")
    tool_headers = ["Timestamp", "Phone", "Order ID", "Tool Name", "Input", "Output"]
    for col_idx, header in enumerate(tool_headers, 1):
        cell = ws3.cell(row=1, column=col_idx, value=header)
        cell.font = header_font
        cell.fill = PatternFill(start_color="E65100", end_color="E65100", fill_type="solid")
        cell.border = thin_border

    tool_logs = [l for l in logs if l.get("action") == "TOOL_CALL"]
    for row_idx, log in enumerate(tool_logs, 2):
        values = [
            log.get("timestamp", ""), log.get("phone", ""), log.get("order_id", ""),
            log.get("tool_name", ""), _truncate(log.get("tool_input", "")),
            _truncate(log.get("tool_output", "")),
        ]
        for col_idx, val in enumerate(values, 1):
            cell = ws3.cell(row=row_idx, column=col_idx, value=val)
            cell.border = thin_border
            cell.alignment = wrap_align
            cell.fill = action_colors["TOOL_CALL"]

    ws3.column_dimensions["A"].width = 22
    ws3.column_dimensions["B"].width = 16
    ws3.column_dimensions["C"].width = 12
    ws3.column_dimensions["D"].width = 18
    ws3.column_dimensions["E"].width = 40
    ws3.column_dimensions["F"].width = 50
    ws3.auto_filter.ref = f"A1:F{len(tool_logs) + 1}"
    ws3.freeze_panes = "A2"

    # ── Sheet 4: Summary Stats ──
    ws4 = wb.create_sheet("Summary")
    stats = {
        "Total Events": len(logs),
        "Incoming Messages": sum(1 for l in logs if l["action"] == "INCOMING_MESSAGE"),
        "Bot Replies": sum(1 for l in logs if l["action"] == "BOT_REPLY"),
        "Manual Sends": sum(1 for l in logs if l["action"] == "MANUAL_SEND"),
        "Tool Calls": sum(1 for l in logs if l["action"] == "TOOL_CALL"),
        "Memory Events": sum(1 for l in logs if l["action"] == "MEMORY_SAVE"),
        "Errors": sum(1 for l in logs if l.get("status") == "error"),
        "Unique Customers": len(set(l.get("phone", "") for l in logs if l.get("phone"))),
    }

    # Unique orders
    order_ids = set()
    for l in logs:
        if l.get("order_id"):
            order_ids.add(l["order_id"])
    stats["Unique Orders Queried"] = len(order_ids)

    ws4.cell(row=1, column=1, value="Metric").font = header_font
    ws4.cell(row=1, column=1).fill = header_fill
    ws4.cell(row=1, column=2, value="Value").font = header_font
    ws4.cell(row=1, column=2).fill = header_fill

    for row_idx, (metric, value) in enumerate(stats.items(), 2):
        ws4.cell(row=row_idx, column=1, value=metric).border = thin_border
        cell = ws4.cell(row=row_idx, column=2, value=value)
        cell.border = thin_border
        cell.font = Font(bold=True, size=12)

    ws4.column_dimensions["A"].width = 28
    ws4.column_dimensions["B"].width = 16

    # Tool call breakdown
    tool_counts = {}
    for l in tool_logs:
        name = l.get("tool_name", "unknown")
        tool_counts[name] = tool_counts.get(name, 0) + 1

    if tool_counts:
        row_start = len(stats) + 4
        ws4.cell(row=row_start, column=1, value="Tool Call Breakdown").font = Font(bold=True, size=12)
        ws4.cell(row=row_start + 1, column=1, value="Tool Name").font = header_font
        ws4.cell(row=row_start + 1, column=1).fill = PatternFill(start_color="E65100", end_color="E65100", fill_type="solid")
        ws4.cell(row=row_start + 1, column=2, value="Count").font = header_font
        ws4.cell(row=row_start + 1, column=2).fill = PatternFill(start_color="E65100", end_color="E65100", fill_type="solid")
        for i, (tool, count) in enumerate(sorted(tool_counts.items(), key=lambda x: -x[1]), row_start + 2):
            ws4.cell(row=i, column=1, value=tool)
            ws4.cell(row=i, column=2, value=count)

    # Save to bytes
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return buffer.read()
