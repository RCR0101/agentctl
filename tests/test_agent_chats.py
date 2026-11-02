import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agentctl import cli, launcher, mcp
from agentctl.adapters.base import ControlError
from agentctl.daemon.bus import Bus
from agentctl.daemon.registry import Registry, Store
from agentctl.daemon.server import RpcError, Server
from agentctl.models import Session


def build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Server, Any, list[Any]]:
    reg = Registry(Bus())
    reg.upsert(
        Session(
            id="protected-user",
            source="claude_code",
            cwd="/project",
            title="My chat",
            status="working",
            controllable=True,
            last_activity=datetime.now(UTC),
        )
    )
    launches: list[Any] = []

    class Agent:
        def __init__(self) -> None:
            self.sent: list[tuple[str, str]] = []

        def register_pane(self, sid: str, sock: str, pane: str) -> None:
            reg.upsert(
                reg.sessions["protected-user"].model_copy(
                    update={"id": sid, "status": "idle", "title": "Agent chat", "tmux_pane": pane}
                )
            )

        async def verified(self, sid: str) -> tuple[str, str]:
            raise ControlError("no terminal")

        async def send(self, sid: str, text: str) -> None:
            self.sent.append((sid, text))

        def holder(self, sid: str) -> None:
            return None

    def launch(*args: Any) -> dict[str, Any]:
        launches.append(args)
        return {
            "session_id": f"agent-{len(launches)}",
            "socket": "/fake",
            "pane": f"%{len(launches)}",
        }

    monkeypatch.setattr(launcher, "launch", launch)
    agent = Agent()
    store = Store(str(tmp_path / "state.db"))
    server = Server(reg, agent, agent, agent, store)  # type: ignore[arg-type]
    return server, agent, launches


def test_agent_messages_default_to_fresh_chats_and_do_not_touch_user_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, agent, launches = build(tmp_path, monkeypatch)

    async def run() -> None:
        original = server.reg.sessions["protected-user"].model_dump()
        first = await server.dispatch("agent_message", {"id": "protected-user", "text": "do work"})
        second = await server.dispatch(
            "agent_message", {"id": "protected-user", "text": "other work"}
        )
        assert first["session_id"] != second["session_id"]
        assert len(launches) == 2 and launches[0][0:2] == ("claude", "/project")
        assert not agent.sent
        await server.deliver_tick(asyncio.Queue())
        assert agent.sent == [("agent-1", "do work"), ("agent-2", "other work")]
        assert server.reg.sessions["protected-user"].model_dump() == original
        assert server.dump(server.reg.sessions["protected-user"])["agent_writable"] is False
        assert server.dump(server.reg.sessions["agent-1"])["agent_writable"] is True
        assert all(args[-1] is None for args in launches)  # never resumes a user thread

    asyncio.run(run())


def test_only_agent_created_chats_can_be_continued_and_humans_still_can_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, agent, launches = build(tmp_path, monkeypatch)

    async def run() -> None:
        with pytest.raises(ValueError, match="user-initiated"):
            await server.dispatch(
                "agent_message", {"id": "protected-user", "text": "no", "new_chat": False}
            )
        with pytest.raises(ValueError, match="user-initiated"):
            await server.dispatch("send", {"id": "protected-user", "text": "no", "_actor": "agent"})
        with pytest.raises(ValueError, match="user-initiated"):
            await server.dispatch(
                "queue", {"id": "protected-user", "text": "no", "_actor": "agent"}
            )
        with pytest.raises(RpcError):
            await server.dispatch(
                "agent_launch", {"agent": "claude", "cwd": "/project", "resume": "protected-user"}
            )
        assert not launches and not server.queued
        created = await server.dispatch("agent_launch", {"agent": "claude", "cwd": "/project"})
        result = await server.dispatch(
            "agent_message", {"id": created["session_id"], "text": "continue", "new_chat": False}
        )
        assert result["session_id"] == created["session_id"] and len(launches) == 1
        await server.deliver_tick(asyncio.Queue())
        assert agent.sent == [(created["session_id"], "continue")]
        await server.dispatch("send", {"id": "protected-user", "text": "human", "now": True})
        assert agent.sent[-1] == ("protected-user", "human")

    asyncio.run(run())


def test_provenance_receipts_and_unknown_delivery_survive_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, agent, launches = build(tmp_path, monkeypatch)

    async def run() -> None:
        params = {"id": "protected-user", "text": "once", "command_id": "request-1"}
        first = await server.dispatch("agent_message", params)
        again = await server.dispatch("agent_message", params)
        assert first["session_id"] == again["session_id"] and len(launches) == 1
        receipt = server.history.entries["request-1"]
        server.history.update(receipt, outcome="sending")  # crash during native input
        restored = Server(server.reg, agent, agent, agent, Store(str(tmp_path / "state.db")))  # type: ignore[arg-type]
        assert restored.history.entries["request-1"]["outcome"] == "unknown"
        restored.agent_chats.require(first["session_id"])
        with pytest.raises(ValueError):
            restored.agent_chats.require("protected-user")
        await restored.deliver_tick(asyncio.Queue())
        assert not agent.sent
        assert "uncertain" in restored.queue_error[first["session_id"]]
        repeated = await restored.dispatch("agent_message", params)
        assert repeated["delivery_status"] == "unknown" and len(launches) == 1

    asyncio.run(run())


@pytest.mark.parametrize("action", ["pause", "end", "takeover", "edit"])
def test_team_pending_delivery_respects_human_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    server, agent, _ = build(tmp_path, monkeypatch)
    svc = server.teams
    coordinator = server.reg.sessions["protected-user"].model_copy(
        update={"id": "coordinator", "status": "idle"}
    )
    server.reg.upsert(coordinator)
    t = svc.create({"name": "Team", "goal": "Work", "coordinator": "coordinator"})
    svc.edit_member(t, {"session_id": "protected-user", "role": "Worker"})
    setup = svc.setup(t, tmp_path)
    svc.authenticate(t, Path(setup["args"][-1]).read_text().strip())
    svc.activate(t)
    a = svc.assign(t, {"member": t.workers[0].id, "text": "team work"}, False)

    async def run() -> None:
        await svc.tick(server.dispatch, {})
        assert a.status == "waiting" and a.delivery_session_id == "agent-1"
        if action == "pause":
            svc.pause(t)
        elif action == "end":
            svc.pause(t, end=True)
        elif action == "takeover":
            svc.takeover(a.delivery_session_id)
        else:
            svc.edit_member(t, {"member": t.workers[0].id, "permission": "observe"})
        await server.deliver_tick(asyncio.Queue())
        assert not agent.sent
        if action == "pause":
            svc.activate(t, resume=True)
            await server.deliver_tick(asyncio.Queue())
            assert agent.sent == [("agent-1", "team work")]

    asyncio.run(run())


def test_mcp_message_tools_and_start_session_use_agent_routes() -> None:
    calls = []

    def call(method: str, **params: Any) -> dict[str, Any]:
        calls.append((method, params))
        return {
            "session_id": "fresh",
            "new_chat": params.get("new_chat", True),
            "delivery_status": "queued",
        }

    srv = mcp.McpServer(allow_write=True, call=call)
    for name in ("send_message", "queue_followup"):
        result = srv.call_tool({"name": name, "arguments": {"session": "user", "text": "work"}})
        assert not result["isError"]
        assert calls[-1][0] == "agent_message" and calls[-1][1]["new_chat"] is True
    srv.call_tool(
        {
            "name": "start_session",
            "arguments": {"agent": "claude", "folder": "/project", "first_message": "hello"},
        }
    )
    assert calls[-2][0] == "agent_launch"
    assert calls[-1][0] == "agent_message" and calls[-1][1]["new_chat"] is False


def test_managed_agent_cli_messages_are_routed_as_agent_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = []

    class Connection:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def sendall(self, data: bytes) -> None:
            messages.append(json.loads(data))

        def makefile(self, mode: str) -> Any:
            import io

            return io.BytesIO(b'{"result": {}}\n')

    monkeypatch.setenv("AGENTCTL_CALLER", "agent")
    monkeypatch.setattr(cli, "_connect", lambda _: Connection())
    cli.call("send", id="user", text="work")
    cli.call("queue", id="user", text="followup")
    assert all(
        m["method"] == "agent_message" and m["params"]["_actor"] == "agent" for m in messages
    )


def test_legacy_mcp_and_agent_shell_callers_are_identified_but_dashboard_is_human() -> None:
    from agentctl.daemon.agent_chats import agent_process

    rows = {
        10: (1, "/bin/zsh"),
        11: (10, "python -m agentctl mcp --allow-write"),
        12: (1, "/bin/claude"),
        13: (12, "/bin/sh -c agentctl"),
        14: (13, "agentctl send user work"),
        15: (12, "python -m agentctl watch"),
        16: (10, "agentctl send user human"),
        17: (12, "agentctl teams"),
    }
    assert agent_process(11, rows, set())
    assert agent_process(14, rows, {12})
    assert not agent_process(15, rows, {12})
    assert not agent_process(16, rows, {12})
    assert not agent_process(17, rows, {12})


def test_legacy_unmarked_agent_send_is_routed_by_socket_caller_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentctl.daemon import server as module

    server, agent, launches = build(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "peer_uid", lambda _: module.os.getuid())
    monkeypatch.setattr(module, "peer_pid", lambda _: 42)

    async def identify(pid: int, holders: set[int]) -> bool:
        return True

    monkeypatch.setattr(module, "identify_agent", identify)

    class Writer:
        replies: list[dict[str, Any]] = []

        def get_extra_info(self, name: str) -> Any:
            return self

        def fileno(self) -> int:
            return 0

        def write(self, data: bytes) -> None:
            self.replies.append(json.loads(data))

        async def drain(self) -> None:
            pass

        def close(self) -> None:
            pass

    async def run() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(
            json.dumps(
                {
                    "id": 1,
                    "method": "send",
                    "params": {"id": "protected-user", "text": "legacy work", "_actor": "human"},
                }
            ).encode()
            + b"\n"
        )
        reader.feed_eof()
        writer = Writer()
        await server.handle(reader, writer)  # type: ignore[arg-type]
        assert writer.replies[0]["result"]["session_id"] == "agent-1"
        assert server.queued == {"agent-1": ["legacy work"]}
        assert len(launches) == 1 and not agent.sent

    asyncio.run(run())


def test_placeholder_setup_prompts_never_receive_agent_message_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentctl import tmux

    server, _, _ = build(tmp_path, monkeypatch)
    sid = "new-placeholder"
    server.reg.upsert(
        server.reg.sessions["protected-user"].model_copy(
            update={"id": sid, "source": "codex", "status": "awaiting_input", "tmux_pane": "%new"}
        )
    )
    server.pending[sid] = ("/fake", "%new")
    server.agent_chats.created(sid)
    receipt = server.history.begin(sid, "1")
    server.history.update(receipt, outcome="queued", sender="agent")
    server.set_queued(sid, ["1"])
    state = {"screen": "Do you trust this folder?"}
    sent = []

    async def screen(sock: str, pane: str) -> str:
        return state["screen"]

    async def send(sock: str, pane: str, text: str, **kwargs: Any) -> None:
        sent.append((pane, text))

    monkeypatch.setattr("agentctl.daemon.server.screen", screen)
    monkeypatch.setattr(tmux, "send_text", send)

    async def run() -> None:
        await server.deliver_tick(asyncio.Queue())
        assert not sent and receipt["outcome"] == "queued"
        assert "human setup" in receipt["error"]
        assert server.dump(server.reg.sessions[sid])["agent_delivery"]["error"] == receipt["error"]
        state["screen"] = "Ask Codex"
        await server.deliver_tick(asyncio.Queue())
        assert sent == [("%new", "1")] and receipt["outcome"] == "sent"
        assert not receipt["error"] and sid not in server.queue_error

    asyncio.run(run())


def test_unix_socket_peer_pid_is_observed_from_the_os() -> None:
    import os
    import socket

    from agentctl.daemon.server import peer_pid

    left, right = socket.socketpair()
    try:
        assert peer_pid(left.fileno()) == os.getpid()
        assert peer_pid(right.fileno()) == os.getpid()
    finally:
        left.close()
        right.close()


def test_verified_alias_migrates_agent_provenance_and_delivery_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _, _ = build(tmp_path, monkeypatch)
    old, new = "new-placeholder", "real-agent-chat"
    for sid in (old, new):
        server.reg.upsert(
            server.reg.sessions["protected-user"].model_copy(
                update={"id": sid, "tmux_pane": "%fresh", "status": "idle"}
            )
        )
    server.pending[old] = ("/fake", "%fresh")
    server.agent_chats.created(old, "protected-user")
    receipt = server.history.begin(old, "work")
    server.history.update(receipt, sender="agent", outcome="queued")
    server.set_queued(old, ["work"])

    async def run() -> None:
        await server.sweep()
        server.agent_chats.require(new)
        with pytest.raises(ValueError):
            server.agent_chats.require("protected-user")
        await server.deliver_tick(asyncio.Queue())
        assert server.queued == {new: ["work"]}
        assert receipt["session_id"] == new

    asyncio.run(run())


def test_legacy_queues_with_no_verified_sender_are_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, agent, _ = build(tmp_path, monkeypatch)
    server.reg.sessions["protected-user"].status = "idle"
    server.store.set_queued("protected-user", ["old unverified work"])
    restored = Server(server.reg, agent, agent, agent, Store(str(tmp_path / "state.db")))  # type: ignore[arg-type]

    async def run() -> None:
        await restored.deliver_tick(asyncio.Queue())
        assert not agent.sent
        assert "verified sender" in restored.queue_error["protected-user"]
        await restored.dispatch("unqueue", {"id": "protected-user"})
        await restored.dispatch("queue", {"id": "protected-user", "text": "human authorized"})
        await restored.deliver_tick(asyncio.Queue())
        assert agent.sent == [("protected-user", "human authorized")]

    asyncio.run(run())


@pytest.mark.parametrize("model", ["", "gpt-terra"])
def test_fresh_message_model_arguments_and_durable_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    server, _, launches = build(tmp_path, monkeypatch)

    async def run() -> None:
        params = {
            "id": "protected-user",
            "text": "work",
            "model": model,
            "command_id": "model-request",
        }
        result = await server.dispatch("agent_message", params)
        assert launches[0][3] == ([f"--model={model}"] if model else [])
        assert launches[0][-1] is None
        assert result["requested_model"] == (model or None)
        assert result["model_selection"] == ("requested" if model else "agent_default")
        assert "effective_model" not in result
        receipt = server.history.entries["model-request"]
        assert receipt["requested_model"] == (model or None)
        assert (await server.dispatch("agent_message", params))["requested_model"] == (
            model or None
        )
        with pytest.raises(RpcError, match="another agent message"):
            await server.dispatch("agent_message", params | {"model": "different"})
        with pytest.raises(RpcError, match="plain model"):
            await server.dispatch("agent_message", params | {"model": "--unsafe"})
        assert len(launches) == 1

    asyncio.run(run())


def test_team_model_default_override_and_frozen_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _, launches = build(tmp_path, monkeypatch)
    svc = server.teams
    server.reg.upsert(
        server.reg.sessions["protected-user"].model_copy(
            update={"id": "coordinator", "status": "idle"}
        )
    )
    team = svc.create({"name": "Team", "goal": "Work", "coordinator": "coordinator"})
    svc.edit_member(team, {"session_id": "protected-user", "role": "Worker", "model": "gpt-terra"})
    setup = svc.setup(team, tmp_path)
    svc.authenticate(team, Path(setup["args"][-1]).read_text().strip())
    svc.activate(team)
    member = team.workers[0]
    assignment = svc.assign(team, {"member": member.id, "text": "work", "request_id": "r"}, False)
    assert assignment.requested_model == "gpt-terra"
    assert assignment.model_selection == "requested"
    defaulted = svc.assign(team, {"member": member.id, "text": "other", "model": ""}, False)
    assert defaulted.model_selection == "agent_default" and defaulted.requested_model == ""
    defaulted.status = "cancelled"
    with pytest.raises(ValueError):
        svc.assign(team, {"member": member.id, "text": "bad", "model": "--unsafe"}, False)
    with pytest.raises(ValueError, match="different assignment"):
        svc.assign(
            team,
            {"member": member.id, "text": "work", "request_id": "r", "model": "different"},
            False,
        )
    # Dispatch uses the frozen assignment request, not a later mutable default.
    member.model = "changed"
    asyncio.run(svc.tick(server.dispatch, {}))
    assert launches[0][3] == ["--model=gpt-terra"]
    assert svc.assignment(team, assignment.id).delivery_session_id == "agent-1"


def test_launch_receipt_and_existing_chat_cannot_claim_model_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _, launches = build(tmp_path, monkeypatch)

    async def run() -> None:
        launched = await server.dispatch(
            "agent_launch", {"agent": "claude", "cwd": "/project", "model": "gpt-terra"}
        )
        assert launched["requested_model"] == "gpt-terra"
        assert launched["model_selection"] == "requested"
        with pytest.raises(RpcError, match="only be requested for a new chat"):
            await server.dispatch(
                "agent_message",
                {
                    "id": launched["session_id"],
                    "new_chat": False,
                    "text": "continue",
                    "model": "different",
                },
            )
        continued = await server.dispatch(
            "agent_message", {"id": launched["session_id"], "new_chat": False, "text": "continue"}
        )
        assert continued["model_selection"] == "existing_chat"
        assert continued["requested_model"] is None and len(launches) == 1

    asyncio.run(run())
