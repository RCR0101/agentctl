import asyncio
import json
import os
import shutil
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from agentctl.adapters.codex import CodexAdapter, Rollout, item_events, origin_of, parse_rollout
from agentctl.daemon.bus import Bus
from agentctl.daemon.registry import Registry

FIX = Path(__file__).parent / "fixtures"
FAKE = Path(__file__).parent / "fake_codex_app_server.py"


def parse_file(path: Path) -> tuple[Rollout, list[str]]:
    r = Rollout(path=path, tid="t")
    kinds = []
    for line in path.read_text().splitlines():
        for ev in parse_rollout(json.loads(line), r):
            kinds.append(ev.kind if ev.kind != "message" else f"msg:{ev.payload['role']}")
    return r, kinds


def test_paginated_rollout_uses_item_completed_only() -> None:
    r, kinds = parse_file(FIX / "codex_paginated.jsonl")
    assert r.paginated and r.originator == "Codex Desktop"
    assert origin_of(r.source, r.originator) == "desktop"
    c = Counter(kinds)
    # tool calls appear as both response_item and item_completed; only the latter counts
    assert c["tool_call"] == 4  # CommandExecution, FileChange, web search, MCP
    assert c["msg:user"] == 1 and c["msg:assistant"] == 1 and c["tool_result"] == 1
    assert r.last_kind == "done"
    assert (r.context, r.window) == (26746, 475000)  # last token_count event


def test_legacy_rollout_uses_event_msgs_and_response_items() -> None:
    r, kinds = parse_file(FIX / "codex_legacy.jsonl")
    assert not r.paginated and r.originator == "agentctl"
    assert kinds == ["msg:user", "msg:assistant", "tool_call", "tool_result"]
    assert r.last_kind == "interrupted"  # turn_aborted is the last status line


def test_item_events_bad_and_unknown_items() -> None:
    now = datetime.now(UTC)
    assert item_events({"type": "Reasoning"}, "t", now) == []
    assert item_events({"type": "brandNewThing", "x": 1}, "t", now) == []
    assert item_events({}, "t", now) == []


def fake_exe(tmp_path: Path, rollout: Path) -> str:
    exe = tmp_path / "codex"
    exe.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE} {rollout} "$@"\n')
    exe.chmod(0o755)
    return str(exe)


def test_fake_app_server_send_and_approve(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout-2026-10-15T00-00-00-01a00000-0000-7000-8000-0000000000aa.jsonl"
    shutil.copy(FIX / "codex_legacy.jsonl", rollout)
    os.utime(rollout, (0, 0))  # old file: nothing live until we act
    reg = Registry(Bus())
    a = CodexAdapter(reg, root=tmp_path, exe=fake_exe(tmp_path, rollout))
    tid = "01a00000-0000-7000-8000-0000000000aa"

    async def run() -> None:
        await a.discover()
        s = reg.sessions[tid]
        # rollout originator "agentctl" outranks thread/list's source
        assert (s.title, s.origin, s.status, s.controllable) == (
            "fix the bug",
            "sdk",
            "ended",
            True,
        )
        await a.send(tid, "please touch x")
        for _ in range(50):
            if s.status == "awaiting_approval":
                break
            await asyncio.sleep(0.02)
        assert s.pending_approval and s.pending_approval.tool == "shell: touch x"
        assert s.pending_approval.options["3"] == "No"
        assert await a.approve(tid, None, allow=True) == "1. Yes"
        for _ in range(50):
            if s.status == "awaiting_input":
                break
            await asyncio.sleep(0.02)
        assert s.status == "awaiting_input"
        kinds = [e.kind for e in reg.events[tid]]
        assert kinds[-4:] == ["message", "approval_request", "tool_call", "tool_result"]
        texts = [e.payload.get("text") for e in reg.events[tid]]
        assert texts.count("please touch x") == 1  # streamed once, not again from the rollout
        assert a.server
        await a.server.stop()

    asyncio.run(run())


def test_foreign_lock_refuses_send(tmp_path: Path) -> None:
    reg = Registry(Bus())
    a = CodexAdapter(reg, root=tmp_path, exe="/usr/bin/false")

    async def held() -> None:  # stands in for lsof: pid 4242 (say, the desktop app) has it open
        a.holders = {"t1": 4242}

    a.scan_locks = held  # type: ignore[method-assign]

    async def run() -> None:
        try:
            await a.send("t1", "hi")
        except Exception as e:
            assert "another Codex process (pid 4242" in str(e)
        else:
            raise AssertionError("send should refuse")

    asyncio.run(run())


def test_new_rollout_without_prompt_yet(tmp_path: Path) -> None:
    """A TUI's rollout is opened by the lock scan before its first prompt is written."""
    from agentctl.daemon.bus import Bus
    from agentctl.daemon.registry import Registry

    meta = Path(__file__).parent / "fixtures" / "codex_paginated.jsonl"
    p = tmp_path / "rollout-2026-10-16T09-37-58-01a00000-0000-7000-8000-000000000001.jsonl"
    p.write_text(meta.read_text().splitlines()[0] + "\n")
    reg = Registry(Bus())
    a = CodexAdapter(reg, root=tmp_path, exe="/usr/bin/false")
    a.open_rollout(p, startup=False)  # used to raise IndexError and kill the watcher
    assert reg.sessions and next(iter(reg.sessions.values())).title is None
