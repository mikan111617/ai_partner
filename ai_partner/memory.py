from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class MemoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS conversation_turns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    context_id TEXT NOT NULL,
                    user_text TEXT NOT NULL,
                    companion_reply TEXT NOT NULL
                )
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    context_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
            """)

    def add_conversation(self, context_id: str, user_text: str, reply: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO conversation_turns(ts, context_id, user_text, companion_reply) VALUES(?,?,?,?)",
                (time.time(), str(context_id), str(user_text), str(reply)),
            )

    def recent_conversation(self, context_id: str, limit: int = 8) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_text, companion_reply, ts FROM conversation_turns WHERE context_id=? ORDER BY id DESC LIMIT ?",
                (str(context_id), max(1, int(limit))),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def add_event(self, context_id: str, kind: str, event: dict[str, Any]) -> None:
        summary = str(event.get("summary") or event.get("text") or kind)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO events(ts, context_id, kind, summary, payload) VALUES(?,?,?,?,?)",
                (time.time(), str(context_id), str(kind), summary, json.dumps(event, ensure_ascii=False)),
            )

    def recent_events(self, context_id: str, limit: int = 12) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, summary, payload, ts FROM events WHERE context_id=? ORDER BY id DESC LIMIT ?",
                (str(context_id), max(1, int(limit))),
            ).fetchall()
        out = []
        for row in reversed(rows):
            try:
                payload = json.loads(row["payload"])
            except Exception:
                payload = {}
            out.append({"kind": row["kind"], "summary": row["summary"], "payload": payload, "ts": row["ts"]})
        return out

    def conversation_context(self, context_id: str, limit: int = 8) -> str:
        rows = self.recent_conversation(context_id, limit)
        if not rows:
            return ""
        lines = ["【直近の会話】"]
        for row in rows:
            lines.append(f"user: {row['user_text']}")
            lines.append(f"assistant: {row['companion_reply']}")
        return "\n".join(lines)

    def event_context(self, context_id: str, limit: int = 12) -> str:
        rows = self.recent_events(context_id, limit)
        if not rows:
            return ""
        return "【現在までに確認済みの出来事】\n" + "\n".join(f"- {row['summary']}" for row in rows)

    def reset_conversation(self) -> int:
        with self._lock, self._conn:
            row = self._conn.execute("SELECT COUNT(*) FROM conversation_turns").fetchone()
            count = int(row[0] if row else 0)
            self._conn.execute("DELETE FROM conversation_turns")
        return count

    def close(self) -> None:
        with self._lock:
            self._conn.close()
