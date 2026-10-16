"""Scripted stand-in for `codex app-server`: one thread; a turn asks to approve one command."""

import json
import sys

TID = "01a00000-0000-7000-8000-0000000000aa"
ROLLOUT = sys.argv[1] if len(sys.argv) > 2 else ""  # argv: <rollout path> app-server


def send(msg: dict) -> None:
    print(json.dumps(msg), flush=True)


def note(method: str, **params: object) -> None:
    send({"method": method, "params": {"threadId": TID, **params}})


cmd = {"type": "commandExecution", "id": "exec-1", "command": "touch x", "status": "inProgress"}
for line in sys.stdin:
    m = json.loads(line)
    method, rid = m.get("method"), m.get("id")
    if method == "initialize":
        send({"id": rid, "result": {"userAgent": "fake", "codexHome": "/tmp"}})
    elif method == "thread/list":
        thread = {"id": TID, "name": None, "preview": "fix the bug\nmore", "cwd": "/w",
                  "source": "cli", "updatedAt": 1792070000, "path": ROLLOUT}  # fmt: skip
        send({"id": rid, "result": {"data": [thread], "nextCursor": None}})
    elif method == "thread/resume":
        send({"id": rid, "result": {"thread": {"id": TID}}})
    elif method == "turn/start":
        send({"id": rid, "result": {"turn": {"id": "turn-1", "status": "inProgress"}}})
        note("thread/status/changed", status={"type": "active", "activeFlags": []})
        note("turn/started", turn={"id": "turn-1"})
        text = m["params"]["input"][0]["text"]
        user = {"type": "userMessage", "id": "u1", "content": [{"type": "text", "text": text}]}
        note("item/completed", item=user, turnId="turn-1")
        note(
            "thread/status/changed", status={"type": "active", "activeFlags": ["waitingOnApproval"]}
        )
        req = {"threadId": TID, "turnId": "turn-1", "itemId": "exec-1", "command": "touch x"}
        send({"id": 0, "method": "item/commandExecution/requestApproval", "params": req})
    elif rid == 0 and "result" in m:  # our approval request answered
        ok = m["result"]["decision"] == "accept"
        note("serverRequest/resolved", requestId=0)
        done = cmd | {
            "status": "completed" if ok else "declined",
            "exitCode": 0,
            "aggregatedOutput": "",
        }
        note("item/completed", item=done, turnId="turn-1")
        note("thread/status/changed", status={"type": "idle"})
        note("turn/completed", turn={"id": "turn-1", "status": "completed"})
    elif method == "thread/unsubscribe":
        send({"id": rid, "result": {"status": "unsubscribed"}})
    elif rid is not None:
        send({"id": rid, "error": {"code": -32601, "message": f"fake: {method}"}})
