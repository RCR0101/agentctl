"""`agentctl watch`: Textual dashboard. A thin client like the CLI: polls `ls` every second and
streams `follow` for the selected session. Below 80 columns it becomes a single pane
(list -> enter -> detail -> esc) with two-line cards, a compact header, tappable approval
buttons and a footer that only shows the keys that apply, for ~45 columns in Termius.

The transcript is one widget per entry, so text can be selected with the mouse (ctrl+c
copies), replies render as Markdown, and each run of tool calls folds into one line.
`v` mirrors the session's terminal with tappable keys, for prompts the transcript can't show
(trust this folder?, update menus, pickers); `n` starts a session, `o` adopts, `x` closes."""

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Collapsible,
    Footer,
    Header,
    Input,
    Markdown,
    OptionList,
    Static,
)
from textual.widgets.button import ButtonVariant
from textual.widgets.option_list import Option

from agentctl import SOCKET_PATH
from agentctl.cli import (
    SRC,
    STATUS_STYLE,
    age,
    ctx_text,
    render,
    session_header,
    short_cwd,
    status_text,
    usage_text,
)
from agentctl.models import short_id

# Screen-view keys: (label, tmux key). Ctrl-C is left out on purpose: `x` closes properly.
SCREEN_KEYS = [
    ("↑", "Up"), ("↓", "Down"), ("←", "Left"), ("→", "Right"), ("⏎", "Enter"),
    ("esc", "Escape"), ("tab", "Tab"), ("1", "1"), ("2", "2"), ("3", "3"), ("y", "y"), ("n", "n"),
]  # fmt: skip

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
        t.append("⚠ ", style="bold yellow")
    if r.get("queued"):
        t.append(f"⏳{len(r['queued'])} ", style="cyan")
    return t


def card_text(r: dict[str, Any], folder: bool = True) -> Text:
    """Phone row: the title gets the whole first line; id, source, age, context, folder below."""
    approval = r["status"] == "awaiting_approval"
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append("● " if r["controllable"] else "· ", style="green" if r["controllable"] else "dim")
    t.append_text(badges(r))
    title_style = "bold yellow" if approval else "dim" if r["status"] == "ended" else "bold"
    t.append(r["title"] or short_cwd(r["cwd"]), style=title_style)
    t.append(f"\n  {short_id(r['id'])} {SRC[r['source']]} {age(r['last_activity'])} ", style="dim")
    if r.get("context_tokens") is not None:
        t.append_text(ctx_text(r, short=True)).append(" ")
    if folder:
        t.append(short_cwd(r["cwd"]), style="blue")
    else:
        t.append_text(status_text(r["status"]))
    return t


def row_text(r: dict[str, Any]) -> Text:
    approval = r["status"] == "awaiting_approval"
    t = Text(no_wrap=True, overflow="ellipsis")
    t.append("● " if r["controllable"] else "· ", style="green" if r["controllable"] else "dim")
    t.append(short_id(r["id"]), style="bold yellow" if approval else "bold")
    t.append(f" {SRC[r['source']]} ", style="dim")
    t.append(f"{age(r['last_activity']):>3} ", style="dim")
    t.append_text(badges(r))
    title_style = "yellow" if approval else "dim" if r["status"] == "ended" else ""
    t.append(r["title"] or short_cwd(r["cwd"]), style=title_style)
    return t


TOOL_KINDS = ("tool_call", "tool_result", "status_change")


class Transcript(VerticalScroll):
    """A session's events as widgets. Consecutive tool calls and results fold into one
    Collapsible ("⚙ 6 tools · bash ×5, read"); click or Enter on it to expand."""

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
        self.group_ts, self.first, self.failed = "", "", 0
        self.last_reply = ""  # for `c`
        self.seen_at: str | None = None
        self.any = False

    async def reset(self, narrow: bool, markdown: bool, seen_at: str | None = None) -> None:
        await self.remove_children()
        self.narrow, self.markdown, self.group = narrow, markdown, None
        self.last_reply = ""
        self.seen_at, self.any = seen_at, False
        self.anchor()  # stay at the newest entry until the user scrolls up

    def add(self, ev: dict[str, Any]) -> None:
        kind, p = ev["kind"], ev["payload"]
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
                self.group = Collapsible(self.body, collapsed=True)
                self.tools.clear()
                self.group_ts = fmt_ts(ev["ts"], self.narrow)
                self.first, self.failed = "", 0
                self.mount(self.group)
            if kind == "tool_call":
                self.tools[p["name"]] += 1
                self.first = self.first or (p["summary"].splitlines() or [""])[0]
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
        if n == 1:
            name = next(iter(self.tools))
            t.append(f"⚙ {name} ", style="yellow").append(self.first[:120], style="dim")
        else:
            names = ", ".join(f"{k} ×{v}" if v > 1 else k for k, v in self.tools.most_common())
            t.append(f"⚙ {n} tools ", style="yellow").append(names, style="dim")
        if self.failed:
            t.append(f" · {self.failed} failed", style="red")
        return t


def fmt_ts(iso: str, narrow: bool) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%H:%M" if narrow else "%H:%M:%S")


HELP = """\
[b]list[/b]
  enter / tap   open a session
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
  i             interrupt the running turn
  v             its terminal, live, with tappable keys
  c             copy the last reply (in v: the screen)
  r             rename (agentctl's own name for it)
  o             adopt: move it into agentctl
  x             close it (stays resumable)
  t             attach to its terminal (ctrl-] d returns)
  m             replies as raw text / Markdown
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


class KeyButton(Button, can_focus=False):
    """Tapping a key mustn't take focus from the session (Esc would stop working)."""


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


class NewSession(ModalScreen[tuple[str, str] | None]):
    """Pick an agent and a folder (recent ones listed) to start a session in."""

    DEFAULT_CSS = """
    NewSession { align: center middle; }
    NewSession > Vertical { width: 95%; max-width: 70; height: auto; max-height: 90%;
                            border: round $accent; padding: 0 1; background: $surface; }
    NewSession .row { height: auto; }
    NewSession .row Button { width: 1fr; min-width: 0; }
    NewSession OptionList { height: auto; max-height: 10; border: none; }
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
            yield Input(value=tilde(self.folders[0]) if self.folders else "~", id="folder")
            if self.folders:
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

    @on(OptionList.OptionSelected, "#recent")
    def recent(self, event: OptionList.OptionSelected) -> None:
        self.query_one("#folder", Input).value = str(event.option.prompt)

    @on(Input.Submitted, "#folder")
    def start(self) -> None:
        folder = os.path.expanduser(self.query_one("#folder", Input).value.strip())
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
    #list { width: 45%; min-width: 36; border: none; padding: 0;
            text-wrap: nowrap; text-overflow: ellipsis; }
    #detail { width: 1fr; }
    #header { height: auto; }
    #log { height: 1fr; }
    #prompt { display: none; }
    #prompt.show { display: block; }
    #actions { height: auto; display: none; }
    #actions.show { display: block; }
    #actions Button { width: 1fr; height: 1; min-width: 0; border: none; margin: 0 0 0 0; }
    #screen { height: 1fr; display: none; overflow-x: auto; }
    #screen-text { width: auto; }
    #keys { height: auto; display: none; layout: grid; grid-size: 12; grid-gutter: 0 1; }
    .narrow #keys { grid-size: 6; }
    #keys Button { width: 1fr; min-width: 0; height: 1; border: none; }
    .screen-view #log { display: none; }
    .screen-view #screen { display: block; }
    .screen-view #keys { display: block; }
    .narrow #list { width: 1fr; min-width: 0; }
    .narrow #detail { display: none; }
    .narrow.detail-view #list { display: none; }
    .narrow.detail-view #detail { display: block; }
    """
    BINDINGS = [
        Binding("j", "down", "down", show=False),
        Binding("k", "up", "up", show=False),
        Binding("enter", "open", "open"),
        Binding("escape", "leave", "back"),
        Binding("s", "send", "send"),
        Binding("a", "approve", "yes"),
        Binding("d", "deny", "no"),
        Binding("i", "interrupt", "stop"),
        Binding("v", "toggle_screen", "screen"),
        Binding("t", "attach", "tmux"),
        Binding("o", "adopt", "adopt"),
        Binding("x", "close", "close"),
        Binding("n", "new", "new"),
        Binding("/", "filter", "find"),
        Binding("e", "toggle_ended", "ended"),
        Binding("b", "toggle_notify", "bell", show=False),
        Binding("c", "copy", "copy", show=False),
        Binding("l", "later", "later"),
        Binding("u", "digest", "new", show=False),
        Binding("p", "toggle_projects", "projects", show=False),
        Binding("r", "rename", "rename", show=False),
        Binding("question_mark", "help", "keys"),
        Binding("m", "toggle_markdown", "markdown", show=False),
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
        self.screen_text = ""  # last screen-view capture, for `c`
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
        self.approvals: set[str] | None = None  # None until the first poll
        self.shape: list[tuple[Any, ...]] = []
        self.show_ended = False
        self.markdown = True  # render replies as Markdown; `m` shows the raw text
        self.choices: dict[str, str] | None = None  # options the approval buttons show

    def compose(self) -> ComposeResult:
        yield Header(icon="◆")
        with Horizontal(id="main"):
            yield OptionList(id="list")
            with Vertical(id="detail"):
                yield Static(id="header")
                yield Transcript(id="log")
                with VerticalScroll(id="screen"):
                    yield Static(id="screen-text")
                with Horizontal(id="keys"):
                    for label, key in SCREEN_KEYS:
                        yield KeyButton(label, name=key, id=f"key-{key}")
                yield Vertical(id="actions")
        yield Input(id="prompt")
        yield Footer(compact=True, show_command_palette=False)

    async def on_mount(self) -> None:
        self.update_subtitle()
        await self.poll()
        self.set_interval(1.0, self.poll)
        await self.poll_usage()
        self.set_interval(30.0, self.poll_usage)
        self.set_interval(1.0, self.refresh_screen)
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
        if action == "open":
            return not detail
        if action == "leave":
            return bool(self.prompt_mode) or (self.narrow and detail)
        if action in ("filter", "toggle_ended"):
            return not detail or not self.narrow
        if action == "send":
            return live and detail
        if action == "interrupt":
            return live and detail and bool(r and r["status"] == "working")
        if action == "attach":
            return detail and bool(r and r["tmux_pane"])
        if action == "toggle_screen":
            return detail and bool(r and r["tmux_pane"])
        if action == "adopt":
            return detail and bool(r and not r["controllable"])
        if action == "close":
            return detail and bool(r and r["tmux_pane"] and r["status"] != "working")
        if action == "new":
            return not detail or not self.narrow
        if action == "later":
            return live and detail and bool(r and r["status"] in ("working", "awaiting_approval"))
        if action in ("digest", "toggle_projects"):
            return not detail or not self.narrow
        if action in ("copy", "rename"):
            return detail and r is not None
        if action == "help":
            return not detail or not self.narrow
        if action == "quit":
            return not (self.narrow and detail)  # esc first; keeps the phone footer to one row
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
                self.notify(f"⚠ {short_id(sid)} {c}", severity="warning", timeout=10)
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
            ol.set_options(opts)
            ids = [o.id for o in opts]
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
        if row:
            self.query_one("#header", Static).update(session_header(row, compact=self.narrow))
        self.update_actions(row)
        self.refresh_bindings()

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
            fmt = (lambda r: card_text(r, folder=False)) if self.narrow else row_text
            opts += [Option(fmt(r), id=r["id"]) for r in group]
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
            self.update_actions(self.current())
            self.refresh_bindings()

    @work(exclusive=True, group="follow")
    async def stream(self, sid: str) -> None:
        log = self.query_one("#log", Transcript)
        row = self.current()
        await log.reset(self.narrow, self.markdown, row.get("seen_at") if row else None)
        try:
            it = self.follow_fn(sid)
            first = await anext(it)
            s = first["session"]
            self.query_one("#header", Static).update(session_header(s, compact=self.narrow))
            with self.batch_update():
                for ev in first["events"]:
                    log.add(ev)
            # pending approvals: the buttons under the log show the options
            async for ev in it:
                log.add(ev)
        except (OSError, RuntimeError, StopAsyncIteration) as e:
            log.mount(Static(Text(f"stream ended: {e}", style="dim")))

    # --- navigation ---

    def action_down(self) -> None:
        w = self.focused
        if isinstance(w, OptionList):
            w.action_cursor_down()
        elif isinstance(w, Transcript):
            w.scroll_down()

    def action_up(self) -> None:
        w = self.focused
        if isinstance(w, OptionList):
            w.action_cursor_up()
        elif isinstance(w, Transcript):
            w.scroll_up()

    @on(OptionList.OptionSelected)
    def action_open(self) -> None:
        if self.selected:
            self.screen.add_class("detail-view")
            self.query_one("#log", Transcript).focus()
            self.refresh_bindings()

    def action_leave(self) -> None:
        if self.prompt_mode:
            self.close_prompt(clear_filter=self.prompt_mode == "filter")
            return
        if self.screen.has_class("screen-view"):  # screen view -> transcript
            self.screen.remove_class("screen-view")
            self.refresh_bindings()
            return
        self.screen.remove_class("detail-view")
        self.query_one("#list", OptionList).focus()
        self.refresh_bindings()

    # --- prompt (send / filter) ---

    def open_prompt(self, mode: str, placeholder: str, value: str = "") -> None:
        self.prompt_mode = mode
        inp = self.query_one("#prompt", Input)
        inp.placeholder, inp.value = placeholder, value
        inp.add_class("show")
        inp.focus()

    def close_prompt(self, clear_filter: bool = False) -> None:
        if clear_filter:
            self.filter = ""
            self.rebuild()
            self.update_subtitle()
        self.prompt_mode = None
        self.query_one("#prompt", Input).remove_class("show")
        target = "#log" if self.screen.has_class("detail-view") else "#list"
        self.query_one(target).focus()

    def action_send(self) -> None:
        if self.selected:
            self.open_prompt(
                "send", f"message to {short_id(self.selected)} · enter sends · esc cancels"
            )

    def action_filter(self) -> None:
        self.open_prompt("filter", "filter by id, title or folder · esc clears", self.filter)

    @on(Input.Changed, "#prompt")
    def filter_changed(self, event: Input.Changed) -> None:
        if self.prompt_mode == "filter":
            self.filter = event.value
            self.rebuild()
            self.update_subtitle()

    @on(Input.Submitted, "#prompt")
    async def prompt_submitted(self, event: Input.Submitted) -> None:
        mode = self.prompt_mode
        self.close_prompt()
        if mode == "send" and event.value.strip():
            await self.control("send", text=event.value)
        elif mode == "rename":
            await self.control("rename", title=event.value)
        elif mode == "queue" and event.value.strip():
            await self.control("queue", text=event.value)

    # --- control ---

    async def control(self, method: str, **params: Any) -> None:
        if not self.selected:
            return
        try:
            r = await self.rpc(method, id=self.selected, **params)
            done = r.get("chosen") or {
                "send": "sent",
                "interrupt": "interrupted",
                "close": "closed",
                "rename": "renamed",
                "queue": "queued for when it's free",
            }.get(method, method)
            self.notify(f"✓ {short_id(self.selected)} {done}", timeout=3)
        except Exception as e:
            self.notify(str(e), severity="error", timeout=8)
        await self.poll()

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

    # --- screen view ---

    async def action_toggle_screen(self) -> None:
        on = not self.screen.has_class("screen-view")
        self.screen.set_class(on, "screen-view")
        self.refresh_bindings()
        if on:
            await self.refresh_screen()

    async def refresh_screen(self) -> None:
        if not self.selected or not self.screen.has_class("screen-view"):
            return
        box = self.query_one("#screen", VerticalScroll)
        at_end = box.is_vertical_scroll_end or box.max_scroll_y == 0
        try:
            r = await self.rpc("screen", id=self.selected)
            text = Text.from_ansi(r["screen"], no_wrap=True)
        except Exception as e:
            text = Text(f"can't show its screen: {e}", style="dim")
        self.screen_text = text.plain
        self.query_one("#screen-text", Static).update(text)
        if at_end:
            box.call_after_refresh(box.scroll_end, animate=False)

    @on(Button.Pressed, "#keys Button")
    async def key_pressed(self, event: Button.Pressed) -> None:
        if self.selected and event.button.name:
            try:
                await self.rpc("keys", id=self.selected, keys=[event.button.name])
            except Exception as e:
                self.notify(str(e), severity="error", timeout=6)
            self.set_timer(0.15, self.refresh_screen)  # once the agent has redrawn

    # --- copy / rename / help ---

    def action_copy(self) -> None:
        """OSC 52: lands in the clipboard of the terminal showing this, e.g. the phone's."""
        if self.screen.has_class("screen-view"):
            text, what = self.screen_text, "screen"
        else:
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
            self.action_open()

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
        await self.poll()
        self.select(r["session_id"])
        self.action_open()
        hint = "send it a first message (s)" if agent != "claude" else "v shows its screen"
        self.notify(f"✓ started {agent} in {tilde(folder)} · {hint}", timeout=6)

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
        name = r["title"] or short_cwd(r["cwd"])
        q = f"Quit {name}? It stays resumable: o (adopt) reopens it."
        if await self.push_screen_wait(Confirm(q, "Quit it")):
            await self.control("close")

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
        tm = [shutil.which("tmux") or "tmux", *(["-S", r["socket"]] if r["socket"] else [])]
        pane = r["pane"]
        with self.suspend():
            subprocess.run([*tm, "select-window", "-t", pane], check=False)
            subprocess.run([*tm, "select-pane", "-t", pane], check=False)
            verb = "switch-client" if os.environ.get("TMUX") else "attach-session"
            subprocess.run([*tm, verb, "-t", pane], check=False)


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
