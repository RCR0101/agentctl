"""Controlled terminal protocol fixture: no real agent, trust grant, or live daemon."""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from test_agent_chats import build

from agentctl.daemon.registry import Store
from agentctl.daemon.server import Server

TERMINAL = r"""
import json, sys
state = "trust"
submitted = []
for line in sys.stdin:
    command = json.loads(line)
    action = command["action"]
    if action == "human_trust":
        state = "prompt"
    elif action == "paste_only":
        state = "pasted"
    elif action == "send":
        if state == "prompt":
            submitted.append(command["text"])
            state = "working"
    screens = {"trust": "Do you trust this folder?", "prompt": "Ask Codex",
               "pasted": "work (not submitted)", "working": "Working"}
    print(json.dumps({"screen": screens[state], "submitted": submitted}), flush=True)
"""


@pytest.mark.parametrize("mode", ["trusted", "cancel", "paste_only", "terminal_loss", "restart"])
def test_controlled_startup_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    from agentctl import tmux

    server, agent, _ = build(tmp_path, monkeypatch)
    sid = "new-controlled"
    server.reg.upsert(
        server.reg.sessions["protected-user"].model_copy(
            update={
                "id": sid,
                "source": "codex",
                "status": "awaiting_input",
                "tmux_pane": "%fixture",
            }
        )
    )
    server.pending[sid] = ("/fixture", "%fixture")
    server.agent_chats.created(sid)
    receipt = server.history.begin(sid, "work", "controlled-work")
    server.history.update(receipt, outcome="queued", sender="agent")
    server.set_queued(sid, ["work"])

    async def run() -> None:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            "-c",
            TERMINAL,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )

        async def command(action: str, **params: Any) -> dict[str, Any]:
            assert proc.stdin and proc.stdout
            proc.stdin.write((json.dumps({"action": action, **params}) + "\n").encode())
            await proc.stdin.drain()
            line = await proc.stdout.readline()
            if not line:
                raise RuntimeError("Controlled terminal lost; inspect before retrying")
            return json.loads(line)

        async def screen(sock: str, pane: str) -> str:
            return (await command("screen"))["screen"]

        attempts = []

        async def send(sock: str, pane: str, text: str, **kwargs: Any) -> None:
            attempts.append(text)
            if mode == "paste_only":
                response = await command("paste_only")
                assert response["submitted"] == []
                raise RuntimeError("Pasted but submission unconfirmed; inspect terminal")
            if mode == "terminal_loss":
                proc.terminate()
                await proc.wait()
                raise RuntimeError("Terminal lost during submission; delivery uncertain")
            response = await command("send", text=text)
            assert response["submitted"] == ["work"]

        monkeypatch.setattr("agentctl.daemon.server.screen", screen)
        monkeypatch.setattr(tmux, "send_text", send)
        try:
            await server.deliver_tick(asyncio.Queue())
            assert not attempts and receipt["outcome"] == "queued"
            assert "human setup" in server.queue_error[sid]
            # Only the test's simulated human changes trust; the delivery worker never does.
            await command("human_trust")
            if mode == "cancel":
                await server.dispatch("unqueue", {"id": sid})
                await server.deliver_tick(asyncio.Queue())
                assert not attempts and receipt["outcome"] == "cancelled"
                return
            await server.deliver_tick(asyncio.Queue())
            if mode in ("paste_only", "terminal_loss"):
                assert receipt["outcome"] == "unknown" and server.queue_error[sid]
                await server.deliver_tick(asyncio.Queue())
                assert attempts == ["work"]  # no replay after uncertainty
            else:
                assert receipt["outcome"] == "sent" and attempts == ["work"]
                assert "settled" not in receipt  # terminal submission is not transcript arrival
            if mode == "restart":
                # Crash after native input began: persisted uncertainty must hold on restart.
                server.history.update(receipt, outcome="sending")
                server.set_queued(sid, ["work"])
                restored = Server(
                    server.reg, agent, agent, agent, Store(str(tmp_path / "state.db"))
                )
                await restored.deliver_tick(asyncio.Queue())
                assert restored.history.entries[receipt["id"]]["outcome"] == "unknown"
                assert attempts == ["work"] and "uncertain" in restored.queue_error[sid]
        finally:
            if proc.returncode is None:
                proc.terminate()
                await proc.wait()

    asyncio.run(run())
