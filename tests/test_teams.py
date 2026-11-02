import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agentctl.daemon.bus import Bus
from agentctl.daemon.history import CommandHistory
from agentctl.daemon.registry import Registry, Store
from agentctl.daemon.teams import TeamError, Teams
from agentctl.mcp import McpServer
from agentctl.models import Session, SessionEvent


def service(tmp_path: Path) -> tuple[Teams, str, str]:
    reg = Registry(Bus())
    for sid in ("coordinator", "worker", "replacement", "outside"):
        reg.upsert(
            Session(
                id=sid,
                source="claude_code",
                status="idle",
                controllable=True,
                last_activity=datetime.now(UTC),
            )
        )
    store = Store(str(tmp_path / "state.db"))
    svc = Teams(reg, CommandHistory(store), store)
    team = svc.create(
        {
            "name": "Release",
            "goal": "Ship the feature",
            "coordinator": "coordinator",
            "permission_ceiling": "delegate",
        }
    )
    svc.edit_member(team, {"session_id": "worker", "role": "Implementation"})
    setup = svc.setup(team, tmp_path)
    token = Path(setup["args"][-1]).read_text().strip()
    svc.authenticate(team, token)
    svc.activate(team)
    return svc, team.id, token


def test_scoped_policy_revocation_and_explicit_proposal(tmp_path: Path) -> None:
    svc, tid, token = service(tmp_path)
    t = svc.resolve(tid)
    params = {"team_id": tid, "team_token": token}
    member = t.workers[0]
    a = svc.rpc("team_assign", params | {"member": member.id, "text": "build it"}, tmp_path)
    assert a["status"] == "proposed"
    with pytest.raises(TeamError, match="not available"):
        svc.rpc(
            "team_assignment_review",
            params | {"assignment": a["id"], "action": "approve"},
            tmp_path,
        )
    svc.review_assignment(t, {"assignment": a["id"], "action": "approve"})
    assert svc.assignments[a["id"]].status == "queued"
    with pytest.raises(TeamError):
        svc.rpc("team_tail", params | {"member": "outside"}, tmp_path)
    svc.edit_member(t, {"member": member.id, "session_id": "replacement", "permission": "observe"})
    assert svc.assignments[a["id"]].status == "cancelled"
    svc.activate(t, resume=True)
    with pytest.raises(TeamError, match="observe"):
        svc.rpc("team_assign", params | {"member": member.id, "text": "build it"}, tmp_path)
    with pytest.raises(TeamError, match="revoked or rebound"):
        svc.rpc("team_result", params | {"assignment": a["id"]}, tmp_path)
    svc.setup(t, tmp_path, rotate=True)
    with pytest.raises(TeamError, match="credential"):
        svc.rpc("team_get", params, tmp_path)


def test_outbox_busy_pause_takeover_and_idempotency(tmp_path: Path) -> None:
    svc, tid, _ = service(tmp_path)
    t = svc.resolve(tid)
    m = t.workers[0]
    svc.edit_member(t, {"member": m.id, "permission": "delegate"})
    svc.activate(t, resume=True)
    args = {"member": m.id, "text": "implement", "request_id": "stable-request"}
    a = svc.assign(t, args, scoped=True)
    assert svc.assign(t, args, scoped=True).id == a.id
    with pytest.raises(TeamError, match="different assignment"):
        svc.assign(t, args | {"text": "changed"}, scoped=True)
    calls = []

    async def send(method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert method == "agent_message" and params["new_chat"]
        calls.append(params)
        svc.reg.upsert(
            svc.reg.sessions["worker"].model_copy(update={"id": "agent-worker", "status": "idle"})
        )
        entry = svc.history.begin("agent-worker", params["text"], params["command_id"])
        svc.history.update(entry, outcome="sent")
        return {"session_id": "agent-worker", "delivery_status": "sent"}

    async def run() -> None:
        svc.reg.sessions["worker"].status = "working"
        svc.pause(t)
        await svc.tick(send, {})
        assert not calls
        svc.activate(t, resume=True)
        svc.takeover("worker")
        await svc.tick(send, {})
        assert not calls
        svc.rpc("team_release", {"team_id": tid, "member": m.id}, tmp_path)
        await svc.tick(send, {})
        await svc.tick(send, {})
        assert len(calls) == 1 and a.status == "sent"
        now = time.time() + 1
        for role, text in [("user", "implement"), ("assistant", "complete\n" + "x" * 9000)]:
            svc.reg.add_event(
                SessionEvent(
                    session_id="agent-worker",
                    kind="message",
                    ts=datetime.fromtimestamp(now, UTC),
                    payload={"role": role, "text": text},
                )
            )
            now += 1
        await svc.tick(send, {})
        assert a.status == "reported_complete" and a.reply.endswith("x" * 9000)
        svc.review_assignment(t, {"assignment": a.id, "action": "accept"})
        assert a.status == "accepted"

    asyncio.run(run())


def test_restart_and_unknown_delivery_never_replay(tmp_path: Path) -> None:
    svc, tid, _ = service(tmp_path)
    t = svc.resolve(tid)
    a = svc.assign(t, {"member": t.workers[0].id, "text": "do it"}, scoped=False)
    calls = []

    async def fail(method: str, params: dict[str, Any]) -> None:
        calls.append(params)
        raise ConnectionError("lost after input")

    asyncio.run(svc.tick(fail, {}))
    asyncio.run(svc.tick(fail, {}))
    assert a.status == "unknown" and len(calls) == 1
    # A crash in the sending window is uncertain as well.
    a.status = "sending"
    with svc.db:
        svc._save("team_assignments", a)
    store = Store(str(tmp_path / "state.db"))
    restored = Teams(svc.reg, CommandHistory(store), store)
    assert restored.run(tid).state == "paused"
    assert restored.assignments[a.id].status == "unknown"
    asyncio.run(restored.tick(fail, {}))
    assert len(calls) == 1


def test_writer_exclusivity_and_coordinator_lifecycle(tmp_path: Path) -> None:
    svc, tid, _ = service(tmp_path)
    t = svc.resolve(tid)
    svc.edit_member(t, {"member": t.workers[0].id, "permission": "delegate"})
    svc.activate(t, resume=True)
    other = svc.create(
        {
            "name": "Other",
            "goal": "Test",
            "coordinator": "outside",
            "permission_ceiling": "delegate",
        }
    )
    svc.edit_member(other, {"session_id": "worker", "role": "shared", "permission": "delegate"})
    svc.setup(other, tmp_path)
    token = (tmp_path / "teams" / f"{other.id}.token").read_text().strip()
    svc.authenticate(other, token)
    with pytest.raises(TeamError, match="already has"):
        svc.activate(other)
    svc.reg.sessions["coordinator"].status = "ended"
    with pytest.raises(TeamError, match="Coordinator is unavailable"):
        svc.assign(t, {"member": t.workers[0].id, "text": "task"}, scoped=True)


def test_scoped_mcp_has_no_global_tools_even_with_allow_write(tmp_path: Path) -> None:
    svc, tid, token = service(tmp_path)
    calls = []

    def call(method: str, **params: Any) -> Any:
        calls.append((method, params))
        return svc.rpc(method, params, tmp_path)

    mcp = McpServer(allow_write=True, call=call, team_id=tid, team_token=token)
    reply = mcp.handle({"id": 1, "method": "initialize"})
    assert "result" in reply
    assert calls[-1][0] == "team_get"
    assert set(mcp.tools) == {
        "get_team",
        "propose_team_setup",
        "read_worker",
        "assign_worker",
        "assignment_result",
        "wait_for_assignment",
    }
    assert mcp.call_tool({"name": "send_message", "arguments": {"session": "outside"}})["isError"]
    assigned = mcp.call_tool(
        {
            "name": "assign_worker",
            "arguments": {
                "member": svc.resolve(tid).workers[0].id,
                "text": "task",
                "request_id": "1",
            },
        }
    )
    assert json.loads(assigned["content"][0]["text"])["status"] == "proposed"
    assert calls[-1][1]["team_token"] == token
    assert "team_token" not in assigned["content"][0]["text"]


def test_server_routes_team_outbox_and_human_queue_takes_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentctl import launcher
    from agentctl.adapters.base import ControlError
    from agentctl.daemon.server import RpcError, Server

    class Agent:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def verified(self, sid: str) -> tuple[str, str]:
            raise ControlError("no terminal")

        async def send(self, sid: str, text: str) -> None:
            self.sent.append(text)

        def register_pane(self, sid: str, sock: str, pane: str) -> None:
            pass

    async def run() -> None:
        svc, tid, token = service(tmp_path)
        agent = Agent()
        server = Server(svc.reg, agent, agent, agent)  # type: ignore[arg-type]
        server.teams, server.history = svc, svc.history
        svc.reg.upsert(svc.reg.sessions["worker"].model_copy(update={"id": "agent-worker"}))
        monkeypatch.setattr(
            launcher,
            "launch",
            lambda *a: {"session_id": "agent-worker", "socket": "/tmp/test", "pane": "%test"},
        )
        t = svc.resolve(tid)
        a = svc.assign(t, {"member": t.workers[0].id, "text": "delegated task"}, False)
        await svc.tick(server.dispatch, {})
        assert a.status == "waiting" and not agent.sent
        assert a.delivery_session_id == "agent-worker"
        await server.deliver_tick(asyncio.Queue())
        await svc.tick(server.dispatch, {})
        assert a.status == "sent" and agent.sent == ["delegated task"]
        assert not t.workers[0].held
        assert svc.history.entries[a.id]["outcome"] == "sent"
        with pytest.raises(RpcError, match="global"):
            await server.dispatch("send", {"id": "outside", "text": "bad", "team_token": token})
        with pytest.raises(TeamError, match="not available"):
            await server.dispatch("teams_list", {"team_id": tid, "team_token": token})
        await server.dispatch("queue", {"id": "worker", "text": "human followup"})
        assert t.workers[0].held
        assert server.queued["worker"] == ["human followup"]

    asyncio.run(run())


@pytest.mark.parametrize("case", ["ended", "superseded", "unconfirmed"])
def test_completion_requires_a_reply_to_the_assignment(tmp_path: Path, case: str) -> None:
    svc, tid, _ = service(tmp_path)
    t = svc.resolve(tid)
    a = svc.assign(t, {"member": t.workers[0].id, "text": "task"}, False)

    async def send(method: str, params: dict[str, Any]) -> dict[str, str]:
        svc.reg.upsert(svc.reg.sessions["worker"].model_copy(update={"id": "agent-worker"}))
        e = svc.history.begin("agent-worker", "task", a.id)
        svc.history.update(e, outcome="sent")
        return {"session_id": "agent-worker", "delivery_status": "sent"}

    async def run() -> None:
        await svc.tick(send, {})
        now = time.time() + 1
        if case != "unconfirmed":
            svc.reg.add_event(
                SessionEvent(
                    session_id="agent-worker",
                    kind="message",
                    ts=datetime.fromtimestamp(now, UTC),
                    payload={"role": "user", "text": "task"},
                )
            )
        if case == "superseded":
            svc.reg.add_event(
                SessionEvent(
                    session_id="agent-worker",
                    kind="message",
                    ts=datetime.fromtimestamp(now + 1, UTC),
                    payload={"role": "user", "text": "unrelated human request"},
                )
            )
            svc.reg.add_event(
                SessionEvent(
                    session_id="agent-worker",
                    kind="message",
                    ts=datetime.fromtimestamp(now + 2, UTC),
                    payload={"role": "assistant", "text": "answer to the unrelated request"},
                )
            )
        else:
            svc.reg.sessions["agent-worker"].status = "ended"
        await svc.tick(send, {})
        assert a.reply == ""
        assert a.status == ("failed" if case == "ended" else "unknown")

    asyncio.run(run())


def test_pending_setup_waits_for_verified_session_binding(tmp_path: Path) -> None:
    svc, tid, _ = service(tmp_path)
    t = svc.resolve(tid)
    svc.reg.upsert(svc.reg.sessions["worker"].model_copy(update={"id": "new-placeholder"}))
    svc.edit_member(t, {"member": t.workers[0].id, "session_id": "new-placeholder"})
    svc.activate(t, resume=True)
    a = svc.assign(t, {"member": t.workers[0].id, "text": "task"}, False)
    calls = []

    async def send(method: str, params: dict[str, Any]) -> dict[str, str]:
        calls.append(params)
        return {"session_id": "fresh", "delivery_status": "queued"}

    async def run() -> None:
        await svc.tick(send, {})
        assert not calls and a.status == "queued"
        svc.migrate_session("new-placeholder", "replacement")
        await svc.tick(send, {})
        assert calls[0]["id"] == "replacement"
        assert a.session_id == "replacement"

    asyncio.run(run())
