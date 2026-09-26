"""SQLite storage. Set DB_PATH to a Render persistent disk (e.g. /var/data/smart.db),
otherwise all data is lost on every redeploy."""
import os, sqlite3, secrets, datetime as dt
from contextlib import contextmanager

DB_PATH = os.getenv("DB_PATH", "smart.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses(
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE, name TEXT, vertical TEXT DEFAULT 'restaurant',
  phone TEXT, owner_phone TEXT, owner_pin TEXT, staff_pin TEXT, logo_url TEXT, address TEXT,
  late_after_min INTEGER DEFAULT 60);
CREATE TABLE IF NOT EXISTS products(
  id INTEGER PRIMARY KEY, business_id INTEGER, name TEXT, category TEXT, price INTEGER,
  unit TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS menu_drafts(
  id INTEGER PRIMARY KEY, business_id INTEGER, items_json TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS vip_customers(
  id INTEGER PRIMARY KEY, business_id INTEGER, phone TEXT, name TEXT,
  discount_pct REAL DEFAULT 0, UNIQUE(business_id, phone));
CREATE TABLE IF NOT EXISTS vip_prices(
  vip_id INTEGER, product_id INTEGER, price INTEGER, PRIMARY KEY(vip_id, product_id));
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY, public_id TEXT UNIQUE, business_id INTEGER,
  customer_phone TEXT, customer_name TEXT, address TEXT, notes TEXT,
  source TEXT DEFAULT 'whatsapp', entered_by TEXT, status TEXT DEFAULT 'received',
  subtotal INTEGER, discount INTEGER DEFAULT 0, total INTEGER,
  driver_name TEXT, driver_token TEXT, customer_confirmed INTEGER DEFAULT 0,
  late_alert_sent INTEGER DEFAULT 0, created_at TEXT, delivered_at TEXT);
CREATE TABLE IF NOT EXISTS order_items(
  id INTEGER PRIMARY KEY, order_id INTEGER, product_id INTEGER, name TEXT,
  qty REAL, unit_price INTEGER, line_total INTEGER);
CREATE TABLE IF NOT EXISTS processed_messages(id TEXT PRIMARY KEY, at TEXT);
CREATE TABLE IF NOT EXISTS contacts(business_id INTEGER, phone TEXT, last_inbound_at TEXT,
  PRIMARY KEY(business_id, phone));
CREATE TABLE IF NOT EXISTS chat_messages(
  id INTEGER PRIMARY KEY, business_id INTEGER, phone TEXT, role TEXT, content TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY, business_id INTEGER, who TEXT, action TEXT, detail TEXT, order_id INTEGER, at TEXT);
CREATE TABLE IF NOT EXISTS login_attempts(k TEXT PRIMARY KEY, fails INTEGER, locked_until TEXT);
CREATE TABLE IF NOT EXISTS order_events(
  id INTEGER PRIMARY KEY, order_id INTEGER, status TEXT, by_who TEXT, at TEXT);
CREATE INDEX IF NOT EXISTS ix_orders_biz_status ON orders(business_id, status);
CREATE INDEX IF NOT EXISTS ix_orders_phone ON orders(business_id, customer_phone);
CREATE INDEX IF NOT EXISTS ix_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS ix_events_order ON order_events(order_id);
CREATE INDEX IF NOT EXISTS ix_audit_biz ON audit_log(business_id, id);
CREATE INDEX IF NOT EXISTS ix_chat ON chat_messages(business_id, phone, id);
"""

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=15000")
    try:
        yield c
        c.commit()
    finally:
        c.close()

MIGRATIONS = [("businesses", "phone_number_id", "TEXT"),
              ("orders", "rating", "INTEGER"), ("orders", "rating_comment", "TEXT"),
              ("orders", "rating_state", "TEXT"), ("orders", "rating_asked_at", "TEXT"),
              ("orders", "cancel_reason", "TEXT"), ("order_events", "detail", "TEXT"),
              ("products", "image_url", "TEXT"), ("products", "note", "TEXT"),
              ("products", "price_confirmed", "INTEGER DEFAULT 1"), ("products", "orderable", "INTEGER DEFAULT 1"),
              ("products", "sort", "INTEGER DEFAULT 100"),
              ("businesses", "volume_tiers", "TEXT"), ("businesses", "tagline", "TEXT"), ("businesses", "city", "TEXT"),
              ("businesses", "contact_phones", "TEXT"), ("businesses", "facebook", "TEXT"), ("businesses", "about", "TEXT"),
              ("businesses", "palette", "TEXT")]

def init_db():
    with conn() as c:
        c.execute("PRAGMA journal_mode=WAL")  # several gunicorn workers can read while one writes
        c.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
            if col not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")

def one(sql, *a):
    with conn() as c:
        r = c.execute(sql, a).fetchone()
        return dict(r) if r else None

def many(sql, *a):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, a).fetchall()]

def run(sql, *a):
    with conn() as c:
        return c.execute(sql, a).lastrowid

def new_token(n=6):
    return secrets.token_urlsafe(n)

AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

def num(v, kind=int, default=None, lo=None):
    """Parse user input safely: '٥٠٠٠', '5,000', ' 7.5 ' -> number; junk -> default (never a 500 error)."""
    try:
        x = kind(float(str(v).translate(AR_DIGITS).replace(",", "").replace("،", "").strip()))
    except (TypeError, ValueError):
        return default
    return default if lo is not None and x < lo else x

def audit(business_id, who, action, detail="", order_id=None):
    """Who did what, kept forever. Shown to the owner on the 'السجل' page."""
    run("INSERT INTO audit_log(business_id,who,action,detail,order_id,at) VALUES(?,?,?,?,?,?)",
        business_id, who or "غير معروف", action, detail, order_id, now())
