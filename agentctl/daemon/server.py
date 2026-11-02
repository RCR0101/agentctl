"""agentctld: owns all session state, serves newline-delimited JSON-RPC on a Unix socket."""

import asyncio
import contextlib
import ctypes
import json
import logging
import os
import re
import signal
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from agentctl import LOG_DIR, SOCKET_PATH, STATE_DIR, launcher, tmux
from agentctl.adapters.base import Adapter, ControlError
from agentctl.adapters.claude_code import ClaudeCodeAdapter
from agentctl.adapters.codex import CodexAdapter
from agentctl.adapters.opencode import OpenCodeAdapter
from agentctl.daemon import fleet, search
from agentctl.daemon.agent_chats import AgentChats, identify_agent
from agentctl.daemon.bus import Bus
from agentctl.daemon.history import CommandHistory
from agentctl.daemon.registry import AmbiguousRef, Registry, Store
from agentctl.daemon.rules import Rule, Rules, decide
from agentctl.daemon.teams import Teams
from agentctl.models import Session, SessionEvent, short_id

log = logging.getLogger("agentctld")
STARTED = time.time()
_libc = ctypes.CDLL(None, use_errno=True)


def peer_uid(fd: int) -> int:
    uid, gid = ctypes.c_uint32(), ctypes.c_uint32()
    if _libc.getpeereid(fd, ctypes.byref(uid), ctypes.byref(gid)) != 0:
        return -1
    return uid.value


def peer_pid(fd: int) -> int:
    # macOS sys/un.h: SOL_LOCAL=0, LOCAL_PEERPID=0x002.
    pid, length = ctypes.c_int32(), ctypes.c_uint32(ctypes.sizeof(ctypes.c_int32))
    if _libc.getsockopt(fd, 0, 0x002, ctypes.byref(pid), ctypes.byref(length)) != 0:
        raise ValueError("Cannot verify the caller of this chat write; no message was sent")
    return pid.value


class RpcError(Exception):
    pass


# Who may call what. The daemon checks the caller's process tree for the methods in
# CALLER_CHECKED; a call from inside an agent is marked `_actor="agent"` and may only use
# AGENT_METHODS (its send/queue become fresh agent chats). Everything a human must decide
# (approving, denying, answering, interrupting, closing, cancelling queued work) is checked
# and kept out of AGENT_METHODS. Team methods (`team_*`, `teams_list`) are always checked and
# authorised by their own scoped credentials.
CALLER_CHECKED = frozenset({
    "send", "queue", "unqueue", "launch", "keys", "approve", "approve_all", "answer",
    "interrupt", "close", "pane", "rules",
})  # fmt: skip
AGENT_METHODS = frozenset({
    "ls", "tail", "screen", "history", "digest", "search", "edited", "file_activity", "usage",
    "ping", "send", "queue", "launch",
})  # fmt: skip
# A human using these on a session takes it over from its team (future team sends wait).
TAKEOVER_METHODS = frozenset({"send", "queue", "keys", "pane", "interrupt", "close"})


def caller_checked(method: str) -> bool:
    return method in CALLER_CHECKED or method.startswith("team_") or method == "teams_list"


# Text on screen meaning a menu is waiting for an answer (permission, trust, confirm). Typed
# text there could be taken as the answer ("1" + Enter = Yes), so sends are refused.
MENU_MARKERS = (
    "Do you want to proceed?",  # Claude Code permission prompt
    "Do you trust",  # Claude Code folder trust
    "Press enter to confirm",  # Codex
    "Permission required",  # opencode
)

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
        self.history = CommandHistory(store)
        self.agent_chats = AgentChats(store)
        self.teams = Teams(registry, self.history, store)
        self.titles = store.titles() if store else {}
        registry.names = self.titles  # the same dict: renames are seen by resolve()
        seen = store.seen() if store else {}
        if "*" not in seen:  # first run with unread tracking: older replies count as seen
            seen["*"] = time.time()
            if store:
                store.set_seen("*", seen["*"])
        self.seen = {k: datetime.fromtimestamp(v, UTC) for k, v in seen.items()}
        self.queued: dict[str, list[str]] = store.queued() if store else {}
        self.legacy_queue = {
            sid
            for sid, texts in self.queued.items()
            if any(
                not any(
                    e["session_id"] == sid
                    and e["text"] == text
                    and e.get("sender") in ("human", "agent")
                    for e in self.history.entries.values()
                )
                for text in texts
            )
        }
        # removed from the list by the user (time removed); back once it has newer activity
        self.hidden: dict[str, float] = (store.get_json("hidden") if store else None) or {}
        self.armed: dict[str, bool] = {}  # may the next follow-up go out yet?
        self.queue_error: dict[str, str] = {}
        self.index = search.Index()
        self.rules = Rules(store)
        self.ruled: dict[str, str] = {}  # session -> request a rule already looked at
        self.reg = registry
        self.claude = claude
        self.codex = codex
        self.opencode = opencode
        # Codex/opencode launched by `new` have no session id until their first message (Claude:
        # until its folder is trusted): until then they're listed as a placeholder ("new-a1b2")
        # that send/keys/screen can target.
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
                    if caller_checked(method):
                        holders = {
                            holder
                            for s in self.reg.sessions.values()
                            if (holder := self.adapters[s.source].holder(s.id))
                        }
                        if await identify_agent(peer_pid(sock.fileno()), holders):
                            params = params | {"_actor": "agent"}
                            if method in ("send", "queue"):
                                method, params = (
                                    "agent_message",
                                    {
                                        "id": params["id"],
                                        "text": params["text"],
                                        "command_id": params.get("command_id"),
                                        "_actor": "agent",
                                        **(
                                            {"team_token": params["team_token"]}
                                            if "team_token" in params
                                            else {}
                                        ),
                                    },
                                )
                    if method == "follow":
                        if params.get("team_token") is not None:
                            raise RpcError("Scoped credentials cannot follow global sessions")
                        await self.follow(reader, writer, req.get("id"), params)
                        return
                    result = await self.dispatch(method, params)
                    reply: dict[str, Any] = {"id": req.get("id"), "result": result}
                except ConnectionError:
                    raise  # the client hung up (e.g. Ctrl-C on `tail -f`): not an error
                except (RpcError, ControlError, launcher.LaunchError, KeyError, ValueError) as e:
                    reply = {"id": req.get("id"), "error": {"message": str(e).strip("'\"")}}
                    if isinstance(e, AmbiguousRef):
                        reply["error"]["choices"] = e.choices
                    if req.get("method") in ("send", "queue", "approve"):  # trace, never the text
                        sid = str((req.get("params") or {}).get("id", ""))[:8]
                        log.info("%s %s refused: %s", req["method"], sid, reply["error"]["message"])
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
        """Record delivery before doing it, so a lost connection never loses the audit trail."""
        if p.get("team_token") is not None and not (
            method.startswith("team_") or method == "teams_list"
        ):
            raise RpcError("Scoped team credentials cannot call global session methods")
        if method in ("agent_message", "agent_launch"):
            if p.get("team_token") is not None:
                raise RpcError("Use scoped team assignment tools")
            return await self.agent_request(method, p)
        if p.get("_actor") == "agent":
            if method == "launch":
                return await self.agent_request("agent_launch", p)
            if method not in AGENT_METHODS and not p.get("team_token"):
                raise RpcError("This control is reserved for the human; use scoped team tools")
            if method in ("send", "queue"):
                sid = self.reg.resolve(self.aliases.get(p["id"], p["id"])).id
                self.agent_chats.require(sid)
        if method.startswith("team_") or method == "teams_list":
            return self.teams.rpc(method, p, STATE_DIR)
        if p.get("team_token") is not None:
            raise RpcError("Scoped team credentials cannot call global session methods")
        if p.get("id") in self.aliases:
            p = p | {"id": self.aliases[p["id"]]}
        if (
            method in TAKEOVER_METHODS
            and not p.get("_team_dispatch")
            and p.get("_actor") != "agent"
        ):
            if p.get("id"):
                if p["id"].startswith("%"):
                    for session in self.reg.sessions.values():
                        if session.tmux_pane == p["id"]:
                            self.teams.takeover(session.id)
                else:
                    self.teams.takeover(self.reg.resolve(p["id"]).id)
        if method in ("tail", "history", "review") and p.get("id") and int(p.get("n", 1)) > 0:
            self.hydrate(str(p["id"]))
        if method == "history":
            s = self.reg.resolve(p["id"])
            return self.history.listing(s.id, self.reg.events.get(s.id, ()))
        if method == "review":
            s = self.reg.resolve(p["id"])
            return await asyncio.to_thread(
                fleet.review_turn, s, list(self.reg.events.get(s.id, ()))
            )
        if method not in ("send", "queue"):
            return await self._dispatch(method, p)
        sid = p["id"] if p["id"].startswith("%") else self.reg.resolve(p["id"]).id
        command_id = p.get("command_id")
        if command_id and command_id in self.history.entries:
            previous = self.history.entries[command_id]
            if previous["session_id"] != sid or previous["text"] != p["text"]:
                raise RpcError("command id already belongs to a different message")
            raise RpcError(f"Message already recorded as {previous['outcome']}; check sent history")
        entry = self.history.begin(sid, p["text"], command_id)
        self.history.update(entry, sender=p.get("_actor", "human"))
        try:
            result = await self._dispatch(method, p)
        except (RpcError, ControlError, KeyError, ValueError) as e:
            self.history.update(entry, outcome="failed", error=str(e))
            raise
        except Exception as e:
            self.history.update(entry, outcome="unknown", error=str(e))
            raise
        self.history.update(entry, outcome="queued" if "queued" in result else "sent")
        return result | {"command_id": entry["id"]}

    def hydrate(self, ref: str) -> None:
        """Someone is reading this session: load its full recent history if startup only
        read the end (it had been untouched for hours)."""
        try:
            s = self.reg.resolve(ref)
        except KeyError:
            return  # the handler reports it
        hydrate = getattr(self.adapters[s.source], "hydrate", None)
        if hydrate:
            hydrate(s.id)

    async def agent_request(self, method: str, p: dict[str, Any]) -> dict[str, Any]:
        """Agent messages use fresh chats; continuing one requires positive provenance."""
        if method == "agent_launch":
            if p.get("resume"):
                raise RpcError("Agents cannot resume user chats")
            model = p.get("model") or ""
            extra = p.get("extra") or []
            if extra:
                if len(extra) != 1 or not extra[0].startswith("--model="):
                    raise RpcError("Agent launches accept a model name, not arbitrary flags")
                model = extra[0].removeprefix("--model=")
            if model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,79}", model):
                raise RpcError("model must be a plain model name")
            return dict(
                await self._dispatch(
                    "launch",
                    {
                        "agent": p["agent"],
                        "cwd": p["cwd"],
                        "name": p.get("name"),
                        "extra": [f"--model={model}"] if model else [],
                        "path": p.get("path") or os.environ.get("PATH", ""),
                        "reference": p.get("reference"),
                        "_actor": "agent",
                    },
                )
            ) | {
                "requested_model": model or None,
                "model_selection": "requested" if model else "agent_default",
            }
        reference = self.reg.resolve(self.aliases.get(p["id"], p["id"]))
        new_chat = p.get("new_chat", True)
        if not isinstance(new_chat, bool):
            raise RpcError("new_chat must be true or false")
        if not new_chat:
            self.agent_chats.require(reference.id)
        model = p.get("model") or ""
        if model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,79}", model):
            raise RpcError("model must be a plain model name")
        if model and not new_chat:
            raise RpcError("A model can only be requested for a new chat")
        model_receipt = {
            "requested_model": model or None,
            "model_selection": ("requested" if model else "agent_default")
            if new_chat
            else "existing_chat",
        }
        assignment_id = p.get("_team_assignment")
        if assignment_id:
            assignment = self.teams.assignments[assignment_id]
            team = self.teams.resolve(assignment.team_id)
            _, member = self.teams.active_member(team, assignment.member_id)
            if member.session_id != reference.id or assignment.session_id != reference.id:
                raise RpcError("Assignment no longer matches the connected worker")
        text = str(p["text"])
        if not text.strip():
            raise RpcError("A message cannot be empty")
        command_id = p.get("command_id")
        if command_id and command_id in self.history.entries:
            previous = self.history.entries[command_id]
            if (
                previous["text"] != text
                or previous.get("reference") != reference.id
                or previous.get("new_chat") != new_chat
                or previous.get("requested_model") != (model or None)
            ):
                raise RpcError("Request id belongs to another agent message")
            return {
                "session_id": previous["session_id"],
                "command_id": command_id,
                "delivery_status": previous["outcome"],
                **model_receipt,
                "new_chat": new_chat,
            }
        entry = self.history.begin(reference.id, text, command_id)
        self.history.update(
            entry,
            sender="agent",
            outcome="creating" if new_chat else "sending",
            reference=reference.id,
            new_chat=new_chat,
            team_assignment=assignment_id,
            **model_receipt,
        )
        try:
            sid = reference.id
            if new_chat:
                agent = {"claude_code": "claude", "codex": "codex", "opencode": "opencode"}
                launched = await self.agent_request(
                    "agent_launch",
                    {
                        "agent": agent[reference.source],
                        "cwd": reference.cwd,
                        "name": "Agent: " + (reference.title or reference.cwd.rsplit("/", 1)[-1]),
                        "reference": reference.id,
                        "model": model,
                    },
                )
                sid = launched["session_id"]
                self.history.update(entry, session_id=sid)
            # Delivery waits locally for setup and the first safe input prompt. Nothing is
            # typed into a trust dialog, and the original conversation is never resumed.
            result = await self._dispatch("queue", {"id": sid, "text": text})
            self.history.update(entry, outcome="queued")
            return dict(result) | {
                "command_id": entry["id"],
                "delivery_status": "queued",
                **model_receipt,
                "new_chat": new_chat,
            }
        except Exception as error:
            self.history.update(entry, outcome="unknown", error=str(error))
            raise

    async def agent_ready(self, sid: str) -> None:
        """A placeholder can contain setup UI; its idle status alone proves nothing."""
        s = self.reg.resolve(sid)
        captured = await self._dispatch("screen", {"id": sid})
        text = captured["screen"]
        if any(marker.lower() in text.lower() for marker in MENU_MARKERS):
            raise RpcError("New agent chat needs human setup; the message remains queued")
        prompt = {"codex": "Ask Codex", "opencode": "Ask anything"}.get(s.source)
        if not prompt or prompt not in text:
            raise RpcError("Waiting for the new agent chat's input prompt")

    async def _dispatch(self, method: str, p: dict[str, Any]) -> Any:
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
            if p.get("_actor") == "agent" and p.get("resume"):
                raise RpcError("Agents must start a fresh chat; resuming user chats is prohibited")
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
                if r["session_id"] not in self.reg.sessions:
                    # e.g. at "trust this folder?": Claude announces the session only after it
                    r["session_id"] = self.add_placeholder(
                        p["agent"], p["cwd"], r["socket"], r["pane"]
                    )
            if not r["session_id"]:
                r["session_id"] = self.add_placeholder(p["agent"], p["cwd"], r["socket"], r["pane"])
            if p.get("_actor") == "agent":
                self.agent_chats.created(r["session_id"], p.get("reference"))
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
            if method == "send" and p.get("_actor") == "agent":
                await self.agent_ready(p["id"])
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
        if method in ("hide", "unhide"):
            s = self.reg.resolve(p["id"])
            if method == "hide":
                self.hidden[s.id] = time.time()
            else:
                self.hidden.pop(s.id, None)
            if self.store:
                self.store.set_json("hidden", self.hidden)
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
            if method == "queue":
                items = [*items, p["text"]]
            elif p.get("index") is not None:  # cancel one
                index = int(p["index"])
                if 0 <= index < len(items):
                    self.history.queued_delivery(s.id, items[index], "cancelled")
                items = [x for i, x in enumerate(items) if i != int(p["index"])]
            else:
                for text in items:
                    self.history.queued_delivery(s.id, text, "cancelled")
                items = []
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
        if method == "search":
            hits = await asyncio.to_thread(
                self.index.search, p["q"], self.transcripts(), int(p.get("limit", 50))
            )
            for h in hits:
                known = self.reg.sessions.get(h["id"])
                h["title"] = self.titles.get(h["id"]) or (known.title if known else None)
                h["status"] = known.status if known else "ended"
            return hits
        if method == "edited":  # files a session edited
            s = self.reg.resolve(p["id"])
            for sid, source, path in self.transcripts([s.id]):
                return await asyncio.to_thread(self.index.edited, sid, source, path)
            since = datetime.fromtimestamp(0, UTC)  # opencode: no file; the events it sent
            return sorted(fleet.edited_files(self.reg.events.get(s.id, ()), s.cwd, since))
        if method == "file_activity":
            sessions = [x for x in self.reg.sessions.values() if x.id not in self.pending]
            hours = float(p.get("hours") or 2)
            return await asyncio.to_thread(
                fleet.file_activity, p["path"], sessions, self.reg.events, hours
            )
        if method == "send" and not p.get("now"):
            # Mid-turn (or with follow-ups already waiting, to keep their order): queue it, so
            # it neither steers the running turn nor lands in an approval menu.
            s = self.reg.resolve(p["id"])
            if s.status in ("working", "awaiting_approval") or self.queued.get(s.id):
                log.info("send %s: %d chars, %s: queued", short_id(s.id), len(p["text"]), s.status)
                return await self._dispatch("queue", {"id": s.id, "text": p["text"]})
        if method in ("send", "approve", "answer", "interrupt", "pane"):
            s = self.reg.resolve(p["id"])
            ad = self.adapters[s.source]
            if method == "send":
                await self.refuse_if_menu(ad, s.id)
                await ad.send(s.id, p["text"])
                log.info(
                    "send %s: %d chars, %s: typed in", short_id(s.id), len(p["text"]), s.status
                )
                return {"session_id": s.id}
            if method == "approve":
                chosen = await ad.approve(s.id, p.get("choice"), bool(p.get("allow", True)))
                return {"session_id": s.id, "chosen": chosen}
            if method == "answer":
                if not isinstance(ad, (ClaudeCodeAdapter, CodexAdapter)):
                    raise RpcError(f"answering questions isn't supported for {s.source} yet")
                return {"session_id": s.id, "chosen": await ad.answer(s.id, p["answers"])}
            if method == "interrupt":
                await ad.interrupt(s.id)
                return {"session_id": s.id}
            sock, pane = await ad.verified(s.id)
            return {"session_id": s.id, "socket": sock, "pane": pane}
        if method == "rules":
            return self.rules.rpc(p)
        if method == "approve_all":
            return await self.approve_all(p)
        if method == "ping":
            return {
                "pid": os.getpid(),
                "started": STARTED,
                "features": ["review", "history", "teams", "agent-chat-isolation"],
            }
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
                if not p.get("all") and self.hidden.get(s.id, 0) >= s.last_activity.timestamp():
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

    def transcripts(self, only: list[str] | None = None) -> list[tuple[str, str, Path]]:
        """(session id, source, transcript file) for Claude and Codex sessions."""
        files = [(sid, "claude_code", t.path) for sid, t in self.claude.by_sid.items()]
        files += [(tid, "codex", r.path) for tid, r in self.codex.rollouts.items()]
        return [f for f in files if only is None or f[0] in only]

    async def refuse_if_menu(self, ad: Adapter, sid: str) -> None:
        """The status can lag the screen by a second; the screen can't. Look before typing."""
        try:
            sock, pane = await ad.verified(sid)
        except ControlError:
            return  # not in a pane (Codex via app-server): the adapter does its own checks
        bottom = "\n".join((await screen(sock, pane)).splitlines()[-20:])
        if any(m in bottom for m in MENU_MARKERS):
            raise RpcError(
                f"{short_id(sid)} has a menu open (permission or trust prompt); a message "
                "could answer it. Answer it first (agentctl approve/deny, or watch)."
            )

    def dump(self, s: Session, conflicts: dict[str, list[str]] | None = None) -> dict[str, Any]:
        d = s.model_dump(mode="json")
        d["agent_writable"] = s.id in self.agent_chats.records
        d["initiated_by"] = "agent" if d["agent_writable"] else "user_or_unknown"
        delivery = next(
            (
                e
                for e in reversed(list(self.history.entries.values()))
                if e["session_id"] == s.id and e.get("sender") == "agent"
            ),
            None,
        )
        if delivery:
            d["agent_delivery"] = {
                "status": delivery["outcome"],
                "error": delivery.get("error", ""),
                "requested_model": delivery.get("requested_model"),
                "model_selection": delivery.get("model_selection", "agent_default"),
            }
        if s.id in self.titles:
            d["title"] = self.titles[s.id]
        seen = self.seen.get(s.id, self.seen["*"])
        d["seen_at"] = seen.isoformat()
        evs = self.reg.events.get(s.id, ())
        d["unread"] = fleet.unread(evs, seen)
        d["preview"] = fleet.preview(evs)
        d["project"] = fleet.project_name(s.cwd)
        d["project_path"] = fleet.project_root(s.cwd)
        d["conflicts"] = (conflicts or {}).get(s.id, [])
        d["queued"] = self.queued.get(s.id, [])
        d["queue_error"] = self.queue_error.get(s.id)
        live = s.controllable and s.status != "ended"
        d["capabilities"] = {
            "send": live,
            "approve": live and s.status == "awaiting_approval",
            "interrupt": live and s.status == "working",
            "attach": bool(s.tmux_pane),
            "adopt": not s.controllable,
        }
        d["control_reason"] = (
            "Session ended; resume or adopt it to continue"
            if s.status == "ended"
            else "Control available"
            if s.controllable
            else "Read only; answer in the owning app, or adopt this session to control it"
        )
        return d

    def set_queued(self, sid: str, items: list[str]) -> None:
        if items:
            self.queued[sid] = items
        else:
            self.queued.pop(sid, None)
            self.legacy_queue.discard(sid)
            self.queue_error.pop(sid, None)
        if self.store:
            self.store.set_queued(sid, items)

    async def apply_rules(self) -> None:
        """Answer permission prompts your approval rules cover, once per request."""
        while True:
            await asyncio.sleep(1)
            await self.rules_tick()

    async def rules_tick(self) -> None:
        for s in list(self.reg.sessions.values()):
            appr = s.pending_approval if s.status == "awaiting_approval" else None
            if appr is None:
                self.ruled.pop(s.id, None)
                continue
            tool = appr.tool or ""
            # one look per request; questions and sessions you can't control are always yours
            if not tool or appr.questions or not s.controllable or self.ruled.get(s.id) == tool:
                continue
            self.ruled[s.id] = tool
            rule = decide(self.rules.rules, tool, s.cwd)
            if rule is None:
                continue
            allow = rule.action == "allow"
            try:
                chosen = await self.adapters[s.source].approve(s.id, None, allow)
            except (ControlError, KeyError, ValueError) as e:
                log.warning("rule %r matched %s but couldn't answer: %s", str(rule), s.id[:8], e)
                self.notice(s.id, f"rule `{rule}` matched {tool} but couldn't answer it: {e}")
                continue
            log.info("rule %r answered %s: %s", str(rule), s.id[:8], chosen)
            verb = "approved" if allow else "denied"
            self.notice(s.id, f"auto-{verb} {tool} ({chosen}) by rule `{rule}`")

    def notice(self, sid: str, text: str) -> None:
        self.reg.add_event(
            SessionEvent(
                session_id=sid, kind="notice", ts=datetime.now(UTC), payload={"text": text}
            )
        )

    async def approve_all(self, p: dict[str, Any]) -> dict[str, Any]:
        """Approve (or deny) every pending request matching a pattern, across sessions. With
        `only`, just those (session, request) pairs, as previewed, if they're still pending."""
        pattern = str(p.get("tool") or "").strip()
        if not pattern:
            raise RpcError("say which requests: e.g. --tool 'Read*' (or --tool '*' for all)")
        only = {(o["id"], o["tool"]) for o in p.get("only") or []}
        allow = bool(p.get("allow", True))
        rule = Rule("allow" if allow else "deny", pattern, p.get("folder"))
        out = []
        for s in self.reg.listing():
            appr = s.pending_approval if s.status == "awaiting_approval" else None
            if appr is None or appr.questions or not appr.tool:
                continue
            if not rule.matches(appr.tool, s.cwd) or (only and (s.id, appr.tool) not in only):
                continue
            row: dict[str, Any] = {"session_id": s.id, "tool": appr.tool, "cwd": s.cwd}
            if not s.controllable:
                row["error"] = "read-only: adopt it first"
            elif not p.get("dry_run"):
                try:
                    row["chosen"] = await self.adapters[s.source].approve(s.id, None, allow)
                except (ControlError, KeyError, ValueError) as e:
                    row["error"] = str(e)
            out.append(row)
        return {"matched": out}

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
            if sid in self.legacy_queue:
                self.queue_error[sid] = (
                    "Older queued work has no verified sender. "
                    "Cancel it and resend from the dashboard."
                )
                continue
            if sid in self.aliases:  # queued on a placeholder that is now real
                self.history.migrate(sid, self.aliases[sid])
                self.set_queued(self.aliases[sid], self.queued[sid])
                self.set_queued(sid, [])
                continue
            s = self.reg.sessions.get(sid)
            if not s or s.status not in ("awaiting_input", "idle"):
                continue
            if not self.armed.get(sid, True):
                continue
            text = self.queued[sid][0]
            receipt = next(
                (
                    e
                    for e in self.history.entries.values()
                    if e["session_id"] == sid
                    and e["text"] == text
                    and e["outcome"] in ("queued", "sending", "unknown")
                ),
                {},
            )
            if not receipt:
                receipt = next(
                    (
                        e
                        for e in reversed(list(self.history.entries.values()))
                        if e["session_id"] == sid and e["text"] == text
                    ),
                    {},
                )
            actor = receipt.get("sender", "human")
            attempted = False
            try:
                if actor == "agent":
                    self.agent_chats.require(sid)
                    if receipt.get("outcome") in ("sending", "unknown"):
                        raise RpcError("Delivery is uncertain; inspect it before sending again")
                    if receipt.get("outcome") in ("failed", "cancelled", "sent"):
                        self.set_queued(sid, self.queued[sid][1:])
                        continue
                    assignment_id = receipt.get("team_assignment")
                    if assignment_id:
                        assignment = self.teams.assignments[assignment_id]
                        if assignment.status in ("cancelled", "failed", "accepted"):
                            self.history.queued_delivery(sid, text, "cancelled")
                            self.set_queued(sid, self.queued[sid][1:])
                            continue
                        if assignment.status == "unknown":
                            raise RpcError("Unknown assignment delivery requires human inspection")
                        team = self.teams.resolve(assignment.team_id)
                        _, member = self.teams.active_member(team, assignment.member_id)
                        if member.session_id != assignment.session_id:
                            raise RpcError("Assignment connection changed before delivery")
                    if sid in self.pending:
                        await self.agent_ready(sid)
                    self.history.update(receipt, outcome="sending", error="")
                    attempted = True
                await self._dispatch(
                    "send", {"id": sid, "text": text, "now": True, "_actor": actor}
                )
            except Exception as e:
                if attempted:
                    self.history.update(receipt, outcome="unknown", error=str(e))
                self.queue_error[sid] = str(e).strip("'\"")
                if receipt and not attempted:
                    self.history.update(receipt, error=self.queue_error[sid])
                continue
            log.info("sent a queued follow-up to %s", short_id(sid))
            if actor == "agent":
                self.history.update(receipt, outcome="sent", error="")
            else:
                self.history.queued_delivery(sid, text, "sent")
            self.armed[sid] = False
            self.queue_error.pop(sid, None)
            self.set_queued(sid, self.queued[sid][1:])

    def add_placeholder(self, agent: str, cwd: str, sock: str, pane: str) -> str:
        pid = f"new-{uuid.uuid4().hex[:4]}"
        self.pending[pid] = (sock, pane)
        self.reg.upsert(
            Session(
                id=pid,
                source="claude_code"
                if agent == "claude"
                else "codex"
                if agent == "codex"
                else "opencode",
                origin="cli",
                cwd=cwd,
                title=f"new {agent} session",
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
                self.history.migrate(pid, real)
                self.agent_chats.migrate(pid, real)
                self.teams.migrate_session(pid, real)
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
    watchers.append(asyncio.create_task(supervise("rules", srv.apply_rules)))
    watchers.append(
        asyncio.create_task(supervise("teams", lambda: srv.teams.watch(srv.dispatch, srv.queued)))
    )
    await srv.stopping.wait()
    for w in watchers:
        w.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await w
    server.close()
    SOCKET_PATH.unlink(missing_ok=True)
    log.info("agentctld stopped")
