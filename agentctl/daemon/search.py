"""Search what was said in every transcript: your messages and the agents' replies, not tool
output. Each file is read once by the adapter's own parser (so the text is what `tail` shows)
and topped up by byte offset; ~2 GB of transcripts give ~10 MB of text, built in about 2 s on
the first search. The same pass collects the files each session edited, over the whole
transcript (the daemon's own events start near the end of each file)."""

import json
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentctl.adapters.claude_code import Transcript, parse_entry
from agentctl.adapters.codex import Rollout, parse_rollout
from agentctl.daemon.fleet import EDIT_TOOLS, edited_files
from agentctl.models import SessionEvent
from agentctl.tailer import FileTail

Msg = tuple[datetime, str, str, str]  # ts, role, text, text lowered
SNIPPET = 70  # chars of context either side of a match


def _claude(path: Path, sid: str) -> tuple[FileTail, Callable[[dict[str, Any]], list[Any]]]:
    t = Transcript(path=path, sid=sid)
    return t, lambda d: parse_entry(d, t)


def _codex(path: Path, sid: str) -> tuple[FileTail, Callable[[dict[str, Any]], list[Any]]]:
    r = Rollout(path=path, tid=sid)
    return r, lambda d: parse_rollout(d, r)


OPENERS = {"claude_code": _claude, "codex": _codex}


class Index:
    def __init__(self) -> None:
        self.files: dict[str, tuple[FileTail, Callable[[dict[str, Any]], list[Any]]]] = {}
        self.msgs: dict[str, list[Msg]] = {}
        self.edits: dict[str, list[SessionEvent]] = {}  # edit tool calls
        self.lock = threading.Lock()  # two dashboards searching at once

    def update(self, sid: str, source: str, path: Path) -> None:
        if sid not in self.files or self.files[sid][0].path != path:
            self.files[sid] = OPENERS[source](path, sid)
            self.msgs[sid], self.edits[sid] = [], []
        tail, parse = self.files[sid]
        offset = tail.offset
        lines = tail.read_lines()
        if tail.offset < offset:  # replaced or truncated: read_lines started over
            self.msgs[sid], self.edits[sid] = [], []
        for raw in lines:
            if source == "claude_code" and b'"tool_result"' in raw:
                continue  # most of the bytes, and never a message
            try:
                d = json.loads(raw)
                evs: list[SessionEvent] = parse(d) if isinstance(d, dict) else []
            except Exception:  # a bad line never stops the search
                continue
            for e in evs:
                text = str(e.payload.get("text") or "")
                if e.kind == "message" and text:
                    self.msgs[sid].append((e.ts, str(e.payload.get("role")), text, text.lower()))
                elif e.kind == "tool_call" and str(e.payload.get("name")).lower() in EDIT_TOOLS:
                    self.edits[sid].append(e)

    def edited(self, sid: str, source: str, path: Path) -> list[str]:
        """Every file the session edited with an edit tool (not via shell commands)."""
        with self.lock:
            self.update(sid, source, path)
            cwd = str(getattr(self.files[sid][0], "cwd", ""))
            return sorted(edited_files(self.edits[sid], cwd, datetime.fromtimestamp(0, UTC)))

    def search(
        self, query: str, files: Iterable[tuple[str, str, Path]], limit: int = 50
    ) -> list[dict[str, Any]]:
        """Newest matches first, at most 3 per session. `files`: (session id, source, path)."""
        q = query.strip().lower()
        if not q:
            return []
        hits: list[dict[str, Any]] = []
        with self.lock:
            for sid, source, path in files:
                if source not in OPENERS:
                    continue
                self.update(sid, source, path)
                found = 0
                for ts, role, text, low in reversed(self.msgs[sid]):
                    i = low.find(q)
                    if i < 0:
                        continue
                    start, end = max(i - SNIPPET, 0), i + len(q) + SNIPPET
                    snippet = " ".join(text[start:end].split())
                    hits.append(
                        {
                            "id": sid,
                            "ts": ts.isoformat(),
                            "role": role,
                            "snippet": ("…" if start else "") + snippet,
                        }
                    )
                    found += 1
                    if found == 3:
                        break
        hits.sort(key=lambda h: h["ts"], reverse=True)
        return hits[:limit]
