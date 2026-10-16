from pathlib import Path

import pytest

from agentctl import setup


def test_alias_block_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(setup, "STATE_DIR", tmp_path / ".agentctl")  # backups go here
    rc = tmp_path / ".zshrc"
    original = "export FOO=1\nalias claude='agentctl claude'\n"
    rc.write_text(original)
    assert setup.aliased(rc, "claude") and not setup.aliased(rc, "codex")
    setup.add_aliases(rc, ["codex"])
    setup.add_aliases(rc, ["opencode"])  # re-run: one block, both aliases
    text = rc.read_text()
    assert text.count(setup.BEGIN) == 1 and "alias codex=" in text and "alias opencode=" in text
    assert setup.remove_aliases(rc)
    assert rc.read_text() == original
    assert not setup.remove_aliases(rc)
    assert len(list((tmp_path / ".agentctl" / "backups").iterdir())) == 2


def test_doctor_runs() -> None:
    rows = setup.checks()  # read-only: never changes anything
    names = {c.name for c in rows}
    assert {"agentctl", "tmux", "daemon", "ssh", "tailscale"} <= names
    assert all(c.status in ("ok", "warn", "fail", "info") for c in rows)
