"""
Cloud-based state tracking for idempotency (Supabase PostgreSQL or local JSON fallback).

Replaces JSON idempotency logs with database entries. Each log entry is a (key, logged_at, expires_at)
tuple where expires_at can be None (never expires) or a future timestamp.

Usage:
  is_reminded(event_id)     → checks if reminder already sent (TTL: 7 days)
  mark_reminded(event_id)   → records reminder sent

Works with DATABASE_URL env var. Falls back to JSON files if not set (local dev).
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

USE_DATABASE = bool(os.getenv("DATABASE_URL"))

if USE_DATABASE:
    from db import _conn


def is_logged(key: str, ttl_days: int = None) -> bool:
    """Check if key is logged and not expired."""
    if USE_DATABASE:
        with _conn() as cur:
            cur.execute(
                """SELECT expires_at FROM idempotency_log WHERE key = %s""",
                (key,),
            )
            row = cur.fetchone()
            if not row:
                return False
            expires = row["expires_at"]
            if expires is None:
                return True
            now = datetime.now(timezone.utc)
            if isinstance(expires, str):
                expires = datetime.fromisoformat(expires)
            elif isinstance(expires, (int, float)):
                expires = datetime.fromtimestamp(expires, tz=timezone.utc)
            return expires > now
    else:
        # Fallback: check JSON file
        log_file = Path(__file__).parent / ".idempotency_log.json"
        if not log_file.exists():
            return False
        data = json.loads(log_file.read_text())
        if key not in data:
            return False
        expires = data[key].get("expires_at")
        if expires is None:
            return True
        now = time.time()
        return expires > now


def mark_logged(key: str, ttl_days: int = None) -> None:
    """Record key as logged with optional TTL."""
    now = datetime.now(timezone.utc)
    expires_at = None
    if ttl_days:
        expires_at = (now + timedelta(days=ttl_days)).isoformat()

    if USE_DATABASE:
        with _conn() as cur:
            cur.execute(
                """INSERT INTO idempotency_log (key, logged_at, expires_at)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (key) DO UPDATE SET (logged_at, expires_at) =
                   (EXCLUDED.logged_at, EXCLUDED.expires_at)""",
                (key, now.isoformat(), expires_at),
            )
    else:
        log_file = Path(__file__).parent / ".idempotency_log.json"
        data = {}
        if log_file.exists():
            data = json.loads(log_file.read_text())

        data[key] = {
            "logged_at": now.isoformat(),
            "expires_at": expires_at,
        }
        log_file.write_text(json.dumps(data, indent=2))


# Convenience wrappers for common use cases
def is_reminded(event_id: str) -> bool:
    return is_logged(f"reminder:{event_id}", ttl_days=7)


def mark_reminded(event_id: str) -> None:
    mark_logged(f"reminder:{event_id}", ttl_days=7)


def is_post_sent(event_id: str) -> bool:
    return is_logged(f"post:{event_id}", ttl_days=30)


def mark_post_sent(event_id: str) -> None:
    mark_logged(f"post:{event_id}", ttl_days=30)


def is_review_sent(event_id: str) -> bool:
    return is_logged(f"review:{event_id}", ttl_days=60)


def mark_review_sent(event_id: str) -> None:
    mark_logged(f"review:{event_id}", ttl_days=60)


def is_winback_sent(email: str) -> bool:
    return is_logged(f"winback:{email}", ttl_days=180)


def mark_winback_sent(email: str) -> None:
    mark_logged(f"winback:{email}", ttl_days=180)
