"""`agentctl watch`: Textual dashboard. A thin client like the CLI: polls `ls` every second and
streams `follow` for the selected session. Below 80 columns it becomes a single pane
(list -> enter -> detail -> esc) with two-line cards, a compact header, tappable approval
buttons and a footer that only shows the keys that apply, for ~45 columns in Termius.

The transcript is one widget per entry, so text can be selected with the mouse (ctrl+c
copies), replies render as Markdown, and each run of tool calls folds into one line.
Enter on a live session puts you in the agent itself (its tmux pane; ctrl-] d comes back), so
typing, pasting, menus and prompts all work as they do there. Sessions without a pane open
their transcript. `n` starts a session and opens it, `o` adopts, `x` closes."""

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import (
    Button,
    Collapsible,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Markdown,
    OptionList,
    Static,
    TextArea,
)
from textual.widgets.button import ButtonVariant
from textual.widgets.collapsible import CollapsibleTitle
from textual.widgets.option_list import Option

from agentctl import HOME, SOCKET_PATH
from agentctl.cli import (
    STATUS_STYLE,
    age,
    agent_label,
    cache_text,
    ctx_text,
    render,
    session_header,
    short_cwd,
    status_text,
    usage_text,
)
from agentctl.models import short_id

GROUPS = [
    ("awaiting_approval", "NEEDS APPROVAL"),
    ("awaiting_input", "WAITING FOR INPUT"),
    ("working", "WORKING"),
    ("idle", "IDLE"),
    ("ended", "ENDED, last 24h"),
]
NARROW = 80

Rpc = Callable[..., Awaitable[Any]]
Follow = Callable[[str], AsyncIterator[dict[str, Any]]]


async def rpc(method: str, **params: Any) -> Any:
    reader, writer = await asyncio.open_unix_connection(str(SOCKET_PATH), limit=16 << 20)
    try:
        msg = {"id": 1, "method": method, "params": params}
        writer.write(json.dumps(msg).encode() + b"\n")
        await writer.drain()
        reply = json.loads(await reader.readline())
    finally:
        writer.close()
    if "error" in reply:
        raise RuntimeError(reply["error"]["message"])
    return reply["result"]


async def follow(sid: str) -> AsyncIterator[dict[str, Any]]:
    """First item: {"session", "events"} backlog; then one event per item."""
    reader, writer = await asyncio.open_unix_connection(str(SOCKET_PATH), limit=16 << 20)
    try:
        msg = {"id": 1, "method": "follow", "params": {"id": sid, "n": 200}}
        writer.write(json.dumps(msg).encode() + b"\n")
        await writer.drain()
        first = json.loads(await reader.readline())
        if "error" in first:
            raise RuntimeError(first["error"]["message"])
        yield first["result"]
        while line := await reader.readline():
            yield json.loads(line)["params"]
    finally:
        writer.close()


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


def card_text(r: dict[str, Any], folder: bool = True) -> Text:
    """Phone row: the title gets the whole first line; id, source, age, context, folder below."""
    approval = r["status"] == "awaiting_approval"
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append_text(badges(r))
    title_style = "bold yellow" if approval else "dim" if r["status"] == "ended" else "bold"
    t.append(r["title"] or short_cwd(r["cwd"]), style=title_style)
    t.append(f"\n{short_id(r['id'])} {agent_label(r)} {age(r['last_activity'])} ", style="dim")
    if r.get("context_tokens") is not None:
        t.append_text(ctx_text(r, short=True)).append(" ")
    if cache_text(r, short=True):
        t.append("cache ", style="dim").append_text(cache_text(r, short=True)).append(" ")
    if folder:
        t.append(short_cwd(r["cwd"]), style="blue")
    else:
        t.append_text(status_text(r["status"]))
    return t


def row_text(r: dict[str, Any], status: bool = False) -> Text:
    """Wide row: ls's columns minus the folder (FILES shows it); status only when the
    list isn't already grouped by it."""
    approval = r["status"] == "awaiting_approval"
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append(short_id(r["id"]), style="bold yellow" if approval else "bold")
    t.append(f" {agent_label(r):<6} ", style="dim")
    if status:
        st = status_text(r["status"])
        t.append_text(st).append(" " * (8 - len(st)))
    t.append(f"{age(r['last_activity']):>3} ", style="dim")
    for cell, width in ((ctx_text(r, short=True), 4), (cache_text(r, short=True), 3)):
        t.append(" " * (width - len(cell))).append_text(cell).append(" ")
    t.append_text(badges(r))
    title_style = "yellow" if approval else "dim" if r["status"] == "ended" else ""
    t.append(r["title"] or short_cwd(r["cwd"]), style=title_style)
    return t


TOOL_KINDS = ("tool_call", "tool_result", "status_change")


class Prompt(TextArea):
    """Message box. Enter submits; a paste keeps its newlines (Textual's Input keeps only the
    first line of a paste, which silently cut multi-line messages). New line: shift/alt+enter,
    ctrl+j, or `\\` then enter (phones have no shift+enter). ↑ on the first line / ↓ on the
    last recall what you sent this session, like a shell. TextArea brings the rest: ctrl+a/e/
    w/u/k, undo, selection."""

    sends: list[str] = []  # what was sent to this session, set when the box opens
    recall = 0  # how far back ↑ has gone; 0 is the draft
    draft = ""

    BINDINGS = [
        Binding("shift+enter,alt+enter,ctrl+j", "newline", show=False),
        Binding("alt+left,alt+b", "cursor_word_left", show=False),
        Binding("alt+right,alt+f", "cursor_word_right", show=False),
        Binding("super+left", "cursor_line_start", show=False),
        Binding("super+right", "cursor_line_end", show=False),
    ]

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    def action_newline(self) -> None:
        self.insert("\n")

    def recall_step(self, step: int) -> None:
        pos = min(max(self.recall + step, 0), len(self.sends))
        if pos == self.recall:
            return
        if self.recall == 0:
            self.draft = self.text
        self.recall = pos
        self.load_text(self.sends[-pos] if pos else self.draft)
        self.move_cursor(self.document.end)

    async def _on_key(self, event: events.Key) -> None:
        row = self.cursor_location[0]
        if event.key in ("up", "down") and self.sends:
            if (event.key == "up" and row == 0) or (
                event.key == "down" and row == self.document.line_count - 1 and self.recall
            ):
                event.prevent_default()
                event.stop()
                self.recall_step(1 if event.key == "up" else -1)
                return
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            row, col = self.cursor_location
            if col and self.document.get_line(row)[col - 1] == "\\":
                self.replace("\n", (row, col - 1), (row, col))
            else:
                self.post_message(self.Submitted(self.text))
        else:
            await super()._on_key(event)


class ToolGroup(Collapsible):
    """A folded run of tool calls. Tap to open; kept out of Tab, so Tab moves between the
    panes (list, files, transcript) and doesn't stop on every `▸ Bash` line."""

    def on_mount(self) -> None:
        self.query_one(CollapsibleTitle).can_focus = False


class Files(DirectoryTree):
    """The project at a glance: no dot-files, caches or dependency folders."""

    ICON_NODE, ICON_NODE_EXPANDED, ICON_FILE = "▸ ", "▾ ", "  "  # not Textual's emoji

    HIDE = {"node_modules", "__pycache__", "venv", "target", "dist", "build", "site-packages"}

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [p for p in paths if not p.name.startswith(".") and p.name not in self.HIDE]


class Transcript(VerticalScroll):
    """A session's events as widgets. Consecutive tool calls and results fold into one
    Collapsible ("▸ 12:01 6 tools · last bash pytest"); click or Enter on it to expand."""

    DEFAULT_CSS = """
    Transcript { padding: 0 1; }
    Transcript Markdown { margin: 0; padding: 0; }
    Transcript Collapsible { border: none; padding: 0; margin: 0; background: transparent; }
    Transcript CollapsibleTitle { padding: 0; }
    Transcript Collapsible Contents { padding: 0 0 0 2; }
    """

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.narrow = False
        self.markdown = True
        self.group: Collapsible | None = None
        self.body = Vertical()
        self.tools: Counter[str] = Counter()
        self.group_ts, self.latest, self.failed = "", "", 0
        self.last_reply = ""  # for `c`
        self.seen_at: str | None = None
        self.any = False
        self.calls: dict[str, tuple[str, str]] = {}  # tool calls without a result: id -> label, ts
        self.last_ts = ""

    async def reset(self, narrow: bool, markdown: bool, seen_at: str | None = None) -> None:
        await self.remove_children()
        self.narrow, self.markdown, self.group = narrow, markdown, None
        self.last_reply = ""
        self.seen_at, self.any = seen_at, False
        self.calls, self.last_ts = {}, ""
        self.anchor()  # stay at the newest entry until the user scrolls up

    def add(self, ev: dict[str, Any]) -> None:
        kind, p = ev["kind"], ev["payload"]
        self.last_ts = ev["ts"]
        if kind == "tool_call":
            self.calls[p.get("id") or ""] = (f"{p['name']} {first_line(p['summary'])}", ev["ts"])
        elif kind == "tool_result":
            self.calls.pop(p.get("tool_use_id") or "", None)
        elif kind == "message":
            self.calls.clear()
        if self.seen_at and ev["ts"] > self.seen_at and kind != "status_change":
            if self.any:  # mark where "new since you last looked" starts
                when = fmt_ts(self.seen_at, True)
                self.mount(Static(Text(f"── new since {when} ──", style="bold cyan")))
                self.group = None
            self.seen_at = None
        self.any = True
        if kind in TOOL_KINDS and (self.group or kind != "status_change"):
            if self.group is None:
                self.body = Vertical()
                self.group = ToolGroup(
                    self.body, collapsed=True, collapsed_symbol="▸", expanded_symbol="▾"
                )
                self.tools.clear()
                self.failed = 0
                self.mount(self.group)
            if kind == "tool_call":  # the folded line follows the newest call, so it visibly moves
                self.tools[p["name"]] += 1
                self.latest = f"{p['name']} {(p['summary'].splitlines() or [''])[0]}"
                self.group_ts = fmt_ts(ev["ts"], self.narrow)
            self.failed += bool(kind == "tool_result" and p.get("is_error"))
            line = Static(render(ev, narrow=True))
            if self.body.is_mounted:
                self.body.mount(line)
            else:  # backlog arrives faster than mounting
                self.body.compose_add_child(line)
            self.group.title = self.group_title()  # type: ignore[assignment]
        else:
            self.group = None
            if kind == "message" and p["role"] == "assistant":
                self.last_reply = p["text"]
            if kind == "message" and p["role"] == "assistant" and self.markdown:
                head = Text(f"{fmt_ts(ev['ts'], self.narrow)} ", style="dim")
                self.mount(Static(head.append("◆", style="bold green")))
                self.mount(Markdown(p["text"]))
            else:
                self.mount(Static(render(ev, narrow=self.narrow, markdown=False)))

    def group_title(self) -> Text:
        n = sum(self.tools.values())
        t = Text(f"{self.group_ts} ", style="dim")
        name, _, rest = self.latest.partition(" ")
        if n > 1:
            t.append(f"{n} tools · last ", style="yellow")
        t.append(f"{name} ", style="yellow").append(rest[:120], style="dim")
        if self.failed:
            t.append(f" · {self.failed} failed", style="red")
        return t


def first_line(s: str) -> str:
    return (s.splitlines() or [""])[0]


def since(iso: str) -> str:
    secs = int((datetime.now(UTC) - datetime.fromisoformat(iso)).total_seconds())
    return f"{secs // 60}m {secs % 60:02d}s" if secs >= 60 else f"{max(secs, 0)}s"


def fmt_ts(iso: str, narrow: bool) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%H:%M" if narrow else "%H:%M:%S")


HELP = """\
[b]list[/b]
  enter / tap   open a session: jump into it (ctrl-] d comes back)
  space         its transcript, without jumping in
  a  d  1-4     approve, deny, pick an option
  n             new session
  u             what's new since you last looked
  p             group by project / by status
  /             filter by id, title or folder
  e             show or hide ended sessions
  q             quit

[b]session[/b]
  s             send a message
  l             queue a message for when it's free
  h             what you sent it from here, and whether each arrived
                (in the send box: ↑ / ↓ recall them)
  i             interrupt the running turn
  c             copy the last reply
  r             rename (agentctl's own name for it)
  o             adopt: move it into agentctl
  x             remove from the list (quits it first if it runs in
                agentctl; nothing is deleted, it can be resumed)
  z             bring back the session x just removed
  m             replies as raw text / Markdown
  ctrl-r        reload everything (it also reconnects by itself)
  esc           back

  b             bell on new approvals
  ctrl-] w      this dashboard, from inside any agent
"""


class Help(ModalScreen[None]):
    DEFAULT_CSS = """
    Help { align: center middle; }
    Help > VerticalScroll { width: auto; max-width: 95%; height: auto; max-height: 95%;
                            border: round $accent; padding: 0 1; background: $surface; }
    """
    BINDINGS = [Binding("escape,q,question_mark", "dismiss", "close")]

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(HELP)

    def on_click(self) -> None:
        self.dismiss()


class History(ModalScreen[str | None]):
    """What this dashboard sent to one session, newest first, and whether each one shows up in
    its transcript. Pick one to put it back in the send box."""

    DEFAULT_CSS = """
    History { align: center middle; }
    History > Vertical { width: 95%; max-width: 100; height: auto; max-height: 90%;
                         border: round $accent; padding: 0 1; background: $surface; }
    History OptionList, History OptionList:focus { height: auto; max-height: 30; border: none; }
    """
    BINDINGS = [Binding("escape,h", "dismiss(None)", "close")]

    def __init__(self, title: str, sends: list[dict[str, Any]], arrived: set[str]) -> None:
        super().__init__()
        self.head, self.sends, self.arrived = title, sends, arrived

    def compose(self) -> ComposeResult:
        opts = []
        for i, s in enumerate(reversed(self.sends)):
            t = Text(no_wrap=True, overflow="ellipsis")
            t.append(s["at"] + " ", style="dim")
            if s["outcome"] == "failed":
                t.append("failed ", style="bold red")
            elif s["text"].strip() in self.arrived:
                t.append("arrived ", style="green")
            else:
                t.append(f"{s['outcome']}, not in transcript yet ", style="yellow")
            t.append(first_line(s["text"]))
            if s.get("error"):
                t.append(f"\n  {s['error']}", style="red")
            opts.append(Option(t, id=str(len(self.sends) - 1 - i)))
        with Vertical():
            yield Static(Text(f"Sent to {self.head} from here", style="bold"))
            if opts:
                yield OptionList(*opts)
            else:
                yield Static(Text("nothing sent from this dashboard yet", style="dim"))

    @on(OptionList.OptionSelected)
    def chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.sends[int(event.option.id or 0)]["text"])


class Digest(ModalScreen[str | None]):
    """What happened since you last looked; pick one to open it."""

    DEFAULT_CSS = """
    Digest { align: center middle; }
    Digest > Vertical { width: 95%; max-width: 90; height: auto; max-height: 90%;
                        border: round $accent; padding: 0 1; background: $surface; }
    Digest OptionList, Digest OptionList:focus { height: auto; max-height: 30; border: none; }
    """
    BINDINGS = [Binding("escape,u", "dismiss(None)", "close")]

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        super().__init__()
        self.rows = rows

    def compose(self) -> ComposeResult:
        opts = []
        for r in self.rows:
            t = Text()
            t.append(f"{r['new_replies']} new ", style="bold cyan").append_text(
                status_text(r["status"])
            )
            t.append(f" {r['title'] or short_id(r['id'])}", style="bold")
            if r["files"]:
                more = f" +{len(r['files']) - 4}" if len(r["files"]) > 4 else ""
                t.append("\n  touched " + ", ".join(r["files"][:4]) + more, style="blue")
            if r["last_reply"]:
                reply = (
                    r["last_reply"] if len(r["last_reply"]) <= 90 else r["last_reply"][:89] + "…"
                )
                t.append("\n  ◆ " + reply, style="dim")
            opts.append(Option(t, id=r["id"]))
        with Vertical():
            yield Static(Text("Since you last looked", style="bold"))
            yield OptionList(*opts)

    @on(OptionList.OptionSelected)
    def chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)


class Confirm(ModalScreen[bool]):
    """Yes/no dialog with big tap targets."""

    DEFAULT_CSS = """
    Confirm { align: center middle; }
    Confirm > Vertical { width: 90%; max-width: 60; height: auto; border: round $warning;
                         padding: 1; background: $surface; }
    Confirm Horizontal { height: auto; margin-top: 1; }
    Confirm Button { width: 1fr; min-width: 0; }
    """
    BINDINGS = [Binding("escape", "dismiss(False)", "cancel"), Binding("y", "dismiss(True)")]

    def __init__(self, question: str, yes: str = "Yes") -> None:
        super().__init__()
        self.question, self.yes = question, yes

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.question)
            with Horizontal():
                yield Button(self.yes, id="yes", variant="warning")
                yield Button("Cancel", id="no")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


def dir_matches(value: str, limit: int = 8) -> list[str]:
    """Folders completing a typed path, in the form typed (`~` kept). Dot-folders only when
    the typed name starts with a dot."""
    if "/" not in value:
        return []
    head, _, part = value.rpartition("/")
    try:
        with os.scandir(os.path.expanduser(head or "/")) as it:
            names = sorted(
                e.name
                for e in it
                if e.name.startswith(part)
                and (part.startswith(".") or not e.name.startswith("."))
                and e.is_dir()
            )
    except OSError:
        return []
    return [f"{head}/{n}" for n in names[:limit]]


class DirSuggester(Suggester):
    def __init__(self) -> None:
        super().__init__(use_cache=False, case_sensitive=True)

    async def get_suggestion(self, value: str) -> str | None:
        m = await asyncio.to_thread(dir_matches, value, 1)
        return m[0] if m else None


class FolderInput(Input):
    """tab (or →) takes the grey completion and goes into it, so tab tab tab walks down."""

    BINDINGS = [Binding("tab", "complete", show=False)]

    def action_complete(self) -> None:
        if self._suggestion and len(self._suggestion) > len(self.value):
            self.value = self._suggestion + "/"
            self.cursor_position = len(self.value)


class NewSession(ModalScreen[tuple[str, str] | None]):
    """Pick an agent and a folder to start a session in. The list under the folder shows
    recent folders, then the folders matching what's typed; tap one to fill it in."""

    DEFAULT_CSS = """
    NewSession { align: center middle; }
    NewSession > Vertical { width: 95%; max-width: 70; height: auto; max-height: 90%;
                            border: round $accent; padding: 0 1; background: $surface; }
    NewSession .row { height: auto; }
    NewSession .row Button { width: 1fr; min-width: 0; }
    NewSession OptionList { height: 8; border: none; }  /* fixed: taps can't miss as it fills */
    NewSession Static { margin-top: 1; }
    """
    BINDINGS = [Binding("escape", "dismiss(None)", "cancel")]

    def __init__(self, agents: list[str], folders: list[str]) -> None:
        super().__init__()
        self.agents, self.folders = agents, folders
        self.agent = agents[0] if agents else ""

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("New session: agent")
            with Horizontal(classes="row"):
                for a in self.agents:
                    yield Button(
                        a, id=f"agent-{a}", variant="primary" if a == self.agent else "default"
                    )
            yield Static("folder")
            start = tilde(self.folders[0]) if self.folders else "~/"
            yield FolderInput(value=start, id="folder", suggester=DirSuggester())
            yield OptionList(*[tilde(f) for f in self.folders], id="recent")
            with Horizontal(classes="row"):
                yield Button("Start", id="start", variant="success")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid.startswith("agent-"):
            self.agent = bid[6:]
            for b in self.query(".row Button").results(Button):
                if (b.id or "").startswith("agent-"):
                    b.variant = "primary" if b.id == bid else "default"
        elif bid == "start":
            self.start()
        else:
            self.dismiss(None)

    def on_mount(self) -> None:
        self.query_one("#folder", Input).focus()

    @on(Input.Changed, "#folder")
    async def typed(self, event: Input.Changed) -> None:
        recent = [tilde(f) for f in self.folders]
        v = event.value
        shown = recent if v in recent else await asyncio.to_thread(dir_matches, v)
        ol = self.query_one("#recent", OptionList)
        if [str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count)] != shown:
            ol.set_options(shown)

    @on(OptionList.OptionSelected, "#recent")
    def recent(self, event: OptionList.OptionSelected) -> None:
        inp = self.query_one("#folder", Input)
        inp.value = str(event.option.prompt) + "/"
        inp.cursor_position = len(inp.value)
        inp.focus()

    @on(Input.Submitted, "#folder")
    def start(self) -> None:
        folder = os.path.expanduser(self.query_one("#folder", Input).value.strip()).rstrip("/")
        folder = folder or "/"
        if not os.path.isdir(folder):
            self.notify(f"not a folder: {folder}", severity="error")
        elif self.agent:
            self.dismiss((self.agent, folder))


def tilde(p: str) -> str:
    home = os.path.expanduser("~")
    return "~" + p[len(home) :] if p == home or p.startswith(home + "/") else p


class Watch(App[None]):
    TITLE = "agentctl"
    CSS = """
    #main { height: 1fr; }
    #left { width: 45%; min-width: 36; }
    #list { border: none; padding: 0; text-wrap: nowrap; text-overflow: ellipsis; }
    #list { height: auto; max-height: 65%; }
    #files-title { color: $text-muted; text-style: bold; padding: 0 1; border-top: solid $panel;
                   text-wrap: nowrap; text-overflow: ellipsis; }
    #files { height: 1fr; padding: 0; }
    #detail { width: 1fr; }
    #header { height: auto; }
    #log { height: 1fr; }
    #prompt { display: none; height: auto; max-height: 8; }
    #prompt.show { display: block; }
    #running { height: 1; color: $warning; display: none; text-wrap: nowrap;
               text-overflow: ellipsis; }
    #queued { height: auto; display: none; }
    #queued Button { width: 1fr; height: 1; min-width: 0; border: none; content-align: left middle;
                     text-wrap: nowrap; text-overflow: ellipsis; }
    #actions { height: auto; display: none; }
    #actions.show { display: block; }
    #actions Button { width: 1fr; height: 1; min-width: 0; border: none; margin: 0 0 0 0; }
    .narrow #left { width: 1fr; min-width: 0; }
    .narrow #detail { display: none; }
    .narrow.detail-view #left { display: none; }
    .narrow.detail-view #detail { display: block; }
    """
    BINDINGS = [
        Binding("j", "down", "down", show=False),
        Binding("k", "up", "up", show=False),
        Binding("enter", "open", "open"),
        Binding("space", "log", "log"),
        Binding("escape", "leave", "back"),
        Binding("a", "approve", "yes"),
        Binding("d", "deny", "no"),
        Binding("s", "send", "send"),
        Binding("i", "interrupt", "stop"),
        Binding("o", "adopt", "adopt"),
        Binding("n", "new", "new"),
        Binding("/", "filter", "find"),
        Binding("x", "close", "remove"),
        Binding("z", "undo_remove", "undo", show=False),
        Binding("e", "toggle_ended", "ended", show=False),
        Binding("b", "toggle_notify", "bell", show=False),
        Binding("c", "copy", "copy", show=False),
        Binding("l", "later", "later", show=False),
        Binding("u", "digest", "new", show=False),
        Binding("p", "toggle_projects", "projects", show=False),
        Binding("r", "rename", "rename", show=False),
        Binding("question_mark", "help", "keys"),
        Binding("m", "toggle_markdown", "markdown", show=False),
        Binding("ctrl+r", "refresh", "refresh", show=False),
        Binding("h", "history", "history", show=False),
        Binding("q", "quit", "quit"),
        *[Binding(str(n), f"choose({n})", show=False) for n in range(1, 5)],
    ]

    def __init__(
        self,
        notify: bool = False,
        rpc: Rpc = rpc,
        follow: Follow = follow,
        agents: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.agents = agents  # installed agents, found on first `n`
        self.removed: str | None = None  # last session `x` removed, for `z`
        self.by_project = False  # `p`: group the list by project instead of status
        self.usage = ""
        self.conflicts: set[tuple[str, str]] | None = None
        self.selected_pane: str | None = None  # follows a placeholder to its real session
        self.rpc, self.follow_fn = rpc, follow
        self.notify_on = notify
        self.rows: list[dict[str, Any]] = []
        self.selected: str | None = None
        self.filter = ""
        self.prompt_mode: str | None = None
        self.prompt_target: str | None = None  # the session the box was opened for
        # what this dashboard sent, per session, until it quits: {at, text, outcome, error}
        self.sent: dict[str, list[dict[str, str]]] = {}
        self.approvals: set[str] | None = None  # None until the first poll
        self.shape: list[tuple[Any, ...]] = []
        self.show_ended = False
        self.markdown = True  # render replies as Markdown; `m` shows the raw text
        self.choices: dict[str, str] | None = None  # options the approval buttons show
        self.last_outcome, self.last_error = "", ""  # of the last control() call
        self.queued_shown: list[str] | None = None
        self.header_shown: tuple[str, bool, str] | None = None
        self.keys_shown: list[bool | None] = []

    def compose(self) -> ComposeResult:
        yield Header(icon="◆")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield OptionList(id="list")
                yield Static(id="files-title")
                yield Files(HOME, id="files")
            with Vertical(id="detail"):
                yield Static(id="header")
                yield Transcript(id="log")
                yield Static(id="running")
                yield Vertical(id="queued")
                yield Vertical(id="actions")
        yield Prompt(id="prompt", soft_wrap=True, show_line_numbers=False)
        yield Footer(compact=True, show_command_palette=False)

    async def on_mount(self) -> None:
        self.update_subtitle()
        await self.poll()
        self.set_interval(1.0, self.poll)
        await self.poll_usage()
        self.set_interval(30.0, self.poll_usage)
        self.set_interval(1.0, self.tick_running)
        self.query_one("#list", OptionList).focus()

    @property
    def narrow(self) -> bool:
        return self.size.width < NARROW

    def on_resize(self) -> None:
        if self.screen.has_class("narrow") != self.narrow:
            self.screen.set_class(self.narrow, "narrow")
            self.shape = []  # cards vs one-line rows
            self.rebuild()
            self.restream()  # timestamp format

    def current(self) -> dict[str, Any] | None:
        return next((r for r in self.rows if r["id"] == self.selected), None)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Footer shows only what applies to the current view and session (and fits a phone)."""
        r = self.current()
        detail = not self.narrow or self.screen.has_class("detail-view")
        live = bool(r and r["controllable"] and r["status"] != "ended")
        if action in ("approve", "deny", "choose"):
            return bool(r and r["status"] == "awaiting_approval")
        if action == "open":  # in the transcript, only jumping in is left to do
            return r is not None and (not (self.narrow and detail) or bool(r["tmux_pane"]))
        if action == "log":
            return r is not None and self.narrow and not detail
        if action == "leave":
            return bool(self.prompt_mode) or (self.narrow and detail)
        if action in ("filter", "toggle_ended"):
            return not detail or not self.narrow
        if action == "send":
            return r is not None
        if action == "interrupt":
            return live and bool(r and r["status"] == "working")
        if action == "adopt":
            return detail and bool(r and not r["controllable"])
        if action == "close":  # quitting an agent mid-turn would abort the turn
            busy = ("working", "awaiting_approval")
            return r is not None and not (r["tmux_pane"] and r["status"] in busy)
        if action == "new":
            return not detail or not self.narrow
        if action == "later":
            return live and bool(r and r["status"] in ("working", "awaiting_approval"))
        if action in ("digest", "toggle_projects"):
            return not detail or not self.narrow
        if action in ("copy", "rename"):
            return detail and r is not None
        if action == "help":
            return not detail or not self.narrow
        if action == "quit":  # a phone footer has room for one row: q is in `?` there
            return not self.narrow
        if action == "undo_remove":
            return self.removed is not None
        return True

    def restream(self) -> None:
        if self.selected:
            sid = self.selected
            self.call_after_refresh(self.stream, sid)

    def update_subtitle(self, extra: str = "") -> None:
        bits = [
            f"filter: {self.filter}" if self.filter else "",
            "bell on" if self.notify_on else "",
            self.usage,
        ]
        self.sub_title = " · ".join(b for b in [extra, *bits] if b)

    # --- data ---

    async def poll(self) -> None:
        try:
            rows = await self.rpc("ls")
        except Exception as e:  # daemon restarting etc.: keep the last view
            self.update_subtitle(f"daemon: {e}")
            return
        self.update_subtitle()
        self.announce(rows)
        self.rows = rows
        self.rebuild()
        r = self.current()
        if r and r.get("unread") and (not self.narrow or self.screen.has_class("detail-view")):
            with contextlib.suppress(Exception):
                await self.rpc("seen", id=r["id"])  # it's on screen: read

    async def poll_usage(self) -> None:
        try:
            self.usage = usage_text(await self.rpc("usage")).plain
        except Exception:
            self.usage = ""
        self.update_subtitle()

    def announce(self, rows: list[dict[str, Any]]) -> None:
        now = {r["id"] for r in rows if r["status"] == "awaiting_approval"}
        if self.approvals is not None:
            for r in rows:
                if r["id"] in now - self.approvals:
                    msg = f"{short_id(r['id'])} needs approval: {r['title'] or short_cwd(r['cwd'])}"
                    self.notify(msg, severity="warning", timeout=8)
                    if self.notify_on:
                        self.bell()
                        mac_notify(msg)
        self.approvals = now
        found = {(r["id"], c) for r in rows for c in r.get("conflicts") or []}
        if self.conflicts is not None:
            for sid, c in found - self.conflicts:
                self.notify(f"{short_id(sid)} {c}", severity="warning", timeout=10)
        self.conflicts = found

    def filtered(self) -> list[dict[str, Any]]:
        f = self.filter.lower()
        return [r for r in self.rows if f in f"{r['id']} {r['title']} {r['cwd']}".lower()]

    def action_toggle_ended(self) -> None:
        self.show_ended = not self.show_ended
        self.rebuild()

    def rebuild(self) -> None:
        rows = self.filtered()
        shape = [
            (
                r["id"], r["status"], r["title"], r["controllable"], age(r["last_activity"]),
                r.get("unread"), len(r.get("conflicts") or []), len(r.get("queued") or []),
                ctx_text(r, short=True).plain, cache_text(r, short=True).plain,
            )
            for r in rows
        ] + [(self.show_ended, self.by_project)]  # fmt: skip
        if shape != self.shape:
            self.shape = shape
            ol = self.query_one("#list", OptionList)
            opts: list[Option] = self.project_options(rows) if self.by_project else []
            for status, label in [] if self.by_project else GROUPS:
                group = [r for r in rows if r["status"] == status]
                if not group:
                    continue
                style = STATUS_STYLE[status] if status == "awaiting_approval" else "bold dim"
                folded = status == "ended" and not self.show_ended and not self.filter
                more = " · e shows" if folded else ""
                head = Text(f" {label} ({len(group)}){more} ", style=style)
                opts.append(Option(head, disabled=True))
                if not folded:
                    fmt = card_text if self.narrow else row_text
                    opts += [Option(fmt(r), id=r["id"]) for r in group]
            ids = [o.id for o in opts]
            if ids == [o.id for o in ol.options]:  # same rows in place: new text, no relayout
                for i, o in enumerate(opts):
                    if o.prompt != ol.get_option_at_index(i).prompt:
                        ol.replace_option_prompt_at_index(i, o.prompt)
            else:
                ol.set_options(opts)
                if self.selected not in ids and self.selected_pane:  # placeholder became real
                    same = next((r for r in rows if r["tmux_pane"] == self.selected_pane), None)
                    if same:
                        self.selected = None
                        self.select(same["id"])
                if self.selected and self.selected in ids:
                    ol.highlighted = ids.index(self.selected)
                elif any(ids):  # setting .highlighted posts no event, so select explicitly
                    i = next(i for i, x in enumerate(ids) if x)
                    ol.highlighted = i
                    self.select(ids[i])
        row = next((r for r in rows if r["id"] == self.selected), None)
        # Each update relayouts the whole screen, transcript included: only on a change
        header = (str(row), self.narrow, cache_text(row).plain) if row else None
        if row and header != self.header_shown:
            self.header_shown = header
            self.query_one("#header", Static).update(session_header(row, compact=self.narrow))
        self.update_actions(row)
        self.update_queued(row)
        self.tick_running()
        keys = [self.check_action(b.action, ()) for b in self.BINDINGS if isinstance(b, Binding)]
        if keys != self.keys_shown:
            self.keys_shown = keys
            self.refresh_bindings()

    @work(exclusive=True, group="files")
    async def show_files(self, r: dict[str, Any] | None) -> None:
        """The selected session's project (git root, else its folder) under the list."""
        from agentctl.daemon.fleet import project_root

        root = await asyncio.to_thread(project_root, r["cwd"]) if r and r["cwd"] else ""
        title = self.query_one("#files-title", Static)
        tree = self.query_one("#files", Files)
        if not root or not os.path.isdir(root):
            title.update(Text(f"FILES · {tilde(root)} is gone" if root else "FILES"))
            tree.display = False
            return
        title.update(Text(f"FILES · {tilde(root)}"))
        tree.display = True
        if str(tree.path) != root:
            tree.path = root

    def project_options(self, rows: list[dict[str, Any]]) -> list[Option]:
        """The list grouped by project (git repo, else folder): projects with sessions that
        need you first; inside each, the daemon's order (needs-you first, then recent)."""
        if not self.show_ended and not self.filter:
            rows = [r for r in rows if r["status"] != "ended"]
        projects: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            projects.setdefault(r.get("project") or short_cwd(r["cwd"]), []).append(r)
        opts: list[Option] = []
        for name, group in projects.items():  # rows are sorted, so is this
            head = Text(f" {name} ({len(group)}) ", style="bold")
            for status in ("awaiting_approval", "awaiting_input", "working"):
                n = sum(r["status"] == status for r in group)
                if n:
                    head.append(f"{n} ").append_text(status_text(status)).append(" ")
            opts.append(Option(head, disabled=True))
            opts += [
                Option(
                    card_text(r, folder=False) if self.narrow else row_text(r, status=True),
                    id=r["id"],
                )
                for r in group
            ]
        if not self.show_ended and not self.filter:
            opts.append(
                Option(Text(" ended sessions hidden · e shows", style="dim"), disabled=True)
            )
        return opts

    def action_toggle_projects(self) -> None:
        self.by_project = not self.by_project
        self.notify("grouped by " + ("project" if self.by_project else "status"), timeout=2)
        self.rebuild()

    def update_actions(self, row: dict[str, Any] | None) -> None:
        """Tappable approval buttons, one per option, under the transcript."""
        appr = row and row["status"] == "awaiting_approval" and row["pending_approval"]
        choices = (appr["options"] or {"1": "Approve", "0": "Deny"}) if appr else None
        if choices == self.choices:
            return
        self.choices = choices
        box = self.query_one("#actions", Vertical)
        box.remove_children()
        box.set_class(bool(choices), "show")
        for k, label in (choices or {}).items():
            no = label.lower().startswith(("no", "reject", "deny"))
            variant: ButtonVariant = "error" if no else "success" if k == "1" else "default"
            box.mount(Button(f"{k}. {label}" if k != "0" else label, id=f"opt{k}", variant=variant))

    def tick_running(self) -> None:
        """While it works: what it's running and for how long, so a long command isn't
        mistaken for a stuck screen."""
        row, log = self.current(), self.query_one("#log", Transcript)
        line = self.query_one("#running", Static)
        line.display = bool(row and row["status"] == "working" and log.last_ts)
        if line.display:
            if log.calls:
                label, ts = list(log.calls.values())[-1]
                line.update(Text(f"▸ {label} · running {since(ts)}"))
            else:
                line.update(Text(f"… thinking · {since(log.last_ts)}"))

    def update_queued(self, row: dict[str, Any] | None) -> None:
        """Messages waiting for the agent to be free; tap one to cancel it."""
        items = list((row or {}).get("queued") or [])
        if items == self.queued_shown:
            return
        self.queued_shown = items
        box = self.query_one("#queued", Vertical)
        box.remove_children()
        box.display = bool(items)
        for i, text in enumerate(items):
            box.mount(Button(f"queued: {first_line(text)}  ✕", name=str(i)))

    @on(Button.Pressed, "#queued Button")
    async def unqueue_pressed(self, event: Button.Pressed) -> None:
        if self.selected:
            i = int(event.button.name or 0)
            try:
                await self.rpc("unqueue", id=self.selected, index=i)
                self.notify("✓ cancelled", timeout=2)
            except Exception as e:
                self.notify(str(e), severity="error", timeout=8)
            await self.poll()

    @on(Button.Pressed, "#actions Button")
    async def option_pressed(self, event: Button.Pressed) -> None:
        key = (event.button.id or "")[3:]
        if key == "0":
            await self.action_deny()
        elif key:
            await self.control("approve", allow=True, choice=int(key))

    @on(OptionList.OptionHighlighted)
    def highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.select(event.option.id)

    def select(self, sid: str | None) -> None:
        if sid and sid != self.selected:
            self.selected = sid
            self.selected_pane = (self.current() or {}).get("tmux_pane")
            self.stream(sid)
            self.show_files(self.current())
            self.update_actions(self.current())
            self.update_queued(self.current())
            self.refresh_bindings()

    @work(exclusive=True, group="follow")
    async def stream(self, sid: str) -> None:
        """Follow the session's events. If the stream drops (e.g. the daemon restarted), keep
        reconnecting, with the transcript reloaded each time, until another session is picked."""
        log = self.query_one("#log", Transcript)
        while True:
            row = self.current()
            try:
                it = self.follow_fn(sid)
                first = await anext(it)
                await log.reset(self.narrow, self.markdown, row.get("seen_at") if row else None)
                s = first["session"]
                self.query_one("#header", Static).update(session_header(s, compact=self.narrow))
                with self.batch_update():
                    for ev in first["events"]:
                        log.add(ev)
                self.tick_running()
                async for ev in it:
                    log.add(ev)
            except (OSError, RuntimeError, StopAsyncIteration):
                pass
            if self.selected != sid:
                return
            await asyncio.sleep(1)

    # --- navigation ---

    def action_down(self) -> None:
        w = self.focused
        if isinstance(w, (OptionList, DirectoryTree)):
            w.action_cursor_down()
        elif isinstance(w, Transcript):
            w.scroll_down()

    def action_up(self) -> None:
        w = self.focused
        if isinstance(w, (OptionList, DirectoryTree)):
            w.action_cursor_up()
        elif isinstance(w, Transcript):
            w.scroll_up()

    @on(OptionList.OptionSelected, "#list")  # not the lists in dialogs
    async def action_open(self) -> None:
        r = self.current()
        if r and r["tmux_pane"]:
            await self.action_attach()
        elif r and not r["controllable"] and r["status"] != "ended":
            self.action_adopt()  # open in another terminal: offer to move it here
        else:
            self.action_log()

    def action_log(self) -> None:
        if self.selected:
            self.screen.add_class("detail-view")
            self.query_one("#log", Transcript).focus()
            self.refresh_bindings()

    def action_leave(self) -> None:
        if self.prompt_mode:
            self.close_prompt(clear_filter=self.prompt_mode == "filter")
            return
        self.screen.remove_class("detail-view")
        self.query_one("#list", OptionList).focus()
        self.refresh_bindings()

    # --- prompt (send / filter) ---

    def open_prompt(self, mode: str, placeholder: str, value: str = "") -> None:
        self.prompt_mode, self.prompt_target = mode, self.selected
        inp = self.query_one("#prompt", Prompt)
        sends = self.sent.get(self.selected or "", []) if mode in ("send", "queue") else []
        inp.sends, inp.recall, inp.draft = [s["text"] for s in sends], 0, ""
        inp.placeholder = placeholder
        inp.load_text(value)
        inp.move_cursor(inp.document.end)
        inp.add_class("show")
        inp.focus()

    def close_prompt(self, clear_filter: bool = False) -> None:
        if clear_filter:
            self.filter = ""
            self.rebuild()
            self.update_subtitle()
        self.prompt_mode = None
        self.query_one("#prompt", Prompt).remove_class("show")
        target = "#log" if self.screen.has_class("detail-view") else "#list"
        self.query_one(target).focus()

    def action_send(self) -> None:
        # Always open the box, even where sending will fail (the error says why and the text is
        # kept): otherwise what you type next would land on the list as commands (a, d, x, …).
        if self.selected:
            self.open_prompt(
                "send", f"message to {short_id(self.selected)} · enter sends · esc cancels"
            )

    def action_filter(self) -> None:
        self.open_prompt("filter", "filter by id, title or folder · esc clears", self.filter)

    @on(TextArea.Changed, "#prompt")
    def filter_changed(self, event: TextArea.Changed) -> None:
        if self.prompt_mode == "filter":
            self.filter = event.text_area.text
            self.rebuild()
            self.update_subtitle()

    @on(Prompt.Submitted)
    async def prompt_submitted(self, event: Prompt.Submitted) -> None:
        mode, target, text = self.prompt_mode, self.prompt_target, event.value
        placeholder = self.query_one("#prompt", Prompt).placeholder
        self.close_prompt()
        if mode in ("send", "queue") and text.strip() and target:
            ok = await self.control(mode, sid=target, text=text)
            outcome = self.last_outcome if ok else "failed"
            at = datetime.now().strftime("%H:%M:%S")
            entry = {"at": at, "text": text, "outcome": outcome, "error": self.last_error}
            self.sent.setdefault(target, []).append(entry)
            if not ok:
                self.open_prompt(mode, str(placeholder), text)  # failed: keep what was typed
                self.prompt_target = target
        elif mode == "rename":
            await self.control("rename", sid=target, title=text)

    # --- control ---

    async def control(self, method: str, sid: str | None = None, **params: Any) -> bool:
        sid = sid or self.selected
        if not sid:
            return False
        try:
            self.last_error = ""
            r = await self.rpc(method, id=sid, **params)
            if "queued" in r:
                method = "queue"
            self.last_outcome = "queued" if method == "queue" else "sent"
            done = r.get("chosen") or {
                "send": "sent",
                "interrupt": "interrupted",
                "close": "closed",
                "rename": "renamed",
                "queue": f"busy: queued ({len(r.get('queued') or [])} waiting)",
            }.get(method, method)
            self.notify(f"✓ {short_id(sid)} {done}", timeout=3)
            ok = True
        except Exception as e:
            self.notify(str(e), severity="error", timeout=8)
            self.last_error = str(e)
            ok = False
        await self.poll()
        return ok

    async def action_approve(self) -> None:
        await self.control("approve", allow=True)

    async def action_deny(self) -> None:
        await self.control("approve", allow=False)

    async def action_choose(self, n: int) -> None:
        row = next((r for r in self.rows if r["id"] == self.selected), None)
        if row and row["status"] == "awaiting_approval":
            await self.control("approve", allow=True, choice=n)

    async def action_interrupt(self) -> None:
        await self.control("interrupt")

    # --- copy / rename / help ---

    def action_copy(self) -> None:
        """OSC 52: lands in the clipboard of the terminal showing this, e.g. the phone's."""
        text, what = self.query_one("#log", Transcript).last_reply, "last reply"
        if not text:
            self.notify(f"no {what} to copy", timeout=3)
            return
        self.copy_to_clipboard(text)
        self.notify(f"copied the {what} ({len(text)} chars)", timeout=3)

    def action_later(self) -> None:
        if self.selected:
            to = short_id(self.selected)
            self.open_prompt("queue", f"for {to} once it's free · enter queues · esc cancels")

    @work
    async def action_digest(self) -> None:
        try:
            rows = await self.rpc("digest")
        except Exception as e:
            self.notify(str(e), severity="error")
            return
        if not rows:
            self.notify("nothing new since you last looked", timeout=3)
            return
        sid = await self.push_screen_wait(Digest(rows))
        if sid:
            self.select(sid)
            self.rebuild()
            self.action_log()  # came to read what's new

    def action_rename(self) -> None:
        r = self.current()
        if r:
            self.open_prompt("rename", "new name · empty restores the agent's · esc cancels",
                             r["title"] or "")  # fmt: skip

    def action_help(self) -> None:
        self.push_screen(Help())

    # --- new / adopt / close ---

    @work
    async def action_new(self) -> None:
        if self.agents is None:
            from agentctl.setup import AGENTS, agent_exe

            found = await asyncio.gather(*(asyncio.to_thread(agent_exe, a) for a in AGENTS))
            self.agents = [a for a, exe in zip(AGENTS, found, strict=True) if exe]
        rows = sorted(self.rows, key=lambda r: r["last_activity"], reverse=True)
        cur = self.current()
        folders = [cur["cwd"]] if cur and cur["cwd"] else []
        folders += [r["cwd"] for r in rows if r["cwd"] and r["cwd"] not in folders]
        choice = await self.push_screen_wait(NewSession(self.agents, folders[:8]))
        if not choice:
            return
        agent, folder = choice
        params: dict[str, Any] = {"agent": agent, "cwd": folder, "name": None, "extra": []}
        try:
            r = await self.rpc("launch", **params, path=os.environ.get("PATH", ""))
        except Exception as e:
            self.notify(str(e), severity="error", timeout=10)
            return
        await self.jump(r["socket"], r["pane"])

    async def jump(self, sock: str | None, pane: str) -> None:
        """Into the agent's pane until ctrl-] d (or it exits), then back here on that session.
        In the ctrl-] w popup the agent is under the popup: close it instead."""
        from agentctl.cli import _attach

        popup = os.environ.get("TMUX", "").split(",")[0] == sock
        with self.suspend():
            _attach(sock, pane)
        if popup:
            self.exit()
            return
        await self.poll()
        row = next((r for r in self.rows if r["tmux_pane"] == pane), None)
        if row:
            self.select(row["id"])
            self.rebuild()

    @work
    async def action_adopt(self) -> None:
        from agentctl.cli import AdoptError, DaemonError, adopt_plan, adopt_run

        if not self.selected:
            return
        try:
            plan = await asyncio.to_thread(adopt_plan, self.selected, False)
        except (AdoptError, DaemonError) as e:
            self.notify(str(e), severity="error", timeout=10)
            return
        if not await self.push_screen_wait(Confirm(plan.question, "Move it here")):
            return
        try:
            await asyncio.to_thread(adopt_run, plan)
        except (AdoptError, DaemonError) as e:
            self.notify(str(e), severity="error", timeout=10)
            return
        self.notify(f"✓ {short_id(plan.session['id'])} is now in agentctl", timeout=5)
        await self.poll()

    @work
    async def action_close(self) -> None:
        r = self.current()
        if not r:
            return
        name, sid = r["title"] or short_cwd(r["cwd"]), r["id"]
        if r["tmux_pane"]:
            q = f"Quit {name} and remove it from the list? It stays resumable: o reopens it."
        else:
            q = f"Remove {name} from the list? Nothing is deleted; it's back if it gets active."
        if not await self.push_screen_wait(
            Confirm(q, "Quit and remove" if r["tmux_pane"] else "Remove")
        ):
            return
        try:
            if r["tmux_pane"]:
                await self.rpc("close", id=sid)
            await self.rpc("hide", id=sid)
            self.removed = sid
            self.notify(f"✓ removed {short_id(sid)} · z brings it back", timeout=6)
        except Exception as e:
            self.notify(str(e), severity="error", timeout=8)
        await self.poll()

    async def action_undo_remove(self) -> None:
        sid, self.removed = self.removed, None
        if sid:
            try:
                await self.rpc("unhide", id=sid)
                self.notify(f"✓ {short_id(sid)} is back", timeout=3)
            except Exception as e:
                self.notify(str(e), severity="error", timeout=8)
            await self.poll()
            self.select(sid)
            self.rebuild()

    @work
    async def action_history(self) -> None:
        r = self.current()
        if not r:
            return
        arrived: set[str] = set()
        with contextlib.suppress(Exception):  # which sends show up in its transcript
            evs = (await self.rpc("tail", id=r["id"], n=500))["events"]
            arrived = {
                str(e["payload"].get("text", "")).strip()
                for e in evs
                if e["kind"] == "message" and e["payload"].get("role") == "user"
            }
        title = r["title"] or short_id(r["id"])
        text = await self.push_screen_wait(History(title, self.sent.get(r["id"], []), arrived))
        if text:
            self.open_prompt("send", f"message to {short_id(r['id'])} · enter sends", text)

    async def action_refresh(self) -> None:
        """Reload everything: list, transcript, files."""
        self.shape = []
        await self.poll()
        self.restream()
        self.query_one("#files", Files).reload()
        self.notify("refreshed", timeout=2)

    def action_toggle_markdown(self) -> None:
        self.markdown = not self.markdown
        self.notify("replies: " + ("rendered" if self.markdown else "raw text"), timeout=2)
        self.restream()

    def action_toggle_notify(self) -> None:
        self.notify_on = not self.notify_on
        self.update_subtitle()
        self.notify(f"notifications {'on' if self.notify_on else 'off'}", timeout=2)

    async def action_attach(self) -> None:
        if not self.selected:
            return
        try:
            r = await self.rpc("pane", id=self.selected)
        except Exception as e:
            self.notify(str(e), severity="error", timeout=8)
            return
        await self.jump(r["socket"], r["pane"])


def mac_notify(msg: str) -> None:
    """macOS notification; the text is an argv item, never part of the AppleScript source."""
    if shutil.which("osascript"):
        script = [
            "-e",
            "on run argv",
            "-e",
            'display notification (item 1 of argv) with title "agentctl"',
            "-e",
            "end run",
        ]
        subprocess.Popen(
            ["osascript", *script, msg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
