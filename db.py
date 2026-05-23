"""
Database store for Greenguard agent — PostgreSQL or SQLite.
  - Crash-safety: check before processing, record after draft creation
  - Metrics: per-email stats queryable for the daily digest
  - State: key/value store for last_digest_sent, gmail_watch_expiry, etc.

If DATABASE_URL env var is set, uses PostgreSQL. Otherwise uses SQLite locally.
"""

import os
import sqlite3
import time
from contextlib import contextmanager

# Detect PostgreSQL vs SQLite
DATABASE_URL = os.getenv("DATABASE_URL")
USE_POSTGRES = bool(DATABASE_URL)
DB_PATH = os.path.join(os.path.dirname(__file__), "greenguard.db")

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras


@contextmanager
def _conn():
    if USE_POSTGRES:
        con = psycopg2.connect(DATABASE_URL)
        cur = con.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield cur
            con.commit()
        finally:
            cur.close()
            con.close()
    else:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        finally:
            con.close()


def init_db() -> None:
    if USE_POSTGRES:
        with _conn() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS processed_emails (
                    id              TEXT PRIMARY KEY,
                    subject         TEXT,
                    sender          TEXT,
                    classification  TEXT,
                    urgency         TEXT,
                    draft_id        TEXT,
                    processed_at    DOUBLE PRECISION NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_state (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS webhook_events (
                    uid                 TEXT PRIMARY KEY,
                    sku                 TEXT,
                    stripe_customer_id  TEXT,
                    invoice_id          TEXT,
                    processed_at        DOUBLE PRECISION NOT NULL
                );
                CREATE TABLE IF NOT EXISTS raw_webhooks (
                    id          SERIAL PRIMARY KEY,
                    trigger     TEXT,
                    payload     TEXT,
                    received_at DOUBLE PRECISION NOT NULL
                );
                CREATE TABLE IF NOT EXISTS abandoned_carts (
                    session_id  TEXT PRIMARY KEY,
                    email       TEXT,
                    items_json  TEXT,
                    created_at  DOUBLE PRECISION NOT NULL,
                    recovered   INTEGER DEFAULT 0
                );
            """)
    else:
        with _conn() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS processed_emails (
                    id              TEXT PRIMARY KEY,
                    subject         TEXT,
                    sender          TEXT,
                    classification  TEXT,
                    urgency         TEXT,
                    draft_id        TEXT,
                    processed_at    REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_state (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS webhook_events (
                    uid                 TEXT PRIMARY KEY,
                    sku                 TEXT,
                    stripe_customer_id  TEXT,
                    invoice_id          TEXT,
                    processed_at        REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS raw_webhooks (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    trigger     TEXT,
                    payload     TEXT,
                    received_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS abandoned_carts (
                    session_id  TEXT PRIMARY KEY,
                    email       TEXT,
                    items_json  TEXT,
                    created_at  REAL NOT NULL,
                    recovered   INTEGER DEFAULT 0
                );
            """)


def _exec(cur, sql: str, params: tuple = ()) -> list:
    if USE_POSTGRES:
        cur.execute(sql, params)
        return cur.fetchall() or []
    else:
        return cur.execute(sql, params).fetchall() or []


# ---------------------------------------------------------------------------
# Crash-safety
# ---------------------------------------------------------------------------

def is_processed(email_id: str) -> bool:
    """Return True if this email was already successfully drafted."""
    with _conn() as cur:
        if USE_POSTGRES:
            cur.execute("SELECT 1 FROM processed_emails WHERE id = %s", (email_id,))
        else:
            cur.execute("SELECT 1 FROM processed_emails WHERE id = ?", (email_id,))
        return bool(cur.fetchone())


def record_email(
    email_id: str,
    subject: str,
    sender: str,
    classification: str,
    urgency: str,
    draft_id: str,
) -> None:
    with _conn() as cur:
        if USE_POSTGRES:
            cur.execute(
                """INSERT INTO processed_emails
                   (id, subject, sender, classification, urgency, draft_id, processed_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (id) DO UPDATE SET (subject, sender, classification, urgency, draft_id, processed_at) =
                   (EXCLUDED.subject, EXCLUDED.sender, EXCLUDED.classification, EXCLUDED.urgency, EXCLUDED.draft_id, EXCLUDED.processed_at)""",
                (email_id, subject, sender, classification, urgency, draft_id, time.time()),
            )
        else:
            cur.execute(
                """INSERT OR REPLACE INTO processed_emails
                   (id, subject, sender, classification, urgency, draft_id, processed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (email_id, subject, sender, classification, urgency, draft_id, time.time()),
            )


# ---------------------------------------------------------------------------
# Metrics for daily digest
# ---------------------------------------------------------------------------

def get_stats(since_ts: float) -> dict:
    """Return activity counts since a Unix timestamp."""
    with _conn() as cur:
        if USE_POSTGRES:
            cur.execute(
                """SELECT classification, urgency, COUNT(*) as n
                   FROM processed_emails
                   WHERE processed_at >= %s
                   GROUP BY classification, urgency""",
                (since_ts,),
            )
        else:
            cur.execute(
                """SELECT classification, urgency, COUNT(*) as n
                   FROM processed_emails
                   WHERE processed_at >= ?
                   GROUP BY classification, urgency""",
                (since_ts,),
            )
        rows = cur.fetchall()

    total = sum(r["n"] for r in rows)
    by_type: dict[str, int] = {}
    for r in rows:
        by_type[r["classification"]] = by_type.get(r["classification"], 0) + r["n"]
    urgency_count = sum(r["n"] for r in rows if r["urgency"] == "high")

    return {"total": total, "by_type": by_type, "high_urgency_count": urgency_count}


def get_high_urgency_emails(since_ts: float) -> list[dict]:
    with _conn() as cur:
        if USE_POSTGRES:
            cur.execute(
                """SELECT subject, sender FROM processed_emails
                   WHERE processed_at >= %s AND urgency = 'high'
                   ORDER BY processed_at DESC""",
                (since_ts,),
            )
        else:
            cur.execute(
                """SELECT subject, sender FROM processed_emails
                   WHERE processed_at >= ? AND urgency = 'high'
                   ORDER BY processed_at DESC""",
                (since_ts,),
            )
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Key/value state
# ---------------------------------------------------------------------------

def get_state(key: str, default: str = "") -> str:
    with _conn() as cur:
        if USE_POSTGRES:
            cur.execute("SELECT value FROM agent_state WHERE key = %s", (key,))
        else:
            cur.execute("SELECT value FROM agent_state WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else default


def set_state(key: str, value: str) -> None:
    with _conn() as cur:
        if USE_POSTGRES:
            cur.execute(
                """INSERT INTO agent_state (key, value) VALUES (%s, %s)
                   ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
                (key, value),
            )
        else:
            cur.execute(
                "INSERT OR REPLACE INTO agent_state (key, value) VALUES (?, ?)",
                (key, value),
            )


# ---------------------------------------------------------------------------
# Webhook idempotency
# ---------------------------------------------------------------------------

def is_webhook_processed(uid: str) -> bool:
    with _conn() as cur:
        if USE_POSTGRES:
            cur.execute("SELECT 1 FROM webhook_events WHERE uid = %s", (uid,))
        else:
            cur.execute("SELECT 1 FROM webhook_events WHERE uid = ?", (uid,))
        return bool(cur.fetchone())


def record_raw_webhook(trigger: str, payload: str) -> None:
    with _conn() as cur:
        if USE_POSTGRES:
            cur.execute(
                "INSERT INTO raw_webhooks (trigger, payload, received_at) VALUES (%s, %s, %s)",
                (trigger, payload, time.time()),
            )
        else:
            cur.execute(
                "INSERT INTO raw_webhooks (trigger, payload, received_at) VALUES (?, ?, ?)",
                (trigger, payload, time.time()),
            )


def get_raw_webhooks() -> list[dict]:
    with _conn() as cur:
        if USE_POSTGRES:
            cur.execute(
                "SELECT trigger, payload, received_at FROM raw_webhooks ORDER BY received_at DESC LIMIT 10"
            )
        else:
            cur.execute(
                "SELECT trigger, payload, received_at FROM raw_webhooks ORDER BY received_at DESC LIMIT 10"
            )
        return [dict(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Abandoned cart recovery
# ---------------------------------------------------------------------------

def save_abandoned_cart(session_id: str, email: str, items_json: str) -> None:
    with _conn() as cur:
        if USE_POSTGRES:
            cur.execute(
                """INSERT INTO abandoned_carts
                   (session_id, email, items_json, created_at, recovered)
                   VALUES (%s, %s, %s, %s, 0)
                   ON CONFLICT (session_id) DO UPDATE SET (email, items_json, recovered) =
                   (EXCLUDED.email, EXCLUDED.items_json, 0)""",
                (session_id, email, items_json, time.time()),
            )
        else:
            cur.execute(
                """INSERT OR REPLACE INTO abandoned_carts
                   (session_id, email, items_json, created_at, recovered)
                   VALUES (?, ?, ?, ?, 0)""",
                (session_id, email, items_json, time.time()),
            )


def get_abandoned_carts(min_age_minutes: int = 60) -> list[dict]:
    cutoff = time.time() - (min_age_minutes * 60)
    with _conn() as cur:
        if USE_POSTGRES:
            cur.execute(
                """SELECT session_id, email, items_json, created_at FROM abandoned_carts
                   WHERE recovered = 0 AND created_at <= %s""",
                (cutoff,),
            )
        else:
            cur.execute(
                """SELECT session_id, email, items_json, created_at FROM abandoned_carts
                   WHERE recovered = 0 AND created_at <= ?""",
                (cutoff,),
            )
        return [dict(r) for r in cur.fetchall()]


def mark_cart_recovered(session_id: str) -> None:
    with _conn() as cur:
        if USE_POSTGRES:
            cur.execute(
                "UPDATE abandoned_carts SET recovered = 1 WHERE session_id = %s",
                (session_id,),
            )
        else:
            cur.execute(
                "UPDATE abandoned_carts SET recovered = 1 WHERE session_id = ?",
                (session_id,),
            )


def record_webhook(uid: str, sku: str, stripe_customer_id: str, invoice_id: str = "") -> None:
    with _conn() as cur:
        if USE_POSTGRES:
            cur.execute(
                """INSERT INTO webhook_events
                   (uid, sku, stripe_customer_id, invoice_id, processed_at)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (uid) DO UPDATE SET (sku, stripe_customer_id, invoice_id, processed_at) =
                   (EXCLUDED.sku, EXCLUDED.stripe_customer_id, EXCLUDED.invoice_id, EXCLUDED.processed_at)""",
                (uid, sku, stripe_customer_id, invoice_id, time.time()),
            )
        else:
            cur.execute(
                """INSERT OR REPLACE INTO webhook_events
                   (uid, sku, stripe_customer_id, invoice_id, processed_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (uid, sku, stripe_customer_id, invoice_id, time.time()),
            )
