"""The command line's look: one status glyph and colour per state, quiet secondary text,
tree lines for detail, gauges for limits, and an inline picker for choosing a session.

Everything here renders with rich and degrades on its own: no colour when piped or with
NO_COLOR, and nothing interactive unless both stdin and stdout are a terminal. `--json`
output never comes through here.
"""

import os
import sys
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.text import Text

from agentctl import HOME
from agentctl.ids import short_id

# glyph, colour, label: the same everywhere a status shows
STATUS = {
    "awaiting_approval": ("▲", "yellow", "needs approval"),
    "awaiting_input": ("◆", "cyan", "your turn"),
    "working": ("●", "green", "working"),
    "idle": ("○", "bright_black", "idle"),
    "ended": ("·", "bright_black", "ended"),
}
AGENT = {"claude_code": "claude", "codex": "codex", "opencode": "opencode"}
DIM = "bright_black"
ACCENT = "magenta"


def glyph(status: str) -> Text:
    g, colour, _ = STATUS[status]
    return Text().append(g, style=f"bold {colour}")


def status_label(status: str) -> Text:
    g, colour, label = STATUS[status]
    return Text().append(f"{g} {label}", style=colour)


def ago(iso: str) -> str:
    secs = int((datetime.now(UTC) - datetime.fromisoformat(iso)).total_seconds())
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{secs // n}{unit}"
    return "now" if secs < 10 else f"{secs}s"


def until(ts: float) -> str:
    secs = int(ts - datetime.now(UTC).timestamp())
    if secs <= 0:
        return "now"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}d {h}h" if d else f"{h}h {m:02d}m" if h else f"{m}m"


def tilde(path: str) -> str:
    return path.replace(str(HOME), "~", 1) if path else ""


def project(r: dict[str, Any]) -> str:
    return str(r.get("project") or Path(r.get("cwd") or "").name or "~")


def title(r: dict[str, Any]) -> str:
    return str(r.get("title") or project(r))


def context_pct(r: dict[str, Any]) -> int | None:
    used, window = r.get("context_tokens"), r.get("context_window")
    return int(used * 100 // window) if used and window else None


# --- small pieces ---


def chip(text: str, style: str) -> Text:
    return Text(text, style=style)


def badges(r: dict[str, Any], stuck: int | None = None, full: int | None = None) -> Text:
    """What asks for a look, in one quiet run: new replies, overlap, queued, quiet, context."""
    parts: list[Text] = []
    if r.get("unread"):
        parts.append(chip(f"{r['unread']} new", "bold cyan"))
    if stuck is not None:
        parts.append(chip(f"quiet {stuck}m", "bold yellow"))
    if full is not None:
        parts.append(chip(f"{full}% context", "bold red"))
    if r.get("conflicts"):
        parts.append(chip("overlap", "yellow"))
    if r.get("queued"):
        parts.append(chip(f"{len(r['queued'])} queued", "cyan"))
    return Text("  ").join(parts)


def hint(*commands: str, prefix: str = "→") -> Text:
    """Next steps, the way modern CLIs end: `→ agentctl approve @  ·  agentctl watch`."""
    t = Text().append(f"{prefix} ", style=DIM)
    for i, c in enumerate(commands):
        if i:
            t.append("  ·  ", style=DIM)
        t.append(c, style="bold")
    return t


def tree(lines: Sequence[Text], indent: int = 2) -> list[Text]:
    """Detail lines under an item, joined with tree connectors."""
    out = []
    for i, ln in enumerate(lines):
        connector = "└ " if i == len(lines) - 1 else "├ "
        out.append(Text().append(" " * indent + connector, style=DIM) + ln)
    return out


def gauge(pct: float, width: int = 20) -> Text:
    """A horizontal bar, coloured by how close it is to the limit."""
    pct = max(0.0, min(pct, 100.0))
    filled = round(pct / 100 * width)
    colour = "red" if pct >= 85 else "yellow" if pct >= 60 else "green"
    return Text().append("━" * filled, style=colour) + Text("━" * (width - filled), style="grey23")


def section(label: str, count: int | None = None, style: str = "bold") -> Text:
    t = Text(label.upper(), style=style)
    if count is not None:
        t.append(f"  {count}", style=DIM)
    return t


def success(msg: str, detail: str = "") -> Text:
    t = Text().append("✓ ", style="bold green") + Text(msg)
    if detail:
        t.append(f"  {detail}", style=DIM)
    return t


def failure(msg: str) -> Text:
    return Text().append("✗ ", style="bold red") + Text(msg)


def note(msg: str, style: str = DIM) -> Text:
    return Text("  " + msg, style=style)


# --- sessions ---


def session_line(r: dict[str, Any], width: int, stuck: int | None, full: int | None) -> Text:
    """`▲ api  Migrate orders table…   2 new   2m  claude  7f3a9c21`: what it is first; who,
    when and which id quieter at the end, aligned."""
    right = Text()
    if r.get("unread"):  # the rest (quiet, context, overlap, queued) is on the tree below
        right.append(f"{r['unread']} new", style="bold cyan").append("  ")
    right.append(f"{ago(r['last_activity']):>4}", style=DIM)
    right.append(f"  {AGENT[r['source']]:<8}", style=DIM)
    right.append(f" {short_id(r['id'])}", style=DIM)
    left = Text()
    left.append_text(glyph(r["status"])).append(" ")
    left.append(f"{project(r)}", style=f"bold {ACCENT}").append("  ")
    ended = r["status"] == "ended"
    if r.get("title"):  # untitled: the project name already says it
        left.append(str(r["title"]), style=DIM if ended else "")
    room = max(width - right.cell_len - 2, 20)
    left.truncate(room, overflow="ellipsis")
    left.pad_right(room - left.cell_len)
    return left + Text("  ") + right


def session_details(r: dict[str, Any], stuck: int | None, full: int | None) -> list[Text]:
    """The tree under a session line: what it wants, what's wrong, what's queued."""
    lines: list[Text] = []
    appr = r.get("pending_approval") or {}
    if r["status"] == "awaiting_approval":
        if qs := appr.get("questions"):
            lines.append(
                Text().append("asks: ", style=DIM) + Text(str(qs[0].get("question")), "yellow")
            )
        else:
            lines.append(
                Text().append("wants ", style=DIM) + Text(appr.get("tool") or "?", "yellow")
            )
    elif r["status"] == "awaiting_input" and (p := r.get("preview")) and p.get("text"):
        lines.append(Text().append("said ", style=DIM) + Text(first_line(p["text"], 90)))
    if stuck is not None:
        lines.append(Text(f"no output for {stuck}m: look at it, or stop it", style="yellow"))
    if full is not None:
        fix = "/compact it" if r["source"] == "claude_code" else "start a fresh session next"
        lines.append(Text(f"{full}% of its context used: {fix}", style="red"))
    for c in r.get("conflicts") or []:
        lines.append(Text(f"overlap: {c}", style="yellow"))
    if r.get("queued"):
        n = len(r["queued"])
        t = Text(f"{n} message{'s' if n > 1 else ''} queued for when it's free", style="cyan")
        if r.get("queue_error"):
            t.append(f" · can't send: {r['queue_error']}", style="red")
        lines.append(t)
    if not r.get("controllable"):
        lines.append(Text(f"read-only · agentctl adopt {short_id(r['id'])}", style=DIM))
    return lines


def first_line(text: str, limit: int) -> str:
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    for mark in ("**", "`", "#"):
        line = line.replace(mark, "")
    return line if len(line) <= limit else line[: limit - 1] + "…"


def approval_card(r: dict[str, Any], show_hint: bool = True) -> Group:
    """What it wants to run, set off by a coloured gutter, and the agent's own options."""
    appr = r.get("pending_approval") or {}
    bar = Text().append("┃ ", style="yellow")
    rows: list[RenderableType] = [
        bar
        + glyph("awaiting_approval")
        + Text().append(f" {title(r)}", style="bold")
        + Text(f"  {project(r)} · {short_id(r['id'])}", style=DIM),
        bar + Text(appr.get("tool") or "(tool unknown)", style="bold yellow"),
    ]
    for k, v in (appr.get("options") or {}).items():
        no = v.lower().startswith(("no", "reject", "deny"))
        style = "red" if no else "green" if k == "1" else ""
        rows.append(bar + Text().append(f"{k} ", style="bold") + Text(v, style=style))
    if show_hint:
        ref = short_id(r["id"])
        if r.get("controllable"):
            pick = Text().append("  (pick the answer)", style=DIM)
            rows.append(
                hint(f"agentctl approve {ref}", f"agentctl deny {ref}", "agentctl approve") + pick
            )
        else:
            rows.append(hint(f"agentctl adopt {ref}", prefix="read-only →"))
    return Group(*rows)


def session_header(r: dict[str, Any]) -> Group:
    """Above a transcript: the title, then one quiet line of status, agent, project, context."""
    meta = Text()
    meta.append_text(status_label(r["status"]))
    for part in (AGENT[r["source"]], project(r)):
        meta.append("  ·  ", style=DIM).append(part, style=DIM)
    if (pct := context_pct(r)) is not None:
        meta.append("  ·  ", style=DIM)
        meta.append(f"{pct}% context", style="red" if pct >= 80 else DIM)
    meta.append("  ·  ", style=DIM).append(short_id(r["id"]), style=DIM)
    lines: list[RenderableType] = [Text(title(r), style="bold"), meta]
    if r["status"] not in ("ended", "awaiting_approval"):  # an approval gets its card below
        lines += tree(session_details(r, None, None)[:4], indent=0)
    return Group(*lines)


# --- transcripts ---

GUTTER = {"user": ("you", "bold cyan"), "assistant": ("◆", "bold green")}


def event(
    ev: dict[str, Any], markdown: bool, width: int, agent: str = "agent"
) -> RenderableType | None:
    """One transcript entry. Your messages and replies get a labelled gutter; tool calls are
    one dim line each, with their outcome (✓ / ✗ and its first line) folded in."""
    from rich.padding import Padding

    p, kind = ev["payload"], ev["kind"]
    ts = datetime.fromisoformat(ev["ts"]).astimezone().strftime("%H:%M")
    if kind == "message":
        label, style = GUTTER.get(p.get("role", ""), ("·", DIM))
        head = Text(f"{label} ", style=style)
        if p.get("role") == "assistant":
            head.append(f"{agent} ", style="bold green")
        head.append(ts, style=DIM)
        if p.get("agent_id"):
            head.append("  subagent", style="magenta")
        text = str(p.get("text", ""))
        if markdown and p.get("role") == "assistant":
            from rich.markdown import Markdown

            body: RenderableType = Markdown(text)
        else:
            body = Text(text, style="bold" if p.get("role") == "user" else "")
        return Group(Text(""), head, Padding(body, (0, 0, 0, 2)))
    if kind == "tool_call":
        summary = first_line(str(p.get("summary", "")), max(width - 24, 20))
        return (
            Text().append("  ▸ ", style=DIM)
            + Text().append(f"{p.get('name')} ", style="yellow")
            + Text(summary, style=DIM)
        )
    if kind == "tool_result":
        lines = str(p.get("text", "")).splitlines() or [""]
        more = f"  +{len(lines) - 1} lines" if len(lines) > 1 else ""
        if p.get("is_error"):
            text = first_line(lines[0], width - 20) + more
            return Text().append("    ✗ ", style="red") + Text(text, style="red")
        return Text().append("    ✓ ", style="green") + Text(
            first_line(lines[0], width - 20) + more, DIM
        )
    if kind == "status_change":
        return Text().append("  ", style=DIM) + status_label(p["to"]) + Text(f"  {ts}", style=DIM)
    if kind == "approval_request":
        return Text().append("  ▲ asked to run ", style="yellow") + Text(
            str(p.get("tool") or "?"), "bold"
        )
    if kind == "error":
        return Text().append("  ✗ ", style="bold red") + Text(str(p.get("text", "")), style="red")
    if kind == "notice":
        return Text().append("  ℹ ", style=ACCENT) + Text(str(p.get("text", "")), style=DIM)
    return None


# --- picking ---


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"


def fuzzy(query: str, text: str) -> bool:
    """Every typed character in order, ignoring case: `dmode` matches "dark mode"."""
    it = iter(text.casefold())
    return all(ch in it for ch in query.casefold().replace(" ", ""))


class Picker:
    """An inline list to choose from, like fzf: arrows (or ctrl-p/n, j/k with no filter)
    move, typing filters, Enter picks, Esc or ctrl-c cancels. Drawn on stderr, gone after."""

    def __init__(
        self,
        prompt: str,
        items: Sequence[tuple[str, Text]],
        console: Console,
        read_key: Callable[[], str] | None = None,
        height: int = 10,
    ) -> None:
        self.prompt, self.items, self.console = prompt, list(items), console
        self.read_key = read_key or _read_key
        self.height, self.query, self.cursor = height, "", 0

    def matches(self) -> list[tuple[str, Text]]:
        """Fuzzy matches, those containing the typed text as-is first (then list order)."""
        q = self.query.casefold()
        hits = [it for it in self.items if fuzzy(q, it[1].plain)]
        return sorted(hits, key=lambda it: q not in it[1].plain.casefold())

    def view(self) -> Group:
        shown = self.matches()
        self.cursor = min(self.cursor, max(len(shown) - 1, 0))
        top = max(0, self.cursor - self.height + 1)
        lines = [
            Text().append(f"{self.prompt} ", style="bold")
            + Text(self.query or "type to filter", style="" if self.query else DIM)
        ]
        for i, (_, label) in enumerate(shown[top : top + self.height], start=top):
            mark = Text().append("❯ ", style=f"bold {ACCENT}") if i == self.cursor else Text("  ")
            line = mark + label.copy()
            if i == self.cursor:
                line.stylize("bold")
            lines.append(line)
        if not shown:
            lines.append(Text("  nothing matches", style=DIM))
        lines.append(Text(f"  {len(shown)}/{len(self.items)} · ↑↓ move · enter pick · esc", DIM))
        return Group(*lines)

    def run(self) -> str | None:
        from rich.live import Live

        with Live(self.view(), console=self.console, transient=True, auto_refresh=False) as live:
            while True:
                key = self.read_key()
                shown = self.matches()
                if key in ("enter",):
                    return shown[self.cursor][0] if shown else None
                if key in ("esc", "ctrl-c"):
                    return None
                if key in ("up", "ctrl-p") or (key == "k" and not self.query):
                    self.cursor = max(self.cursor - 1, 0)
                elif key in ("down", "ctrl-n") or (key == "j" and not self.query):
                    self.cursor = min(self.cursor + 1, max(len(shown) - 1, 0))
                elif key == "backspace":
                    self.query, self.cursor = self.query[:-1], 0
                elif len(key) == 1 and key.isprintable():
                    self.query, self.cursor = self.query + key, 0
                live.update(self.view(), refresh=True)


def _read_key() -> str:
    """One keypress from the terminal, as a name: "up", "enter", "esc", "a", …"""
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = os.read(fd, 1)
        if ch == b"\x1b":
            import select

            if not select.select([fd], [], [], 0.03)[0]:
                return "esc"
            seq = os.read(fd, 2)
            return {b"[A": "up", b"[B": "down", b"OA": "up", b"OB": "down"}.get(seq, "")
        return {
            b"\r": "enter", b"\n": "enter", b"\x03": "ctrl-c", b"\x7f": "backspace",
            b"\x08": "backspace", b"\x10": "ctrl-p", b"\x0e": "ctrl-n", b"\x04": "ctrl-c",
        }.get(ch, ch.decode(errors="ignore"))  # fmt: skip
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def pick_session(
    rows: Iterable[dict[str, Any]], prompt: str, console: Console, read_key: Any = None
) -> str | None:
    """Choose a session from a list; returns its id, or None if cancelled."""
    items = []
    for r in rows:
        label = glyph(r["status"]) + Text(f" {project(r)}", style=ACCENT)
        label.append(f"  {title(r)}").append(f"  {ago(r['last_activity'])}", style=DIM)
        label.append(f"  {short_id(r['id'])}", style=DIM)
        if r["status"] == "awaiting_approval":
            tool = (r.get("pending_approval") or {}).get("tool")
            label.append(f"  wants {tool}", style="yellow")
        items.append((str(r["id"]), label))
    if not items:
        return None
    return Picker(prompt, items, console, read_key).run()


def pick_option(options: dict[str, str], console: Console, read_key: Any = None) -> str | None:
    items = []
    for k, v in options.items():
        no = v.lower().startswith(("no", "reject", "deny"))
        items.append((k, Text().append(f"{k}  ", style="bold") + Text(v, "red" if no else "green")))
    return Picker("answer:", items, console, read_key).run()


def suggest(ref: str, rows: Iterable[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    """Sessions that look like what was typed, for a "did you mean" after a miss."""
    import difflib

    rows = list(rows)
    names = {r["id"]: f"{project(r)} {title(r)}".casefold() for r in rows}
    want = ref.casefold()
    scored = sorted(
        rows,
        key=lambda r: (
            -max(
                difflib.SequenceMatcher(None, want, word).ratio() for word in names[r["id"]].split()
            )
        ),
    )
    best = [
        r for r in scored
        if max(difflib.SequenceMatcher(None, want, w).ratio() for w in names[r["id"]].split())
        >= 0.6 or fuzzy(want, names[r["id"]])
    ]  # fmt: skip
    return best[:limit]
