"""Saved team topology is separate from runs, assignments and session liveness."""

import time
import uuid
from typing import Literal

from pydantic import BaseModel, Field

Permission = Literal["observe", "propose", "delegate"]


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class Member(BaseModel):
    id: str = Field(default_factory=lambda: new_id("member"))
    session_id: str
    model: str = Field(default="", pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,79})?$")
    role: str = Field(min_length=1, max_length=80)
    brief: str = Field(default="", max_length=12000)
    permission: Permission = "propose"
    enabled: bool = True
    held: bool = False


class Team(BaseModel):
    id: str = Field(default_factory=lambda: new_id("team"))
    name: str = Field(min_length=1, max_length=80)
    goal: str = Field(min_length=1, max_length=24000)
    coordinator: str
    permission_ceiling: Permission = "delegate"
    workers: list[Member] = Field(default_factory=list)
    revision: int = 1


class TeamProposal(BaseModel):
    id: str = Field(default_factory=lambda: new_id("proposal"))
    team_id: str
    request_id: str | None = None
    changes: list[dict[str, str]] = Field(min_length=1, max_length=20)
    status: Literal["pending", "approved", "rejected"] = "pending"
    created: float = Field(default_factory=time.time)
    detail: str = ""


class Run(BaseModel):
    id: str = Field(default_factory=lambda: new_id("run"))
    team_id: str
    state: Literal["running", "paused", "ended"] = "running"
    snapshot: Team
    created: float = Field(default_factory=time.time)
    reason: str = ""


class Assignment(BaseModel):
    id: str = Field(default_factory=lambda: new_id("assignment"))
    team_id: str
    run_id: str
    member_id: str
    session_id: str  # frozen binding: role edits must never redirect previously issued work
    requested_model: str = Field(
        default="", pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,79})?$"
    )
    model_selection: Literal["requested", "agent_default"] = "agent_default"
    delivery_session_id: str | None = None  # fresh agent chat, separate from the user's reference
    text: str = Field(min_length=1, max_length=64000)
    status: Literal[
        "proposed",
        "queued",
        "waiting",
        "sending",
        "sent",
        "reported_complete",
        "accepted",
        "failed",
        "blocked",
        "unknown",
        "cancelled",
    ] = "proposed"
    created: float = Field(default_factory=time.time)
    sent_at: float | None = None
    reply: str = ""
    detail: str = ""
    request_id: str | None = None
