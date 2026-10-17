"""Claude Code: tail ~/.claude/projects/*/<id>.jsonl, status from ~/.claude/sessions/<pid>.json.

Formats are documented in docs/formats.md. Read-only: nothing here writes under ~/.claude.
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from agentctl import CLAUDE_DIR, tmux
from agentctl.adapters.base import ControlError
from agentctl.daemon.registry import Registry, Store
from agentctl.models import ApprovalRequest, EventKind, Origin, Session, SessionEvent, Status
from agentctl.tailer import FileTail

log = logging.getLogger(__name__)

TEXT_CAP = 4000
# Without a live PID file, a transcript written to this recently still counts as live.
LIVE_GRACE = timedelta(seconds=120)

# ~/.claude/sessions/<pid>.json "status" enum, from the 2.1.300 binary: busy|shell|idle|waiting.
PID_STATUS: dict[str, Status] = {
    "busy": "working",
    "waiting": "awaiting_approval",
    "idle": "awaiting_input",
    "shell": "idle",  # unverified meaning; seen on a live interactive session
}
LAST_KIND_STATUS: dict[str | None, Status] = {
    "prompt": "working",
    "tool_use": "working",
    "tool_result": "working",
    "end_turn": "awaiting_input",
    "interrupted": "awaiting_input",
    None: "idle",
}
# Hook event -> status. Note: Stop does NOT fire after an Escape interrupt or a denied
# permission (verified), which is why the PID file outranks hooks.
HOOK_STATUS: dict[str, Status] = {
    "SessionStart": "awaiting_input",
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    "PostToolUse": "working",
    "PermissionRequest": "awaiting_approval",
    "Stop": "awaiting_input",
    "SessionEnd": "ended",
}
NOTIFICATION_STATUS: dict[str, Status] = {
    "permission_prompt": "awaiting_approval",
    "idle_prompt": "awaiting_input",
}
# ponytail: sessions without PID files (desktop?) stay live on hooks alone for this long.
HOOK_LIVE = timedelta(hours=24)
TOOL_KEYS = (
    "command",
    "file_path",
    "filePath",
    "path",
    "pattern",
    "url",
    "query",
    "description",
    "prompt",
)


def origin_of(entrypoint: str | None) -> Origin:
    if entrypoint == "cli":
        return "cli"
    if entrypoint == "claude-desktop":
        return "desktop"
    if entrypoint and entrypoint.startswith("sdk"):
        return "sdk"
    return "unknown"


def cap(s: str, n: int = TEXT_CAP) -> str:
    return s if len(s) <= n else s[:n] + f"… [{len(s) - n} more chars]"


def first_line(s: str | None, n: int = 80) -> str | None:
    """A title from free text: its first non-blank line, or None (never raises on "")."""
    lines = (s or "").strip().splitlines()
    return lines[0][:n] if lines else None


def parse_ts(v: Any) -> datetime:
    try:
        return datetime.fromisoformat(v)
    except (TypeError, ValueError):
        return datetime.now(UTC)


def tool_summary(inp: Any) -> str:
    if not isinstance(inp, dict):
        return ""
    for k in TOOL_KEYS:
        if isinstance(inp.get(k), str):
            return cap(inp[k], 300)
    return cap(json.dumps(inp), 300)


_TAG = re.compile(r"<([a-z-]+)>(.*?)</\1>", re.S)
_PASTE = re.compile(r'<pasted_content id="(\w+)">\n(.*?)\n?</pasted_content id="\1">\n?', re.S)
_OUTPUT = ("local-command-stdout", "local-command-stderr", "bash-stdout", "bash-stderr")


def unwrap_pastes(text: str) -> str:
    """Claude Code wraps a large paste (agentctl sends every message as one) in
    <pasted_content id="…"> tags; show what was pasted."""
    return _PASTE.sub(r"\2", text).strip() if "<pasted_content" in text else text


def local_command(text: str) -> tuple[str, str] | None:
    """Claude Code records slash commands and `!` shell commands as tagged user text. Returns
    (kind, text) to show instead: ("message", "/login") for the command, ("tool_result", …) for
    its output ("tool_error" if stderr has text), ("skip", "") for the caveat boilerplate.
    None if `text` isn't one of those."""
    if not text.lstrip().startswith("<"):
        return None
    tags = dict(_TAG.findall(text))
    if "local-command-caveat" in tags:
        return "skip", ""
    if "command-name" in tags:
        return "message", f"{tags['command-name']} {tags.get('command-args', '')}".strip()
    if "bash-input" in tags:
        return "message", "! " + tags["bash-input"].strip()
    if any(k in tags for k in _OUTPUT):
        out = "\n".join(tags[k].strip() for k in _OUTPUT if tags.get(k, "").strip())
        err = any(tags.get(k, "").strip() for k in _OUTPUT if k.endswith("stderr"))
        return ("tool_error" if err else "tool_result", out) if out else ("skip", "")
    return None


def result_text(c: Any) -> str:
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict))
    return ""


def pid_alive(pid: int) -> bool:
    # ponytail: PID reuse not checked; compare procStart against `ps -o lstart` if it bites.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass
class Transcript(FileTail):
    sid: str = ""
    cwd: str = ""
    entrypoint: str | None = None
    custom_title: str | None = None
    ai_title: str | None = None
    first_prompt: str | None = None
    last_kind: str | None = None
    last_tool: str | None = None
    model: str | None = None
    context: int | None = None
    cache_ttl: int | None = None  # seconds, from the latest request that wrote cache
    last_request: datetime | None = None

    @property
    def title(self) -> str | None:
        t = self.custom_title or self.ai_title or self.first_prompt
        return first_line(t)


def parse_entry(d: dict[str, Any], t: Transcript) -> list[SessionEvent]:
    """Map one transcript line to events, updating t's metadata. Unknown types yield nothing."""
    typ = d.get("type")
    if typ == "custom-title":
        t.custom_title = d.get("customTitle")
    elif typ == "ai-title":
        t.ai_title = d.get("aiTitle")
    if typ not in ("user", "assistant", "system") or d.get("isSidechain"):
        return []
    t.cwd = d.get("cwd") or t.cwd
    t.entrypoint = d.get("entrypoint") or t.entrypoint
    ts = parse_ts(d.get("timestamp"))
    agent = d.get("agentId")  # in-process subagent/teammate turns: flagged, and don't drive status
    out: list[SessionEvent] = []

    def ev(kind: EventKind, **payload: Any) -> None:
        if agent:
            payload["agent_id"] = agent
        out.append(SessionEvent(session_id=t.sid, kind=kind, ts=ts, payload=payload))

    def mark(kind: str) -> None:
        if not agent:
            t.last_kind = kind

    if typ == "system":
        if d.get("level") == "error":
            ev("error", text=cap(str(d.get("content", ""))))
        return out
    msg = d.get("message") or {}
    content = msg.get("content")

    def user_text(text: str) -> None:
        kind, shown = local_command(text) or ("message", text)
        if kind == "message":
            ev("message", role="user", text=cap(shown))
        elif kind in ("tool_result", "tool_error"):
            ev("tool_result", tool_use_id=None, text=cap(shown), is_error=kind == "tool_error")

    if typ == "user":
        if d.get("isMeta") or d.get("isCompactSummary"):
            return out
        if isinstance(content, str):
            content = unwrap_pastes(content)
            mark("prompt")
            if t.first_prompt is None and not agent and not content.startswith("<"):
                t.first_prompt = content
            user_text(content)
            return out
        for b in content if isinstance(content, list) else []:
            bt = b.get("type")
            if bt == "tool_result":
                mark("tool_result")
                ev(
                    "tool_result",
                    tool_use_id=b.get("tool_use_id"),
                    text=cap(result_text(b.get("content"))),
                    is_error=bool(b.get("is_error")),
                )
            elif bt == "text":
                text = unwrap_pastes(b.get("text", ""))
                mark("interrupted" if text.startswith("[Request interrupted") else "prompt")
                user_text(text)
            elif bt == "image":
                ev("message", role="user", text="[image]")
        return out
    # assistant: one line per content block, each repeating the request's usage
    usage = msg.get("usage")
    if isinstance(usage, dict) and not agent and msg.get("model") != "<synthetic>":
        t.model, t.last_request = msg.get("model"), ts
        t.context = sum(
            int(usage.get(k) or 0)
            for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        )
        cc = usage.get("cache_creation") or {}
        if cc.get("ephemeral_1h_input_tokens"):
            t.cache_ttl = 3600
        elif cc.get("ephemeral_5m_input_tokens"):
            t.cache_ttl = 300
    if d.get("isApiErrorMessage"):
        ev("error", text=cap(result_text(content)))
        mark("end_turn")
        return out
    for b in content if isinstance(content, list) else []:
        bt = b.get("type")
        if bt == "text" and b.get("text"):
            ev("message", role="assistant", text=cap(b["text"]))
        elif bt == "tool_use":
            summary = tool_summary(b.get("input"))
            if not agent:
                t.last_tool = f"{b.get('name')}: {summary}"
            ev("tool_call", id=b.get("id"), name=b.get("name"), summary=summary)
    stop = msg.get("stop_reason")
    if stop == "tool_use":
        mark("tool_use")
    elif stop in ("end_turn", "stop_sequence"):
        mark("end_turn")
    return out


class _Handler(FileSystemEventHandler):
    def __init__(self, cb: Callable[[Path], object]) -> None:
        self.cb = cb

    def on_any_event(self, event: FileSystemEvent) -> None:
        for p in (event.src_path, getattr(event, "dest_path", "")):
            if p:
                self.cb(Path(os.fsdecode(p)))


class ClaudeCodeAdapter:
    source = "claude_code"

    def __init__(
        self,
        reg: Registry,
        root: Path = CLAUDE_DIR,
        store: Store | None = None,
        debug: bool = False,
    ) -> None:
        self.reg = reg
        self.projects = root / "projects"
        self.pids_dir = root / "sessions"
        self.store = store
        self.debug = debug
        self.by_sid: dict[str, Transcript] = {}
        self.live: dict[str, dict[str, Any]] = {}  # session id -> PID file contents
        self.in_tmux: dict[str, dict[str, Any]] = {}  # session id -> PID file of a tmux process
        self.big_models: set[str] = set()  # models seen using more than 200k of context
        # From the status line wrapper (exact where the transcript only allows a guess):
        self.exact: dict[str, dict[str, Any]] = {}  # session -> {"window", "cache_expires"}
        # newest plan limits any session reported; kept across restarts (status lines only
        # redraw when a session is active)
        self.limits: dict[str, Any] | None = store.get_json("claude_limits") if store else None
        self.state: dict[str, dict[str, Any]] = store.load() if store else {}  # pane + last hook

    async def discover(self) -> None:
        self.scan_pids()
        for p in self.projects.glob("*/*.jsonl"):
            self.open_transcript(p, startup=True)

    async def watch(self) -> None:
        loop = asyncio.get_running_loop()
        handler = _Handler(lambda p: loop.call_soon_threadsafe(self.on_path, p))
        obs = Observer()
        obs.schedule(handler, str(self.projects), recursive=True)
        if self.pids_dir.is_dir():
            obs.schedule(handler, str(self.pids_dir))
        obs.start()
        try:
            while True:
                await asyncio.sleep(1)
                self.tick()
        finally:
            obs.stop()

    def on_path(self, p: Path) -> None:
        if p.parent == self.pids_dir and p.suffix == ".json":
            self.scan_pids()
        elif p.suffix == ".jsonl" and p.parent.parent == self.projects:
            t = self.by_sid.get(p.stem)
            if t is None:
                self.open_transcript(p, startup=False)
            else:
                self.ingest(t, publish=True)
                self.sync(t.sid)

    def tick(self) -> None:
        self.scan_pids()  # a few tiny files; catches anything FSEvents missed (e.g. at startup)
        for s in list(self.reg.sessions.values()):
            # "new-…": the daemon's placeholder for a launch Claude hasn't announced yet
            if s.source == self.source and s.status != "ended" and not s.id.startswith("new-"):
                self.sync(s.id)

    # --- transcripts ---

    def open_transcript(self, p: Path, startup: bool) -> None:
        try:
            st = p.stat()
        except FileNotFoundError:
            return
        t = Transcript(path=p, sid=p.stem, ino=st.st_ino)
        for raw in t.skip_to_tail() if startup else []:
            self.parse_line(t, raw)  # metadata only; events dropped
        self.by_sid[t.sid] = t
        self.ensure_session(t.sid)
        self.ingest(t, publish=not startup)
        self.sync(t.sid)

    def parse_line(self, t: Transcript, raw: bytes) -> list[SessionEvent]:
        if not raw.strip():
            return []
        try:
            d = json.loads(raw)
            return parse_entry(d, t) if isinstance(d, dict) else []
        except Exception as e:  # never let one bad line kill the watcher
            log.warning("skipping bad line in %s: %s", t.path.name, type(e).__name__)
            if self.debug:
                log.debug("bad line: %r", raw[:500])
            return []

    def ingest(self, t: Transcript, publish: bool) -> None:
        for raw in t.read_lines():
            for ev in self.parse_line(t, raw):
                self.reg.add_event(ev, publish=publish)

    # --- hooks and panes ---

    def ensure_session(self, sid: str, cwd: str = "", **kw: Any) -> Session:
        return self.reg.upsert(
            Session(
                id=sid,
                source="claude_code",
                cwd=cwd,
                last_activity=kw.pop("last_activity", datetime.fromtimestamp(0, UTC)),
                **kw,
            )
        )

    def save(self, sid: str) -> None:
        if self.store:
            self.store.put(sid, self.state[sid])

    def on_hook(
        self, event: str, payload: dict[str, Any], pane: str | None, env: str | None
    ) -> None:
        sid = payload.get("session_id")
        if not isinstance(sid, str):
            return
        row = self.state.setdefault(sid, {})
        if pane:
            row["pane"], row["socket"] = pane, env.split(",")[0] if env else None
        status = HOOK_STATUS.get(event)
        if event == "Notification":
            status = NOTIFICATION_STATUS.get(payload.get("notification_type", ""))
        row["hook_event"], row["hook_ts"] = event, time.time()
        if status:
            row["hook_status"] = status
        if event == "PermissionRequest":
            row["detail"] = f"{payload.get('tool_name')}: {tool_summary(payload.get('tool_input'))}"
        self.save(sid)
        self.ensure_session(sid, payload.get("cwd", "")).last_activity = datetime.now(UTC)
        tp = payload.get("transcript_path")
        if sid not in self.by_sid and isinstance(tp, str) and Path(tp).is_file():
            self.open_transcript(Path(tp), startup=False)
        self.sync(sid)

    def register_pane(self, sid: str, socket: str | None, pane: str) -> None:
        self.state.setdefault(sid, {}).update(socket=socket, pane=pane)
        self.save(sid)
        if sid in self.reg.sessions:
            self.sync(sid)

    def target(self, sid: str) -> tuple[str | None, str | None]:
        """(tmux socket, pane) of the session's live process that runs in tmux, if any. Only a
        live process's own PID file decides the pane; hooks/launcher just supply the socket."""
        info, row = self.in_tmux.get(sid), self.state.get(sid) or {}
        if not info:
            return None, None
        pane = str(info["tmux"]).rsplit(".", 1)[-1]
        return (row.get("socket") if row.get("pane") == pane else None), pane

    # --- session state ---

    def scan_pids(self) -> None:
        # Two processes can hold one session (e.g. `adopt --force`): status follows the most
        # recently updated one, control goes to the one running in tmux.
        live: dict[str, dict[str, Any]] = {}
        in_tmux: dict[str, dict[str, Any]] = {}
        for f in self.pids_dir.glob("*.json"):
            try:
                d = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            sid = d.get("sessionId")
            if not (isinstance(d.get("pid"), int) and sid and pid_alive(d["pid"])):
                continue
            if sid not in live or d.get("updatedAt", 0) > live[sid].get("updatedAt", 0):
                live[sid] = d
            if d.get("tmux"):
                in_tmux[sid] = d
        changed = set(live) | set(self.live)
        self.live, self.in_tmux = live, in_tmux
        for sid in changed:
            if sid not in self.reg.sessions:
                info = live[sid]
                self.ensure_session(
                    sid,
                    info.get("cwd", ""),
                    origin=origin_of(info.get("entrypoint")),
                    title=info.get("name"),
                    last_activity=datetime.fromtimestamp(info.get("startedAt", 0) / 1000, UTC),
                )
            self.sync(sid)

    def on_statusline(self, p: dict[str, Any]) -> None:
        """What Claude Code sends its status line: exact context window, prompt-cache expiry,
        and the plan's 5-hour / 7-day limits (subscribers only). Formats: docs/formats.md."""
        sid = p.get("session_id")
        cw, pc = p.get("context_window") or {}, p.get("prompt_cache") or {}
        if sid:
            self.exact[sid] = {
                "window": cw.get("context_window_size"),
                "cache_expires": pc.get("expires_at"),
            }
            if sid in self.reg.sessions:
                self.sync(sid)
        rl = p.get("rate_limits") or {}
        windows = [
            {"name": name, "used_percent": float(w.get("used_percentage") or 0),
             "resets_at": w.get("resets_at") or 0}
            for key, name in (("five_hour", "5h"), ("seven_day", "week"))
            if isinstance(w := rl.get(key), dict)
        ]  # fmt: skip
        if windows:
            self.limits = {"plan": None, "at": datetime.now(UTC).isoformat(), "windows": windows}
            if self.store:
                self.store.set_json("claude_limits", self.limits)

    def usage(self) -> dict[str, Any] | None:
        """Plan limits; a window whose reset time has passed counts as 0% used."""
        if not self.limits:
            return None
        now = time.time()
        windows = [
            w | {"used_percent": 0.0} if w["resets_at"] and w["resets_at"] < now else w
            for w in self.limits["windows"]
        ]
        return self.limits | {"windows": windows}

    def holder(self, sid: str) -> int | None:
        """pid of the process that has this session open, if any."""
        info = self.live.get(sid)
        return int(info["pid"]) if info else None

    def sync(self, sid: str) -> None:
        s, t = self.reg.sessions.get(sid), self.by_sid.get(sid)
        if s is None:
            return
        if t:
            s.cwd = t.cwd or s.cwd
            s.title = t.title or s.title
            if t.entrypoint:
                s.origin = origin_of(t.entrypoint)
            mt = datetime.fromtimestamp(t.mtime, UTC)
            if mt > s.last_activity:
                s.last_activity = mt
            if t.context is not None:
                s.context_tokens = t.context
                # ponytail: Claude Code derives the window from model metadata it doesn't
                # persist. 1M if the id says so, or if any session of this model has gone past
                # 200k (proof it has the 1M window); else assume 200k.
                if t.context > 200_000 and t.model and t.model not in self.big_models:
                    self.big_models.add(t.model)
                    for other in self.by_sid.values():
                        if other.model == t.model and other.sid != sid:
                            self.sync(other.sid)
                big = "[1m]" in (t.model or "") or t.model in self.big_models
                s.context_window = 1_000_000 if big else 200_000
            if t.last_request and t.cache_ttl:
                s.cache_expires_at = t.last_request + timedelta(seconds=t.cache_ttl)
        ex = self.exact.get(sid) or {}
        if ex.get("window"):
            s.context_window, s.context_window_exact = ex["window"], True
        if ex.get("cache_expires"):
            s.cache_expires_at = datetime.fromtimestamp(ex["cache_expires"], UTC)
        _, s.tmux_pane = self.target(sid)
        s.controllable = s.tmux_pane is not None
        self.refresh_status(sid)

    def refresh_status(self, sid: str) -> None:
        s, t = self.reg.sessions.get(sid), self.by_sid.get(sid)
        if s is None:
            return
        info, row = self.live.get(sid), self.state.get(sid) or {}
        now, mtime = time.time(), t.mtime if t else 0.0
        fallback = LAST_KIND_STATUS.get(t.last_kind if t else None, "idle")
        hook_st, hook_ts = row.get("hook_status"), row.get("hook_ts") or 0.0
        if info:  # authoritative: rewritten by the live process on every change
            raw, why = info.get("status", ""), info.get("waitingFor")
            status = "awaiting_input" if why == "dialog open" else PID_STATUS.get(raw, fallback)
        elif hook_st == "ended" and hook_ts >= mtime - 2:  # SessionEnd, nothing written since
            status = "ended"
        elif hook_st and s.origin != "cli" and now - hook_ts < HOOK_LIVE.total_seconds():
            # no PID file (desktop/sdk): trust hooks, unless the transcript is newer (interrupts)
            status = hook_st if hook_st == "ended" or hook_ts >= mtime else fallback
        elif t and now - mtime < LIVE_GRACE.total_seconds():
            status = fallback
        else:
            status = "ended"
        if status == "awaiting_approval":
            hooked = row.get("hook_status") == "awaiting_approval" and hook_ts >= mtime - 5
            tool = row.get("detail") if hooked else (t.last_tool if t else None)
            prev = s.pending_approval
            s.pending_approval = ApprovalRequest(
                tool=tool or (prev.tool if prev else None),
                detail=(info or {}).get("waitingFor"),
                options=prev.options if prev else {},
            )
        else:
            s.pending_approval = None
        if self.reg.set_status(sid, status) and status == "awaiting_approval":
            with contextlib.suppress(RuntimeError):  # no loop in unit tests
                asyncio.get_running_loop().create_task(self.announce_approval(sid))

    async def announce_approval(self, sid: str) -> None:
        s = self.reg.sessions[sid]
        if s.controllable:
            await asyncio.sleep(0.3)  # let the prompt render
            with contextlib.suppress(ControlError):
                sock, pane = await self.verified(sid)
                opts = tmux.parse_options(await tmux.capture(sock, pane))
                if s.pending_approval:
                    s.pending_approval.options = opts
        if s.pending_approval:
            self.reg.add_event(
                SessionEvent(
                    session_id=sid,
                    kind="approval_request",
                    ts=datetime.now(UTC),
                    payload=s.pending_approval.model_dump(),
                )
            )

    # --- control ---

    async def verified(self, sid: str) -> tuple[str | None, str]:
        """The pane, after checking the session's live claude process really runs in it."""
        if sid not in self.live:
            raise ControlError("session has no live claude process (read-only)")
        sock, pane = self.target(sid)
        info = self.in_tmux.get(sid)
        if not pane or not info:
            raise ControlError("session isn't running in tmux; `agentctl adopt` it to control it")
        root = await tmux.pane_pid(sock, pane)
        if root is None or not await tmux.is_descendant(info["pid"], root):
            raise ControlError(f"tmux pane {pane} no longer hosts this session")
        return sock, pane

    async def send(self, session_id: str, text: str) -> None:
        if self.reg.sessions[session_id].status == "awaiting_approval":
            raise ControlError("session is waiting for approval: approve or deny it first")
        sock, pane = await self.verified(session_id)
        await tmux.send_text(sock, pane, text)

    async def approve(self, session_id: str, choice: int | None, allow: bool) -> str:
        if self.reg.sessions[session_id].status != "awaiting_approval":
            raise ControlError("session isn't waiting for approval")
        sock, pane = await self.verified(session_id)
        opts = tmux.parse_options(await tmux.capture(sock, pane))
        if not opts:
            raise ControlError("no numbered prompt visible in the pane; use `agentctl attach`")
        if choice is not None:
            key: str | None = str(choice)
        elif allow:
            key = "1"
        else:
            key = next((k for k, v in opts.items() if v.lower().startswith("no")), None)
            if key is None:
                await tmux.keys(sock, pane, "Escape")
                return "Esc (cancel)"
        if key not in opts:
            raise ControlError(f"no option {key}; choices: " + ", ".join(opts))
        await tmux.literal(sock, pane, key)  # a digit selects immediately; no Enter
        return f"{key}. {opts[key]}"

    async def interrupt(self, session_id: str) -> None:
        sock, pane = await self.verified(session_id)
        await tmux.keys(sock, pane, "Escape")
