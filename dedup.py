"""
Item 5 — SQLite Message-ID deduplication.
Belt-and-suspenders guard: even if the Gmail label call fails mid-processing,
the same email is never drafted twice.
"""
import sqlite3
from pathlib import Path

_DB_PATH = Path(__file__).parent / "processed.db"


class ProcessedDB:
    def __init__(self, path: Path = _DB_PATH):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS seen "
            "(msg_id TEXT PRIMARY KEY, ts INTEGER DEFAULT (unixepoch()))"
        )
        self._conn.commit()

    def seen(self, message_id: str) -> bool:
        if not message_id:
            return False
        return self._conn.execute(
            "SELECT 1 FROM seen WHERE msg_id=?", (message_id,)
        ).fetchone() is not None

    def mark(self, message_id: str) -> None:
        if not message_id:
            return
        self._conn.execute(
            "INSERT OR IGNORE INTO seen (msg_id) VALUES (?)", (message_id,)
        )
        self._conn.commit()

    def prune(self, days: int = 30) -> None:
        """Keep DB small — drop entries older than N days."""
        self._conn.execute(
            "DELETE FROM seen WHERE ts < unixepoch() - ?", (days * 86_400,)
        )
        self._conn.commit()
