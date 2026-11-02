import json
import sqlite3
from collections import deque
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentctl.daemon.bus import Bus
from agentctl.models import STATUS_ORDER, Session, SessionEvent, Status, short_id

# ponytail: in-memory ring of recent events per session; tail -n beyond this needs a file read.
EVENTS_PER_SESSION = 500


def _folder(cwd: str) -> str:
    return Path(cwd).name.casefold() if cwd else ""


class AmbiguousRef(KeyError):
    """Several sessions match; `choices` lets a client offer a pick instead of an error."""

    def __init__(self, message: str, choices: list[dict[str, str]]) -> None:
        super().__init__(message)
        self.choices = choices


class Registry:
    def __init__(self, bus: Bus) -> None:
        self.bus = bus
        self.sessions: dict[str, Session] = {}
        self.events: dict[str, deque[SessionEvent]] = {}
        self.names: dict[str, str] = {}  # agentctl's own names (`rename`), by session id

    def upsert(self, s: Session) -> Session:
        return self.sessions.setdefault(s.id, s)

    def add_event(self, ev: SessionEvent, publish: bool = True) -> None:
        self.events.setdefault(ev.session_id, deque(maxlen=EVENTS_PER_SESSION)).append(ev)
        s = self.sessions.get(ev.session_id)
        if s and ev.ts > s.last_activity:
            s.last_activity = ev.ts
        if publish:
            self.bus.publish(ev)

    def set_status(
        self,
        sid: str,
        status: Status,
        reason: str | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        s = self.sessions.get(sid)
        if s is None:
            return False
        if reason is not None and (
            s.status_reason != reason or s.status_evidence != (evidence or {})
        ):
            s.status_reason = reason
            s.status_evidence = deepcopy(evidence or {})
            s.status_observed_at = datetime.now(UTC)
        if s.status == status:
            return False
        if reason is None:
            # A status-only writer must not inherit a prior transition's explanation.
            s.status_reason, s.status_observed_at, s.status_evidence = None, None, {}
        old, s.status = s.status, status
        self.bus.publish(
            SessionEvent(
                session_id=sid,
                kind="status_change",
                ts=datetime.now(UTC),
                payload={"from": old, "to": status, "reason": reason, "evidence": evidence},
            )
        )
        return True

    def remove(self, sid: str) -> None:
        self.sessions.pop(sid, None)
        self.events.pop(sid, None)

    def resolve(self, ref: str) -> Session:
        """The session someone means. In order: an exact id; `@`, the session that most needs
        you; an exact name (yours from `rename`, or the agent's title); an id prefix; a folder
        name; a word in the name. For names and folders, a single live session wins over ended
        ones; an id prefix must be unique."""
        if ref in self.sessions:
            return self.sessions[ref]
        if ref == "@":
            for s in self.listing():
                if s.status in ("awaiting_approval", "awaiting_input"):
                    return s
            raise KeyError("no session is waiting for you")
        want = ref.strip().casefold()
        if not want:
            raise KeyError("no session id or name given")
        named = [
            (s, (self.names.get(s.id) or s.title or "").casefold()) for s in self.sessions.values()
        ]
        steps: list[tuple[bool, list[Session]]] = [
            (True, [s for s, n in named if n == want]),
            (False, [s for s in self.sessions.values() if s.id.startswith((ref, f"ses_{ref}"))]),
            (True, [s for s in self.sessions.values() if _folder(s.cwd) == want]),
            (True, [s for s, n in named if len(want) >= 3 and want in n]),
        ]
        for prefer_live, hits in steps:
            live = [s for s in hits if s.status != "ended"] if prefer_live else hits
            if len(hits) == 1 or len(live) == 1:
                return (live if len(live) == 1 else hits)[0]
            if hits:
                choices = sorted(live or hits, key=lambda s: -s.last_activity.timestamp())
                shown = ", ".join(f"{short_id(s.id)} ({self.label(s)})" for s in choices[:6])
                more = f" and {len(choices) - 6} more" if len(choices) > 6 else ""
                raise AmbiguousRef(
                    f"ambiguous {ref!r}: {shown}{more}; type more of an id",
                    [
                        {"id": s.id, "label": self.label(s), "status": s.status, "cwd": s.cwd}
                        for s in choices[:20]
                    ],
                )
        raise KeyError(f"no session matches {ref!r} (by id, name or folder)")

    def label(self, s: Session) -> str:
        return self.names.get(s.id) or s.title or _folder(s.cwd) or s.id

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
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, session_id TEXT, "
            "created REAL, data TEXT)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS commands_session ON commands(session_id, created)"
        )

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

    def commands(self) -> list[dict[str, Any]]:
        return [
            json.loads(r[0]) for r in self.db.execute("SELECT data FROM commands ORDER BY created")
        ]

    def save_command(self, entry: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO commands VALUES (?, ?, ?, ?)",
            (entry["id"], entry["session_id"], entry["created"], json.dumps(entry)),
        )
        self.db.commit()

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
