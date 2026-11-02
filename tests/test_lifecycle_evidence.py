"""Observation provenance never upgrades missing liveness to a process-exit proof."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agentctl.adapters.claude_code import ClaudeCodeAdapter
from agentctl.adapters.codex import CodexAdapter
from agentctl.adapters.opencode import OpenCodeAdapter
from agentctl.daemon.bus import Bus
from agentctl.daemon.registry import Registry
from agentctl.models import Session


@pytest.mark.parametrize("source", ["claude_code", "codex", "opencode"])
def test_running_disconnected_and_inferred_ended_explain_actual_signals(
    tmp_path: Path, source: str
) -> None:
    reg = Registry(Bus())
    session = Session(id="s", source=source, last_activity=datetime.now(UTC) - timedelta(days=2))
    reg.upsert(session)
    original_activity = session.last_activity
    assert session.status_reason is None and not session.status_evidence
    if source == "claude_code":
        adapter = ClaudeCodeAdapter(reg, root=tmp_path)
        adapter.live["s"] = {"pid": 123, "status": "busy"}

        def refresh():
            return adapter.refresh_status("s")

        def disconnect():
            return adapter.live.clear()
    elif source == "codex":
        adapter = CodexAdapter(reg, root=tmp_path, exe="fake")
        adapter.loaded["s"] = {"status": {"type": "active", "activeFlags": []}}

        def refresh():
            return adapter.sync("s")

        def disconnect():
            return adapter.loaded.clear()
    else:
        adapter = OpenCodeAdapter(reg, db=tmp_path / "unused.db")
        adapter.live["s"] = {"pid": 123, "busy": True, "pane": "%fixture"}

        def refresh():
            return adapter.sync("s")

        def disconnect():
            return adapter.live.clear()

    refresh()
    assert session.status == "working"
    assert (
        session.status_reason
        and "observed" in session.status_reason
        or session.status_reason == "app_server_active"
    )
    assert session.status_observed_at
    disconnect()
    refresh()
    assert session.status == "ended"
    assert session.status_reason == "no_recent_live_evidence_inferred_ended"
    assert session.status_evidence["process_exit_confirmed"] is False
    observed = session.status_observed_at
    refresh()
    assert session.status_observed_at == observed  # no artificial evidence churn
    assert session.last_activity == original_activity


def test_explicit_session_end_hook_is_distinct_from_missing_evidence(tmp_path: Path) -> None:
    reg = Registry(Bus())
    reg.upsert(Session(id="s", source="claude_code", last_activity=datetime.now(UTC)))
    adapter = ClaudeCodeAdapter(reg, root=tmp_path)
    adapter.state["s"] = {"hook_status": "ended", "hook_ts": 123.0}
    adapter.refresh_status("s")
    session = reg.sessions["s"]
    assert session.status_reason == "session_end_hook_observed"
    assert session.status_evidence["hook_ts"] == 123.0
    assert not session.status_evidence["process_exit_confirmed"]


def test_evidence_snapshot_does_not_alias_mutable_observations() -> None:
    reg = Registry(Bus())
    reg.upsert(Session(id="s", source="codex", last_activity=datetime.now(UTC)))
    raw = {"app_server": {"flags": ["busy"]}}
    reg.set_status("s", "working", "observed", raw)
    raw["app_server"]["flags"].clear()
    assert reg.sessions["s"].status_evidence["app_server"]["flags"] == ["busy"]


def test_status_only_transition_does_not_inherit_old_evidence() -> None:
    reg = Registry(Bus())
    reg.upsert(Session(id="s", source="codex", last_activity=datetime.now(UTC)))
    reg.set_status("s", "working", "app_server_active", {"type": "active"})
    reg.set_status("s", "ended")
    session = reg.sessions["s"]
    assert session.status_reason is None and session.status_observed_at is None
    assert session.status_evidence == {}
