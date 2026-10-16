"""Codex: a private `codex app-server` (stdio JSON-RPC) for discovery and control, plus tailing
~/.codex/sessions/**/rollout-*.jsonl for threads it isn't streaming. Formats: docs/formats.md.

Threads another Codex process has open (the desktop app, a TUI) hold an flock on
~/.codex/thread-writer-locks/<id>.lock. agentctl reads those but never writes to such a thread.
"""

import asyncio
import contextlib
import itertools
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentctl import CODEX_DIR, launcher, tmux
from agentctl.adapters.base import ControlError
from agentctl.adapters.claude_code import LIVE_GRACE, cap, first_line, parse_ts
from agentctl.daemon.registry import Registry
from agentctl.models import ApprovalRequest, EventKind, Origin, Session, SessionEvent, Status
from agentctl.tailer import FileTail

log = logging.getLogger(__name__)

APPROVALS = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}
# CommandExecutionApprovalDecision / FileChangeApprovalDecision share these four.
DECISIONS = {
    "1": ("accept", "Yes"),
    "2": ("acceptForSession", "Yes, and don't ask again this session"),
    "3": ("decline", "No"),
    "4": ("cancel", "No, and stop the turn"),
}
LAST_STATUS: dict[str | None, Status] = {"working": "working", None: "idle"}
THREAD_PAGES = 5  # ponytail: newest 500 interactive threads; paginate further if anyone has more
REFRESH_SECS = 15


class CodexError(Exception):
    pass


def origin_of(source: Any, originator: str | None) -> Origin:
    if originator and "desktop" in originator.lower():
        return "desktop"
    if originator == "codex-tui" or source == "cli":
        return "cli"
    if source == "vscode" and not originator:
        return "desktop"  # thread/list doesn't say; the desktop app reports "vscode"
    if source in ("exec", "appServer") or originator:
        return "sdk"
    return "unknown"


def is_subagent(source: Any) -> bool:
    return isinstance(source, dict) and any("sub" in k.lower() for k in source)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def item_events(item: dict[str, Any], sid: str, ts: datetime) -> list[SessionEvent]:
    """A ThreadItem as SessionEvents. Accepts both the app-server's camelCase items and the
    PascalCase/snake_case ones paginated rollouts store."""
    typ = str(item.get("type", ""))
    kind = typ[:1].lower() + typ[1:]
    out: list[SessionEvent] = []

    def ev(k: EventKind, **payload: Any) -> None:
        out.append(SessionEvent(session_id=sid, kind=k, ts=ts, payload=payload))

    if kind == "userMessage":
        ev("message", role="user", text=cap(_text(item.get("content"))))
    elif kind == "agentMessage":
        text = item.get("text") or _text(item.get("content"))
        if text:
            ev("message", role="assistant", text=cap(text))
    elif kind == "commandExecution":
        cmd = item.get("command")
        cmd = cmd[-1] if isinstance(cmd, list) and cmd else str(cmd or "")
        ev("tool_call", id=item.get("id"), name="shell", summary=cap(cmd, 300))
        status = item.get("status")
        if status in ("completed", "failed", "declined"):
            code = item.get("exitCode", item.get("exit_code"))
            output = item.get("aggregatedOutput") or item.get("aggregated_output") or ""
            ev(
                "tool_result",
                tool_use_id=item.get("id"),
                text=cap(output or f"[{status}]"),
                is_error=status != "completed" or str(code) not in ("0", "None"),
            )
    elif kind == "fileChange":
        ch = item.get("changes") or []
        paths = list(ch) if isinstance(ch, dict) else [c.get("path", "") for c in ch]
        ev("tool_call", id=item.get("id"), name="edit", summary=", ".join(paths)[:300])
    elif kind == "mcpToolCall":
        name = f"{item.get('server')}.{item.get('tool')}"
        ev(
            "tool_call",
            id=item.get("id"),
            name=name,
            summary=cap(json.dumps(item.get("arguments")), 300),
        )
    elif kind in ("webSearch", "extension") and (item.get("query") or kind == "webSearch"):
        ev("tool_call", id=item.get("id"), name="web_search", summary=str(item.get("query") or ""))
    return out


@dataclass
class Rollout(FileTail):
    tid: str = ""
    paginated: bool = True
    originator: str | None = None
    source: Any = None
    cwd: str = ""
    first_prompt: str | None = None
    last_kind: str | None = None  # working | done | interrupted
    context: int | None = None
    # newest token_count rate_limits per bucket ("codex", "premium", ...)
    limits: dict[str, tuple[datetime, dict[str, Any]]] = field(default_factory=dict)
    window: int | None = None


def parse_rollout(d: dict[str, Any], r: Rollout) -> list[SessionEvent]:
    typ, p = d.get("type"), d.get("payload") or {}
    if not isinstance(p, dict):
        return []
    ts = parse_ts(d.get("timestamp"))
    if typ == "session_meta":
        r.paginated = p.get("history_mode") == "paginated"
        r.originator, r.source, r.cwd = p.get("originator"), p.get("source"), p.get("cwd", "")
        return []
    if typ == "event_msg":
        et = p.get("type")
        if et == "task_started":
            r.last_kind = "working"
        elif et == "task_complete":
            r.last_kind = "done"
        elif et == "turn_aborted":
            r.last_kind = "interrupted"
        elif et == "token_count" and isinstance(p.get("info"), dict):
            info = p["info"]
            r.context = (info.get("last_token_usage") or {}).get("input_tokens", r.context)
            r.window = info.get("model_context_window") or r.window
            if isinstance(p.get("rate_limits"), dict):
                rl = p["rate_limits"]
                if rl.get("primary") or rl.get("secondary"):  # some buckets report no windows
                    r.limits[str(rl.get("limit_id") or "codex")] = (ts, rl)
        elif et == "item_completed" and r.paginated:
            evs = item_events(p.get("item") or {}, r.tid, ts)
            if r.first_prompt is None and evs and evs[0].payload.get("role") == "user":
                r.first_prompt = evs[0].payload["text"]
            return evs
        elif et in ("user_message", "agent_message") and not r.paginated:
            role = "user" if et == "user_message" else "assistant"
            if role == "user" and r.first_prompt is None:
                r.first_prompt = p.get("message")
            return [
                SessionEvent(
                    session_id=r.tid,
                    kind="message",
                    ts=ts,
                    payload={"role": role, "text": cap(str(p.get("message", "")))},
                )
            ]
        return []
    if typ == "response_item" and not r.paginated:  # legacy files: tools only appear here
        pt = p.get("type")
        if pt in ("custom_tool_call", "function_call"):
            raw = p.get("input") if pt == "custom_tool_call" else p.get("arguments")
            payload = {"id": p.get("call_id"), "name": p.get("name"), "summary": cap(str(raw), 300)}
            return [SessionEvent(session_id=r.tid, kind="tool_call", ts=ts, payload=payload)]
        if pt in ("custom_tool_call_output", "function_call_output"):
            text = _text(p.get("output"))
            payload = {"tool_use_id": p.get("call_id"), "text": cap(text), "is_error": False}
            return [SessionEvent(session_id=r.tid, kind="tool_result", ts=ts, payload=payload)]
    return []


class AppServer:
    """`codex app-server` over stdio. Requests flow both ways; ids are per direction."""

    def __init__(self, on_message: Callable[[dict[str, Any]], None], exe: str) -> None:
        self.on_message = on_message
        self.exe = exe
        self.proc: asyncio.subprocess.Process | None = None
        self.ids = itertools.count(1)
        self.waiting: dict[int, asyncio.Future[Any]] = {}

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            self.exe,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=64 << 20,
        )
        asyncio.get_running_loop().create_task(self._read())
        await self.request(
            "initialize",
            {
                "clientInfo": {"name": "agentctl", "title": "agentctl", "version": "0.1"},
                "capabilities": None,
            },
        )
        self._write({"method": "initialized"})

    def _write(self, msg: dict[str, Any]) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(msg).encode() + b"\n")

    async def request(self, method: str, params: dict[str, Any], timeout: float = 30) -> Any:
        if not self.alive:
            raise CodexError("codex app-server is not running")
        rid = next(self.ids)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.waiting[rid] = fut
        self._write({"id": rid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.waiting.pop(rid, None)

    def respond(self, rid: Any, result: Any = None, error: str | None = None) -> None:
        if error is not None:
            self._write({"id": rid, "error": {"code": -32000, "message": error}})
        else:
            self._write({"id": rid, "result": result})

    async def _read(self) -> None:
        assert self.proc and self.proc.stdout
        async for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "id" in msg and "method" not in msg:  # response to us
                fut = self.waiting.get(msg["id"])
                if fut and not fut.done():
                    if "error" in msg:
                        fut.set_exception(CodexError(msg["error"].get("message", "error")))
                    else:
                        fut.set_result(msg.get("result"))
            else:
                try:
                    self.on_message(msg)
                except Exception:
                    log.exception("handling %s", msg.get("method"))
        log.warning("codex app-server exited")
        for fut in self.waiting.values():
            if not fut.done():
                fut.set_exception(CodexError("codex app-server exited"))

    async def stop(self) -> None:
        if self.alive and self.proc:
            self.proc.terminate()
            await self.proc.wait()


class CodexAdapter:
    source = "codex"

    def __init__(
        self, reg: Registry, root: Path = CODEX_DIR, exe: str | None = None, debug: bool = False
    ) -> None:
        self.reg = reg
        self.sessions_dir = root / "sessions"
        self.locks_dir = root / "thread-writer-locks"
        self.debug = debug
        self.exe = exe or launcher.best_codex()
        self.server: AppServer | None = None
        self.rollouts: dict[str, Rollout] = {}
        self.loaded: dict[str, dict[str, Any]] = {}  # threads our app-server has resumed
        self.pending: dict[str, dict[str, Any]] = {}  # thread -> unanswered server request
        self.holders: dict[str, int] = {}  # thread -> pid of another process holding its lock
        # tmux servers to look for TUIs on: yours (default) and agentctl's own
        self.sockets: set[str | None] = {None, launcher.socket_path()}
        self.not_in_tmux: set[int] = set()  # holder pids already checked (e.g. the desktop app)
        self.thread_panes: dict[str, tuple[str | None, str]] = {}
        self.tui_prompt: dict[str, dict[str, str]] = {}  # thread -> menu showing in its TUI pane

    # --- lifecycle ---

    async def ensure_server(self) -> AppServer:
        if not self.exe:
            raise ControlError("codex is not installed")
        if self.server is None or not self.server.alive:
            self.server = AppServer(self.on_message, self.exe)
            await self.server.start()
            self.loaded.clear()
        return self.server

    async def discover(self) -> None:
        if not self.exe:
            log.info("codex not found; codex adapter disabled")
            return
        await self.scan_locks()
        try:
            await self.refresh_threads(startup=True)
        except (CodexError, ControlError, OSError, TimeoutError) as e:
            log.warning("codex discovery failed: %s", e)

    async def watch(self) -> None:
        # FSEvents never fires for rollouts: Codex keeps the file open and appends (verified).
        # So poll: one stat per live/recent thread per second.
        if not self.exe:
            return
        last_refresh = time.monotonic()
        try:
            for n in itertools.count():
                await asyncio.sleep(1)
                if n % 2 == 0:
                    await self.scan_locks()
                    await self.scan_tui_prompts()
                    if set(self.holders) - set(self.reg.sessions):  # a thread we haven't seen
                        last_refresh = 0
                if time.monotonic() - last_refresh > REFRESH_SECS:
                    last_refresh = time.monotonic()
                    with contextlib.suppress(CodexError, ControlError, OSError, TimeoutError):
                        await self.refresh_threads()
                for tid, r in list(self.rollouts.items()):
                    s = self.reg.sessions.get(tid)
                    if tid in self.holders or tid in self.loaded or (s and s.status != "ended"):
                        self.ingest(r, publish=True)
                        self.sync(tid)
        finally:
            if self.server:
                await self.server.stop()

    async def refresh_threads(self, startup: bool = False) -> None:
        srv = await self.ensure_server()
        cursor = None
        for _ in range(THREAD_PAGES):
            params = {
                "limit": 100,
                "cursor": cursor,
                "sortKey": "updated_at",
                "useStateDbOnly": True,
            }
            res = await srv.request("thread/list", params)
            for t in res["data"]:
                self.upsert_thread(t, startup)
            cursor = res.get("nextCursor")
            if not cursor:
                break

    def upsert_thread(self, t: dict[str, Any], startup: bool = False) -> None:
        tid = t["id"]
        s = self.ensure_session(tid)
        s.cwd = t.get("cwd") or s.cwd
        title = t.get("name") or (t.get("preview") or "").strip()
        s.title = first_line(title) or s.title
        s.origin = origin_of(
            t.get("source"), self.rollouts[tid].originator if tid in self.rollouts else None
        )
        updated = datetime.fromtimestamp(t.get("updatedAt") or 0, UTC)
        s.last_activity = max(s.last_activity, updated)
        if tid not in self.rollouts and t.get("path"):
            self.open_rollout(Path(t["path"]), startup)
        self.sync(tid)

    def ensure_session(self, tid: str) -> Session:
        return self.reg.upsert(
            Session(id=tid, source="codex", last_activity=datetime.fromtimestamp(0, UTC))
        )

    # --- rollouts ---

    def open_rollout(self, p: Path, startup: bool) -> None:
        r = Rollout(path=p, tid=p.stem[-36:])
        try:
            with p.open("rb") as f:
                meta = json.loads(f.readline() or b"{}")
            parse_rollout(meta, r)
            head = r.skip_to_tail() if startup else []
        except (OSError, ValueError):
            return
        if is_subagent(r.source):
            return
        for raw in head:
            self.parse_line(r, raw)
        self.rollouts[r.tid] = r
        s = self.ensure_session(r.tid)
        s.cwd = s.cwd or r.cwd
        s.origin = origin_of(r.source, r.originator)
        self.ingest(r, publish=not startup)
        s.title = s.title or first_line(r.first_prompt)
        self.sync(r.tid)

    def parse_line(self, r: Rollout, raw: bytes) -> list[SessionEvent]:
        try:
            d = json.loads(raw)
            return parse_rollout(d, r) if isinstance(d, dict) else []
        except Exception as e:  # never let one bad line kill the watcher
            log.warning("skipping bad line in %s: %s", r.path.name, type(e).__name__)
            return []

    def ingest(self, r: Rollout, publish: bool) -> None:
        for raw in r.read_lines():
            for ev in self.parse_line(r, raw):
                if r.tid not in self.loaded:  # our app-server streams those threads instead
                    self.reg.add_event(ev, publish=publish)
        s = self.reg.sessions.get(r.tid)
        if s and r.mtime:
            s.last_activity = max(s.last_activity, datetime.fromtimestamp(r.mtime, UTC))

    # --- locks, panes, status ---

    async def scan_locks(self) -> None:
        """Which other process has each thread open: its rollout file (desktop app and TUI keep
        it open) or its writer lock (desktop app). One `lsof -c codex`, ~55 ms."""
        proc = await asyncio.create_subprocess_exec(
            "lsof", "-Fpn", "-c", "codex", stdout=asyncio.subprocess.PIPE
        )
        out, _ = await proc.communicate()
        ours = self.server.proc.pid if self.server and self.server.proc else -1
        holders: dict[str, int] = {}
        pid = 0
        for ln in out.decode(errors="replace").splitlines():
            if ln.startswith("p"):
                pid = int(ln[1:])
            elif not ln.startswith("n") or pid == ours:
                continue
            elif ln.endswith(".lock") and Path(ln[1:]).parent == self.locks_dir:
                holders[Path(ln[1:]).stem] = pid
            elif "/rollout-" in ln and ln.endswith(".jsonl"):
                path = Path(ln[1:])
                holders[path.stem[-36:]] = pid
                if path.stem[-36:] not in self.rollouts:  # brand-new thread: pick it up now
                    self.open_rollout(path, startup=False)
        changed = set(holders.items()) ^ set(self.holders.items())
        self.holders = holders
        await self.map_panes(holders)
        for tid, _ in changed:
            if tid in self.reg.sessions:
                self.sync(tid)

    async def scan_tui_prompts(self) -> None:
        """TUI approvals aren't in the rollout, so read the menu off panes we launched."""
        for tid, (sock, pane) in list(self.thread_panes.items()):
            s = self.reg.sessions.get(tid)
            if (
                tid not in self.holders
                or s is None
                or s.status not in ("working", "awaiting_approval")
            ):
                continue
            try:
                screen = await tmux.capture(sock, pane)
            except ControlError:
                continue
            opts = tmux.parse_options(screen) if "Press enter to confirm" in screen else {}
            if opts and not self.tui_prompt.get(tid):
                self.tui_prompt[tid] = opts
                tool = next(
                    (ln.strip() for ln in screen.splitlines() if ln.strip().startswith("$ ")), None
                )
                s.pending_approval = ApprovalRequest(tool=tool, detail="Codex TUI", options=opts)
                self.sync(tid)
                self.reg.add_event(
                    SessionEvent(
                        session_id=tid,
                        kind="approval_request",
                        ts=datetime.now(UTC),
                        payload=s.pending_approval.model_dump(),
                    )
                )
            elif not opts and tid in self.tui_prompt:  # answered in the pane itself
                del self.tui_prompt[tid]
                self.sync(tid)

    async def map_panes(self, holders: dict[str, int]) -> None:
        """Thread -> tmux pane, for holders running inside any pane on the default server or
        one `agentctl new codex` used. Covers TUIs started by hand, and survives restarts."""
        todo = {t: p for t, p in holders.items() if t not in self.thread_panes}
        todo = {t: p for t, p in todo.items() if p not in self.not_in_tmux}
        if not todo:
            return
        panes = [(sk, pn, pp) for sk in self.sockets for pn, pp in await tmux.all_panes(sk)]
        parent = await tmux.parents()
        for tid, pid in todo.items():
            for sock, pane, root in panes:
                if await tmux.is_descendant(pid, root, parent):
                    self.thread_panes[tid] = (sock, pane)
                    break
            else:
                self.not_in_tmux.add(pid)

    def limits(self) -> dict[str, Any] | None:
        """Your Codex plan's usage windows: the newest report of each limit bucket ("codex" is
        the main one; others are prefixed, e.g. "premium 5h"):
        {"plan", "at", "windows": [{"name", "used_percent", "resets_at"}]}. A window whose
        reset time has passed is reported as 0% used."""
        newest: dict[str, tuple[datetime, dict[str, Any]]] = {}
        for r in self.rollouts.values():
            for bucket, (ts, rl) in r.limits.items():
                if bucket not in newest or ts > newest[bucket][0]:
                    newest[bucket] = (ts, rl)
        if not newest:
            return None
        now = time.time()
        windows = []
        for bucket in sorted(newest, key=lambda b: b != "codex"):  # the main bucket first
            rl = newest[bucket][1]
            for key in ("primary", "secondary"):
                w = rl.get(key) or {}
                if not w:
                    continue
                mins = w.get("window_minutes") or 0
                name = "5h" if mins == 300 else "week" if mins == 10080 else f"{mins // 60}h"
                name = name if bucket == "codex" else f"{bucket} {name}"
                reset = w.get("resets_at") or 0
                used = 0.0 if reset and reset < now else float(w.get("used_percent") or 0)
                windows.append({"name": name, "used_percent": used, "resets_at": reset})
        at, rl = max(newest.values(), key=lambda x: x[0])
        return {"plan": rl.get("plan_type"), "at": at.isoformat(), "windows": windows}

    def holder(self, tid: str) -> int | None:
        return self.holders.get(tid)

    def register_pane(self, sock: str | None, pane: str) -> None:
        self.sockets.add(sock)

    def sync(self, tid: str) -> None:
        s = self.reg.sessions.get(tid)
        if s is None:
            return
        r, loaded = self.rollouts.get(tid), self.loaded.get(tid)
        pane = self.thread_panes.get(tid) if tid in self.holders else None
        s.tmux_pane = pane[1] if pane else None
        if r and r.context is not None:
            s.context_tokens, s.context_window, s.context_window_exact = r.context, r.window, True
        s.controllable = bool(self.exe) and (pane is not None or tid not in self.holders)
        last = LAST_STATUS.get(r.last_kind if r else None, "awaiting_input")
        st = (loaded or {}).get("status") or {}
        if tid in self.pending or tid in self.tui_prompt:
            status: Status = "awaiting_approval"
        elif st.get("type") == "active":
            flags = st.get("activeFlags") or []
            status = "awaiting_approval" if "waitingOnApproval" in flags else "working"
            status = "awaiting_input" if "waitingOnUserInput" in flags else status
        elif st.get("type") == "idle":
            status = "awaiting_input"
        elif tid in self.holders:  # open in the desktop app or a TUI
            status = last
        elif r and time.time() - r.mtime < LIVE_GRACE.total_seconds():
            status = last
        else:
            status = "ended"
        if status != "awaiting_approval":
            s.pending_approval = None
        self.reg.set_status(tid, status)

    # --- app-server messages ---

    def on_message(self, m: dict[str, Any]) -> None:
        method, p = m.get("method", ""), m.get("params") or {}
        tid = p.get("threadId")
        if "id" in m:  # server -> client request
            if not tid:
                if self.server:
                    self.server.respond(m["id"], error="not supported by agentctl")
                return
            self.pending[tid] = {"id": m["id"], "method": method, "params": p}
            self.announce(tid, method, p)
            return
        if method == "thread/started":
            self.upsert_thread(p["thread"])
            return
        if not tid or tid not in self.loaded:
            return
        now = datetime.now(UTC)
        if method == "thread/status/changed":
            self.loaded[tid]["status"] = p.get("status")
        elif method == "turn/started":
            self.loaded[tid]["turn"] = p["turn"]["id"]
        elif method == "turn/completed":
            self.loaded[tid]["turn"] = None
            asyncio.get_running_loop().create_task(self.release(tid))
        elif method == "item/completed":
            for ev in item_events(p.get("item") or {}, tid, now):
                self.reg.add_event(ev)
        elif method == "serverRequest/resolved":
            req = self.pending.get(tid)
            if req and req["id"] == p.get("requestId"):
                del self.pending[tid]
        elif method == "thread/name/updated" and p.get("name"):
            self.reg.sessions[tid].title = p["name"]
        elif method == "error":
            err = p.get("error") or {}
            text = err.get("message") if isinstance(err, dict) else str(err)
            self.reg.add_event(
                SessionEvent(session_id=tid, kind="error", ts=now, payload={"text": text})
            )
        self.sync(tid)

    def announce(self, tid: str, method: str, p: dict[str, Any]) -> None:
        s = self.ensure_session(tid)
        if method == "item/commandExecution/requestApproval":
            tool = f"shell: {p.get('command') or '?'}"
        elif method == "item/fileChange/requestApproval":
            tool = f"edit: {p.get('reason') or p.get('grantRoot') or 'file changes'}"
        else:
            tool = f"{method} (answer it in Codex; agentctl can only deny it)"
        opts = {k: v[1] for k, v in DECISIONS.items()} if method in APPROVALS else {}
        s.pending_approval = ApprovalRequest(
            tool=cap(tool, 300), detail=p.get("reason"), options=opts
        )
        self.sync(tid)
        self.reg.add_event(
            SessionEvent(
                session_id=tid,
                kind="approval_request",
                ts=datetime.now(UTC),
                payload=s.pending_approval.model_dump(),
            )
        )

    async def release(self, tid: str) -> None:
        """Unload a thread once its turn is done so the desktop app can open it again."""
        await asyncio.sleep(1)
        if self.server and tid in self.loaded and not self.loaded[tid].get("turn"):
            with contextlib.suppress(CodexError, TimeoutError):
                await self.server.request("thread/unsubscribe", {"threadId": tid})
            self.loaded.pop(tid, None)
            self.sync(tid)

    # --- control ---

    def _foreign(self, tid: str) -> tuple[str | None, str] | None:
        """Pane if the thread is open in a TUI we launched; error if open anywhere else."""
        if tid not in self.holders:
            return None
        pane = self.thread_panes.get(tid)
        if pane:
            return pane
        raise ControlError(
            f"thread is open in another Codex process (pid {self.holders[tid]}, e.g. the desktop "
            "app); agentctl won't write to it concurrently. Close it there first."
        )

    async def verified(self, tid: str) -> tuple[str | None, str]:
        pane = self._foreign(tid)
        if not pane:
            raise ControlError("thread isn't running in a tmux pane (control it with send/approve)")
        return pane

    async def send(self, session_id: str, text: str) -> None:
        tid = session_id
        await self.scan_locks()  # fresh check: never write to a thread someone just opened
        pane = self._foreign(tid)
        if pane:
            if tid in self.tui_prompt:
                raise ControlError("thread is waiting for approval: approve or deny it first")
            # The TUI treats fast input as a paste and swallows an immediate Enter.
            await tmux.send_text(pane[0], pane[1], text, settle=0.6)
            return
        if tid in self.pending:
            raise ControlError("thread is waiting for approval: approve or deny it first")
        srv = await self.ensure_server()
        if tid not in self.loaded:
            await srv.request("thread/resume", {"threadId": tid})
            self.loaded[tid] = {"status": {"type": "idle"}, "turn": None}
        inp = [{"type": "text", "text": text, "text_elements": []}]
        turn = self.loaded[tid].get("turn")
        if turn:
            await srv.request("turn/steer", {"threadId": tid, "input": inp, "expectedTurnId": turn})
        else:
            res = await srv.request("turn/start", {"threadId": tid, "input": inp})
            self.loaded[tid]["turn"] = res["turn"]["id"]
        self.sync(tid)

    async def approve(self, session_id: str, choice: int | None, allow: bool) -> str:
        tid = session_id
        pane = self._foreign(tid)
        if pane:
            return await self.approve_tui(tid, pane, choice, allow)
        req = self.pending.get(tid)
        if not req or not self.server:
            raise ControlError("thread isn't waiting for approval")
        if req["method"] not in APPROVALS:
            if allow:
                raise ControlError(f"agentctl can't answer {req['method']}; only deny it")
            self.server.respond(req["id"], error="denied via agentctl")
            label = "denied"
        else:
            key = str(choice) if choice is not None else ("1" if allow else "3")
            if key not in DECISIONS:
                raise ControlError(f"no option {key}; choices: " + ", ".join(DECISIONS))
            self.server.respond(req["id"], {"decision": DECISIONS[key][0]})
            label = f"{key}. {DECISIONS[key][1]}"
        del self.pending[tid]
        self.sync(tid)
        return label

    async def approve_tui(
        self, tid: str, pane: tuple[str | None, str], choice: int | None, allow: bool
    ) -> str:
        """TUI menu options end in their shortcut key: "Yes, proceed (y)", "No, … (esc)"."""
        opts = tmux.parse_options(await tmux.capture(*pane))
        if not opts:
            raise ControlError("no approval menu visible in the pane; use `agentctl attach`")
        if choice is not None:
            key: str | None = str(choice)
        elif allow:
            key = "1"
        else:
            key = next((k for k, v in opts.items() if v.lower().startswith("no")), None)
        if key not in opts:
            raise ControlError(f"no option {key}; choices: " + ", ".join(opts))
        shortcut = opts[key].rsplit("(", 1)[-1].rstrip(")") if opts[key].endswith(")") else ""
        if shortcut == "esc":
            await tmux.keys(pane[0], pane[1], "Escape")
        elif len(shortcut) == 1:
            await tmux.literal(pane[0], pane[1], shortcut)
        else:
            raise ControlError(f"option {key} has no shortcut key; use `agentctl attach`")
        self.tui_prompt.pop(tid, None)
        s = self.reg.sessions.get(tid)
        if s:
            s.pending_approval = None
        self.sync(tid)
        return f"{key}. {opts[key]}"

    async def interrupt(self, session_id: str) -> None:
        tid = session_id
        pane = self._foreign(tid)
        if pane:
            await tmux.keys(pane[0], pane[1], "Escape")
            return
        turn = (self.loaded.get(tid) or {}).get("turn")
        if not turn or not self.server:
            raise ControlError("no running turn started by agentctl on this thread")
        await self.server.request("turn/interrupt", {"threadId": tid, "turnId": turn})
