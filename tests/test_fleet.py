import asyncio
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agentctl.daemon import fleet
from agentctl.daemon.bus import Bus
from agentctl.daemon.registry import Registry
from agentctl.models import Session, SessionEvent

NOW = datetime.now(UTC)


def sess(sid: str, cwd: str, status: str = "working") -> Session:
    return Session(id=sid, source="claude_code", cwd=cwd, status=status, last_activity=NOW)  # type: ignore[arg-type]


def ev(sid: str, kind: str, ago: int = 0, **payload: Any) -> SessionEvent:
    return SessionEvent(
        session_id=sid,
        kind=kind,  # type: ignore[arg-type]
        ts=NOW - timedelta(minutes=ago),
        payload=payload,
    )


def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "pkg").mkdir()
    return tmp_path


def test_conflicts_same_repo_and_same_file(tmp_path: Path) -> None:
    root = repo(tmp_path)
    a, b = sess("aaaa1111", str(root)), sess("bbbb2222", str(root / "pkg"))
    c = sess("cccc3333", str(root), status="awaiting_input")  # idle: not "working in" it
    events = {
        "aaaa1111": [ev("aaaa1111", "tool_call", name="Edit", summary=str(root / "pkg/x.py"))],
        "cccc3333": [ev("cccc3333", "tool_call", name="edit", summary="pkg/x.py, pkg/y.py")],
    }
    out = fleet.conflicts([a, b, c], events)
    assert f"also working in {root.name}: bbbb2222" in out["aaaa1111"]
    assert "x.py also edited by cccc3333" in out["aaaa1111"]
    assert out["cccc3333"] == ["x.py also edited by aaaa1111"]
    assert fleet.project_name(str(root / "pkg")) == root.name  # subfolder -> its repo


def test_old_edits_dont_conflict(tmp_path: Path) -> None:
    a, b = sess("a", str(tmp_path), "awaiting_input"), sess("b", str(tmp_path), "awaiting_input")
    events = {
        "a": [ev("a", "tool_call", ago=300, name="Write", summary="f.py")],
        "b": [ev("b", "tool_call", name="Write", summary="f.py")],
    }
    assert fleet.conflicts([a, b], events) == {}


def test_unread_and_digest(tmp_path: Path) -> None:
    s = sess("aaaa1111", str(tmp_path), "awaiting_input")
    evs = [
        ev("aaaa1111", "message", ago=30, role="assistant", text="old"),
        ev("aaaa1111", "tool_call", ago=5, name="Edit", summary="app.py"),
        ev("aaaa1111", "message", ago=4, role="user", text="hi"),
        ev("aaaa1111", "message", ago=1, role="assistant", text="Fixed the bug.\nDetails…"),
    ]
    seen = NOW - timedelta(minutes=10)
    assert fleet.unread(evs, seen) == 1
    rows = fleet.digest([s], {"aaaa1111": evs}, {}, seen)
    assert rows == [
        {
            "id": "aaaa1111",
            "title": None,
            "status": "awaiting_input",
            "new_replies": 1,
            "files": ["app.py"],
            "last_reply": "Fixed the bug.",
        }
    ]
    assert fleet.digest([s], {"aaaa1111": evs}, {"aaaa1111": NOW}, seen) == []


def test_queued_follow_up_waits_for_the_agent() -> None:
    from agentctl.daemon.server import Server

    sent: list[str] = []

    class Agent:
        source = "claude_code"

        async def send(self, sid: str, text: str) -> None:
            sent.append(text)

        async def verified(self, sid: str) -> tuple[str, str]:
            from agentctl.adapters.base import ControlError

            raise ControlError("not in a pane")  # so no screen to check

    reg = Registry(Bus())
    s = reg.upsert(sess("aaaa1111", "/w"))
    srv = Server(reg, Agent(), Agent(), Agent())  # type: ignore[arg-type]

    async def run() -> None:
        with reg.bus.subscribe() as q:
            srv.set_queued("aaaa1111", ["run the tests", "then commit"])
            await srv.deliver_tick(q)
            assert sent == []  # still working
            reg.set_status("aaaa1111", "awaiting_input")
            await srv.deliver_tick(q)
            assert sent == ["run the tests"]
            reg.add_event(ev("aaaa1111", "message", role="user", text="run the tests"))
            await srv.deliver_tick(q)
            assert sent == ["run the tests"]  # its echo isn't the agent starting: still waits
            reg.set_status("aaaa1111", "working")  # it picked the first one up...
            reg.set_status("aaaa1111", "awaiting_input")  # ...and finished
            await srv.deliver_tick(q)
            assert sent == ["run the tests", "then commit"]
            assert srv.queued == {}

    asyncio.run(run())
    assert s.status == "awaiting_input"


def test_send_refused_while_a_menu_is_on_screen(monkeypatch: Any) -> None:
    from agentctl.daemon import server as srv_mod
    from agentctl.daemon.server import RpcError, Server

    sent: list[str] = []

    class Agent:
        source = "claude_code"

        async def verified(self, sid: str) -> tuple[str, str]:
            return "/tmp/sock", "%1"

        async def send(self, sid: str, text: str) -> None:
            sent.append(text)

    reg = Registry(Bus())
    reg.upsert(sess("aaaa1111", "/w", "awaiting_input"))  # status hasn't caught up yet
    srv = Server(reg, Agent(), Agent(), Agent())  # type: ignore[arg-type]
    shown = "⏺ Bash(rm -rf build)\n Do you want to proceed?\n ❯ 1. Yes\n   2. No"

    async def screen(sock: str, pane: str) -> str:
        return shown

    monkeypatch.setattr(srv_mod, "screen", screen)

    async def run() -> None:
        try:
            await srv.dispatch("send", {"id": "aaaa1111", "text": "1"})
        except RpcError as e:
            assert "menu open" in str(e)
        else:
            raise AssertionError("a send into an open permission menu must be refused")
        assert sent == []

    asyncio.run(run())
