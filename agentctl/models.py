from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

Source = Literal["claude_code", "codex", "opencode"]
Origin = Literal["cli", "desktop", "sdk", "unknown"]
Status = Literal["working", "awaiting_approval", "awaiting_input", "idle", "ended"]
EventKind = Literal[
    "message", "tool_call", "tool_result", "status_change", "approval_request", "error"
]

# Sort order for listings: what needs me first.
STATUS_ORDER: dict[str, int] = {
    "awaiting_approval": 0,
    "awaiting_input": 1,
    "working": 2,
    "idle": 3,
    "ended": 4,
}


class ApprovalRequest(BaseModel):
    tool: str | None = None  # e.g. "Bash: touch hello.txt"
    detail: str | None = None  # native reason, e.g. "permission prompt"
    options: dict[str, str] = {}  # numbered choices read off the pane


class Session(BaseModel):
    id: str
    source: Source
    origin: Origin = "unknown"
    cwd: str = ""
    title: str | None = None
    status: Status = "ended"
    controllable: bool = False
    tmux_pane: str | None = None
    last_activity: datetime
    pending_approval: ApprovalRequest | None = None
    context_tokens: int | None = None  # tokens the last request sent (= context in use)
    context_window: int | None = None
    context_window_exact: bool = False  # Claude doesn't record its window; it's inferred
    cache_expires_at: datetime | None = None  # Claude only: last request + its cache TTL


class SessionEvent(BaseModel):
    session_id: str
    kind: EventKind
    ts: datetime
    payload: dict[str, Any]


def short_id(sid: str) -> str:
    """8 chars to show and type. opencode ids all start with "ses_", so skip that."""
    return sid.removeprefix("ses_")[:8]
