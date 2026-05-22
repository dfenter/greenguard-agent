"""
SQLite store for Greenguard agent.
  - Crash-safety: check before processing, record after draft creation
  - Metrics: per-email stats queryable for the daily digest
  - State: key/value store for last_digest_sent, gmail_watch_expiry, etc.
"""

import os
import sqlite3
import time
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "greenguard.db")


@contextmanager
def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
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


# ---------------------------------------------------------------------------
# Crash-safety
# ---------------------------------------------------------------------------

def is_processed(email_id: str) -> bool:
    """Return True if this email was already successfully drafted."""
    with _conn() as con:
        return bool(
            con.execute(
                "SELECT 1 FROM processed_emails WHERE id = ?", (email_id,)
            ).fetchone()
        )


def record_email(
    email_id: str,
    subject: str,
    sender: str,
    classification: str,
    urgency: str,
    draft_id: str,
) -> None:
    with _conn() as con:
        con.execute(
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
    with _conn() as con:
        rows = con.execute(
            """SELECT classification, urgency, COUNT(*) as n
               FROM processed_emails
               WHERE processed_at >= ?
               GROUP BY classification, urgency""",
            (since_ts,),
        ).fetchall()

    total = sum(r["n"] for r in rows)
    high_urgency = [
        {"subject": r["subject"], "sender": r["sender"]}
        for r in con.execute(
            """SELECT subject, sender FROM processed_emails
               WHERE processed_at >= ? AND urgency = 'high'
               ORDER BY processed_at DESC""",
            (since_ts,),
        ).fetchall()
    ] if False else []  # fetched separately below

    by_type: dict[str, int] = {}
    for r in rows:
        by_type[r["classification"]] = by_type.get(r["classification"], 0) + r["n"]

    urgency_count = sum(r["n"] for r in rows if r["urgency"] == "high")

    return {"total": total, "by_type": by_type, "high_urgency_count": urgency_count}


def get_high_urgency_emails(since_ts: float) -> list[dict]:
    with _conn() as con:
        return [
            dict(r)
            for r in con.execute(
                """SELECT subject, sender FROM processed_emails
                   WHERE processed_at >= ? AND urgency = 'high'
                   ORDER BY processed_at DESC""",
                (since_ts,),
            ).fetchall()
        ]


# ---------------------------------------------------------------------------
# Key/value state
# ---------------------------------------------------------------------------

def get_state(key: str, default: str = "") -> str:
    with _conn() as con:
        row = con.execute(
            "SELECT value FROM agent_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default


def set_state(key: str, value: str) -> None:
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO agent_state (key, value) VALUES (?, ?)",
            (key, value),
        )


# ---------------------------------------------------------------------------
# Webhook idempotency
# ---------------------------------------------------------------------------

def is_webhook_processed(uid: str) -> bool:
    with _conn() as con:
        return bool(
            con.execute(
                "SELECT 1 FROM webhook_events WHERE uid = ?", (uid,)
            ).fetchone()
        )


def record_raw_webhook(trigger: str, payload: str) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO raw_webhooks (trigger, payload, received_at) VALUES (?, ?, ?)",
            (trigger, payload, time.time()),
        )


def get_raw_webhooks() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT trigger, payload, received_at FROM raw_webhooks ORDER BY received_at DESC LIMIT 10"
        ).fetchall()
        return [{"trigger": r["trigger"], "payload": r["payload"], "received_at": r["received_at"]} for r in rows]


# ---------------------------------------------------------------------------
# Abandoned cart recovery
# ---------------------------------------------------------------------------

def save_abandoned_cart(session_id: str, email: str, items_json: str) -> None:
    with _conn() as con:
        con.execute(
            """INSERT OR REPLACE INTO abandoned_carts
               (session_id, email, items_json, created_at, recovered)
               VALUES (?, ?, ?, ?, 0)""",
            (session_id, email, items_json, time.time()),
        )


def get_abandoned_carts(min_age_minutes: int = 60) -> list[dict]:
    cutoff = time.time() - (min_age_minutes * 60)
    with _conn() as con:
        rows = con.execute(
            """SELECT session_id, email, items_json, created_at FROM abandoned_carts
               WHERE recovered = 0 AND created_at <= ?""",
            (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_cart_recovered(session_id: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE abandoned_carts SET recovered = 1 WHERE session_id = ?",
            (session_id,),
        )


def record_webhook(uid: str, sku: str, stripe_customer_id: str, invoice_id: str = "") -> None:
    with _conn() as con:
        con.execute(
            """INSERT OR REPLACE INTO webhook_events
               (uid, sku, stripe_customer_id, invoice_id, processed_at)
               VALUES (?, ?, ?, ?, ?)""",
            (uid, sku, stripe_customer_id, invoice_id, time.time()),
        )
