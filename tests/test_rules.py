"""Approval rules and bulk approve: who may set them, what they match, what they answer."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from agentctl import cli
from agentctl.adapters.base import ControlError
from agentctl.daemon import server as server_mod
from agentctl.daemon.bus import Bus
from agentctl.daemon.registry import Registry, Store
from agentctl.daemon.rules import Rule, decide, parse
from agentctl.daemon.server import AGENT_METHODS, CALLER_CHECKED, RpcError, Server
from agentctl.models import ApprovalRequest, Session


def test_parse_rules() -> None:
    assert parse("allow Read*") == Rule("allow", "Read*")
    assert parse("deny Bash: rm -rf *  in ~/src") == Rule("deny", "Bash: rm -rf *", "~/src")
    # "in" inside the command isn't a folder
    assert parse("allow Bash: grep x in y") == Rule("allow", "Bash: grep x in y")
    for bad in ("maybe Read*", "allow", "allow *", "allow  ** "):
        with pytest.raises(ValueError):
            parse(bad)
    assert parse("deny *") == Rule("deny", "*")  # denying everything is allowed, if odd


def test_decide_deny_wins_and_folders_scope() -> None:
    rules = [parse("allow Bash: *  in /src/api"), parse("deny Bash: rm *"), parse("allow Read*")]
    assert decide(rules, "Bash: npm test", "/src/api/pkg") == rules[0]
    assert decide(rules, "Bash: npm test", "/src/web") is None  # outside the folder
    assert decide(rules, "Bash: rm -rf build", "/src/api") == rules[1]  # deny first
    assert decide(rules, "read: notes.md", "/x") == rules[2]  # case doesn't matter
    assert decide(rules, "Edit: a.py", "/x") is None


class Adapter:
    def __init__(self, reg: Registry) -> None:
        self.reg = reg
        self.answered: list[tuple[str, bool]] = []
        self.broken: set[str] = set()

    async def approve(self, sid: str, choice: int | None, allow: bool) -> str:
        if sid in self.broken:
            raise ControlError("no numbered prompt visible")
        self.answered.append((sid, allow))
        self.reg.sessions[sid].status = "working"
        return "1. Yes" if allow else "3. No"

    def holder(self, sid: str) -> None:
        return None


def build(tmp_path: Path, *rows: tuple[str, str, str, bool]) -> tuple[Server, Adapter]:
    """(id, cwd, pending tool, controllable) sessions, all waiting for approval."""
    reg = Registry(Bus())
    for sid, cwd, tool, ctl in rows:
        reg.upsert(
            Session(
                id=sid,
                source="claude_code",
                cwd=cwd,
                status="awaiting_approval",
                controllable=ctl,
                last_activity=datetime.now(UTC),
                pending_approval=ApprovalRequest(tool=tool, options={"1": "Yes", "3": "No"}),
            )
        )
    ad = Adapter(reg)
    return Server(reg, ad, ad, ad, Store(str(tmp_path / "state.db"))), ad  # type: ignore[arg-type]


def notices(srv: Server, sid: str) -> list[str]:
    return [e.payload["text"] for e in srv.reg.events.get(sid, ()) if e.kind == "notice"]


def test_rules_answer_matching_prompts_once_and_note_it(tmp_path: Path) -> None:
    srv, ad = build(
        tmp_path,
        ("aaaa1111", "/src/api", "Read: a.py", True),
        ("bbbb2222", "/src/api", "Bash: rm -rf /", True),
        ("cccc3333", "/src/api", "Edit: b.py", True),
        ("dddd4444", "/src/api", "Read: c.py", False),  # read-only: never
    )
    srv.rules.rpc({"op": "add", "rule": "allow Read*"})
    srv.rules.rpc({"op": "add", "rule": "deny Bash: rm *"})

    async def run() -> None:
        await srv.rules_tick()
        await srv.rules_tick()  # once per request

    asyncio.run(run())
    assert ad.answered == [("aaaa1111", True), ("bbbb2222", False)]
    assert "auto-approved Read: a.py (1. Yes) by rule `allow Read*`" in notices(srv, "aaaa1111")
    assert notices(srv, "bbbb2222") and "auto-denied" in notices(srv, "bbbb2222")[0]
    assert srv.reg.sessions["cccc3333"].status == "awaiting_approval"  # no rule: yours
    # rules survive a daemon restart
    again = Server(srv.reg, ad, ad, ad, Store(str(tmp_path / "state.db")))  # type: ignore[arg-type]
    assert again.rules.rpc({})["rules"] == ["allow Read*", "deny Bash: rm *"]


def test_rules_never_answer_questions(tmp_path: Path) -> None:
    srv, ad = build(tmp_path, ("aaaa1111", "/x", "AskUserQuestion", True))
    srv.reg.sessions["aaaa1111"].pending_approval.questions = [{"question": "?"}]  # type: ignore[union-attr]
    srv.rules.rpc({"op": "add", "rule": "allow Ask*"})
    asyncio.run(srv.rules_tick())
    assert ad.answered == []


def test_a_rule_that_cannot_answer_says_so(tmp_path: Path) -> None:
    srv, ad = build(tmp_path, ("aaaa1111", "/x", "Read: a", True))
    ad.broken.add("aaaa1111")
    srv.rules.rpc({"op": "add", "rule": "allow Read*"})
    asyncio.run(srv.rules_tick())
    assert "couldn't answer it" in notices(srv, "aaaa1111")[0]


def test_agents_cannot_change_rules_or_bulk_approve(tmp_path: Path) -> None:
    srv, _ = build(tmp_path, ("aaaa1111", "/x", "Read: a", True))
    for method in ("rules", "approve_all", "approve", "unqueue"):
        assert method in CALLER_CHECKED and method not in AGENT_METHODS
        with pytest.raises(RpcError, match="reserved for the human"):
            asyncio.run(srv.dispatch(method, {"op": "add", "rule": "allow *", "_actor": "agent"}))
    assert srv.rules.rules == []


def test_every_control_is_caller_checked() -> None:
    """A method that answers or drives a session must be checked, or an agent's call to it
    would go through as a human's."""
    for method in ("approve", "approve_all", "answer", "interrupt", "close", "keys", "rules"):
        assert server_mod.caller_checked(method)
        assert method not in AGENT_METHODS


def test_approve_all_previews_then_answers_only_what_was_shown(tmp_path: Path) -> None:
    srv, ad = build(
        tmp_path,
        ("aaaa1111", "/src/api", "Read: a.py", True),
        ("bbbb2222", "/src/web", "Read: b.py", True),
        ("cccc3333", "/src/api", "Bash: ls", True),
    )

    async def run() -> None:
        preview = (await srv.dispatch("approve_all", {"tool": "read*", "dry_run": True}))["matched"]
        assert {m["session_id"] for m in preview} == {"aaaa1111", "bbbb2222"} and not ad.answered
        only = [{"id": "aaaa1111", "tool": "Read: a.py"}]
        done = (await srv.dispatch("approve_all", {"tool": "read*", "only": only}))["matched"]
        assert [m["chosen"] for m in done] == ["1. Yes"]
        assert ad.answered == [("aaaa1111", True)]  # bbbb2222 wasn't confirmed
        with pytest.raises(RpcError, match="say which requests"):
            await srv.dispatch("approve_all", {"tool": ""})

    asyncio.run(run())


def test_cli_approve_all_confirms_before_answering(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    pending = [{"session_id": "aaaa1111", "tool": "Read: a.py", "cwd": "/src/api"}]

    def call(method: str, **p: Any) -> Any:
        calls.append(p)
        if p.get("dry_run"):
            return {"matched": pending}
        return {"matched": [pending[0] | {"chosen": "1. Yes"}]}

    monkeypatch.setattr(cli, "call", call)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    r = CliRunner().invoke(cli.app, ["approve", "--all", "--tool", "Read*"], input="n\n")
    assert r.exit_code == 1 and len(calls) == 1  # previewed, then you said no
    r = CliRunner().invoke(cli.app, ["approve", "--all", "--tool", "Read*", "-y"])
    assert r.exit_code == 0 and calls[-1]["only"] == [{"id": "aaaa1111", "tool": "Read: a.py"}]
    r = CliRunner().invoke(cli.app, ["approve", "--all"])
    assert r.exit_code == 1 and "--tool" in r.output  # no blanket approve
