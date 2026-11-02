import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentctl.adapters.base import ControlError
from agentctl.daemon.bus import Bus
from agentctl.daemon.history import CommandHistory
from agentctl.daemon.registry import Registry, Store
from agentctl.daemon.server import RpcError, Server
from agentctl.messages import message_key
from agentctl.models import Session, SessionEvent


def observation(text: str, ts: float) -> SessionEvent:
    return SessionEvent(
        session_id="s",
        kind="message",
        ts=datetime.fromtimestamp(ts, UTC),
        payload={"role": "user", "text": text},
    )


def test_history_survives_restart_and_marks_interrupted_delivery_unknown(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "state.db"))
    history = CommandHistory(store)
    sent = history.begin("s", "hello")
    history.update(sent, outcome="sent")
    pending = history.begin("s", "possibly sent")
    store.db.close()
    restored = CommandHistory(Store(str(tmp_path / "state.db")))
    assert restored.entries[sent["id"]]["outcome"] == "sent"
    assert restored.entries[pending["id"]]["outcome"] == "unknown"


def test_receipts_require_later_distinct_observations_and_match_the_whole_text() -> None:
    history = CommandHistory()
    first = history.begin("s", "continue")
    history.update(first, outcome="sent")
    child = observation("continue", first["created"] + 1)
    child.payload["agent_id"] = "child"
    assert "settled" not in history.listing("s", [child])[0]
    assert "settled" not in history.listing("s", [observation("continue", first["created"] - 1)])[0]
    received = observation("continue", first["created"] + 1)
    assert history.listing("s", [received])[0]["settled"] == "arrived"
    second = history.begin("s", "continue")
    history.update(second, outcome="sent")
    assert "settled" not in history.listing("s", [received])[1]
    # Old receipt has left the event buffer; the first command must not consume the new one.
    later = observation("continue", second["created"] + 2)
    assert history.listing("s", [later])[1]["settled"] == "arrived"
    assert message_key("x" * 500 + "a") != message_key("x" * 500 + "b")
    assert message_key("fix\tit now") == message_key("fix  it   now")


def test_server_records_queue_delivery_cancellation_and_refuses_duplicate_commands(
    tmp_path: Path,
) -> None:
    class Agent:
        sent: list[str] = []

        async def verified(self, sid: str) -> tuple[str, str]:
            raise ControlError("no pane")

        async def send(self, sid: str, text: str) -> None:
            self.sent.append(text)

    async def run() -> None:
        registry = Registry(Bus())
        session = registry.upsert(
            Session(
                id="s",
                source="claude_code",
                status="working",
                controllable=True,
                last_activity=datetime.now(UTC),
            )
        )
        agent = Agent()
        server = Server(registry, agent, agent, agent, Store(str(tmp_path / "state.db")))  # type: ignore[arg-type]
        result = await server.dispatch("send", {"id": "s", "text": "go", "command_id": "one"})
        assert result["queued"] == ["go"]
        assert (await server.dispatch("history", {"id": "s"}))[0]["outcome"] == "queued"
        with pytest.raises(RpcError, match="already recorded"):
            await server.dispatch("send", {"id": "s", "text": "go", "command_id": "one"})
        session.status = "awaiting_input"
        await server.deliver_tick(asyncio.Queue())
        assert agent.sent == ["go"]
        assert (await server.dispatch("history", {"id": "s"}))[0]["outcome"] == "sent"
        await server.dispatch("queue", {"id": "s", "text": "cancel me"})
        await server.dispatch("unqueue", {"id": "s", "index": 0})
        assert (await server.dispatch("history", {"id": "s"}))[1]["outcome"] == "cancelled"

    asyncio.run(run())
