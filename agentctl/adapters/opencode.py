"""opencode: sessions and transcripts from its SQLite DB (opened read-only), live status and
approvals from agentctl's plugin (opencode_plugin.js), which agentctl loads into the opencode
TUIs it launches. Formats: docs/formats.md.

Without the plugin (an opencode you started yourself) a session is read-only and its status is
inferred from the DB, which doesn't record pending permission prompts.
"""

import asyncio
import contextlib
import json
import logging
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentctl import OPENCODE_DB, OPENCODE_MODELS, tmux
from agentctl.adapters.base import ControlError
from agentctl.adapters.claude_code import LIVE_GRACE, cap, first_line, tool_summary
from agentctl.daemon.registry import Registry, Store
from agentctl.models import ApprovalRequest, Session, SessionEvent, Status, short_id

log = logging.getLogger(__name__)

# The TUI's permission prompt: "Allow once   Allow always   Reject", focus on the first.
OPTIONS = {"1": "Allow once", "2": "Allow always", "3": "Reject"}
PROMPT_MARK = "Permission required"
BACKLOG_MS = 7 * 86400 * 1000  # parts older than this aren't loaded at startup
OVERLAP_MS = 2000  # re-read rows this close to the watermark; `done` dedupes them
DEFAULT_TITLE = "New session - "


def ms(t: int | float) -> datetime:
    return datetime.fromtimestamp(t / 1000, UTC)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def context_windows(path: Path = OPENCODE_MODELS) -> dict[str, int]:
    """'provider/model' -> context limit, from opencode's models.dev cache."""
    try:
        data = json.loads(path.read_text())
        return {
            f"{p}/{m}": int(info["limit"]["context"])
            for p, prov in data.items()
            for m, info in (prov.get("models") or {}).items()
            if (info.get("limit") or {}).get("context")
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


class OpenCodeAdapter:
    source = "opencode"

    def __init__(self, reg: Registry, db: Path = OPENCODE_DB, store: Store | None = None) -> None:
        self.reg = reg
        self.db_path = db
        self.store = store
        self.conn: sqlite3.Connection | None = None
        self.sig: tuple[float, ...] = ()
        self.mark = 0  # ms: newest part/message time_updated seen
        self.done: set[str] = set()  # parts / errored messages already turned into events
        self.parent: dict[str, str] = {}  # subagent session -> its parent
        self.last: dict[str, dict[str, Any]] = {}  # session -> its newest message
        self.windows: dict[str, int] = {}
        self.errors: list[tuple[str, int, str]] = []
        # session -> live TUI showing it: {pid, socket, pane, busy, perm}. From the plugin.
        self.live: dict[str, dict[str, Any]] = {}
        for sid, row in (store.load() if store else {}).items():
            with contextlib.suppress(ValueError, TypeError):
                d = json.loads(row.get("detail") or "")
                if sid.startswith("ses_") and d.get("pid") and alive(d["pid"]):
                    self.live[sid] = {"pid": d["pid"], "socket": row["socket"], "pane": row["pane"]}

    # --- lifecycle ---

    async def discover(self) -> None:
        if not self.db_path.exists():
            log.info("no opencode DB at %s; opencode adapter idle", self.db_path)
            return
        self.windows = context_windows()
        self.mark = int(time.time() * 1000) - BACKLOG_MS
        self.poll(publish=False)
        self.first_prompts()

    def first_prompts(self) -> None:
        """opencode titles sessions with a model call; when that fails the title stays
        "New session - <date>". Fall back to the first prompt, as for Claude and Codex."""
        todo = {
            sid for sid, s in self.reg.sessions.items() if sid.startswith("ses_") and not s.title
        }
        rows = self.db().execute(
            "SELECT p.session_id, json_extract(p.data, '$.text') FROM part p "
            "JOIN message m ON m.id = p.message_id "
            "WHERE json_extract(m.data, '$.role') = 'user' "
            "AND json_extract(p.data, '$.type') = 'text' "
            "AND coalesce(json_extract(p.data, '$.synthetic'), 0) = 0 "
            "ORDER BY p.time_created DESC"  # so the earliest prompt is written last
        )
        for sid, text in rows if todo else ():
            if sid in todo and text and text.strip():
                self.reg.sessions[sid].title = first_line(text)

    async def watch(self) -> None:
        while True:
            await asyncio.sleep(1)
            try:
                self.poll(publish=True)
            except sqlite3.Error as e:  # opencode migrating its DB, etc.: retry next tick
                log.warning("opencode DB read failed: %s", e)
                self.close()
            for sid in list(self.live):
                if not alive(self.live[sid]["pid"]):
                    self.drop(sid)

    def close(self) -> None:
        if self.conn:
            self.conn.close()
        self.conn, self.sig = None, ()

    def db(self) -> sqlite3.Connection:
        if self.conn is None:
            # mode=ro: SQLite refuses every write on this connection
            self.conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        return self.conn

    # --- DB polling ---

    def poll(self, publish: bool) -> None:
        sig = tuple(
            p.stat().st_mtime if p.exists() else 0
            for p in (self.db_path, self.db_path.with_name(self.db_path.name + "-wal"))
        )
        if sig == self.sig or not sig[0]:
            return
        self.sig = sig
        db = self.db()
        since = self.mark - OVERLAP_MS
        # ponytail: full scan of `session` and of `part` past the watermark (no index on
        # time_updated); ~ms at thousands of rows. Track per-session cursors if that grows.
        rows = db.execute(
            "SELECT id, parent_id, directory, title, time_updated, time_archived FROM session"
        ).fetchall()
        for sid, parent, cwd, title, updated, archived in rows:
            if parent:
                self.parent[sid] = parent
            elif not archived:
                s = self.ensure(sid, cwd)
                if title and not title.startswith(DEFAULT_TITLE):
                    s.title = title[:80]
                s.last_activity = max(s.last_activity, ms(updated))
        for mid, sid, created, updated, data in db.execute(
            "SELECT id, session_id, time_created, time_updated, data FROM message "
            "WHERE time_updated > ? ORDER BY time_created",
            (since,),
        ):
            self.mark = max(self.mark, updated)
            self.on_message(mid, sid, created, updated, json.loads(data), publish)
        for pid_, sid, updated, data, role in db.execute(
            "SELECT p.id, p.session_id, p.time_updated, p.data, json_extract(m.data, '$.role') "
            "FROM part p JOIN message m ON m.id = p.message_id "
            "WHERE p.time_updated > ? ORDER BY p.time_created",
            (since,),
        ):
            self.mark = max(self.mark, updated)
            if pid_ not in self.done:
                self.on_part(pid_, sid, updated, json.loads(data), role, publish)
        for sid, ts, text in self.errors:  # an error ends its message: after that message's parts
            self.emit(sid, "error", ts, publish, text=text)
        self.errors.clear()
        for sid in self.reg.sessions:
            if sid.startswith("ses_"):
                self.sync(sid)

    def root(self, sid: str) -> str:
        for _ in range(10):
            if sid not in self.parent:
                break
            sid = self.parent[sid]
        return sid

    def ensure(self, sid: str, cwd: str = "") -> Session:
        s = self.reg.upsert(Session(id=sid, source="opencode", origin="cli", last_activity=ms(0)))
        s.cwd = s.cwd or cwd
        return s

    def on_message(
        self, mid: str, sid: str, created: int, updated: int, d: dict[str, Any], publish: bool
    ) -> None:
        cur = self.last.get(sid)
        if cur is None or created >= cur["created"]:
            self.last[sid] = {
                "created": created,
                "updated": updated,
                "role": d.get("role"),
                "done": bool((d.get("time") or {}).get("completed")) or bool(d.get("error")),
            }
        if d.get("role") != "assistant" or sid in self.parent:
            return
        s = self.reg.sessions.get(sid)
        tok = d.get("tokens") or {}
        cache = tok.get("cache") or {}
        used = (tok.get("input") or 0) + (cache.get("read") or 0) + (cache.get("write") or 0)
        if s and used:
            s.context_tokens = used
            window = self.windows.get(f"{d.get('providerID')}/{d.get('modelID')}")
            s.context_window, s.context_window_exact = window, bool(window)
        err = d.get("error")
        if err and f"err:{mid}" not in self.done:
            self.done.add(f"err:{mid}")
            text = (err.get("data") or {}).get("message") or err.get("name") or str(err)
            self.errors.append((sid, updated, cap(str(text), 500)))

    def on_part(
        self, part_id: str, sid: str, ts: int, d: dict[str, Any], role: str, publish: bool
    ) -> None:
        root = self.root(sid)
        sub = {"agent_id": sid} if root != sid else {}
        typ = d.get("type")
        if typ == "text" and not d.get("synthetic") and not d.get("ignored"):
            if role == "user":
                if sub:  # a subagent's prompt is the parent's tool call; skip the repeat
                    self.done.add(part_id)
                    return
                s = self.reg.sessions.get(root)
                if s and not s.title:
                    s.title = first_line(d.get("text"))
            elif not (d.get("time") or {}).get("end"):
                return  # still streaming
            self.done.add(part_id)
            self.emit(root, "message", ts, publish, role=role, text=cap(d.get("text") or ""), **sub)
        elif typ == "tool":
            st = d.get("state") or {}
            if st.get("status") not in ("completed", "error"):
                return
            self.done.add(part_id)
            tool = str(d.get("tool") or "?")
            inp = st.get("input")
            self.emit(
                root, "tool_call", ts, publish,
                id=d.get("callID"), name=tool, summary=tool_summary(inp), **sub,
            )  # fmt: skip
            is_err = st["status"] == "error"
            out = st.get("error") if is_err else st.get("output")
            self.emit(
                root, "tool_result", ts, publish,
                tool_use_id=d.get("callID"), text=cap(str(out or "")), is_error=is_err, **sub,
            )  # fmt: skip
        else:
            self.done.add(part_id)  # reasoning, step markers, patches, files

    def emit(self, sid: str, kind: Any, ts: int, publish: bool, **payload: Any) -> None:
        if sid in self.reg.sessions:
            ev = SessionEvent(session_id=sid, kind=kind, ts=ms(ts), payload=payload)
            self.reg.add_event(ev, publish=publish)

    # --- plugin events ---

    def on_hook(
        self, event: str, p: dict[str, Any], pid: int | None, pane: str | None, tmux_env: str | None
    ) -> None:
        sid = p.get("sessionID")
        if not sid or not pid:
            return
        if sid not in self.parent and sid not in self.reg.sessions:
            self.lookup_parent(sid)
        root = self.root(sid)
        if root != sid and not event.startswith("permission."):
            return  # a subagent's busy/idle says nothing about whether its parent is done
        for other, row in list(self.live.items()):  # a TUI shows one session at a time
            if row["pid"] == pid and other != root:
                self.drop(other)
        row = self.live.setdefault(root, {"busy": False, "perm": None})
        row.update(pid=pid, pane=pane, socket=tmux_env.split(",")[0] if tmux_env else None)
        s = self.ensure(root)
        if event == "session.status":
            row["busy"] = (p.get("status") or {}).get("type") != "idle"
        elif event == "session.idle":
            row["busy"] = False
        elif event == "permission.asked":
            row["perm"] = p.get("id")
            what = " ".join(p.get("patterns") or []) or tool_summary(p.get("metadata"))
            s.pending_approval = ApprovalRequest(
                tool=cap(f"{p.get('permission')}: {what}", 300),
                detail="opencode permission",
                options=dict(OPTIONS),
            )
            now = int(time.time() * 1000)
            self.sync(root)
            self.emit(root, "approval_request", now, True, **s.pending_approval.model_dump())
        elif event == "permission.replied" and row.get("perm") == p.get("requestID"):
            row["perm"] = None
        if self.store:
            detail = json.dumps({"pid": pid})
            self.store.put(root, {"socket": row["socket"], "pane": pane, "detail": detail})
        self.sync(root)

    def lookup_parent(self, sid: str) -> None:
        with contextlib.suppress(sqlite3.Error):
            r = self.db().execute("SELECT parent_id FROM session WHERE id = ?", (sid,)).fetchone()
            if r and r[0]:
                self.parent[sid] = r[0]

    def register_pane(self, sid: str, sock: str | None, pane: str, pid: int) -> None:
        """`adopt`/resume: the session is known before the plugin says anything."""
        self.live[sid] = {"pid": pid, "socket": sock, "pane": pane, "busy": False, "perm": None}
        self.ensure(sid)
        self.sync(sid)

    def holder(self, sid: str) -> int | None:
        row = self.live.get(sid)  # only known for opencodes agentctl started
        return int(row["pid"]) if row else None

    def drop(self, sid: str) -> None:
        self.live.pop(sid, None)
        if self.store:
            self.store.put(sid, {})
        self.sync(sid)

    # --- status ---

    def sync(self, sid: str) -> None:
        s = self.reg.sessions.get(sid)
        if s is None:
            return
        row = self.live.get(sid)
        status: Status
        if row:
            status = "awaiting_approval" if row.get("perm") else "working"
            if not row.get("perm") and not row.get("busy"):
                status = "awaiting_input"
        else:
            last = self.last.get(sid) or {}
            recent = time.time() - s.last_activity.timestamp() < LIVE_GRACE.total_seconds()
            if recent and last and not (last["role"] == "assistant" and last["done"]):
                status = "working"  # in an opencode agentctl doesn't control
            else:
                status = "idle" if recent else "ended"
        s.controllable = bool(row and row.get("pane"))
        s.tmux_pane = row.get("pane") if row else None
        if status != "awaiting_approval":
            s.pending_approval = None
        self.reg.set_status(sid, status)

    # --- control ---

    async def verified(self, sid: str) -> tuple[str | None, str]:
        row = self.live.get(sid)
        if not row or not row.get("pane"):
            raise ControlError(
                "session isn't open in an opencode agentctl launched; "
                f"`agentctl adopt {short_id(sid)}` reopens it there"
            )
        root = await tmux.pane_pid(row["socket"], row["pane"])
        if root is None or not await tmux.is_descendant(row["pid"], root):
            self.drop(sid)
            raise ControlError("its opencode pane is gone")
        return row["socket"], row["pane"]

    async def send(self, session_id: str, text: str) -> None:
        sock, pane = await self.verified(session_id)
        if self.live[session_id].get("perm"):
            raise ControlError("session is waiting for approval: approve or deny it first")
        await tmux.send_text(sock, pane, text, settle=0.3)

    async def approve(self, session_id: str, choice: int | None, allow: bool) -> str:
        sock, pane = await self.verified(session_id)
        if not self.live[session_id].get("perm"):
            raise ControlError("session isn't waiting for approval")
        key = str(choice) if choice is not None else ("1" if allow else "3")
        if key not in OPTIONS:
            raise ControlError("no option " + key + "; choices: " + ", ".join(OPTIONS))
        if PROMPT_MARK not in await tmux.capture(sock, pane):
            raise ControlError("no permission prompt visible in the pane; use `agentctl attach`")
        await tmux.keys(sock, pane, *["Right"] * (int(key) - 1), "Enter")
        self.live[session_id]["perm"] = None  # permission.replied confirms it
        self.sync(session_id)
        return f"{key}. {OPTIONS[key]}"

    async def interrupt(self, session_id: str) -> None:
        sock, pane = await self.verified(session_id)
        await tmux.keys(sock, pane, "Escape")  # the first Escape only arms the interrupt
        await asyncio.sleep(0.2)
        await tmux.keys(sock, pane, "Escape")
