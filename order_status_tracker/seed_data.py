"""
Seed Data — Populate realistic products, order items, and shipping details
==========================================================================
Run once on startup to add new tables and sample data.
Safe to run multiple times (uses INSERT OR IGNORE).
"""

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "orders.db"


def _now():
    return datetime.now(timezone.utc).astimezone()


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def seed(db_path=DB_PATH):
    """Create new tables and populate seed data."""
    db = sqlite3.connect(db_path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        # ── 1. Products Table ──
        db.execute("""
            CREATE TABLE IF NOT EXISTS products (
                product_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                price REAL NOT NULL,
                category TEXT,
                returnable INTEGER NOT NULL DEFAULT 1
            )
        """)

        products = [
            ("PROD001", "Blue Cotton T-Shirt", 499.0, "Clothing", 1),
            ("PROD002", "Wireless Earbuds", 1499.0, "Electronics", 1),
            ("PROD003", "Phone Case", 299.0, "Accessories", 1),
            ("PROD004", "Screen Guard (2-pack)", 199.0, "Accessories", 0),  # Non-returnable
            ("PROD005", "USB-C Cable", 149.0, "Accessories", 0),           # Non-returnable
            ("PROD006", "Sports Water Bottle", 399.0, "Lifestyle", 1),
        ]
        for p in products:
            db.execute("INSERT OR IGNORE INTO products VALUES (?, ?, ?, ?, ?)", p)

        # ── 2. Order Items Table ──
        db.execute("""
            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                product_id TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                price REAL NOT NULL,
                FOREIGN KEY(order_id) REFERENCES orders(order_id),
                FOREIGN KEY(product_id) REFERENCES products(product_id)
            )
        """)
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_oi_unique ON order_items(order_id, product_id)")

        order_items = [
            ("ORD101", "PROD001", 2, 499.0),   # Blue T-Shirt ×2
            ("ORD102", "PROD002", 1, 1499.0),   # Wireless Earbuds ×1
            ("ORD103", "PROD003", 1, 299.0),    # Phone Case ×1
            ("ORD103", "PROD004", 2, 199.0),    # Screen Guard ×2
        ]
        for oi in order_items:
            db.execute("INSERT OR IGNORE INTO order_items(order_id, product_id, quantity, price) VALUES (?, ?, ?, ?)", oi)

        # ── 3. Returns Table ──
        db.execute("""
            CREATE TABLE IF NOT EXISTS returns (
                return_id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                product_id TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'requested',
                refund_amount REAL,
                refund_method TEXT,
                requested_at TEXT NOT NULL,
                resolved_at TEXT,
                FOREIGN KEY(order_id) REFERENCES orders(order_id)
            )
        """)

        # ── 4. Refund Limits Table ──
        db.execute("""
            CREATE TABLE IF NOT EXISTS refund_limits (
                customer_phone TEXT PRIMARY KEY,
                total_refunded REAL DEFAULT 0,
                refund_count INTEGER DEFAULT 0,
                last_refund_at TEXT
            )
        """)

        # ── 5. Add new columns to orders (safe ALTER — ignore if exists) ──
        new_columns = [
            ("tracking_number", "TEXT"),
            ("carrier", "TEXT"),
            ("estimated_delivery", "TEXT"),
            ("customer_phone", "TEXT"),
            ("customer_name", "TEXT"),
            ("total_amount", "REAL"),
            ("payment_method", "TEXT DEFAULT 'COD'"),
            ("delivered_at", "TEXT"),
        ]
        for col_name, col_type in new_columns:
            try:
                db.execute(f"ALTER TABLE orders ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass  # Column already exists

        # ── 6. Update existing orders with rich data ──
        now = _now()

        # ORD101: Delivered 2 days ago — eligible for return
        delivered_2d = _iso(now - timedelta(days=2))
        db.execute("""
            UPDATE orders SET
                status='Delivered',
                tracking_number='DLV20261109001',
                carrier='Delhivery',
                estimated_delivery=?,
                customer_phone='916374459246',
                customer_name='Ravi',
                total_amount=998.0,
                payment_method='UPI',
                delivered_at=?,
                updated_at=?
            WHERE order_id='ORD101'
        """, (delivered_2d, delivered_2d, delivered_2d))

        # ORD102: Delivered 10 days ago — return window expired
        delivered_10d = _iso(now - timedelta(days=10))
        db.execute("""
            UPDATE orders SET
                status='Delivered',
                tracking_number='BB20261101042',
                carrier='BlueDart',
                estimated_delivery=?,
                customer_phone='919876543210',
                customer_name='Priya',
                total_amount=1499.0,
                payment_method='Card',
                delivered_at=?,
                updated_at=?
            WHERE order_id='ORD102'
        """, (delivered_10d, delivered_10d, delivered_10d))

        # ORD103: Shipped (not yet delivered) — no return allowed
        shipped_at = _iso(now - timedelta(days=1))
        est_delivery = _iso(now + timedelta(days=2))
        db.execute("""
            UPDATE orders SET
                status='Shipped',
                tracking_number='DTDC20261110078',
                carrier='DTDC',
                estimated_delivery=?,
                customer_phone='918765432109',
                customer_name='Amit',
                total_amount=697.0,
                payment_method='COD',
                delivered_at=NULL,
                updated_at=?
            WHERE order_id='ORD103'
        """, (est_delivery, shipped_at))

        # Add status history entries
        for order_id, statuses_with_dates in [
            ("ORD101", [("Shipped", now - timedelta(days=5)), ("On the way", now - timedelta(days=3)), ("Delivered", now - timedelta(days=2))]),
            ("ORD102", [("Shipped", now - timedelta(days=14)), ("On the way", now - timedelta(days=12)), ("Delivered", now - timedelta(days=10))]),
            ("ORD103", [("Shipped", now - timedelta(days=1))]),
        ]:
            for status, dt in statuses_with_dates:
                existing = db.execute(
                    "SELECT id FROM status_history WHERE order_id=? AND status=?", (order_id, status)
                ).fetchone()
                if not existing:
                    db.execute(
                        "INSERT INTO status_history(order_id, status, changed_at) VALUES (?, ?, ?)",
                        (order_id, status, _iso(dt)),
                    )

        db.commit()
        print("SEED: Database seeded successfully with products, items, and shipping data.", flush=True)
    finally:
        db.close()


if __name__ == "__main__":
    seed()
    print("Done!")
