"""Session cards and task-first rows, shared by both dashboard layouts."""

import re
import textwrap
from datetime import UTC, datetime
from typing import Any

from rich.text import Text

from agentctl.cli import (
    age,
    agent_label,
    cache_text,
    ctx_text,
    project_display,
    short_cwd,
    status_text,
)
from agentctl.waiting import context_full

GROUP_STYLE = {
    "awaiting_approval": ("!", "bold black on yellow"),
    "awaiting_input": ("?", "bold black on cyan"),
    "working": (">", "bold black on green"),
    "idle": ("-", "bold black on white"),
    "ended": ("x", "bold white on bright_black"),
}


def group_heading(status: str, label: str, count: int, width: int, more: str = "") -> Text:
    """Full-width status banners, recognizable by text and markers as well as color."""
    marker, style = GROUP_STYLE[status]
    heading = Text(
        f" [{marker}] {label} ({count}){more} ", style=style, no_wrap=True, overflow="crop"
    )
    heading.truncate(max(width, 1), overflow="crop")
    heading.pad_right(max(width - heading.cell_len, 0))
    return heading


def badges(r: dict[str, Any]) -> Text:
    """'3 new', overlapping agents, queued follow-ups: the things that ask for a look."""
    t = Text()
    if r.get("unread"):
        t.append(f"{r['unread']} new ", style="bold cyan")
    if r.get("conflicts"):
        t.append("! ", style="bold yellow")
    if r.get("queued"):
        t.append(f"{len(r['queued'])} queued ", style="cyan")
    return t


def plain_md(text: str) -> str:
    """Markdown's marks out of a one-line preview: [text](url) -> text; *, `, # dropped (not
    _, which is in too many names)."""
    return re.sub(r"[*`#]", "", re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text))


def plain_project(r: dict[str, Any]) -> str:
    return str(r.get("project") or short_cwd(r["cwd"]))


def project_labels(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Each session's project name, with its parent folder only where two different projects
    share a name (`api · work`, `api · personal`): otherwise the parent is just noise."""
    paths: dict[str, set[str]] = {}
    for r in rows:
        name = str(r.get("project") or short_cwd(r["cwd"]))
        paths.setdefault(name, set()).add(str(r.get("project_path") or r["cwd"]))
    return {
        r["id"]: project_display(r)
        if len(paths[str(r.get("project") or short_cwd(r["cwd"]))]) > 1
        else str(r.get("project") or short_cwd(r["cwd"]))
        for r in rows
    }


def notable(r: dict[str, Any]) -> list[Text]:
    """The resource details worth a glance right now, and only those: a context window that's
    nearly full, a prompt cache about to expire while it waits on you."""
    cells = []
    if context_full(r) is not None:
        cells.append(ctx_text(r, short=True))
    exp = r.get("cache_expires_at")
    if exp and r["status"] in ("awaiting_input", "awaiting_approval"):
        left = (datetime.fromisoformat(exp) - datetime.now(UTC)).total_seconds()
        if 0 < left < 300:
            cells.append(Text("cache ", style="yellow").append_text(cache_text(r, short=True)))
    return cells


BAR = {"awaiting_approval": "yellow", "awaiting_input": "cyan", "working": "green"}
MARK = {"assistant": "◆ ", "user": "› ", "tool": "▸ "}


def card_text(
    r: dict[str, Any],
    width: int = 44,
    folder: bool = True,
    status: bool = False,
    project: str | None = None,
) -> Text:
    """Phone card, a status-coloured bar down its left: the title; project, agent and age
    (plus the status when the list isn't grouped by it, and a nearly full context); then what
    it asks of you, is running, or last said, so you can triage from here. The id is in the
    session's own header."""
    approval = r["status"] == "awaiting_approval"
    title_style = "bold yellow" if approval else "dim" if r["status"] == "ended" else "bold"
    head = badges(r).append(r["title"] or short_cwd(r["cwd"]), style=title_style)
    meta = Text()
    if folder:
        meta.append(project or plain_project(r), style="bold cyan")
        if short_cwd(r["cwd"]) != r.get("project", short_cwd(r["cwd"])):
            meta.append(f"/{short_cwd(r['cwd'])}", style="blue")
        meta.append(" · ", style="dim")
    meta.append(f"{agent_label(r)} · {age(r['last_activity'])}", style="dim")
    if status:
        meta.append(" · ", style="dim").append_text(status_text(r["status"]))
    for cell in notable(r):
        meta.append(" · ", style="dim").append_text(cell)
    lines, inner = [head, meta], max(width - 2, 10)
    p = r.get("preview")
    if approval:
        appr = r.get("pending_approval") or {}
        if qs := appr.get("questions"):
            more = f" (+{len(qs) - 1} more)" if len(qs) > 1 else ""
            lines.append(Text(f"? {qs[0].get('question')}{more}", style="yellow"))
        else:
            lines.append(Text(f"! {appr.get('tool') or 'wants your approval'}", style="yellow"))
    elif p and r["status"] != "ended":
        style = "green" if r["status"] == "working" else "dim"
        said = MARK.get(p["kind"], "") + plain_md(p["text"])
        lines += [Text(w, style) for w in textwrap.wrap(said, inner, max_lines=2, placeholder="…")]
    t = Text(no_wrap=True, overflow="ellipsis")
    for i, ln in enumerate(lines):
        ln.truncate(inner, overflow="ellipsis")
        t.append("\n" if i else "").append("▌ ", style=BAR.get(r["status"], "dim")).append_text(ln)
    return t


def row_text(
    r: dict[str, Any], status: bool = False, project: str | None = None, folder: bool = True
) -> Text:
    """Task first, then a quiet second line: project, agent and age, plus only what's worth a
    glance (status when not grouped by it, a nearly full context, a cache about to expire).
    The id and full details are in the header of the selected session."""
    approval = r["status"] == "awaiting_approval"
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append_text(badges(r))
    title_style = "bold yellow" if approval else "dim" if r["status"] == "ended" else "bold"
    t.append(r["title"] or short_cwd(r["cwd"]), style=title_style)
    t.append("\n")
    if folder:
        t.append(project or plain_project(r), style="cyan")
        t.append(" · ", style="dim")
    t.append(f"{agent_label(r)} · {age(r['last_activity'])}", style="dim")
    if status:
        t.append(" · ", style="dim").append_text(status_text(r["status"]))
    for cell in notable(r):
        t.append(" · ", style="dim").append_text(cell)
    if approval:
        appr = r.get("pending_approval") or {}
        qs = appr.get("questions")
        text = qs[0].get("question", "Question") if qs else appr.get("tool") or "Needs approval"
        t.append(f"\n! {text}", style="yellow")
    elif (p := r.get("preview")) and r["status"] != "ended":
        t.append("\n" + MARK.get(p["kind"], "") + plain_md(p["text"]), style="dim")
    return t
