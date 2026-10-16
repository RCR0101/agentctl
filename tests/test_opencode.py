import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path

from agentctl.adapters.opencode import OPTIONS, OpenCodeAdapter
from agentctl.daemon.bus import Bus
from agentctl.daemon.registry import Registry

NOW = int(time.time() * 1000)
CWD = "/w/proj"
SCHEMA = """
CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT, title TEXT,
  time_updated INTEGER, time_archived INTEGER);
CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
  time_updated INTEGER, data TEXT);
CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER,
  time_updated INTEGER, data TEXT);
"""


def make_db(path: Path) -> None:
    """Rows shaped like opencode 1.18.29's (see docs/formats.md), trimmed to what agentctl reads."""
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.executemany(
        "INSERT INTO session VALUES (?,?,?,?,?,?)",
        [
            ("ses_a", None, CWD, "New session - 2026-10-16T03:03:47.336Z", NOW, None),
            ("ses_sub", "ses_a", CWD, "Find things (@explore subagent)", NOW, None),
            ("ses_old", None, CWD, "archived one", NOW, NOW),
        ],
    )
    user = {"role": "user", "time": {"created": NOW}}
    asst = {
        "role": "assistant",
        "providerID": "opencode",
        "modelID": "big-pickle",
        "time": {"created": NOW, "completed": NOW + 9},
        "finish": "stop",
        "tokens": {"input": 956, "output": 152, "cache": {"write": 0, "read": 218368}},
    }
    aborted = {
        "role": "assistant",
        "error": {"name": "MessageAbortedError", "data": {"message": "Aborted"}},
    }
    db.executemany(
        "INSERT INTO message VALUES (?,?,?,?,?)",
        [
            ("msg_1", "ses_a", NOW, NOW, json.dumps(user)),
            ("msg_2", "ses_a", NOW + 1, NOW + 9, json.dumps(asst)),
            ("msg_3", "ses_sub", NOW + 2, NOW + 3, json.dumps(asst)),
            ("msg_4", "ses_a", NOW + 10, NOW + 11, json.dumps(aborted)),
        ],
    )
    parts = [
        ("prt_1", "msg_1", "ses_a", {"type": "text", "text": "run echo hi\nplease"}),
        ("prt_2", "msg_1", "ses_a", {"type": "text", "text": "<file>", "synthetic": True}),
        ("prt_3", "msg_2", "ses_a", {"type": "reasoning", "text": "hmm"}),
        ("prt_4", "msg_2", "ses_a", {
            "type": "tool", "tool": "bash", "callID": "c1",
            "state": {"status": "completed", "input": {"command": "echo hi"}, "output": "hi"},
        }),
        ("prt_5", "msg_2", "ses_a", {
            "type": "tool", "tool": "bash", "callID": "c2",
            "state": {"status": "error", "input": {"command": "rm x"},
                      "error": "The user rejected permission to use this specific tool call."},
        }),
        ("prt_6", "msg_2", "ses_a", {"type": "text", "text": "Done.", "time": {"end": 2}}),
        ("prt_7", "msg_2", "ses_a", {"type": "text", "text": "streami"}),  # no time.end yet
        ("prt_8", "msg_3", "ses_sub", {
            "type": "tool", "tool": "read", "callID": "c3",
            "state": {"status": "completed", "input": {"filePath": "/w/proj/a.py"}, "output": "x"},
        }),
    ]  # fmt: skip
    db.executemany(
        "INSERT INTO part VALUES (?,?,?,?,?,?)",
        [(i, m, s, NOW + n, NOW + n, json.dumps(d)) for n, (i, m, s, d) in enumerate(parts)],
    )
    db.commit()
    db.close()


def adapter(tmp_path: Path) -> tuple[OpenCodeAdapter, Registry]:
    make_db(tmp_path / "opencode.db")
    reg = Registry(Bus())
    a = OpenCodeAdapter(reg, db=tmp_path / "opencode.db")
    a.windows = {"opencode/big-pickle": 200000}
    a.mark = NOW - 1000
    a.poll(publish=False)
    return a, reg


def test_sessions_and_transcript(tmp_path: Path) -> None:
    a, reg = adapter(tmp_path)
    assert set(reg.sessions) == {"ses_a"}  # subagent and archived sessions aren't listed
    s = reg.sessions["ses_a"]
    assert s.title == "run echo hi" and s.cwd == CWD  # default title replaced by the prompt
    assert s.context_tokens == 956 + 218368 and s.context_window == 200000
    evs = [(e.kind, e.payload.get("text") or e.payload.get("summary")) for e in reg.events["ses_a"]]
    assert evs == [
        ("message", "run echo hi\nplease"),
        ("tool_call", "echo hi"),
        ("tool_result", "hi"),
        ("tool_call", "rm x"),
        ("tool_result", "The user rejected permission to use this specific tool call."),
        ("message", "Done."),
        ("tool_call", "/w/proj/a.py"),  # the subagent's, folded into its parent
        ("tool_result", "x"),
        ("error", "Aborted"),
    ]
    assert reg.events["ses_a"][6].payload["agent_id"] == "ses_sub"
    a.sig = ()
    a.poll(publish=False)  # re-reading the overlap window adds nothing
    assert len(reg.events["ses_a"]) == 9


def test_plugin_events_drive_status(tmp_path: Path) -> None:
    a, reg = adapter(tmp_path)
    s = reg.sessions["ses_a"]
    a.sync("ses_a")
    assert s.status == "idle" and not s.controllable  # recent, last turn aborted, no TUI known
    pid, tmux = os.getpid(), "/tmp/tmux-501/agentctl,1,0"
    a.on_hook("session.status", {"sessionID": "ses_a", "status": {"type": "busy"}}, pid, "%3", tmux)
    assert s.status == "working" and s.controllable and s.tmux_pane == "%3"
    asked = {
        "id": "per_1",
        "sessionID": "ses_sub",  # a subagent's prompt shows in, and is answered from, the parent
        "permission": "bash",
        "patterns": ["echo three > out3.txt"],
    }
    a.on_hook("permission.asked", asked, pid, "%3", tmux)
    assert s.status == "awaiting_approval" and s.pending_approval
    assert s.pending_approval.tool == "bash: echo three > out3.txt"
    assert s.pending_approval.options == OPTIONS
    a.on_hook("session.idle", {"sessionID": "ses_sub"}, pid, "%3", tmux)  # subagent idle: ignored
    assert s.status == "awaiting_approval"
    a.on_hook("permission.replied", {"sessionID": "ses_sub", "requestID": "per_1"}, pid, "%3", tmux)
    assert s.status == "working" and s.pending_approval is None
    a.on_hook("session.idle", {"sessionID": "ses_a"}, pid, "%3", tmux)
    assert s.status == "awaiting_input"
    assert a.live["ses_a"]["socket"] == "/tmp/tmux-501/agentctl"


def test_tui_switching_sessions_moves_the_pane(tmp_path: Path) -> None:
    a, reg = adapter(tmp_path)
    pid = os.getpid()
    busy = {"status": {"type": "busy"}}
    a.on_hook("session.status", {"sessionID": "ses_a"} | busy, pid, "%3", None)
    a.on_hook("session.status", {"sessionID": "ses_new"} | busy, pid, "%3", None)  # /new in TUI
    assert not reg.sessions["ses_a"].controllable and reg.sessions["ses_new"].controllable


def test_approve_refuses_without_prompt(tmp_path: Path) -> None:
    a, _ = adapter(tmp_path)
    from agentctl.adapters.base import ControlError

    try:
        asyncio.run(a.approve("ses_a", None, True))
    except ControlError as e:
        assert "adopt" in str(e)
    else:
        raise AssertionError("approve without a TUI must fail")
