"""Session cards and task-first rows, shared by both dashboard layouts."""

import re
import textwrap
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
from agentctl.models import short_id

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


BAR = {"awaiting_approval": "yellow", "awaiting_input": "cyan", "working": "green"}
MARK = {"assistant": "◆ ", "user": "› ", "tool": "▸ "}


def card_text(r: dict[str, Any], width: int = 44, folder: bool = True) -> Text:
    """Phone card, a status-coloured bar down its left: the title; id, agent, age, context and
    folder; then what it asks of you, is running, or last said, so you can triage from here."""
    approval = r["status"] == "awaiting_approval"
    title_style = "bold yellow" if approval else "dim" if r["status"] == "ended" else "bold"
    head = badges(r).append(r["title"] or short_cwd(r["cwd"]), style=title_style)
    meta = Text(f"{short_id(r['id'])} · {agent_label(r)} · {age(r['last_activity'])}", "dim")
    for cell in (ctx_text(r, short=True), cache_text(r, short=True)):
        if cell.plain:
            meta.append(" · ", style="dim").append_text(cell)
    project = project_display(r)
    meta.append(" · ", style="dim").append(project, style="bold cyan")
    if folder and short_cwd(r["cwd"]) != r.get("project", short_cwd(r["cwd"])):
        meta.append(" · ", style="dim").append(short_cwd(r["cwd"]), style="blue")
    meta.append(" · ", style="dim").append_text(status_text(r["status"]))
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


def row_text(r: dict[str, Any], status: bool = False) -> Text:
    """Task first, with identity and resource details on a quieter second line."""
    approval = r["status"] == "awaiting_approval"
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append_text(badges(r))
    title_style = "bold yellow" if approval else "dim" if r["status"] == "ended" else "bold"
    t.append(r["title"] or short_cwd(r["cwd"]), style=title_style)
    project = project_display(r)
    t.append(f"\n{project}", style="bold cyan")
    t.append(f" · {short_id(r['id'])} · {agent_label(r)} · {age(r['last_activity'])}", style="dim")
    if status:
        t.append(" · ", style="dim").append_text(status_text(r["status"]))
    for cell in (ctx_text(r, short=True), cache_text(r, short=True)):
        if cell.plain:
            t.append(" · ", style="dim").append_text(cell)
    if approval:
        appr = r.get("pending_approval") or {}
        qs = appr.get("questions")
        text = qs[0].get("question", "Question") if qs else appr.get("tool") or "Needs approval"
        t.append(f"\n! {text}", style="yellow")
    elif (p := r.get("preview")) and r["status"] != "ended":
        t.append("\n" + MARK.get(p["kind"], "") + plain_md(p["text"]), style="dim")
    return t
