"""Durable command receipts. Observation of a transcript confirms arrival, never execution."""

import time
import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from agentctl.daemon.registry import Store
from agentctl.messages import message_key
from agentctl.models import SessionEvent


class CommandHistory:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store
        self.entries = {e["id"]: e for e in store.commands()} if store else {}
        for entry in self.entries.values():
            if entry["outcome"] in ("sending", "creating"):
                self.update(entry, outcome="unknown", error="Daemon restarted during delivery")

    def update(self, entry: dict[str, Any], **changes: Any) -> None:
        entry.update(changes)
        if self.store:
            self.store.save_command(entry)

    def begin(self, sid: str, text: str, command_id: str | None = None) -> dict[str, Any]:
        now = time.time()
        entry = {
            "id": command_id or uuid.uuid4().hex,
            "session_id": sid,
            "created": now,
            "at": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
            "text": text,
            "outcome": "sending",
            "error": "",
        }
        self.entries[entry["id"]] = entry
        self.update(entry)
        return entry

    def queued_delivery(self, sid: str, text: str, outcome: str) -> None:
        entry = next(
            (
                e
                for e in self.entries.values()
                if e["session_id"] == sid and e["outcome"] == "queued" and e["text"] == text
            ),
            None,
        )
        if entry:
            self.update(entry, outcome=outcome)

    def migrate(self, old: str, new: str) -> None:
        for entry in self.entries.values():
            if entry["session_id"] == old:
                self.update(entry, session_id=new)

    def listing(self, sid: str, events: Iterable[SessionEvent]) -> list[dict[str, Any]]:
        entries = [e for e in self.entries.values() if e["session_id"] == sid]
        # Consume each observation once: two identical sends need two later user messages.
        observed = [
            (e.ts.timestamp(), message_key(str(e.payload.get("text", ""))))
            for e in events
            if e.kind == "message"
            and e.payload.get("role") == "user"
            and not e.payload.get("agent_id")
        ]
        observed.sort()
        for entry in entries:
            if entry["outcome"] in ("failed", "cancelled"):
                continue
            key = message_key(entry["text"])
            if entry.get("settled") == "arrived":
                # A receipt is immutable. An old command must never claim a newer repeat.
                match = next(
                    (
                        i
                        for i, (ts, observed_key) in enumerate(observed)
                        if ts == entry.get("receipt_at") and observed_key == key
                    ),
                    None,
                )
                if match is not None:
                    observed.pop(match)
                continue
            match = next(
                (
                    i
                    for i, (ts, observed_key) in enumerate(observed)
                    if ts >= entry["created"] and observed_key == key
                ),
                None,
            )
            if match is not None:
                ts, _ = observed.pop(match)
                if entry.get("settled") != "arrived":
                    self.update(entry, settled="arrived", receipt_at=ts, error="")
        return [dict(e) for e in entries[-200:]]
