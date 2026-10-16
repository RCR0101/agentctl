import asyncio
import json
import os
import shutil
from collections import Counter
from datetime import timedelta
from pathlib import Path

import pytest

from agentctl.adapters.claude_code import ClaudeCodeAdapter
from agentctl.daemon.bus import Bus
from agentctl.daemon.registry import Registry

FIX = Path(__file__).parent / "fixtures"
SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def env(tmp_path: Path) -> tuple[Registry, ClaudeCodeAdapter, Path]:
    root = tmp_path / ".claude"
    (root / "projects" / "-Users-me-proj").mkdir(parents=True)
    (root / "sessions").mkdir()
    reg = Registry(Bus())
    return reg, ClaudeCodeAdapter(reg, root=root), root


def transcript(root: Path) -> Path:
    return root / "projects" / "-Users-me-proj" / f"{SID}.jsonl"


def line(**d: object) -> str:
    base = {"sessionId": SID, "cwd": "/Users/me/proj", "entrypoint": "cli", "isSidechain": False}
    return json.dumps(base | d) + "\n"


def test_real_cli_fixture(env: tuple[Registry, ClaudeCodeAdapter, Path]) -> None:
    reg, a, root = env
    shutil.copy(FIX / "claude_cli.jsonl", transcript(root))
    asyncio.run(a.discover())
    s = reg.sessions[SID]
    assert (s.origin, s.cwd, s.title) == ("cli", "/Users/me/proj", "Sample AI title")
    kinds = Counter(e.kind for e in reg.events[SID])
    assert kinds["tool_call"] >= 1 and kinds["tool_result"] >= 1 and kinds["message"] >= 2
    assert s.status == "awaiting_input"  # last turn ended with end_turn, file fresh
    # context = input + cache read + cache write of the last request; 1h cache tier
    assert s.context_tokens == 92009 and s.context_window == 200_000
    t = a.by_sid[SID]
    assert t.last_request and s.cache_expires_at == t.last_request + timedelta(hours=1)


def test_real_desktop_fixture(env: tuple[Registry, ClaudeCodeAdapter, Path]) -> None:
    reg, a, root = env
    shutil.copy(FIX / "claude_desktop.jsonl", transcript(root))
    asyncio.run(a.discover())
    assert reg.sessions[SID].origin == "desktop"


def test_incremental_partial_bad_and_unknown_lines(
    env: tuple[Registry, ClaudeCodeAdapter, Path],
) -> None:
    reg, a, root = env
    p = transcript(root)
    p.write_text("")
    a.open_transcript(p, startup=False)
    t = a.by_sid[SID]

    full = line(type="user", message={"role": "user", "content": "hello there"})
    with p.open("a") as f:
        f.write(full[:20])  # partial write
    a.ingest(t, publish=True)
    assert SID not in reg.events
    with p.open("a") as f:
        f.write(full[20:])
        f.write('{"type": "user", "message": {"con\n')  # truncated JSON
        f.write(line(type="brand-new-entry-type", foo=1))
        f.write(line(type="user", isSidechain=True, message={"content": "sidechain"}))
        f.write(line(type="user", isMeta=True, message={"content": "meta"}))
    a.ingest(t, publish=True)
    assert [e.payload["text"] for e in reg.events[SID]] == ["hello there"]
    assert t.title == "hello there" and t.last_kind == "prompt"

    # subagent turns are flagged and don't move status
    with p.open("a") as f:
        f.write(
            line(
                type="assistant",
                agentId="a1",
                message={"content": [{"type": "text", "text": "sub"}], "stop_reason": "end_turn"},
            )
        )
    a.ingest(t, publish=True)
    assert reg.events[SID][-1].payload["agent_id"] == "a1"
    assert t.last_kind == "prompt"

    # truncation/rotation: re-read from the start
    p.write_text(
        line(
            type="user",
            message={"content": [{"type": "text", "text": "[Request interrupted by user]"}]},
        )
    )
    a.ingest(t, publish=True)
    assert reg.events[SID][-1].payload["text"].startswith("[Request interrupted")
    assert t.last_kind == "interrupted"


def test_pid_file_status(env: tuple[Registry, ClaudeCodeAdapter, Path]) -> None:
    reg, a, root = env
    shutil.copy(FIX / "claude_cli.jsonl", transcript(root))
    pidf = root / "sessions" / "1.json"
    info = {"pid": os.getpid(), "sessionId": SID, "status": "waiting", "waitingFor": "approve Bash"}
    pidf.write_text(json.dumps(info))
    asyncio.run(a.discover())
    s = reg.sessions[SID]
    assert s.status == "awaiting_approval" and s.pending_approval
    assert s.pending_approval.detail == "approve Bash"

    pidf.write_text(json.dumps(info | {"status": "busy"}))
    a.scan_pids()
    assert s.status == "working" and s.pending_approval is None

    pidf.write_text(json.dumps(info | {"pid": 2**22 + 12345}))  # no such process
    a.scan_pids()
    assert s.status == "awaiting_input"  # back to transcript inference
    os.utime(transcript(root), (0, 0))
    a.ingest(a.by_sid[SID], publish=True)
    a.tick()
    assert s.status == "ended"


def test_resolve_prefix(env: tuple[Registry, ClaudeCodeAdapter, Path]) -> None:
    reg, a, root = env
    for sid in ("abc111", "abc222"):
        (root / "projects" / "-Users-me-proj" / f"{sid}.jsonl").write_text("")
    asyncio.run(a.discover())
    assert reg.resolve("abc1").id == "abc111"
    with pytest.raises(KeyError, match="ambiguous"):
        reg.resolve("abc")
    with pytest.raises(KeyError, match="no session"):
        reg.resolve("zzz")


def test_window_learned_from_any_session_of_the_model(
    env: tuple[Registry, ClaudeCodeAdapter, Path],
) -> None:
    reg, a, root = env

    def usage(n: int) -> str:
        u = {"input_tokens": n, "cache_creation": {"ephemeral_5m_input_tokens": 1}}
        msg = {"model": "claude-x", "usage": u, "content": [], "stop_reason": "end_turn"}
        return line(type="assistant", message=msg, timestamp="2026-10-15T00:00:00Z")

    proj = root / "projects" / "-Users-me-proj"
    (proj / "small.jsonl").write_text(usage(150_000))
    a.open_transcript(proj / "small.jsonl", startup=False)
    assert reg.sessions["small"].context_window == 200_000  # no evidence yet: assume 200k
    (proj / "big.jsonl").write_text(usage(300_000))
    a.open_transcript(proj / "big.jsonl", startup=False)
    assert reg.sessions["small"].context_window == 1_000_000  # claude-x proved it has 1M
    assert reg.sessions["small"].cache_expires_at is not None  # 5m tier seen
