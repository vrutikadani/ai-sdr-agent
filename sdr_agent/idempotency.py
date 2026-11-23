"""Remember which inbound events were fully processed.

An event (a form webhook or an inbound reply) is marked done only after its side
effects succeeded. If processing raises, nothing is recorded, so a retry of the
same event runs again instead of being skipped.

Storage is SQLite from the standard library. The default is in-memory (one
process, lost on restart). Pass a file path to keep keys across restarts.

Not handled here: two workers processing the same event at the same moment.
That needs a claim step (insert an "in progress" row first, with a timeout).
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from os import PathLike
from typing import Union


class IdempotencyStore:
    def __init__(self, path: Union[str, PathLike] = ":memory:"):
        self._db = sqlite3.connect(str(path))
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS processed_events ("
            " key TEXT PRIMARY KEY, result TEXT NOT NULL, processed_at TEXT NOT NULL)"
        )
        self._db.commit()

    def is_done(self, key: str) -> bool:
        row = self._db.execute("SELECT 1 FROM processed_events WHERE key = ?", (key,)).fetchone()
        return row is not None

    def mark_done(self, key: str, result: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO processed_events (key, result, processed_at) VALUES (?, ?, ?)",
            (key, result, datetime.now(timezone.utc).isoformat()),
        )
        self._db.commit()


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]


def normalize_body(body: str) -> str:
    return re.sub(r"\s+", " ", body).strip().lower()


def form_key(lead_id: str, submission_id: str | None, email: str, form_message: str) -> str:
    """Keyed on the form submission id. Without one, fall back to a hash of the content."""
    if submission_id:
        return f"form:{submission_id}"
    return f"form:hash:{_digest(lead_id, email.strip().lower(), normalize_body(form_message))}"


def reply_key(lead_id: str, message_id: str | None, sender: str, body: str) -> str:
    """Keyed on Message-ID. Without one, hash thread (lead) + sender + normalized body.

    Tradeoff: if a lead sends the exact same text twice with no Message-ID, the
    second one is treated as a duplicate.
    """
    if message_id:
        return f"reply:{lead_id}:{message_id.strip()}"
    return f"reply:hash:{_digest(lead_id, sender.strip().lower(), normalize_body(body))}"
