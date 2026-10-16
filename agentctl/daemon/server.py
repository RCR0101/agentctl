"""agentctld: owns all session state, serves newline-delimited JSON-RPC on a Unix socket."""

import asyncio
import contextlib
import ctypes
import json
import logging
import os
import signal
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler
from typing import Any

from agentctl import LOG_DIR, SOCKET_PATH, STATE_DIR, launcher, tmux
from agentctl.adapters.base import Adapter, ControlError
from agentctl.adapters.claude_code import ClaudeCodeAdapter
from agentctl.adapters.codex import CodexAdapter
from agentctl.adapters.opencode import OpenCodeAdapter
from agentctl.daemon import fleet
from agentctl.daemon.bus import Bus
from agentctl.daemon.registry import Registry, Store
from agentctl.models import Session, SessionEvent, short_id

log = logging.getLogger("agentctld")
STARTED = time.time()
_libc = ctypes.CDLL(None, use_errno=True)


def peer_uid(fd: int) -> int:
    uid, gid = ctypes.c_uint32(), ctypes.c_uint32()
    if _libc.getpeereid(fd, ctypes.byref(uid), ctypes.byref(gid)) != 0:
        return -1
    return uid.value


class RpcError(Exception):
    pass


# Keys the screen view may press in a session's terminal (tmux key names).
KEYS = {"Up", "Down", "Left", "Right", "Enter", "Escape", "Tab", "BTab", "Space", "y", "n"}
KEYS |= {str(d) for d in range(10)}


class Server:
    def __init__(
        self,
        registry: Registry,
        claude: ClaudeCodeAdapter,
        codex: CodexAdapter,
        opencode: OpenCodeAdapter,
        store: Store | None = None,
    ) -> None:
        self.store = store
        self.titles = store.titles() if store else {}
        seen = store.seen() if store else {}
        if "*" not in seen:  # first run with unread tracking: older replies count as seen
            seen["*"] = time.time()
            if store:
                store.set_seen("*", seen["*"])
        self.seen = {k: datetime.fromtimestamp(v, UTC) for k, v in seen.items()}
        self.queued: dict[str, list[str]] = store.queued() if store else {}
        self.armed: dict[str, bool] = {}  # may the next follow-up go out yet?
        self.queue_error: dict[str, str] = {}
        self.reg = registry
        self.claude = claude
        self.codex = codex
        self.opencode = opencode
        # Codex/opencode launched by `new` have no session id until their first message: until
        # then they're listed as a placeholder ("new-a1b2") that send/keys/screen can target.
        self.pending: dict[str, tuple[str, str]] = {}  # placeholder id -> (socket, pane)
        self.aliases: dict[str, str] = {}  # replaced placeholder id -> real session id
        self.adapters: dict[str, Adapter] = {
            "claude_code": claude,
            "codex": codex,
            "opencode": opencode,
        }
        self.stopping = asyncio.Event()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        sock: socket.socket = writer.get_extra_info("socket")
        if peer_uid(sock.fileno()) != os.getuid():
            log.warning("rejecting connection from foreign uid")
            writer.close()
            return
        try:
            async for line in reader:
                req: dict[str, Any] = {}
                try:
                    req = json.loads(line)
                    method, params = req["method"], req.get("params") or {}
                    if method == "follow":
                        await self.follow(reader, writer, req.get("id"), params)
                        return
                    result = await self.dispatch(method, params)
                    reply: dict[str, Any] = {"id": req.get("id"), "result": result}
                except ConnectionError:
                    raise  # the client hung up (e.g. Ctrl-C on `tail -f`): not an error
                except (RpcError, ControlError, launcher.LaunchError, KeyError, ValueError) as e:
                    reply = {"id": req.get("id"), "error": {"message": str(e).strip("'\"")}}
                except Exception as e:  # anything else: answer, don't drop the connection
                    log.exception("%s failed", req.get("method"))
                    reply = {"id": req.get("id"), "error": {"message": f"{type(e).__name__}: {e}"}}
                writer.write(json.dumps(reply).encode() + b"\n")
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def dispatch(self, method: str, p: dict[str, Any]) -> Any:
        if p.get("id") in self.aliases:  # a placeholder id keeps working after it's replaced
            p = p | {"id": self.aliases[p["id"]]}
        if method == "hook":
            ev, payload = p["event"], p.get("payload") or {}
            if p.get("source") == "opencode":
                self.opencode.on_hook(ev, payload, p.get("pid"), p.get("pane"), p.get("tmux"))
            else:
                self.claude.on_hook(ev, payload, p.get("pane"), p.get("tmux"))
            return None
        if method == "launch":
            args = (p["agent"], p["cwd"], p.get("name"), p.get("extra") or [])
            path = p.get("path") or os.environ.get("PATH", "")
            r = await asyncio.to_thread(launcher.launch, *args, path, p.get("resume"))
            if p["agent"] == "codex":
                self.codex.register_pane(r["socket"], r["pane"])
            elif p["agent"] == "opencode":
                pid = await tmux.pane_pid(r["socket"], r["pane"])
                if r["session_id"] and pid:  # a new one is mapped by the plugin's first event
                    self.opencode.register_pane(r["session_id"], r["socket"], r["pane"], pid)
            else:
                self.claude.register_pane(r["session_id"], r["socket"], r["pane"])
            if not r["session_id"]:
                r["session_id"] = self.add_placeholder(p["agent"], p["cwd"], r["socket"], r["pane"])
            return r
        if method == "register_pane":
            if p.get("source") == "codex":  # thread id unknown until the TUI opens one
                self.codex.register_pane(p.get("socket"), p["pane"])
            else:
                self.claude.register_pane(p["session_id"], p.get("socket"), p["pane"])
            return {"ok": True}
        if method == "send" and p["id"].startswith("%"):
            # a pane `new` opened, before its first message creates a session (codex, opencode)
            await tmux.send_text(launcher.socket_path(), p["id"], p["text"], settle=0.6)
            return {"session_id": p["id"]}
        if method in ("send", "keys", "screen", "pane", "close", "interrupt") and (
            not p["id"].startswith("%") and self.reg.resolve(p["id"]).id in self.pending
        ):
            return await self.placeholder(method, self.reg.resolve(p["id"]).id, p)
        if method == "keys":
            bad = [k for k in p["keys"] if k not in KEYS]
            if bad:
                raise RpcError(f"key not allowed: {', '.join(bad)}")
            s = self.reg.resolve(p["id"])
            sock, pane = await self.adapters[s.source].verified(s.id)
            await tmux.keys(sock, pane, *p["keys"])
            return {"session_id": s.id}
        if method == "screen":
            s = self.reg.resolve(p["id"])
            sock, pane = await self.adapters[s.source].verified(s.id)
            return {"session_id": s.id, "screen": await screen(sock, pane)}
        if method == "close":
            s = self.reg.resolve(p["id"])
            if s.status in ("working", "awaiting_approval") and not p.get("force"):
                raise RpcError(f"{short_id(s.id)} is mid-turn; closing would abort it")
            ad = self.adapters[s.source]
            sock, pane = await ad.verified(s.id)
            await close_pane(sock, pane, ad.holder(s.id))
            return {"session_id": s.id}
        if method == "seen":
            s = self.reg.resolve(p["id"])
            self.seen[s.id] = datetime.now(UTC)
            if self.store:
                self.store.set_seen(s.id, time.time())
            return {"session_id": s.id}
        if method == "digest":
            live = [x for x in self.reg.listing() if x.id not in self.pending]
            events = {sid: list(evs) for sid, evs in self.reg.events.items()}
            return fleet.digest(live, events, self.seen, self.seen["*"])
        if method in ("queue", "unqueue"):
            s = self.reg.resolve(p["id"])
            items = self.queued.get(s.id, [])
            items = [*items, p["text"]] if method == "queue" else []
            self.set_queued(s.id, items)
            return {"session_id": s.id, "queued": items}
        if method == "statusline":
            self.claude.on_statusline(p)
            return None
        if method == "usage":
            return {"codex": self.codex.limits(), "claude": self.claude.usage()}
        if method == "rename":
            s = self.reg.resolve(p["id"])
            title = (p.get("title") or "").strip()[:80] or None
            if self.store:
                self.store.set_title(s.id, title)
            if title:
                self.titles[s.id] = title
            else:
                self.titles.pop(s.id, None)
            return {"session_id": s.id, "title": title}
        if method == "holder":
            s = self.reg.resolve(p["id"])
            if s.source == "claude_code":  # fresh: `adopt` polls this after stopping one
                self.claude.scan_pids()
            elif s.source == "codex":
                await self.codex.scan_locks()
            return {"session_id": s.id, "pid": self.adapters[s.source].holder(s.id)}
        if method in ("send", "approve", "interrupt", "pane"):
            s = self.reg.resolve(p["id"])
            ad = self.adapters[s.source]
            if method == "send":
                await ad.send(s.id, p["text"])
                return {"session_id": s.id}
            if method == "approve":
                chosen = await ad.approve(s.id, p.get("choice"), bool(p.get("allow", True)))
                return {"session_id": s.id, "chosen": chosen}
            if method == "interrupt":
                await ad.interrupt(s.id)
                return {"session_id": s.id}
            sock, pane = await ad.verified(s.id)
            return {"session_id": s.id, "socket": sock, "pane": pane}
        if method == "ping":
            return {"pid": os.getpid(), "started": STARTED}
        if method == "shutdown":
            # Codex turns started via `agentctl send` run in our app-server: stopping kills them.
            busy = [t[:8] for t, v in self.codex.loaded.items() if v.get("turn")]
            if busy and not p.get("force"):
                raise RpcError(
                    f"Codex turn running in {', '.join(busy)}; stopping would abort it. "
                    "Wait, or use --force."
                )
            self.stopping.set()
            return {"ok": True}
        if method == "ls":
            cutoff = datetime.now(UTC) - timedelta(days=1)
            out = []
            for s in self.reg.listing():
                if p.get("source") and s.source != p["source"]:
                    continue
                if p.get("status") and s.status != p["status"]:
                    continue
                # Hide long-dead sessions unless asked: there are hundreds of old transcripts.
                if not p.get("all") and s.status == "ended" and s.last_activity < cutoff:
                    continue
                out.append(s)
            await asyncio.to_thread(lambda: [fleet.project_root(s.cwd) for s in out])
            cf = fleet.conflicts(out, self.reg.events)
            return [self.dump(s, cf) for s in out]
        if method == "tail":
            s = self.reg.resolve(p["id"])
            n = int(p.get("n", 50))
            evs = list(self.reg.events.get(s.id, ()))[-n:] if n > 0 else []
            cf = fleet.conflicts(list(self.reg.sessions.values()), self.reg.events)
            return {
                "session": self.dump(s, cf),
                "events": [e.model_dump(mode="json") for e in evs],
            }
        raise RpcError(f"unknown method {method!r}")

    def dump(self, s: Session, conflicts: dict[str, list[str]] | None = None) -> dict[str, Any]:
        d = s.model_dump(mode="json")
        if s.id in self.titles:
            d["title"] = self.titles[s.id]
        seen = self.seen.get(s.id, self.seen["*"])
        d["seen_at"] = seen.isoformat()
        d["unread"] = fleet.unread(self.reg.events.get(s.id, ()), seen)
        d["project"] = fleet.project_name(s.cwd)
        d["conflicts"] = (conflicts or {}).get(s.id, [])
        d["queued"] = self.queued.get(s.id, [])
        d["queue_error"] = self.queue_error.get(s.id)
        return d

    def set_queued(self, sid: str, items: list[str]) -> None:
        if items:
            self.queued[sid] = items
        else:
            self.queued.pop(sid, None)
            self.queue_error.pop(sid, None)
        if self.store:
            self.store.set_queued(sid, items)

    async def deliver_queued(self) -> None:
        """Send each queued follow-up once its agent is free: waiting for input, or idle.
        After a delivery, the next waits until the agent has worked on it, so two follow-ups
        never land in the same idle moment."""
        with self.reg.bus.subscribe() as q:
            while True:
                await asyncio.sleep(1)
                await self.deliver_tick(q)

    async def deliver_tick(self, q: asyncio.Queue[SessionEvent]) -> None:
        """One pass: arm sessions that started working since their last delivery, then send
        the next follow-up to each free, armed session. Status changes queue up on the bus, so
        even a turn shorter than a tick is seen. (Arming on any event would race: the echo of
        the message just sent can arrive before the agent starts on it.)"""
        while not q.empty():
            e = q.get_nowait()
            if e.kind == "status_change" and e.payload.get("to") in (
                "working",
                "awaiting_approval",
            ):
                self.armed[e.session_id] = True
        for sid in list(self.queued):
            if sid in self.aliases:  # queued on a placeholder that is now real
                self.set_queued(self.aliases[sid], self.queued[sid])
                self.set_queued(sid, [])
                continue
            s = self.reg.sessions.get(sid)
            if not s or s.status not in ("awaiting_input", "idle"):
                continue
            if not self.armed.get(sid, True):
                continue
            text = self.queued[sid][0]
            try:
                await self.dispatch("send", {"id": sid, "text": text})
            except (ControlError, RpcError, KeyError) as e:
                self.queue_error[sid] = str(e).strip("'\"")
                continue
            log.info("sent a queued follow-up to %s", short_id(sid))
            self.armed[sid] = False
            self.queue_error.pop(sid, None)
            self.set_queued(sid, self.queued[sid][1:])

    def add_placeholder(self, agent: str, cwd: str, sock: str, pane: str) -> str:
        pid = f"new-{uuid.uuid4().hex[:4]}"
        self.pending[pid] = (sock, pane)
        self.reg.upsert(
            Session(
                id=pid,
                source="codex" if agent == "codex" else "opencode",
                origin="cli",
                cwd=cwd,
                title=f"new {agent} session · send it a first message",
                status="awaiting_input",
                controllable=True,
                tmux_pane=pane,
                last_activity=datetime.now(UTC),
            )
        )
        return pid

    async def placeholder(self, method: str, pid: str, p: dict[str, Any]) -> Any:
        sock, pane = self.pending[pid]
        if method == "send":
            await tmux.send_text(sock, pane, p["text"], settle=0.6)
        elif method == "keys":
            if any(k not in KEYS for k in p["keys"]):
                raise RpcError("key not allowed")
            await tmux.keys(sock, pane, *p["keys"])
        elif method == "interrupt":
            await tmux.keys(sock, pane, "Escape")
        elif method == "screen":
            return {"session_id": pid, "screen": await screen(sock, pane)}
        elif method == "close":
            await close_pane(sock, pane, None)
            self.pending.pop(pid, None)
            self.reg.remove(pid)
        return {"session_id": pid, "socket": sock, "pane": pane}

    async def sweep_placeholders(self) -> None:
        """Drop a placeholder once its real session shows up in the same pane, or its pane
        is gone."""
        while True:
            await asyncio.sleep(1)
            await self.sweep()

    async def sweep(self) -> None:
        for pid, (sock, pane) in list(self.pending.items()):
            real = next(
                (
                    s.id
                    for s in self.reg.sessions.values()
                    if s.tmux_pane == pane and s.id not in self.pending
                ),
                None,
            )
            if real:
                self.aliases[pid] = real
            if real or await tmux.pane_pid(sock, pane) is None:
                self.pending.pop(pid, None)
                self.reg.remove(pid)

    async def follow(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        rid: Any,
        p: dict[str, Any],
    ) -> None:
        # Subscribe and snapshot the backlog in one step so nothing is lost or repeated.
        backlog = await self.dispatch("tail", p)
        sid = backlog["session"]["id"]
        with self.reg.bus.subscribe() as q:
            writer.write(json.dumps({"id": rid, "result": backlog}).encode() + b"\n")
            await writer.drain()
            eof = asyncio.ensure_future(reader.read())  # client hangs up -> b""
            try:
                while True:
                    get = asyncio.ensure_future(q.get())
                    done, _ = await asyncio.wait({get, eof}, return_when=asyncio.FIRST_COMPLETED)
                    if eof in done:
                        get.cancel()
                        return
                    ev = get.result()
                    if ev.session_id == sid:
                        msg = {"method": "event", "params": ev.model_dump(mode="json")}
                        writer.write(json.dumps(msg).encode() + b"\n")
                        await writer.drain()
            finally:
                eof.cancel()


async def supervise(name: str, watch: Callable[[], Awaitable[None]]) -> None:
    """Keep an adapter's watch loop alive: a bug in one tick must not silently stop all
    updates for that agent (it did once: an empty Codex prompt raised IndexError)."""
    while True:
        try:
            await watch()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s watcher crashed; restarting it", name)
            await asyncio.sleep(1)


async def screen(sock: str | None, pane: str) -> str:
    """The pane as the user would see it, with colours (ANSI), trailing blank lines dropped."""
    out = await tmux.run(sock, "capture-pane", "-p", "-e", "-t", pane)
    return out.rstrip("\n ")


async def close_pane(sock: str | None, pane: str, pid: int | None) -> None:
    """Quit the agent like Ctrl-C would (SIGINT; Claude exits cleanly on it), then remove the
    pane if it's one of agentctl's. A pane in your own tmux is left, back at its shell."""
    pid = pid or await tmux.pane_pid(sock, pane)
    if pid:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGINT)
        for _ in range(50):
            await asyncio.sleep(0.1)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
    if sock == launcher.socket_path():
        with contextlib.suppress(ControlError):
            await tmux.run(sock, "kill-pane", "-t", pane)


def setup_logging(debug: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    h = RotatingFileHandler(LOG_DIR / "agentctld.log", maxBytes=1_000_000, backupCount=3)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(h)
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    logging.getLogger("watchdog").setLevel(logging.WARNING)


def socket_in_use() -> bool:
    with socket.socket(socket.AF_UNIX) as s:
        try:
            s.connect(str(SOCKET_PATH))
            return True
        except OSError:
            return False


async def main(debug: bool = False) -> None:
    os.umask(0o077)
    STATE_DIR.mkdir(mode=0o700, exist_ok=True)
    STATE_DIR.chmod(0o700)
    setup_logging(debug)
    if socket_in_use():
        raise SystemExit("agentctld already running")
    SOCKET_PATH.unlink(missing_ok=True)

    reg = Registry(Bus())
    store = Store(str(STATE_DIR / "state.db"))
    claude = ClaudeCodeAdapter(reg, store=store, debug=debug)
    codex = CodexAdapter(reg, debug=debug)
    opencode = OpenCodeAdapter(reg, store=store)
    for ad in (claude, codex, opencode):
        await ad.discover()
    srv = Server(reg, claude, codex, opencode, store)
    server = await asyncio.start_unix_server(srv.handle, path=str(SOCKET_PATH), limit=16 << 20)
    SOCKET_PATH.chmod(0o600)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, srv.stopping.set)
    log.info(
        "agentctld %d listening on %s, %d sessions", os.getpid(), SOCKET_PATH, len(reg.sessions)
    )
    watchers = [
        asyncio.create_task(supervise(ad.source, ad.watch)) for ad in (claude, codex, opencode)
    ]
    watchers.append(asyncio.create_task(supervise("placeholders", srv.sweep_placeholders)))
    watchers.append(asyncio.create_task(supervise("queue", srv.deliver_queued)))
    await srv.stopping.wait()
    for w in watchers:
        w.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await w
    server.close()
    SOCKET_PATH.unlink(missing_ok=True)
    log.info("agentctld stopped")
