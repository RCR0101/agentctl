from typing import Any

import pytest

from agentctl import cli


def test_restart_waits_for_old_daemon_to_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    pings = 0

    def call(method: str, **params: Any) -> dict[str, Any]:
        nonlocal pings
        calls.append((method, params))
        if method == "shutdown":
            return {"ok": True}
        pings += 1
        if pings == 3:
            raise cli.DaemonError("not running")
        return {"pid": 1 if pings <= 2 else 2}

    monkeypatch.setattr(cli, "call", call)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    assert cli.restart_daemon() == {"pid": 2}
    assert calls[1] == ("shutdown", {"autostart": False, "force": False})
    assert calls[-1] == ("ping", {})


def test_restart_preserves_the_running_turn_safeguard(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def call(method: str, **params: Any) -> dict[str, Any]:
        calls.append(method)
        if method == "shutdown":
            assert params["force"] is False
            raise cli.DaemonError("Codex turn running; stopping would abort it")
        return {"pid": 1}

    monkeypatch.setattr(cli, "call", call)
    with pytest.raises(cli.DaemonError, match="turn running"):
        cli.restart_daemon()
    assert calls == ["ping", "shutdown"]
