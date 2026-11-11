"""
Guardrails — Server-side enforcement of return/refund rules
============================================================
All business rules are enforced HERE in the API layer, NOT in the LLM prompt.
The agent cannot bypass these checks.
"""

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "orders.db"

# ── Policy Constants ──
RETURN_WINDOW_DAYS = 7        # Returns allowed within 7 days of delivery
MAX_RETURNS_PER_MONTH = 3     # Maximum 3 returns per customer per month
MAX_REFUND_PER_MONTH = 5000   # Maximum ₹5,000 refund per customer per month
HIGH_VALUE_THRESHOLD = 2000   # Refunds > ₹2,000 need human approval


def _connect():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def _now():
    return datetime.now(timezone.utc).astimezone()


def _now_iso():
    return _now().isoformat(timespec="seconds")


def _month_start():
    """First day of current month as ISO string."""
    n = _now()
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


def check_return_eligibility(order_id: str, product_id: str, customer_phone: str = None) -> dict:
    """
    Run ALL guardrail checks for a return request.
    Returns {"eligible": True/False, ...details...}
    """
    db = _connect()
    try:
        # ── 1. Order must exist ──
        order = db.execute(
            "SELECT order_id, status, customer_phone, customer_name, delivered_at FROM orders WHERE order_id=?",
            (order_id.upper(),)
        ).fetchone()
        if not order:
            return {"eligible": False, "reason": f"Order {order_id} was not found."}

        phone = customer_phone or order["customer_phone"] or ""

        # ── 2. Order must be Delivered ──
        if order["status"] != "Delivered":
            return {
                "eligible": False,
                "reason": f"Returns are only allowed for delivered orders. Your order is currently '{order['status']}'.",
                "order_id": order_id.upper(),
                "current_status": order["status"],
            }

        # ── 3. Product must exist in order ──
        item = db.execute(
            """SELECT oi.product_id, oi.quantity, oi.price, p.name, p.returnable
               FROM order_items oi
               JOIN products p ON p.product_id = oi.product_id
               WHERE oi.order_id=? AND oi.product_id=?""",
            (order_id.upper(), product_id.upper()),
        ).fetchone()
        if not item:
            return {
                "eligible": False,
                "reason": f"Product {product_id} was not found in order {order_id}.",
                "order_id": order_id.upper(),
            }

        # ── 4. Product must be returnable ──
        if not item["returnable"]:
            return {
                "eligible": False,
                "reason": f"'{item['name']}' is marked as non-returnable and cannot be returned.",
                "order_id": order_id.upper(),
                "product": item["name"],
            }

        # ── 5. Check 7-day return window ──
        delivered_at = order["delivered_at"]
        if delivered_at:
            try:
                delivery_dt = datetime.fromisoformat(delivered_at)
                days_since = (_now() - delivery_dt).days
                days_remaining = RETURN_WINDOW_DAYS - days_since
                if days_since > RETURN_WINDOW_DAYS:
                    return {
                        "eligible": False,
                        "reason": f"Return window ({RETURN_WINDOW_DAYS} days) has expired. "
                                  f"Order was delivered {days_since} days ago.",
                        "order_id": order_id.upper(),
                        "product": item["name"],
                        "delivered_days_ago": days_since,
                    }
            except (ValueError, TypeError):
                days_remaining = RETURN_WINDOW_DAYS  # If date parsing fails, allow

        # ── 6. Check monthly return count ──
        month_start = _month_start()
        limits = db.execute(
            "SELECT total_refunded, refund_count FROM refund_limits WHERE customer_phone=?",
            (phone,)
        ).fetchone()

        refund_count = limits["refund_count"] if limits else 0
        total_refunded = limits["total_refunded"] if limits else 0

        if refund_count >= MAX_RETURNS_PER_MONTH:
            return {
                "eligible": False,
                "reason": f"Monthly return limit reached ({MAX_RETURNS_PER_MONTH} returns per month). "
                          f"You have already made {refund_count} returns this month.",
                "order_id": order_id.upper(),
                "product": item["name"],
            }

        # ── 7. Check monthly refund amount ──
        refund_amount = item["price"] * item["quantity"]
        if total_refunded + refund_amount > MAX_REFUND_PER_MONTH:
            remaining = MAX_REFUND_PER_MONTH - total_refunded
            return {
                "eligible": False,
                "reason": f"Monthly refund limit of ₹{MAX_REFUND_PER_MONTH:,.0f} would be exceeded. "
                          f"You have ₹{remaining:,.0f} remaining this month.",
                "order_id": order_id.upper(),
                "product": item["name"],
                "refund_amount": refund_amount,
            }

        # ── 8. Check if return already exists for this item ──
        existing = db.execute(
            "SELECT return_id, status FROM returns WHERE order_id=? AND product_id=? AND status NOT IN ('denied', 'cancelled')",
            (order_id.upper(), product_id.upper()),
        ).fetchone()
        if existing:
            return {
                "eligible": False,
                "reason": f"A return (ID: {existing['return_id']}) already exists for this item. "
                          f"Current status: {existing['status']}.",
                "order_id": order_id.upper(),
                "product": item["name"],
                "existing_return_id": existing["return_id"],
            }

        # ── ALL CHECKS PASSED ──
        needs_escalation = refund_amount > HIGH_VALUE_THRESHOLD
        return {
            "eligible": True,
            "order_id": order_id.upper(),
            "product_id": product_id.upper(),
            "product": item["name"],
            "quantity": item["quantity"],
            "refund_amount": refund_amount,
            "refund_method": "original_payment",
            "days_remaining": days_remaining if delivered_at else RETURN_WINDOW_DAYS,
            "needs_escalation": needs_escalation,
            "escalation_reason": "High-value refund (>₹2,000) requires manager approval" if needs_escalation else None,
            "customer_name": order["customer_name"],
        }
    finally:
        db.close()


def start_return(order_id: str, product_id: str, reason: str, customer_phone: str = None) -> dict:
    """
    Create a return request AFTER eligibility is confirmed.
    Runs eligibility check again as a safety net.
    """
    # Re-check eligibility (defense in depth)
    eligibility = check_return_eligibility(order_id, product_id, customer_phone)
    if not eligibility.get("eligible"):
        return {"success": False, **eligibility}

    db = _connect()
    try:
        # Generate return ID
        last = db.execute("SELECT MAX(CAST(SUBSTR(return_id, 5) AS INTEGER)) AS last_num FROM returns").fetchone()
        next_num = (last["last_num"] or 1000) + 1
        return_id = f"RET-{next_num}"

        # Get customer phone from order if not provided
        if not customer_phone:
            order = db.execute("SELECT customer_phone FROM orders WHERE order_id=?", (order_id.upper(),)).fetchone()
            customer_phone = order["customer_phone"] if order else ""

        # Create return record
        now = _now_iso()
        db.execute(
            """INSERT INTO returns (return_id, order_id, product_id, reason, status, refund_amount, refund_method, requested_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (return_id, order_id.upper(), product_id.upper(), reason,
             "requested", eligibility["refund_amount"], "original_payment", now),
        )

        # Update refund limits
        existing = db.execute(
            "SELECT * FROM refund_limits WHERE customer_phone=?", (customer_phone,)
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE refund_limits SET refund_count = refund_count + 1, last_refund_at=? WHERE customer_phone=?",
                (now, customer_phone),
            )
        else:
            db.execute(
                "INSERT INTO refund_limits (customer_phone, total_refunded, refund_count, last_refund_at) VALUES (?, 0, 1, ?)",
                (customer_phone, now),
            )

        # Auto-approve if not high-value
        status = "requested"
        if not eligibility.get("needs_escalation"):
            status = "approved"
            db.execute("UPDATE returns SET status='approved' WHERE return_id=?", (return_id,))
            # Update total_refunded on approval
            db.execute(
                "UPDATE refund_limits SET total_refunded = total_refunded + ? WHERE customer_phone=?",
                (eligibility["refund_amount"], customer_phone),
            )

        db.commit()

        result = {
            "success": True,
            "return_id": return_id,
            "order_id": order_id.upper(),
            "product": eligibility["product"],
            "reason": reason,
            "status": status,
            "refund_amount": eligibility["refund_amount"],
            "refund_method": "original_payment",
            "message": f"Return {return_id} created successfully.",
        }

        if eligibility.get("needs_escalation"):
            result["status"] = "pending_approval"
            result["message"] = (
                f"Return {return_id} created but requires manager approval "
                f"(refund amount ₹{eligibility['refund_amount']:,.0f} exceeds ₹{HIGH_VALUE_THRESHOLD:,.0f})."
            )

        return result
    finally:
        db.close()


def get_return_status(return_id: str) -> dict:
    """Get the current status of a return request."""
    db = _connect()
    try:
        row = db.execute(
            """SELECT r.return_id, r.order_id, r.product_id, r.reason, r.status,
                      r.refund_amount, r.refund_method, r.requested_at, r.resolved_at,
                      p.name AS product_name
               FROM returns r
               JOIN products p ON p.product_id = r.product_id
               WHERE r.return_id=?""",
            (return_id.upper(),),
        ).fetchone()
        if not row:
            return {"error": f"Return {return_id} was not found."}
        return dict(row)
    finally:
        db.close()


def get_returns_for_order(order_id: str) -> list[dict]:
    """Get all returns associated with an order."""
    db = _connect()
    try:
        rows = db.execute(
            """SELECT r.return_id, r.product_id, r.reason, r.status,
                      r.refund_amount, r.refund_method, r.requested_at, r.resolved_at,
                      p.name AS product_name
               FROM returns r
               JOIN products p ON p.product_id = r.product_id
               WHERE r.order_id=?
               ORDER BY r.requested_at DESC""",
            (order_id.upper(),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()
