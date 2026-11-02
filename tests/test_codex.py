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
        for _ in range(250):  # up to 5s: a busy machine can be slow to start the fake server
            # Status and prompt details arrive in separate app-server messages.
            if s.status == "awaiting_approval" and s.pending_approval is not None:
                break
            await asyncio.sleep(0.02)
        assert s.pending_approval and s.pending_approval.tool == "shell: touch x"
        assert s.pending_approval.options["3"] == "No"
        assert await a.approve(tid, None, allow=True) == "1. Yes"
        for _ in range(250):
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


def test_limits_survive_an_empty_bucket_report(tmp_path: Path) -> None:
    """Codex also reports other buckets ("premium") with no windows; the newest report of any
    kind used to replace the real 5h/week numbers with nothing."""
    import time as _time

    from agentctl.adapters.codex import Rollout, parse_rollout
    from agentctl.daemon.bus import Bus
    from agentctl.daemon.registry import Registry

    reset = int(_time.time()) + 3600
    main = {
        "limit_id": "codex",
        "plan_type": "plus",
        "primary": {"used_percent": 99.0, "window_minutes": 300, "resets_at": reset},
        "secondary": {"used_percent": 43.0, "window_minutes": 10080, "resets_at": reset},
    }
    empty = {"limit_id": "premium", "plan_type": "plus", "primary": None, "secondary": None}
    r = Rollout(path=tmp_path / "r.jsonl", tid="t1")
    for ts, rl in (("2026-10-16T06:38:53.5Z", main), ("2026-10-16T06:38:53.9Z", empty)):
        line = {
            "timestamp": ts,
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {}, "rate_limits": rl},
        }
        parse_rollout(line, r)
    a = CodexAdapter(Registry(Bus()), root=tmp_path, exe="/usr/bin/false")
    a.rollouts["t1"] = r
    lim = a.limits()
    assert lim and [(w["name"], w["used_percent"]) for w in lim["windows"]] == [
        ("5h", 99.0),
        ("week", 43.0),
    ]


def test_request_user_input_is_shown_and_answered(tmp_path: Path, monkeypatch: object) -> None:
    import pytest

    from agentctl import tmux
    from agentctl.adapters import codex

    mp = monkeypatch
    assert isinstance(mp, pytest.MonkeyPatch)
    meta = Path(__file__).parent / "fixtures" / "codex_paginated.jsonl"
    tid = "01a00000-0000-7000-8000-000000000002"
    p = tmp_path / f"rollout-2026-10-17T20-28-28-{tid}.jsonl"
    raw = [
        {"id": "colour", "header": "Colour", "question": "Colour?", "isOther": True,
         "options": [{"label": "red", "description": ""}, {"label": "blue", "description": ""}]},
        {"id": "name", "header": "Name", "question": "Name?", "options": None},
    ]  # fmt: skip
    call = {"type": "function_call", "name": "request_user_input", "call_id": "c1",
            "arguments": json.dumps({"questions": raw})}  # fmt: skip
    line = {"timestamp": "2026-10-17T14:58:48.306Z", "type": "response_item", "payload": call}
    p.write_text(meta.read_text().splitlines()[0] + "\n" + json.dumps(line) + "\n")
    reg = Registry(Bus())
    a = CodexAdapter(reg, root=tmp_path, exe="/usr/bin/false")
    a.open_rollout(p, startup=False)
    s = reg.sessions[tid]
    assert s.status == "awaiting_approval" and s.pending_approval
    qs = s.pending_approval.questions
    assert [q["id"] for q in qs] == ["colour", "name"] and qs[1]["options"] == []

    sent: list[str] = []

    async def keys(sock: object, pane: str, *k: str) -> None:
        sent.extend(k)

    async def literal(sock: object, pane: str, text: str) -> None:
        sent.append(f"<{text}>")

    async def verified(t: str) -> tuple[str, str]:
        return "sock", "%1"

    async def nap(_: float) -> None:
        pass

    mp.setattr(tmux, "keys", keys)
    mp.setattr(tmux, "literal", literal)
    mp.setattr(a, "verified", verified)
    mp.setattr(asyncio, "sleep", nap)
    answers = [{"text": "teal"}, {"text": "sam"}]
    assert asyncio.run(a.answer(tid, answers)) == "teal; sam"
    # "None of the above" is after the options: down to it, tab for its note, type, enter
    assert sent == ["Down", "Down", "Tab", "<teal>", "Enter", "<sam>", "Enter"]
    assert s.status != "awaiting_approval"  # answered

    class Server:  # our own app-server asked instead: the answer is a reply, no keys
        replies: list[tuple[object, object]] = []

        def respond(self, rid: object, result: object = None, error: str | None = None) -> None:
            self.replies.append((rid, result))

    srv = Server()
    a.server = srv  # type: ignore[assignment]
    a.pending[tid] = {"id": 7, "method": codex.ASK, "params": {"questions": raw}}
    a.announce(tid, codex.ASK, {"questions": raw})
    sent.clear()
    asyncio.run(a.answer(tid, [{"choices": [2]}, {"text": "sam"}]))
    assert not sent and tid not in a.pending
    expected = {"answers": {"colour": {"answers": ["blue"]}, "name": {"answers": ["sam"]}}}
    assert srv.replies == [(7, expected)]

    done = {"type": "function_call_output", "call_id": "c1", "output": "{}"}
    with p.open("a") as f:
        f.write(json.dumps({"timestamp": "2026-10-17T14:59:31Z", "type": "response_item",
                            "payload": done}) + "\n")  # fmt: skip
    r = a.rollouts[tid]
    r.asking = ("c1", qs)
    a.ingest(r, publish=True)
    assert r.asking is None


def test_limits_are_asked_for_not_only_read_from_replies(tmp_path: Path) -> None:
    """The weekly window reset early; rollouts said 76% until Codex next replied."""
    import time as _time

    reset = int(_time.time()) + 3600
    a = CodexAdapter(Registry(Bus()), root=tmp_path, exe="/usr/bin/false")
    old = {"limit_id": "codex", "primary": {"used_percent": 76.0, "window_minutes": 10080,
           "resets_at": reset}}  # fmt: skip
    r = Rollout(path=tmp_path / "r.jsonl", tid="t1")
    r.limits["codex"] = (datetime(2026, 10, 17, tzinfo=UTC), old)
    a.rollouts["t1"] = r

    class Server:
        async def request(self, method: str, params: object) -> dict[str, object]:
            assert method == "account/rateLimits/read"
            w = {"usedPercent": 0, "windowDurationMins": 10080, "resetsAt": reset}
            return {"rateLimitsByLimitId": {"codex": {"planType": "plus", "primary": w}}}

    async def server() -> Server:
        return Server()

    a.ensure_server = server  # type: ignore[assignment,method-assign]
    asyncio.run(a.read_limits())
    lim = a.limits()
    assert lim and lim["plan"] == "plus" and lim["windows"][0]["used_percent"] == 0
