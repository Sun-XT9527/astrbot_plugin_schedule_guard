"""Audit storage only: no timetable/recurrence database."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class AuditStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS checks (event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, checked_at TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL, delivery TEXT NOT NULL, payload TEXT NOT NULL)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS checks_time ON checks(checked_at DESC)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, record: dict):
        with self.connect() as db:
            db.execute(
                "INSERT INTO checks VALUES(?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET checked_at=excluded.checked_at,status=excluded.status,delivery=excluded.delivery,payload=excluded.payload",
                (
                    record["event_id"],
                    record["session_id"],
                    record["checked_at"],
                    record["mode"],
                    record["status"],
                    record["delivery"],
                    json.dumps(record, ensure_ascii=False),
                ),
            )

    def delivery(self, event_id: str, state: str, sent_at: str | None = None):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM checks WHERE event_id=?", (event_id,)).fetchone()
            if not row:
                return
            payload = json.loads(row[0])
            payload.update(delivery=state, sent_at=sent_at)
            db.execute(
                "UPDATE checks SET delivery=?,payload=? WHERE event_id=?",
                (state, json.dumps(payload, ensure_ascii=False), event_id),
            )

    def next_run(self, event_id: str, next_run_time: str):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM checks WHERE event_id=?", (event_id,)).fetchone()
            if row:
                payload = json.loads(row[0])
                payload["next_run_time"] = next_run_time
                db.execute(
                    "UPDATE checks SET payload=? WHERE event_id=?",
                    (json.dumps(payload, ensure_ascii=False), event_id),
                )

    def list(self, limit=100):
        with self.connect() as db:
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT payload FROM checks ORDER BY checked_at DESC LIMIT ?",
                    (max(1, min(300, limit)),),
                )
            ]

    def stats(self):
        with self.connect() as db:
            return {
                f"{mode}:{delivery}": count
                for mode, delivery, count in db.execute(
                    "SELECT mode,delivery,COUNT(*) FROM checks GROUP BY mode,delivery"
                )
            }

    def delete_simulations(self, event_id: str | None = None) -> int:
        """Delete simulated checks while retaining all live audit records.

        Args:
            event_id: A single simulation ID, or None to clear all simulations.

        Returns:
            The number of deleted simulation records.
        """
        with self.connect() as db:
            if event_id is None:
                cursor = db.execute("DELETE FROM checks WHERE mode='simulation'")
            else:
                cursor = db.execute(
                    "DELETE FROM checks WHERE mode='simulation' AND event_id=?", (event_id,)
                )
            return cursor.rowcount
