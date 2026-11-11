import sqlite3
import hmac
import os
from dotenv import load_dotenv
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Depends, Query
from fastapi.responses import FileResponse
from fastapi import Header
from pydantic import BaseModel, Field

from seed_data import seed
from guardrails import (
    check_return_eligibility,
    start_return,
    get_return_status,
    get_returns_for_order,
)

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
DB_PATH = ROOT / "orders.db"
STATUSES = ["Shipped", "On the way", "Delivered"]
ORDER_API_KEY = os.getenv("ORDER_API_KEY", "").strip()


def require_order_api_key(x_api_key: str = Header(default="")):
    if not ORDER_API_KEY or not hmac.compare_digest(x_api_key, ORDER_API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing order API key.")


app = FastAPI(title="Order Status Tracker", docs_url=None, redoc_url=None)


def connect():
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def _now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def initialize_db():
    with connect() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'Not started',
                updated_at TEXT
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS status_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                status TEXT NOT NULL,
                changed_at TEXT NOT NULL,
                FOREIGN KEY(order_id) REFERENCES orders(order_id)
            )
        """)
        for order_id in (f"ORD{number}" for number in range(101, 111)):
            db.execute("INSERT OR IGNORE INTO orders(order_id) VALUES (?)", (order_id,))

    # Run seed to add products, items, shipping details, new tables
    seed(DB_PATH)


@app.on_event("startup")
def startup():
    initialize_db()


@app.get("/")
def home():
    return FileResponse(ROOT / "index.html")


# =============================================
# EXISTING: Order Status APIs
# =============================================

@app.get("/api/orders/{order_id}")
def get_order(order_id: str, _auth=Depends(require_order_api_key)):
    with connect() as db:
        row = db.execute(
            """SELECT order_id, status, updated_at, tracking_number, carrier,
                      estimated_delivery, customer_phone, customer_name,
                      total_amount, payment_method, delivered_at
               FROM orders WHERE order_id=?""",
            (order_id.upper(),)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Order not found.")
        history = db.execute(
            "SELECT status, changed_at FROM status_history WHERE order_id=? ORDER BY id DESC",
            (order_id.upper(),),
        ).fetchall()
        return {**dict(row), "history": [dict(item) for item in history]}


@app.get("/api/orders")
def list_orders():
    with connect() as db:
        rows = db.execute(
            """SELECT order_id, status, updated_at, customer_name, customer_phone,
                      total_amount, carrier, tracking_number
               FROM orders ORDER BY order_id"""
        ).fetchall()
        result = []
        for row in rows:
            history = db.execute(
                "SELECT status, changed_at FROM status_history WHERE order_id=? ORDER BY id DESC",
                (row["order_id"],),
            ).fetchall()
            result.append({
                **dict(row),
                "history": [dict(item) for item in history],
            })
        return {"orders": result, "statuses": STATUSES}


class StatusUpdate(BaseModel):
    status: str


@app.post("/api/orders/{order_id}/status")
def update_status(order_id: str, update: StatusUpdate):
    if update.status not in STATUSES:
        raise HTTPException(status_code=400, detail="Choose Shipped, On the way, or Delivered.")

    with connect() as db:
        db.execute("BEGIN IMMEDIATE")
        order = db.execute("SELECT status FROM orders WHERE order_id=?", (order_id.upper(),)).fetchone()
        if order is None:
            raise HTTPException(status_code=404, detail="Order not found.")

        current = order["status"]
        current_step = STATUSES.index(current) if current in STATUSES else -1
        requested_step = STATUSES.index(update.status)
        if requested_step != current_step + 1:
            raise HTTPException(
                status_code=409,
                detail=f"Next status for {order_id.upper()} is {STATUSES[current_step + 1] if current_step + 1 < len(STATUSES) else 'already Delivered'}.",
            )

        changed_at = _now()
        db.execute("UPDATE orders SET status=?, updated_at=? WHERE order_id=?", (update.status, changed_at, order_id.upper()))
        db.execute(
            "INSERT INTO status_history(order_id, status, changed_at) VALUES (?, ?, ?)",
            (order_id.upper(), update.status, changed_at),
        )

        # If delivered, record delivered_at
        if update.status == "Delivered":
            db.execute("UPDATE orders SET delivered_at=? WHERE order_id=?", (changed_at, order_id.upper()))

        return {"order_id": order_id.upper(), "status": update.status, "changed_at": changed_at}


# =============================================
# NEW: Order Items & Shipping
# =============================================

@app.get("/api/orders/{order_id}/items")
def get_order_items(order_id: str, _auth=Depends(require_order_api_key)):
    """Get all items in an order with product details."""
    with connect() as db:
        order = db.execute("SELECT order_id, customer_name FROM orders WHERE order_id=?", (order_id.upper(),)).fetchone()
        if not order:
            raise HTTPException(status_code=404, detail="Order not found.")

        items = db.execute(
            """SELECT oi.product_id, p.name, p.category, p.returnable,
                      oi.quantity, oi.price, (oi.quantity * oi.price) AS subtotal
               FROM order_items oi
               JOIN products p ON p.product_id = oi.product_id
               WHERE oi.order_id=?""",
            (order_id.upper(),),
        ).fetchall()

        return {
            "order_id": order_id.upper(),
            "customer_name": order["customer_name"],
            "items": [dict(i) for i in items],
            "item_count": len(items),
        }


@app.get("/api/orders/{order_id}/shipping")
def get_shipping(order_id: str, _auth=Depends(require_order_api_key)):
    """Get shipping/tracking details for an order."""
    with connect() as db:
        row = db.execute(
            """SELECT order_id, status, tracking_number, carrier,
                      estimated_delivery, delivered_at, customer_name
               FROM orders WHERE order_id=?""",
            (order_id.upper(),),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Order not found.")
        return dict(row)


# =============================================
# NEW: Returns & Refunds
# =============================================

class ReturnCheckRequest(BaseModel):
    order_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    customer_phone: str = Field(default="")


class ReturnStartRequest(BaseModel):
    order_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    customer_phone: str = Field(default="")


@app.post("/api/returns/check")
def api_check_return(req: ReturnCheckRequest, _auth=Depends(require_order_api_key)):
    """Check if a return is eligible. Runs all guardrail checks."""
    return check_return_eligibility(req.order_id, req.product_id, req.customer_phone)


@app.post("/api/returns/start")
def api_start_return(req: ReturnStartRequest, _auth=Depends(require_order_api_key)):
    """Start a return request. Re-runs guardrails as a safety net."""
    result = start_return(req.order_id, req.product_id, req.reason, req.customer_phone)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("reason", "Return request denied."))
    return result


@app.get("/api/returns/{return_id}")
def api_get_return(return_id: str, _auth=Depends(require_order_api_key)):
    """Get the status of a return request."""
    result = get_return_status(return_id)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@app.get("/api/returns/by-order/{order_id}")
def api_returns_by_order(order_id: str, _auth=Depends(require_order_api_key)):
    """Get all returns for a specific order."""
    returns = get_returns_for_order(order_id)
    return {"order_id": order_id.upper(), "returns": returns, "total": len(returns)}


# =============================================
# NEW: Products List
# =============================================

@app.get("/api/products")
def list_products(_auth=Depends(require_order_api_key)):
    """List all products in the catalog."""
    with connect() as db:
        rows = db.execute("SELECT * FROM products ORDER BY product_id").fetchall()
        return {"products": [dict(r) for r in rows]}
