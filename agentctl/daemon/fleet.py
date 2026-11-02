"""Views across sessions: what's new since you last looked, agents stepping on each other
(same repo, same files), which project each session belongs to, and follow-ups queued for
when an agent is free."""

import os
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from agentctl.models import STATUS_ORDER, Session, SessionEvent, short_id
from agentctl.waiting import STUCK_MINUTES

# Tool names that change files, across Claude (Edit, Write, ...), Codex (edit, apply_patch)
# and opencode (edit, write, patch). Their summary is the path (Codex: "a, b").
EDIT_TOOLS = {"edit", "write", "multiedit", "notebookedit", "apply_patch", "patch"}
BUSY = ("working", "awaiting_approval")
STUCK_SECONDS = STUCK_MINUTES * 60
CONFLICT_WINDOW = timedelta(hours=2)  # edits older than this don't count as overlapping


@lru_cache(maxsize=1024)
def project_root(cwd: str) -> str:
    """The git work tree containing cwd, else cwd itself.
    ponytail: cached for the daemon's lifetime; a folder that becomes a repo later needs a
    restart to be grouped with it."""
    if not cwd or not os.path.isdir(cwd):
        return cwd
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return cwd
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else cwd


def file_diff(path: str) -> str:
    """The file's changes since the last commit; a new file shows whole, as added."""

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        cmd = ["git", "-C", os.path.dirname(path), *args]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=10)

    try:
        if git("rev-parse", "--git-dir").returncode:
            return "not in a git repository"
        if git("ls-files", "--error-unmatch", "--", path).returncode:  # untracked
            return git("diff", "--no-index", "--", "/dev/null", path).stdout or "empty new file"
        r = git("diff", "HEAD", "--", path)
        if r.returncode:  # e.g. no commits yet
            return r.stderr.strip() or "git diff failed"
        return r.stdout or "no changes since the last commit"
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"git failed: {e}"


def review_turn(session: Session, events: Iterable[SessionEvent]) -> dict[str, object]:
    """Evidence from the latest observed turn, plus explicitly shared project changes."""
    evs = list(events)
    start = next(
        (
            i
            for i in range(len(evs) - 1, -1, -1)
            if evs[i].kind == "message"
            and evs[i].payload.get("role") == "user"
            and not evs[i].payload.get("agent_id")
        ),
        None,
    )
    turn = evs[start:] if start is not None else evs
    replies = [
        str(e.payload.get("text", ""))
        for e in turn
        if e.kind == "message"
        and e.payload.get("role") == "assistant"
        and not e.payload.get("agent_id")
    ]
    since = turn[0].ts if turn else datetime.now(UTC)
    root = project_root(session.cwd)
    changes = "Project folder unavailable" if not root else ""
    diff_stat = ""
    if root:
        try:
            result = subprocess.run(
                ["git", "-C", root, "status", "--short", "--untracked-files=normal"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            changes = result.stdout if result.returncode == 0 else "Git status unavailable"
            stat = subprocess.run(
                ["git", "-C", root, "diff", "HEAD", "--stat"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            diff_stat = stat.stdout if stat.returncode == 0 else "Diff since HEAD unavailable"
        except (OSError, subprocess.TimeoutExpired) as error:
            changes = f"Git status unavailable: {error}"
    calls = {
        e.payload["id"]: e.payload for e in turn if e.kind == "tool_call" and e.payload.get("id")
    }
    results = []
    for e in turn:
        if e.kind == "tool_result":
            call = calls.get(e.payload.get("tool_use_id"), {})
            results.append(
                {
                    "name": call.get("name") or "tool",
                    "summary": call.get("summary", ""),
                    "text": e.payload.get("text", ""),
                    "is_error": bool(e.payload.get("is_error")),
                }
            )
    return {
        "request": str(evs[start].payload.get("text", "")) if start is not None else "",
        "reply": "\n\n".join(replies),
        "tool_calls": sum(e.kind == "tool_call" for e in turn),
        "tool_failures": sum(
            e.kind == "tool_result" and bool(e.payload.get("is_error")) for e in turn
        ),
        "tool_results": results,
        "files": sorted(edited_files(turn, session.cwd, since)),
        "project": root,
        "project_changes": changes.strip(),
        "project_diff_stat": diff_stat.strip(),
    }


def project_name(cwd: str) -> str:
    root = project_root(cwd)
    return Path(root).name or root or "?"


def edited_files(evs: Iterable[SessionEvent], cwd: str, since: datetime) -> dict[str, datetime]:
    """Absolute path -> when this session last edited it (since `since`)."""
    out: dict[str, datetime] = {}
    for e in evs:
        if e.kind != "tool_call" or e.ts < since:
            continue
        if str(e.payload.get("name", "")).lower() not in EDIT_TOOLS:
            continue
        for p in str(e.payload.get("summary") or "").split(", "):
            p = p.strip()
            if p and "\n" not in p:
                path = os.path.normpath(os.path.join(cwd, os.path.expanduser(p)))
                out[path] = max(out.get(path, e.ts), e.ts)
    return out


def file_activity(
    path: str, sessions: list[Session], events: Mapping[str, Iterable[SessionEvent]], hours: float
) -> dict[str, object]:
    """Who else is on this file: sessions that edited it in the last `hours`, and live
    sessions working in the same repo right now."""
    path = os.path.normpath(os.path.expanduser(path))
    since = datetime.now(UTC) - timedelta(hours=hours)
    edits = []
    for s in sessions:
        when = edited_files(events.get(s.id, ()), s.cwd, since).get(path)
        if when:
            edits.append({"id": s.id, "title": s.title, "status": s.status, "at": when.isoformat()})
    root = project_root(os.path.dirname(path))
    busy = [
        {"id": s.id, "title": s.title, "status": s.status}
        for s in sessions
        if s.status in BUSY and s.cwd and project_root(s.cwd) == root
    ]
    return {"path": path, "edited_by": edits, "working_in_repo": busy}


def unread(evs: Iterable[SessionEvent], seen: datetime) -> int:
    """Replies the agent wrote since you last looked."""
    return sum(
        1
        for e in evs
        if e.kind == "message" and e.payload.get("role") == "assistant" and e.ts > seen
    )


def preview(evs: Sequence[SessionEvent]) -> dict[str, str] | None:
    """The newest message or tool call, for a phone card: what it said or is running."""
    for e in reversed(evs):
        if e.kind == "message" and e.payload.get("text"):
            kind, text = str(e.payload.get("role")), str(e.payload["text"])
        elif e.kind == "tool_call":
            kind, text = "tool", f"{e.payload.get('name')} {e.payload.get('summary') or ''}"
        else:
            continue
        return {"kind": kind, "text": " ".join(text.split())[:300], "ts": e.ts.isoformat()}
    return None


def conflicts(
    sessions: list[Session], events: Mapping[str, Iterable[SessionEvent]]
) -> dict[str, list[str]]:
    """Per session, warnings about other live agents working in the same git repo, or that
    edited the same files in the last couple of hours."""
    out: dict[str, list[str]] = {}
    busy: dict[str, list[Session]] = {}
    for s in sessions:
        if s.status in BUSY and s.cwd:
            busy.setdefault(project_root(s.cwd), []).append(s)
    for root, group in busy.items():
        if len(group) > 1:
            for s in group:
                others = ", ".join(short_id(o.id) for o in group if o is not s)
                out.setdefault(s.id, []).append(f"also working in {Path(root).name}: {others}")
    since = datetime.now(UTC) - CONFLICT_WINDOW
    editors: dict[str, set[str]] = {}
    for s in sessions:
        if s.status == "ended" or not s.cwd:
            continue
        for f in edited_files(events.get(s.id, ()), s.cwd, since):
            editors.setdefault(f, set()).add(s.id)
    for f, sids in editors.items():
        if len(sids) > 1:
            for sid in sids:
                others = ", ".join(sorted(short_id(o) for o in sids if o != sid))
                out.setdefault(sid, []).append(f"{Path(f).name} also edited by {others}")
    return out


def digest(
    sessions: list[Session],
    events: dict[str, list[SessionEvent]],
    seen: dict[str, datetime],
    default_seen: datetime,
) -> list[dict[str, object]]:
    """One entry per session with replies you haven't seen: what it finished, which files it
    touched, and the start of its last reply. Sessions needing you come first."""
    rows: list[tuple[tuple[float, float], dict[str, object]]] = []
    for s in sessions:
        evs = events.get(s.id, [])
        since = seen.get(s.id, default_seen)
        n = unread(evs, since)
        if not n:
            continue
        replies = [e for e in evs if e.kind == "message" and e.payload.get("role") == "assistant"]
        last = str(replies[-1].payload.get("text", "")).strip() if replies else ""
        new = [e for e in evs if e.ts > since]
        failed = sum(1 for e in new if e.kind == "tool_result" and e.payload.get("is_error"))
        errors = [
            (str(e.payload.get("text", "")).strip().splitlines() or [""])[0][:200]
            for e in new
            if e.kind == "error"
        ]
        quiet = (datetime.now(UTC) - s.last_activity).total_seconds()
        stuck = s.status == "working" and quiet > STUCK_SECONDS
        # what needs you, then what went wrong, then the rest by status
        rank = float(STATUS_ORDER[s.status])
        if rank > STATUS_ORDER["awaiting_input"] and (stuck or errors):
            rank = STATUS_ORDER["awaiting_input"] + 0.5
        order = (rank, -s.last_activity.timestamp())
        row: dict[str, object] = {
            "id": s.id,
            "title": s.title,
            "status": s.status,
            "new_replies": n,
            "files": sorted(Path(f).name for f in edited_files(evs, s.cwd, since)),
            "last_reply": last.splitlines()[0][:200] if last else "",
            "errors": errors[-3:],
            "failed_tools": failed,
            "stuck_minutes": int(quiet // 60) if stuck else None,
        }
        rows.append((order, row))
    return [row for _, row in sorted(rows, key=lambda x: x[0])]
