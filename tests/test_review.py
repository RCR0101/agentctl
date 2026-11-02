import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentctl.daemon.fleet import review_turn
from agentctl.models import Session, SessionEvent


def test_review_scopes_turn_and_shows_shared_shell_changes(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "shell-created.txt").write_text("created outside edit tools")
    now = datetime.now(UTC)
    session = Session(id="s", source="claude_code", cwd=str(tmp_path), last_activity=now)

    def event(kind: str, **payload: object) -> SessionEvent:
        return SessionEvent(session_id="s", kind=kind, ts=now, payload=payload)  # type: ignore[arg-type]

    old = SessionEvent(
        session_id="s",
        kind="message",
        ts=now - timedelta(minutes=5),
        payload={"role": "assistant", "text": "Old answer"},
    )
    report = review_turn(
        session,
        [
            old,
            event("message", role="user", text="Fix parser"),
            event("tool_call", id="t", name="Bash", summary="pytest -q"),
            event("tool_result", tool_use_id="t", text="2 passed", is_error=False),
            event("tool_call", id="e", name="Edit", summary="parser.py"),
            event("message", role="user", agent_id="child", text="Subagent prompt"),
            event("message", role="assistant", text="New answer"),
            event("message", role="assistant", text="Complete ending."),
        ],
    )
    assert report["request"] == "Fix parser"
    assert report["reply"] == "New answer\n\nComplete ending."
    assert report["tool_calls"] == 2 and report["tool_failures"] == 0
    assert report["files"] == [str(tmp_path / "parser.py")]
    assert "shell-created.txt" in str(report["project_changes"])
    assert report["tool_results"] == [
        {"name": "Bash", "summary": "pytest -q", "text": "2 passed", "is_error": False}
    ]
