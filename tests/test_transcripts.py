"""Replies must survive parsing, startup tailing and rendering without losing their endings."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentctl.adapters.claude_code import Transcript, parse_entry
from agentctl.adapters.codex import Rollout, item_events, parse_rollout
from agentctl.adapters.opencode import OpenCodeAdapter
from agentctl.daemon.bus import Bus
from agentctl.daemon.registry import Registry
from agentctl.models import Session, SessionEvent
from agentctl.tailer import HEAD_BYTES, TAIL_BYTES, FileTail

REPLY = "A detailed answer with unicode λ.\n" * 600 + "THE COMPLETE ENDING."


@pytest.mark.parametrize("source", ["claude", "codex", "codex_legacy", "opencode"])
def test_long_reply_keeps_its_ending(source: str, tmp_path: Path) -> None:
    now = datetime.now(UTC)
    events: list[SessionEvent]
    if source == "claude":
        t = Transcript(path=tmp_path / "claude.jsonl", sid="s")
        events = parse_entry(
            {
                "type": "assistant",
                "timestamp": now.isoformat(),
                "message": {"content": [{"type": "text", "text": REPLY}]},
            },
            t,
        )
    elif source == "codex":
        events = item_events({"type": "agentMessage", "text": REPLY}, "s", now)
    elif source == "codex_legacy":
        r = Rollout(path=tmp_path / "codex.jsonl", tid="s", paginated=False)
        events = parse_rollout(
            {
                "type": "event_msg",
                "timestamp": now.isoformat(),
                "payload": {"type": "agent_message", "message": REPLY},
            },
            r,
        )
    else:
        registry = Registry(Bus())
        registry.upsert(Session(id="s", source="opencode", last_activity=now))
        adapter = OpenCodeAdapter(registry, db=tmp_path / "unused.db")
        adapter.on_part(
            "p", "s", 1000, {"type": "text", "text": REPLY, "time": {"end": 2}}, "assistant", False
        )
        events = list(registry.events["s"])
    assert events[0].payload["text"] == REPLY
    assert "more chars" not in events[0].payload["text"]


def test_startup_tail_includes_a_reply_larger_than_its_window(tmp_path: Path) -> None:
    path = tmp_path / "large.jsonl"
    metadata = json.dumps({"type": "session_meta"}).encode() + b"\n"
    reply = json.dumps({"text": "x" * (TAIL_BYTES + HEAD_BYTES) + "ENDING"}).encode() + b"\n"
    path.write_bytes(metadata + reply)
    tail = FileTail(path)
    lines = tail.skip_to_tail() + tail.read_lines()
    assert [json.loads(line) for line in lines] == [
        {"type": "session_meta"},
        {"text": "x" * (TAIL_BYTES + HEAD_BYTES) + "ENDING"},
    ]


def test_startup_tail_does_not_repeat_head_records(tmp_path: Path) -> None:
    path = tmp_path / "many.jsonl"
    path.write_bytes(b'{"text":"' + b"a" * HEAD_BYTES + b'"}\n' + b'{"text":"b"}\n' * TAIL_BYTES)
    tail = FileTail(path)
    lines = tail.skip_to_tail() + tail.read_lines()
    assert lines and all(json.loads(line)["text"] == "b" for line in lines)
