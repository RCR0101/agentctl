"""Waiting for a session's turn to end: shared by `agentctl wait` and the MCP wait_for tool."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

Outcome = Literal["done", "approval", "idle", "timeout", "uncertain", "undelivered"]

# Exit codes for `agentctl wait`, so scripts can branch without parsing text.
EXIT: dict[Outcome, int] = {
    "done": 0,
    "approval": 2,  # stopped on a permission prompt: a human has to answer it
    "timeout": 3,
    "idle": 4,  # nothing was running, and nothing started within the grace period
    "uncertain": 5,  # a delivery may or may not have reached the agent
    "undelivered": 5,  # queued work that can't be sent
}

STUCK_MINUTES = 10  # working with no new output for this long looks stuck
START_GRACE = 20  # seconds: a message just sent takes a moment to start a turn
POLL = 2


def stuck_minutes(r: dict[str, Any]) -> int | None:
    """Minutes a working session has gone without output, once that looks stuck."""
    if r.get("status") != "working" or not r.get("last_activity"):
        return None
    quiet = (datetime.now(UTC) - datetime.fromisoformat(r["last_activity"])).total_seconds()
    return int(quiet // 60) if quiet > STUCK_MINUTES * 60 else None


CONTEXT_FULL = 80  # percent of the context window: time to compact or start fresh


def context_full(r: dict[str, Any]) -> int | None:
    """Percent of its context window a live session uses, once that's worth a warning."""
    used, window = r.get("context_tokens"), r.get("context_window")
    if r.get("status") == "ended" or not used or not window:
        return None
    pct = int(used * 100 // window)
    return pct if pct >= CONTEXT_FULL else None


@dataclass
class Waited:
    outcome: Outcome
    session: dict[str, Any]
    reply: str = ""  # the last assistant reply, when the turn finished
    detail: str = ""  # the approval asked for, or why work is undelivered


def wait_turn(
    call: Callable[..., Any],
    ref: str,
    timeout: float,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> Waited:
    """Block until the session finishes its turn (and its queued follow-ups), stops for
    approval, or the timeout passes. Polls the local daemon only; no model calls."""
    sleep = sleep or time.sleep  # looked up per call, so tests can patch time.sleep
    clock = clock or time.monotonic
    deadline = clock() + timeout
    grace = clock() + START_GRACE
    s = call("tail", id=ref, n=0)["session"]
    started = s["status"] == "working"
    while clock() < deadline:
        delivery = s.get("agent_delivery") or {}
        if delivery.get("status") == "unknown":
            return Waited("uncertain", s)
        if s.get("queue_error") and not s["queue_error"].startswith("Waiting for"):
            return Waited("undelivered", s, detail=s["queue_error"])
        if s["status"] == "awaiting_approval":
            appr = s.get("pending_approval") or {}
            return Waited("approval", s, detail=appr.get("tool") or "?")
        if s["status"] == "working":
            started = True
        elif (started or clock() > grace) and not s.get("queued"):
            break
        sleep(POLL)
        s = call("tail", id=s["id"], n=0)["session"]
    else:
        return Waited("timeout", s)
    if not started:
        return Waited("idle", s)
    evs = call("tail", id=s["id"], n=30)["events"]
    reply = next(
        (e["payload"]["text"] for e in reversed(evs)
         if e["kind"] == "message" and e["payload"].get("role") == "assistant"),
        "",
    )  # fmt: skip
    return Waited("done", s, reply=reply)
