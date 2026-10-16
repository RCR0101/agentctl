import json
import sqlite3
from collections import deque
from datetime import UTC, datetime
from typing import Any

from agentctl.daemon.bus import Bus
from agentctl.models import STATUS_ORDER, Session, SessionEvent, Status

# ponytail: in-memory ring of recent events per session; tail -n beyond this needs a file read.
EVENTS_PER_SESSION = 500


class Registry:
    def __init__(self, bus: Bus) -> None:
        self.bus = bus
        self.sessions: dict[str, Session] = {}
        self.events: dict[str, deque[SessionEvent]] = {}

    def upsert(self, s: Session) -> Session:
        return self.sessions.setdefault(s.id, s)

    def add_event(self, ev: SessionEvent, publish: bool = True) -> None:
        self.events.setdefault(ev.session_id, deque(maxlen=EVENTS_PER_SESSION)).append(ev)
        s = self.sessions.get(ev.session_id)
        if s and ev.ts > s.last_activity:
            s.last_activity = ev.ts
        if publish:
            self.bus.publish(ev)

    def set_status(self, sid: str, status: Status, reason: str | None = None) -> bool:
        s = self.sessions.get(sid)
        if s is None or s.status == status:
            return False
        old, s.status = s.status, status
        self.bus.publish(
            SessionEvent(
                session_id=sid,
                kind="status_change",
                ts=datetime.now(UTC),
                payload={"from": old, "to": status, "reason": reason},
            )
        )
        return True

    def remove(self, sid: str) -> None:
        self.sessions.pop(sid, None)
        self.events.pop(sid, None)

    def resolve(self, prefix: str) -> Session:
        if prefix in self.sessions:
            return self.sessions[prefix]
        hits = [s for sid, s in self.sessions.items() if sid.startswith((prefix, f"ses_{prefix}"))]
        if not hits:
            raise KeyError(f"no session matches {prefix!r}")
        if len(hits) > 1:
            ids = ", ".join(sorted(s.id[:12] for s in hits))
            raise KeyError(f"ambiguous id {prefix!r}: {ids}")
        return hits[0]

    def listing(self) -> list[Session]:
        return sorted(
            self.sessions.values(),
            key=lambda s: (STATUS_ORDER[s.status], -s.last_activity.timestamp()),
        )


class Store:
    """~/.agentctl/state.db: session -> tmux pane, plus the last hook seen. Survives restarts."""

    COLS = ("socket", "pane", "hook_event", "hook_status", "hook_ts", "detail")

    def __init__(self, path: str) -> None:
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, socket TEXT,"
            " pane TEXT, hook_event TEXT, hook_status TEXT, hook_ts REAL, detail TEXT)"
        )
        # names given with `agentctl rename`; agentctl's own, never written to the agent
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS titles (session_id TEXT PRIMARY KEY, title TEXT)"
        )
        # when you last looked at each session ("*": when agentctl first started tracking)
        self.db.execute("CREATE TABLE IF NOT EXISTS seen (session_id TEXT PRIMARY KEY, ts REAL)")
        # follow-ups to send once the agent is free: JSON list of texts
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS queued (session_id TEXT PRIMARY KEY, texts TEXT)"
        )
        # small JSON values that must outlive a restart (e.g. the last Claude plan limits)
        self.db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")

    def load(self) -> dict[str, dict[str, Any]]:
        rows = self.db.execute(f"SELECT session_id, {', '.join(self.COLS)} FROM sessions")
        return {r[0]: dict(zip(self.COLS, r[1:], strict=True)) for r in rows}

    def titles(self) -> dict[str, str]:
        return dict(self.db.execute("SELECT session_id, title FROM titles").fetchall())

    def set_title(self, sid: str, title: str | None) -> None:
        if title:
            self.db.execute("INSERT OR REPLACE INTO titles VALUES (?, ?)", (sid, title))
        else:
            self.db.execute("DELETE FROM titles WHERE session_id = ?", (sid,))
        self.db.commit()

    def seen(self) -> dict[str, float]:
        return dict(self.db.execute("SELECT session_id, ts FROM seen").fetchall())

    def set_seen(self, sid: str, ts: float) -> None:
        self.db.execute("INSERT OR REPLACE INTO seen VALUES (?, ?)", (sid, ts))
        self.db.commit()

    def queued(self) -> dict[str, list[str]]:
        rows = self.db.execute("SELECT session_id, texts FROM queued").fetchall()
        return {sid: json.loads(t) for sid, t in rows if json.loads(t)}

    def set_queued(self, sid: str, texts: list[str]) -> None:
        if texts:
            self.db.execute("INSERT OR REPLACE INTO queued VALUES (?, ?)", (sid, json.dumps(texts)))
        else:
            self.db.execute("DELETE FROM queued WHERE session_id = ?", (sid,))
        self.db.commit()

    def get_json(self, key: str) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def set_json(self, key: str, value: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))
        self.db.commit()

    def put(self, sid: str, row: dict[str, Any]) -> None:
        vals = [row.get(c) for c in self.COLS]
        self.db.execute(
            f"INSERT OR REPLACE INTO sessions VALUES (?, {', '.join('?' * len(self.COLS))})",
            [sid, *vals],
        )
        self.db.commit()
