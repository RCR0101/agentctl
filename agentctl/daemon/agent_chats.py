"""Positive provenance for agent-created chats. Unverified chats are protected."""

import asyncio
import os
import shlex
import time
from typing import Any

from agentctl.daemon.registry import Store


def agentctl_args(command: str) -> list[str]:
    try:
        parts = shlex.split(command)
    except ValueError:
        return []
    for i, part in enumerate(parts):
        if os.path.basename(part) == "agentctl":
            return parts[i + 1 :]
    return []


def agent_process(pid: int, rows: dict[int, tuple[int, str]], holders: set[int]) -> bool:
    # A dashboard opened over an agent's terminal is still the human's UI.
    args = agentctl_args(rows.get(pid, (0, ""))[1])
    if args[:1] == ["watch"] or args == ["teams"]:
        return False
    seen = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        parent, command = rows.get(pid, (0, ""))
        if pid in holders or agentctl_args(command)[:1] == ["mcp"]:
            return True
        if command and os.path.basename(command.split()[0]) in ("claude", "codex", "opencode"):
            return True
        pid = parent
    return False


async def identify_agent(pid: int, holders: set[int]) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "ps",
        "-axo",
        "pid=,ppid=,command=",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    output, _ = await proc.communicate()
    rows: dict[int, tuple[int, str]] = {}
    for line in output.decode(errors="replace").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows[int(parts[0])] = (int(parts[1]), parts[2])
    if proc.returncode or pid not in rows:
        raise ValueError("Cannot verify the caller of this chat write; no message was sent")
    return agent_process(pid, rows, holders)


class AgentChats:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store
        self.records: dict[str, dict[str, Any]] = (
            (store.get_json("agent_chats") or {}) if store else {}
        )

    def save(self) -> None:
        if self.store:
            self.store.set_json("agent_chats", self.records)

    def created(self, sid: str, reference: str | None = None) -> None:
        self.records[sid] = {"created": time.time(), "reference": reference}
        self.save()

    def require(self, sid: str) -> None:
        if sid not in self.records:
            raise ValueError(
                "Agents cannot write to user-initiated or unverified chats. "
                "Start a new chat (new_chat=true)."
            )

    def migrate(self, old: str, new: str) -> None:
        if old in self.records:
            self.records[new] = self.records.pop(old)
            self.save()
