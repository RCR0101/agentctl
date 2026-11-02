"""`agentctl watch`: Textual dashboard. A thin client like the CLI: polls `ls` every second and
streams `follow` for the selected session. Below 80 columns it becomes a single pane
(list -> enter -> detail -> esc) with two-line cards, a compact header, tappable approval
buttons and a footer that only shows the keys that apply, for ~45 columns in Termius.

The transcript is one widget per entry, so text can be selected with the mouse (ctrl+c
copies), replies render as Markdown, and each run of tool calls folds into one line.
Desktop Enter opens the agent's terminal (ctrl-] d returns); Space opens a fullscreen
transcript, and Esc returns to the dashboard. Phone Enter opens the fullscreen transcript.
`n` starts a session, `o` adopts, `x` hides. Local review and delivery history use recorded
events and Git, without additional model calls."""

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import time
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.style import Style
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.notifications import SeverityLevel
from textual.screen import Screen
from textual.theme import BUILTIN_THEMES
from textual.widgets import (
    Button,
    Collapsible,
    DirectoryTree,
    Footer,
    Header,
    Markdown,
    OptionList,
    Static,
    TextArea,
)
from textual.widgets.button import ButtonVariant
from textual.widgets.collapsible import CollapsibleTitle
from textual.widgets.directory_tree import DirEntry
from textual.widgets.option_list import Option
from textual.widgets.tree import TreeNode

from agentctl import HOME, SOCKET_PATH, STATE_DIR
from agentctl.cli import (
    age,
    cache_text,
    ctx_text,
    project_display,
    render,
    session_header,
    short_cwd,
    status_text,
    usage_text,
)
from agentctl.models import short_id
from agentctl.tui.common import Rpc, nocase, sent_key
from agentctl.tui.composer import ControlResult, Prompt
from agentctl.tui.presentation import card_text, group_heading, row_text
from agentctl.tui.review import Review
from agentctl.tui.screens import (
    Confirm,
    Diff,
    Digest,
    Help,
    History,
    Menu,
    NewSession,
    Questions,
    Search,
    first_line,
    fmt_ts,
    since,
    tilde,
)
from agentctl.waiting import context_full, stuck_minutes

THEME_FILE = STATE_DIR / "theme"  # the last theme picked with T or --theme
DEFAULT_THEME = "textual-dark"


def saved_theme() -> str:
    with contextlib.suppress(OSError):
        name = THEME_FILE.read_text().strip()
        if name in BUILTIN_THEMES:
            return name
    return DEFAULT_THEME


REPLIES_FILE = STATE_DIR / "replies"  # quick replies under the send box, one per line
DEFAULT_REPLIES = [
    "continue",
    "yes, go ahead",
    "run the tests and fix what fails",
    "summarise what you changed",
]
LONG_TURN = 120  # seconds: a turn this long gets a "finished" toast
CACHE_WARN = 120  # seconds of prompt cache left when a waiting Claude session gets a warning
ARRIVAL_GRACE = 30  # seconds a sent message may stay out of a free agent's transcript
STALL = 10  # seconds without a finished poll before the watchdog restarts polling
RPC_TIMEOUT = 30


def quick_replies(row: dict[str, Any] | None = None) -> list[str]:
    """Your quick replies; `/compact` first when that Claude session's context is nearly full."""
    replies = DEFAULT_REPLIES
    with contextlib.suppress(OSError):
        lines = [ln.strip() for ln in REPLIES_FILE.read_text().splitlines() if ln.strip()]
        if lines:
            replies = lines
    if row and row.get("source") == "claude_code" and context_full(row) is not None:
        return ["/compact", *(r for r in replies if r != "/compact")]
    return replies


def name_of(r: dict[str, Any]) -> str:
    return str(r["title"] or short_cwd(r["cwd"]))


def user_texts(evs: list[dict[str, Any]]) -> set[str]:
    return {
        sent_key(str(e["payload"].get("text", "")))
        for e in evs
        if e["kind"] == "message"
        and e["payload"].get("role") == "user"
        and not e["payload"].get("agent_id")
    }


GROUPS = [
    ("awaiting_approval", "NEEDS APPROVAL"),
    ("awaiting_input", "WAITING FOR INPUT"),
    ("working", "WORKING"),
    ("idle", "IDLE"),
    ("ended", "ENDED, last 24h"),
]
NARROW = 80
# Phone: (label, action) along the bottom of each page, and under its "more" (only what applies)
TAPBAR = {
    "list": [("+ new", "new"), ("search", "search"), ("unread", "digest"), ("more", "menu")],
    "session": [("‹ back", "leave"), ("reply", "send"), ("files", "files"),
                ("stop", "interrupt"), ("more", "menu")],
    "files": [("‹ back", "leave")],
}  # fmt: skip
TAPBAR_VARIANT: dict[str, ButtonVariant] = {
    "new": "primary", "send": "primary", "submit": "primary", "review": "warning",
}  # fmt: skip
MENU = {
    "list": [("teams", "teams"), ("search transcripts", "search"),
             ("commands", "command_palette"),
             ("group by project / by status", "toggle_projects"),
             ("show or hide ended (last 24h)", "toggle_ended"), ("filter", "filter"),
             ("bell + notifications on / off", "toggle_notify"), ("theme", "change_theme"),
             ("keys", "help"), ("quit", "quit")],
    "session": [("open its terminal", "attach"), ("review latest turn", "results"),
                ("commands", "command_palette"), ("queue a message for later", "later"),
                ("what you sent it", "history"), ("copy the last reply", "copy"),
                ("rename", "rename"), ("adopt into agentctl", "adopt"),
                ("hide (keep running)", "hide"), ("quit and hide", "close"),
                ("raw text / Markdown", "toggle_markdown"),
                ("keys", "help")],
    "files": [],
}  # fmt: skip

Follow = Callable[[str], AsyncIterator[dict[str, Any]]]


async def rpc(method: str, **params: Any) -> Any:
    try:
        async with asyncio.timeout(RPC_TIMEOUT):
            return await _rpc(method, **params)
    except TimeoutError:
        hint = "; check sent history before retrying" if method in ("send", "queue") else ""
        raise RuntimeError(
            f"{method}: daemon did not respond within {RPC_TIMEOUT}s{hint}"
        ) from None


async def _rpc(method: str, **params: Any) -> Any:
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


TOOL_KINDS = ("tool_call", "tool_result", "status_change")


class Sessions(OptionList):
    """Desktop clicks select a preview; Enter opens the agent. Phone taps open its page."""

    async def _on_click(self, event: events.Click) -> None:
        event.prevent_default()  # this handler explicitly owns the base class's click behavior
        if self.app.size.width < NARROW:
            await super()._on_click(event)
            return
        event.stop()
        index = event.style.meta.get("option")
        if index is not None and not self.get_option_at_index(index).disabled:
            self.highlighted = index


class ToolGroup(Collapsible):
    """A folded run of tool calls. Tap to open; kept out of Tab, so Tab moves between the
    panes (list, files, transcript) and doesn't stop on every `▸ Bash` line."""

    def on_mount(self) -> None:
        self.query_one(CollapsibleTitle).can_focus = False


class Files(DirectoryTree):
    """The project at a glance: no dot-files, caches or dependency folders."""

    ICON_NODE, ICON_NODE_EXPANDED, ICON_FILE = "▸ ", "▾ ", "  "  # not Textual's emoji

    HIDE = {"node_modules", "__pycache__", "venv", "target", "dist", "build", "site-packages"}

    edited: frozenset[str] = frozenset()  # files the selected session edited: marked ●
    edited_dirs: frozenset[str] = frozenset()  # their folders: a dim ●

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [p for p in paths if not p.name.startswith(".") and p.name not in self.HIDE]

    def set_edited(self, paths: Iterable[str]) -> None:
        edited = frozenset(paths)
        if edited != self.edited:
            self.edited = edited
            self.edited_dirs = frozenset(str(d) for f in edited for d in Path(f).parents)
            self._invalidate()  # redraw the labels

    def render_label(self, node: TreeNode[DirEntry], base_style: Style, style: Style) -> Text:
        label = super().render_label(node, base_style, style)
        path = str(node.data.path) if node.data else ""
        if path in self.edited:
            label.append(" ●", style="bold yellow")
        elif path in self.edited_dirs:
            label.append(" ●", style="dim yellow")
        return label


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


class Watch(App[None]):
    TITLE = "agentctl"
    CSS = """
    CommandPalette SearchIcon { display: none; }
    #main { height: 1fr; }
    #overview { height: auto; padding: 0 1; background: $panel; }
    #connection { height: 1; padding: 0 1; color: $text-muted; }
    #connection.degraded { color: $warning; }
    #left { width: 35%; min-width: 32; border-right: solid $panel; }
    #list { border: none; padding: 0 1; text-wrap: nowrap; text-overflow: ellipsis; }
    #list { height: 1fr; max-height: 100%; }
    #empty { display: none; height: auto; padding: 1; color: $text-muted; }
    #empty.show { display: block; }
    .show-files #list { height: 50%; }
    #usage { display: none; }
    #usage.has-usage { display: block; }
    #usage { height: auto; padding: 0 1; border-top: solid $panel; text-wrap: nowrap;
             text-overflow: ellipsis; }
    #files-title { display: none; color: $text-muted; text-style: bold; padding: 0 1;
                   border-top: solid $panel;
                   text-wrap: nowrap; text-overflow: ellipsis; }
    #files { height: 1fr; padding: 0; }
    #detail { width: 1fr; }
    .detail-view #left { display: none; }
    .detail-view #detail { display: block; }
    #header { height: auto; }
    #log { height: 1fr; }
    #prompt { display: none; height: auto; max-height: 8; }
    #prompt.show { display: block; }
    #composer-title { display: none; height: auto; padding: 0 1; background: $panel; }
    #composer-title.show { display: block; }
    #replies { display: none; height: 1; }
    #replies.show { display: block; }
    #replies Button { width: 1fr; min-width: 0; height: 1; border: none; margin: 0 1 0 0;
                      text-wrap: nowrap; text-overflow: ellipsis; }
    #running { height: 1; color: $warning; display: none; text-wrap: nowrap;
               text-overflow: ellipsis; }
    #queued { height: auto; display: none; }
    #queued Button { width: 1fr; height: 1; min-width: 0; border: none; content-align: left middle;
                     text-wrap: nowrap; text-overflow: ellipsis; }
    #actions { height: auto; display: none; }
    #actions.show { display: block; }
    #actions Button { width: 1fr; height: 1; min-width: 0; border: none; margin: 0 0 0 0; }
    #actions Static { max-height: 3; padding: 0 1; }
    #tapbar { display: none; height: 3; }
    .composing #tapbar { display: block; }
    #tapbar Button { width: 1fr; min-width: 0; height: 3; margin: 0; }
    #files { display: none; }
    .show-files #files-title, .show-files #files.has-root { display: block; }
    .narrow #left { width: 1fr; min-width: 0; border: none; }
    .narrow #detail { display: none; }
    .narrow.detail-view #left { display: none; }
    .narrow.detail-view #detail { display: block; }
    /* phone: one full-height page at a time, a bar of large tap targets at the bottom */
    .narrow #list { height: 1fr; max-height: 100%; padding: 0; }
    .narrow #usage { dock: top; border-top: none; border-bottom: solid $panel; }
    .narrow #files-title, .narrow #files.has-root { display: none; }
    .narrow Footer { display: none; }
    FooterKey .footer-key--key { padding: 0 0 0 2; }
    FooterKey .footer-key--description { padding: 0 0 0 1; }
    .narrow #tapbar { display: block; }
    .narrow #replies { layout: vertical; height: auto; }
    .narrow #replies Button { width: 100%; margin: 0 0 1 0; background: $panel;
                              text-align: left; content-align: left middle; }
    .narrow #actions Button { height: auto; min-height: 3; text-wrap: wrap; }
    .narrow.files-view #left { display: block; }
    .narrow.files-view #detail, .narrow.files-view #list, .narrow.files-view #usage.has-usage {
        display: none;
    }
    .narrow.files-view #files-title, .narrow.files-view #files.has-root { display: block; }
    """
    BINDINGS = nocase(
        [
            Binding("j", "down", "down", show=False),
            Binding("k", "up", "up", show=False),
            Binding("enter", "open", "open"),
            Binding("space", "log", "fullscreen"),
            Binding("ctrl+t", "attach", "terminal", show=False),
            Binding("g", "review", "review", show=False),
            Binding("w", "results", "review turn", show=False),
            Binding("v", "files", "files", show=False),
            Binding("escape", "leave", "back"),
            Binding("a", "approve", "approve"),
            Binding("d", "deny", "deny"),
            Binding("s", "send", "reply"),
            Binding("ctrl+enter,ctrl+s", "submit", show=False),
            Binding("i", "interrupt", "stop"),
            Binding("o", "adopt", "adopt"),
            Binding("n", "new", "new"),
            Binding("/", "filter", "find"),
            Binding("x", "hide", "hide"),
            Binding("ctrl+x", "close", "quit and hide", show=False),
            Binding("z", "undo_remove", "undo", show=False),
            Binding("e", "toggle_ended", "ended", show=False),
            Binding("b", "toggle_notify", "bell", show=False),
            Binding("c", "copy", "copy", show=False),
            Binding("l", "later", "later", show=False),
            Binding("u", "digest", "unread"),
            Binding("p", "toggle_projects", "projects", show=False),
            Binding("r", "rename", "rename", show=False),
            Binding("question_mark", "help", "keys"),
            Binding("m", "toggle_markdown", "markdown", show=False),
            Binding("ctrl+r", "refresh", "refresh", show=False),
            Binding("h", "history", "history", show=False),
            Binding("t", "change_theme", "theme", show=False),
            Binding("f", "search", "search", show=False),
            Binding("ctrl+g", "teams", "teams", show=False),
            Binding("q", "quit", "quit"),
            Binding("ctrl+q", "quit_app", show=False, priority=True),
            *[Binding(str(n), f"choose({n})", show=False) for n in range(1, 5)],
        ]
    )

    def __init__(
        self,
        notify: bool = False,
        rpc: Rpc = rpc,
        follow: Follow = follow,
        agents: list[str] | None = None,
        theme: str | None = None,
        start_teams: bool = False,
    ) -> None:
        super().__init__()
        self.saved_theme = saved_theme()
        self.theme = theme or self.saved_theme
        self.agents = agents  # installed agents, found on first `n`
        self.start_teams = start_teams
        self.removed: str | None = None  # last session `x` removed, for `z`
        self.by_project = True  # `p`: switch between project and status grouping
        self.conflicts: set[tuple[str, str]] | None = None
        self.selected_pane: str | None = None  # follows a placeholder to its real session
        self.rpc, self.follow_fn = rpc, follow
        self.notify_on = notify
        self.rows: list[dict[str, Any]] = []
        self.selected: str | None = None
        self.filter = ""
        self.prompt_mode: str | None = None
        self.prompt_target: str | None = None  # the session the box was opened for
        self.prompt_return_focus = "list"
        self.prompt_token = 0
        self.submitting = False
        self.drafts: dict[tuple[str, str], str] = {}
        self.composer_shown = ""
        self.files_return_fullscreen = False
        self.files_return_focus = "list"
        # Durable daemon receipts, plus connection failures that never reached the daemon.
        self.sent: dict[str, list[dict[str, Any]]] = {}
        self.last_success: float | None = None
        self.connection_error = ""
        self.stream_state = ""
        self.connection_shown: tuple[str, bool] | None = None
        self.overview_shown = ""
        self.empty_shown = ""
        self.approvals: set[str] | None = None  # None until the first poll
        self.shape: list[tuple[Any, ...]] = []
        self.show_ended = False
        self.markdown = True  # render replies as Markdown; `m` shows the raw text
        self.actions_shown: tuple[Any, ...] | None = None  # tool, options and control availability
        self.queued_shown: Any | None = None
        self.header_shown: tuple[str, bool, str] | None = None
        self.working_since: dict[str, float] = {}  # when each running turn was first seen
        self.cache_warned: set[tuple[str, str]] = set()  # (session, cache expiry) warned about
        self.stuck_warned: set[tuple[str, str]] = set()  # (session, last activity) warned about
        self.context_warned: set[str] = set()  # sessions over CONTEXT_FULL; cleared below it
        self.opened: str | None = None  # opened from search: listed even if old and ended
        self.polls = 0
        self.polled_at = time.monotonic()  # when a poll last finished; see watchdog
        self.watched_at = time.monotonic()
        self.keys_shown: list[bool | None] = []
        self.tapbar_shown: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        yield Header(icon="◆")
        yield Static(id="overview")
        yield Static(id="connection")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Static(id="empty")
                yield Sessions(id="list")
                yield Static(id="usage")
                yield Static(id="files-title")
                yield Files(HOME, id="files")
            with Vertical(id="detail"):
                yield Static(id="header")
                yield Transcript(id="log")
                yield Static(id="running")
                yield Vertical(id="queued")
                yield Vertical(id="actions")
        yield Static(id="composer-title")
        yield Prompt(id="prompt", soft_wrap=True, show_line_numbers=False)
        yield Horizontal(id="replies")
        yield Horizontal(id="tapbar")
        yield Footer(show_command_palette=True)

    def get_system_commands(self, screen: Screen[Any]) -> Iterable[SystemCommand]:
        yield from super().get_system_commands(screen)
        commands = [
            ("Teams", "Connect a coordinator to worker sessions and review assignments", "teams"),
            ("Review approvals", "Go to the next session needing a decision", "review"),
            ("Unread replies", "See what changed since you last looked", "digest"),
            ("Review latest turn", "Request, full reply, tool outcomes and Git changes", "results"),
            ("Reply to session", "Send a message to the selected session", "send"),
            ("Project files", "Show or hide the project's file browser", "files"),
            ("Open terminal", "Attach to the selected agent's terminal", "attach"),
            (
                "Fullscreen transcript",
                "Read the session across the whole screen; Esc returns",
                "log",
            ),
            ("Sent history", "Review delivery states or reuse a message", "history"),
            ("New session", "Choose an agent and project folder", "new"),
            ("Search transcripts", "Search messages across sessions", "search"),
            ("Group sessions", "Switch between project and status groups", "toggle_projects"),
            ("Show ended sessions", "Toggle ended sessions in the list", "toggle_ended"),
            ("Queue follow-up", "Send a message once the agent is free", "later"),
            ("Adopt session", "Move this session into agentctl", "adopt"),
            ("Refresh", "Reconnect and reload session data", "refresh"),
        ]
        for title, help_text, action in commands:
            if self.check_action(action, ()):

                async def invoke(action: str = action) -> None:
                    await self.run_action(action)

                yield SystemCommand(title, help_text, invoke)

    async def on_mount(self) -> None:
        self.update_subtitle()
        await self.poll()
        self.poll_timer = self.set_interval(1.0, self.poll)
        await self.poll_usage()
        self.set_interval(30.0, self.poll_usage)
        self.set_interval(1.0, self.tick_running)
        self.set_interval(1.0, self.watchdog)
        self.query_one("#list", OptionList).focus()
        if self.start_teams:
            self.action_teams()

    @property
    def narrow(self) -> bool:
        return self.size.width < NARROW

    def on_resize(self) -> None:
        dashboard = self.screen_stack[0]
        if dashboard.has_class("narrow") != self.narrow:
            dashboard.set_class(self.narrow, "narrow")
            self.shape = []  # cards vs one-line rows
            self.rebuild()
            self.restream()  # timestamp format

    def current(self) -> dict[str, Any] | None:
        return next((r for r in self.rows if r["id"] == self.selected), None)

    def watch_theme(self, theme: str) -> None:
        if theme != self.saved_theme:  # a pick (T, --theme), not the start-up default
            self.saved_theme = theme
            with contextlib.suppress(OSError):
                THEME_FILE.write_text(theme + "\n")

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Footer shows only what applies to the current view and session (and fits a phone)."""
        if self.screen.has_class("teams-dialog") and action != "quit_app":
            return False
        r = self.current()
        if action == "quit_app":
            return True
        if self.prompt_mode:
            # Editor text must not become session commands if focus briefly leaves the box.
            if action not in ("leave", "submit"):
                return False
            return not self.submitting
        detail = not self.narrow or self.screen.has_class("detail-view")
        live = bool(r and r["controllable"] and r["status"] != "ended")
        if action in ("approve", "deny", "choose"):
            return bool(r and r["controllable"] and r["status"] == "awaiting_approval")
        if action == "open":
            return r is not None and self.focused is self.query_one("#list")
        if action == "log":
            return r is not None
        if action == "leave":
            return bool(self.filter) or any(
                self.screen.has_class(c) for c in ("detail-view", "files-view", "show-files")
            )
        if action in ("filter", "toggle_ended"):
            return not detail or not self.narrow
        if action == "send":
            return r is not None
        if action == "interrupt":
            return live and bool(r and r["status"] == "working")
        if action == "adopt":
            return detail and bool(r and not r["controllable"])
        if action == "hide":
            return r is not None
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
        if action == "answer":
            return bool(self.questions())
        if action in ("files", "results"):
            return r is not None and (detail or not self.narrow)
        if action == "review":
            return any(x["status"] == "awaiting_approval" for x in self.rows)
        if action == "attach":
            return bool(r and r["tmux_pane"])
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
        ]
        self.sub_title = " · ".join(b for b in [extra, *bits] if b)

    def update_connection(self) -> None:
        elapsed = time.monotonic() - self.last_success if self.last_success is not None else None
        degraded = bool(self.connection_error) or elapsed is None or elapsed > STALL
        if degraded:
            age_text = f" · last update {int(elapsed)}s ago" if elapsed is not None else ""
            text = f"Reconnecting{age_text} · {self.connection_error or 'waiting for daemon'}"
        else:
            text = "Live · " + (self.stream_state or "select a session to read its transcript")
        degraded = degraded or "reconnecting" in self.stream_state.lower()
        if (text, degraded) == self.connection_shown:
            return
        self.connection_shown = (text, degraded)
        box = self.query_one("#connection", Static)
        box.update(Text(text), layout=False)
        box.set_class(degraded, "degraded")

    # --- data ---

    async def poll(self) -> None:
        try:
            rows = await self.rpc("ls")
        except Exception as e:  # daemon restarting etc.: keep the last view
            self.update_subtitle(f"daemon: {e}")
            self.connection_error = str(e)
            self.update_connection()
            self.polled_at = time.monotonic()
            return
        self.last_success = time.monotonic()
        self.connection_error = ""
        self.update_connection()
        self.update_subtitle()
        if self.opened and all(r["id"] != self.opened for r in rows):
            with contextlib.suppress(Exception):
                rows.append((await self.rpc("tail", id=self.opened, n=0))["session"])
        self.announce(rows)
        self.rows = rows
        self.rebuild()
        await self.check_arrivals(rows)
        self.polls += 1
        if self.selected and self.polls % 5 == 0:
            self.load_edited(self.selected)
        r = self.current()
        if (
            r
            and r.get("unread")
            and self.screen is self.screen_stack[0]
            and (not self.narrow or self.screen.has_class("detail-view"))
        ):
            with contextlib.suppress(Exception):
                await self.rpc("seen", id=r["id"])  # it's on screen: read
        self.polled_at = time.monotonic()

    def watchdog(self) -> None:
        """The poll timer awaits each poll, so one that never finishes (or a cancelled one,
        which ends the timer) would freeze the list for good: restart it."""
        now, last = time.monotonic(), self.watched_at
        self.update_connection()
        self.watched_at = now
        if now - last > 3:  # everything was frozen (e.g. the terminal stopped reading): wait
            self.polled_at = now
        elif now - self.polled_at > STALL:
            self.polled_at = now
            self.poll_timer.stop()  # cancels a poll stuck inside it
            self.poll_timer = self.set_interval(1.0, self.poll)
            self.notify("the list had stopped refreshing; restarted it", severity="warning")

    async def poll_usage(self) -> None:
        """Plan limits under the list, with when each window resets."""
        try:
            text = usage_text(await self.rpc("usage"), resets=True)
        except Exception:
            text = Text()
        box = self.query_one("#usage", Static)
        box.update(text)
        box.set_class(bool(text.plain), "has-usage")

    def alert(self, msg: str, severity: SeverityLevel = "warning", timeout: float = 8) -> None:
        """A toast; with `b` on, also the bell and a Mac notification."""
        self.notify(msg, severity=severity, timeout=timeout)
        if self.notify_on:
            self.bell()
            mac_notify(msg)

    def announce(self, rows: list[dict[str, Any]]) -> None:
        now = {r["id"] for r in rows if r["status"] == "awaiting_approval"}
        if self.approvals is not None:
            for r in rows:
                if r["id"] in now - self.approvals:
                    asks = (r["pending_approval"] or {}).get("questions")
                    what = "has a question" if asks else "needs approval"
                    self.alert(f"{short_id(r['id'])} {what}: {name_of(r)}")
        self.approvals = now
        t, utc = time.time(), datetime.now(UTC)
        for r in rows:
            sid, st = r["id"], r["status"]
            if st in ("working", "awaiting_approval"):
                self.working_since.setdefault(sid, t)
            elif sid in self.working_since:
                took = t - self.working_since.pop(sid)
                if st != "ended" and took >= LONG_TURN:
                    mins = round(took / 60)
                    self.alert(f"{short_id(sid)} finished after {mins} min: {name_of(r)}",
                               "information")  # fmt: skip
            exp = r.get("cache_expires_at")
            if exp and st in ("awaiting_input", "awaiting_approval"):
                left = (datetime.fromisoformat(exp) - utc).total_seconds()
                if 0 < left <= CACHE_WARN and (sid, exp) not in self.cache_warned:
                    self.cache_warned.add((sid, exp))
                    self.alert(f"{short_id(sid)}'s prompt cache expires in {int(left)}s; reply "
                               f"before then to keep it: {name_of(r)}")  # fmt: skip
            stuck = stuck_minutes(r)
            if stuck is not None and (sid, r["last_activity"]) not in self.stuck_warned:
                self.stuck_warned.add((sid, r["last_activity"]))
                self.alert(f"{short_id(sid)} has been working with no output for {stuck} min; "
                           f"see its terminal (ctrl+t) or stop it (i): {name_of(r)}")  # fmt: skip
            pct = context_full(r)
            if pct is None:
                self.context_warned.discard(sid)
            elif sid not in self.context_warned:
                self.context_warned.add(sid)
                hint = (
                    "/compact is in its quick replies"
                    if r["source"] == "claude_code"
                    else ("consider a fresh session for the next task")
                )
                self.alert(f"{short_id(sid)} has used {pct}% of its context; {hint}: "
                           f"{name_of(r)}")  # fmt: skip

    async def check_arrivals(self, rows: list[dict[str, Any]]) -> None:
        """Did what you sent show up in the transcript? A toast when it does; a warning if it
        still hasn't ARRIVAL_GRACE seconds after the agent was free to take it."""
        byid = {r["id"]: r for r in rows}
        for sid, entries in list(self.sent.items()):
            waiting = [
                e
                for e in entries
                if e["outcome"] not in ("failed", "cancelled") and "settled" not in e
            ]
            if not waiting:
                continue
            try:
                history = await self.rpc("history", id=sid)
                receipts = {e["id"]: e for e in history} if isinstance(history, list) else {}
                events = (await self.rpc("tail", id=sid, n=200))["events"]
                got = user_texts(events)
            except Exception:
                continue
            r = byid.get(sid)
            free = bool(r and r["status"] in ("awaiting_input", "idle") and not r.get("queued"))
            for e in waiting:
                receipt = receipts.get(e.get("id", ""))
                arrived = (
                    receipt.get("settled") == "arrived"
                    if receipt
                    else ("id" not in e and sent_key(e["text"]) in got)
                )
                if receipt:
                    e["outcome"] = receipt["outcome"]
                    if e["outcome"] in ("failed", "cancelled"):
                        continue
                if arrived:
                    e["settled"] = "arrived"
                    self.notify(f"{short_id(sid)} has your message: {first_line(e['text'])}",
                                timeout=3)  # fmt: skip
                elif not free:
                    e.pop("free_at", None)
                elif time.time() - e.setdefault("free_at", time.time()) >= ARRIVAL_GRACE:
                    e["settled"] = "missing"
                    self.alert(f"{short_id(sid)} still hasn't got {first_line(e['text'])!r}; "
                               "h shows what you sent, to resend it", timeout=15)  # fmt: skip
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
        self.notify("Ended: last 24h by activity; f searches older history", timeout=5)
        self.rebuild()

    def rebuild(self) -> None:
        rows = self.filtered()
        overview = Text()
        counts = [
            (sum(r["status"] == "awaiting_approval" for r in self.rows), "approval", "yellow"),
            (sum(bool(r.get("unread")) for r in self.rows), "unread", "cyan"),
            (sum(r["status"] == "working" for r in self.rows), "working", "green"),
            (sum(bool(r.get("conflicts")) for r in self.rows), "overlap", "yellow"),
        ]
        for count, label, style in counts:
            if count:
                if overview.plain:
                    overview.append(" · ", style="dim")
                overview.append(f"{count} {label}", style=style)
        overview = overview or Text("All caught up", style="dim")
        if overview.plain != self.overview_shown:
            self.overview_shown = overview.plain
            self.query_one("#overview", Static).update(overview)
        empty = self.query_one("#empty", Static)
        visible = [r for r in rows if self.show_ended or self.filter or r["status"] != "ended"]
        empty.set_class(not visible, "show")
        empty_text = (
            "No matching sessions.\nEsc clears the filter."
            if self.filter
            else "Ended sessions hidden (last 24h).\ne shows them; f searches older history."
            if rows
            else "No recent sessions.\nn starts one; f searches older history."
        )
        if empty_text != self.empty_shown:
            self.empty_shown = empty_text
            empty.update(Text(empty_text))
        shape = [
            (
                r["id"], r["status"], r["title"], r["controllable"], age(r["last_activity"]),
                r.get("unread"), len(r.get("conflicts") or []), len(r.get("queued") or []),
                ctx_text(r, short=True).plain, cache_text(r, short=True).plain,
                repr(r.get("preview")), repr(r.get("pending_approval")), r["cwd"],
                r.get("project"), r["source"], r.get("origin"),
            )
            for r in rows
        ] + [(self.show_ended, self.by_project)]  # fmt: skip
        if shape != self.shape:
            self.shape = shape
            ol = self.query_one("#list", OptionList)
            opts: list[Option | None] = self.project_options(rows) if self.by_project else []
            for status, label in [] if self.by_project else GROUPS:
                group = [r for r in rows if r["status"] == status]
                if not group:
                    continue
                folded = status == "ended" and not self.show_ended and not self.filter
                more = " · e shows" if folded else ""
                head = group_heading(status, label, len(group), self.card_width(), more)
                opts.append(Option(head, disabled=True))
                if not folded:
                    opts += self.cards(group) if self.narrow else [
                        Option(row_text(r), id=r["id"]) for r in group
                    ]  # fmt: skip
            ids = [o.id for o in opts if o]
            if ids == [o.id for o in ol.options]:  # same rows in place: new text, no relayout
                for i, o in enumerate(o for o in opts if o):
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
                else:
                    self.selected, self.selected_pane = None, None
                    self.header_shown = None
                    self.clear_detail()
                    self.show_files(None)
        row = next((r for r in rows if r["id"] == self.selected), None)
        # Each update relayouts the whole screen, transcript included: only on a change
        header = (str(row), self.narrow, cache_text(row).plain) if row else None
        if row and header != self.header_shown:
            self.header_shown = header
            self.query_one("#header", Static).update(session_header(row, compact=self.narrow))
        self.update_actions(row)
        self.update_queued(row)
        self.update_composer()
        self.update_tapbar()
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
            tree.remove_class("has-root")
            return
        title.update(Text(f"FILES · {tilde(root)}"))
        tree.add_class("has-root")
        if str(tree.path) != root:
            tree.path = root

    def card_width(self) -> int:
        return (
            self.query_one("#list", OptionList).scrollable_content_region.width or self.size.width
        )

    def cards(self, group: list[dict[str, Any]], folder: bool = True) -> list[Option | None]:
        """Phone cards, a rule between each (None: OptionList's divider)."""
        out: list[Option | None] = []
        for r in group:
            out += [Option(card_text(r, self.card_width(), folder), id=r["id"]), None]
        return out[:-1]

    def project_options(self, rows: list[dict[str, Any]]) -> list[Option | None]:
        """The list grouped by project (git repo, else folder): projects with sessions that
        need you first; inside each, the daemon's order (needs-you first, then recent)."""
        if not self.show_ended and not self.filter:
            rows = [r for r in rows if r["status"] != "ended"]
        projects: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            projects.setdefault(
                r.get("project_path") or r.get("project") or short_cwd(r["cwd"]), []
            ).append(r)
        opts: list[Option | None] = []
        for _, group in projects.items():  # rows are sorted, so is this
            name = project_display(group[0])
            head = Text(f" {name} ({len(group)}) ", style="bold")
            for status in ("awaiting_approval", "awaiting_input", "working"):
                n = sum(r["status"] == status for r in group)
                if n:
                    head.append(f"{n} ").append_text(status_text(status)).append(" ")
            opts.append(Option(head, disabled=True))
            opts += self.cards(group, folder=False) if self.narrow else [
                Option(row_text(r, status=True), id=r["id"]) for r in group
            ]  # fmt: skip
        if not self.show_ended and not self.filter:
            opts.append(
                Option(
                    Text(" ended (last 24h) hidden · e shows · f searches older", style="dim"),
                    disabled=True,
                )
            )
        return opts

    def action_toggle_projects(self) -> None:
        self.by_project = not self.by_project
        self.notify("grouped by " + ("project" if self.by_project else "status"), timeout=2)
        self.rebuild()

    def update_actions(self, row: dict[str, Any] | None) -> None:
        """Tappable approval buttons, one per option, under the transcript, below what they
        approve (on a phone the tool call is folded away in the transcript)."""
        appr = row and row["status"] == "awaiting_approval" and row["pending_approval"]
        choices = (appr["options"] or {"1": "Approve", "0": "Deny"}) if appr else None
        tool = appr and appr.get("tool")
        if appr and (qs := appr.get("questions")):
            choices = {"q": f"answer: {qs[0].get('question')}", "0": "cancel the question"}
            tool = None  # the answer button says what it asks
        controllable = bool(row and row["controllable"])
        if (tool, choices, controllable) == self.actions_shown:
            return
        self.actions_shown = (tool, choices, controllable)
        box = self.query_one("#actions", Vertical)
        box.remove_children()
        box.set_class(bool(choices), "show")
        if tool:
            box.mount(Static(Text(f"! {tool}", style="bold yellow")))
        if choices and not controllable:
            reason = (row or {}).get("control_reason") or "Read only · answer in the owning app"
            box.mount(Static(Text(reason, style="dim")))
        for k, label in (choices or {}).items():
            no = label.lower().startswith(("no", "reject", "deny"))
            variant: ButtonVariant = "error" if no else "success" if k in "1q" else "default"
            text = label if k in "0q" else f"{k}. {label}"
            box.mount(Button(text, id=f"opt{k}", variant=variant, disabled=not controllable))

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
        error = (row or {}).get("queue_error")
        shown = (items, error)
        if shown == self.queued_shown:
            return
        self.queued_shown = shown
        box = self.query_one("#queued", Vertical)
        box.remove_children()
        box.display = bool(items or error)
        if error:
            box.mount(
                Static(Text(f"Delivery held: {error}. Inspect terminal/history before retrying."))
            )
        for i, text in enumerate(items):
            box.mount(Button(f"queued: {first_line(text)} · cancel", name=str(i)))

    @on(Button.Pressed, "#queued Button")
    async def unqueue_pressed(self, event: Button.Pressed) -> None:
        if self.selected:
            i = int(event.button.name or 0)
            try:
                await self.rpc("unqueue", id=self.selected, index=i)
                self.notify("Cancelled", timeout=2)
            except Exception as e:
                self.notify(str(e), severity="error", timeout=8)
            await self.poll()

    @on(Button.Pressed, "#actions Button")
    async def option_pressed(self, event: Button.Pressed) -> None:
        key = (event.button.id or "")[3:]
        if key == "q":
            self.action_answer()
        elif key == "0":
            await self.action_deny()
        elif key:
            await self.control("approve", allow=True, choice=int(key))

    @on(OptionList.OptionHighlighted, "#list")  # not the lists in dialogs
    def highlighted(self, event: OptionList.OptionHighlighted) -> None:
        listing = self.query_one("#list", OptionList)
        if listing.highlighted is None or (
            listing.get_option_at_index(listing.highlighted).id != event.option.id
        ):
            return  # discard a highlight posted before a refresh or a later navigation event
        self.select(event.option.id)
        if self.prompt_mode:
            self.query_one("#prompt", Prompt).focus()

    def select(self, sid: str | None) -> None:
        if sid and sid != self.selected:
            if sid != self.opened:
                self.opened = None
            self.selected = sid
            listing = self.query_one("#list", OptionList)
            for index, option in enumerate(listing.options):
                if option.id == sid and listing.highlighted != index:
                    listing.highlighted = index
                    break
            self.load_history(sid)
            self.load_edited(sid)
            self.selected_pane = (self.current() or {}).get("tmux_pane")
            self.stream(sid)
            self.show_files(self.current())
            self.update_actions(self.current())
            self.update_queued(self.current())
            self.refresh_bindings()

    @work(exclusive=True, group="follow")
    async def clear_detail(self) -> None:
        self.stream_state = "Select a session to read its transcript"
        self.update_connection()
        self.query_one("#header", Static).update(Text("Select a session", style="dim"))
        await self.query_one("#log", Transcript).reset(self.narrow, self.markdown)

    @work(exclusive=True, group="follow")
    async def stream(self, sid: str) -> None:
        """Follow the session's events. If the stream drops (e.g. the daemon restarted), keep
        reconnecting, with the transcript reloaded each time, until another session is picked."""
        log = self.query_one("#log", Transcript)
        self.stream_state = "Loading transcript"
        self.update_connection()
        while True:
            row = self.current()
            try:
                it = self.follow_fn(sid)
                first = await anext(it)
                self.stream_state = "Transcript connected"
                self.update_connection()
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
            self.stream_state = "Transcript reconnecting"
            self.update_connection()
            await asyncio.sleep(1)

    # --- navigation ---

    def on_key(self, event: events.Key) -> None:
        if (
            self.prompt_mode
            and not self.submitting
            and event.is_printable
            and self.screen is self.screen_stack[0]
        ):
            # A click on the transcript may move focus; keep subsequent typing in the draft.
            prompt = self.query_one("#prompt", Prompt)
            if self.focused is not prompt and event.character:
                event.prevent_default()
                event.stop()
                prompt.focus()
                prompt.insert(event.character)

    def action_quit_app(self) -> None:
        self.exit()

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
        if self.prompt_mode:
            self.query_one("#prompt", Prompt).focus()
            return
        row = self.current()
        if not self.narrow and row and row.get("tmux_pane"):
            await self.action_attach()
        else:
            self.action_log()

    def action_log(self) -> None:
        if self.selected:
            self.screen.remove_class("show-files", "files-view")
            self.screen.add_class("detail-view")
            self.query_one("#log", Transcript).focus()
            self.refresh_bindings()
            self.update_tapbar()

    def action_files(self) -> None:
        """Phone: a project page. Desktop: an optional browser under the session list."""
        if not self.narrow:
            shown = not self.screen.has_class("show-files")
            if shown:
                self.files_return_fullscreen = self.screen.has_class("detail-view")
                self.files_return_focus = getattr(self.focused, "id", None) or "list"
                self.screen.remove_class("detail-view")
                self.screen.add_class("show-files")
                self.query_one("#files").focus()
            else:
                self.screen.remove_class("show-files")
                self.screen.set_class(self.files_return_fullscreen, "detail-view")
                self.query_one(f"#{self.files_return_focus}").focus()
            self.refresh_bindings()
            return
        if self.screen.has_class("files-view"):
            self.screen.remove_class("files-view")
            self.query_one("#log").focus()
            self.refresh_bindings()
            self.update_tapbar()
            return
        self.screen.add_class("files-view")
        self.query_one("#files", Files).focus()
        self.update_tapbar()

    def action_leave(self) -> None:
        if self.prompt_mode:
            if self.submitting:
                return
            self.close_prompt(clear_filter=self.prompt_mode == "filter")
            return
        if self.screen.has_class("files-view"):  # back to the session
            self.screen.remove_class("files-view")
            self.query_one("#log", Transcript).focus()
        elif self.screen.has_class("show-files"):
            self.action_files()
        elif not self.screen.has_class("detail-view") and self.filter:
            self.filter = ""
            self.rebuild()
            self.update_subtitle()
        else:
            self.screen.remove_class("detail-view")
            self.query_one("#list", OptionList).focus()
        self.refresh_bindings()
        self.update_tapbar()

    def page(self) -> str:
        sc = self.screen_stack[0]  # the dashboard, even under a dialog
        return "files" if sc.has_class("files-view") else "session" if sc.has_class(
            "detail-view") else "list"  # fmt: skip

    def update_tapbar(self) -> None:
        """Phone: the big buttons along the bottom, for what this page is for."""
        if not self.narrow and not self.prompt_mode:
            if self.tapbar_shown:
                self.query_one("#tapbar").remove_children()
                self.tapbar_shown = []
            return
        page = self.page()
        if self.prompt_mode:  # typing: a phone keyboard has no esc, and enter isn't obvious
            done = {"filter": "done", "rename": "save"}.get(self.prompt_mode, "send")
            if self.prompt_mode in ("send", "queue") and self.composer_queues():
                done = "queue"
            if self.submitting:
                done = "sending…"
            wanted = [("cancel", "leave"), (done, "submit")]
        elif page == "list":
            unread = sum(1 for r in self.rows if r.get("unread"))
            waiting = sum(1 for r in self.rows if r["status"] == "awaiting_approval")
            wanted = []
            for label, a in TAPBAR[page]:
                if a == "search" and waiting:  # approving goes via its page, where you see
                    label, a = f"review {waiting}", "review"  # what; search is under more
                elif a == "digest":
                    if not unread:
                        continue
                    label = f"unread {unread}"
                wanted.append((label, a))
        else:
            wanted = TAPBAR[page]
        items = [
            (label, a)
            for label, a in wanted
            if self.prompt_mode or a == "menu" or self.check_action(a, ())
        ]
        if items == self.tapbar_shown:
            return
        self.tapbar_shown = items
        bar = self.query_one("#tapbar", Horizontal)
        bar.remove_children()
        buttons = [
            Button(
                label, name=a, variant=TAPBAR_VARIANT.get(a, "default"), disabled=self.submitting
            )
            for label, a in items
        ]
        for b in buttons:
            b.can_focus = False  # for tapping; Tab keeps moving between the panes
        bar.mount_all(buttons)

    @on(Button.Pressed, "#tapbar Button")
    async def tapbar_pressed(self, event: Button.Pressed) -> None:
        await self.run_action(event.button.name or "")

    def action_review(self) -> None:
        """Phone: the page of the highlighted session if it's waiting on you, else the first
        that is, with its approval buttons under what they approve."""
        r = self.current()
        if not (r and r["status"] == "awaiting_approval"):
            r = next((x for x in self.rows if x["status"] == "awaiting_approval"), None)
        if r:
            self.filter = ""
            self.update_subtitle()
            self.select(r["id"])
            self.rebuild()
            self.action_log()

    def action_submit(self) -> None:
        inp = self.query_one("#prompt", Prompt)
        if self.prompt_mode and not self.submitting:
            inp.action_submit()

    def action_menu(self) -> None:
        items = [(label, a) for label, a in MENU[self.page()] if self.check_action(a, ())]

        def chosen(action: str | None) -> None:
            if action:
                self.call_later(self.run_action, action)

        self.push_screen(Menu(items), chosen)

    # --- prompt (send / filter) ---

    def open_prompt(
        self, mode: str, placeholder: str, value: str | None = None, target: str | None = None
    ) -> None:
        if self.prompt_mode:
            self.query_one("#prompt", Prompt).focus()
            return
        target = target or self.selected
        self.prompt_return_focus = getattr(self.focused, "id", None) or "list"
        if self.prompt_return_focus not in ("list", "log", "files"):
            self.prompt_return_focus = "log" if self.screen.has_class("detail-view") else "list"
        self.prompt_mode, self.prompt_target = mode, target
        self.screen.add_class("composing")
        self.prompt_token += 1
        inp = self.query_one("#prompt", Prompt)
        inp.token, inp.submitted, inp.read_only = self.prompt_token, False, False
        sends = self.sent.get(target or "", []) if mode in ("send", "queue") else []
        inp.sends, inp.recall, inp.draft = [s["text"] for s in sends], 0, ""
        inp.placeholder = placeholder
        if value is None:
            value = self.drafts.get((mode, target or ""), "")
        inp.load_text(value)
        inp.move_cursor(inp.document.end)
        inp.add_class("show")
        inp.focus()
        box = self.query_one("#replies", Horizontal)
        box.remove_children()
        box.set_class(mode in ("send", "queue"), "show")
        if mode in ("send", "queue"):
            row = next((r for r in self.rows if r["id"] == target), None)
            replies = quick_replies(row)
            replies = replies[:3] if self.narrow else replies  # room to read
            box.mount_all(Button(f"› {r}" if self.narrow else r, name=r) for r in replies)
        self.update_composer()
        self.refresh_bindings()
        self.update_tapbar()

    def composer_queues(self) -> bool:
        target = next((r for r in self.rows if r["id"] == self.prompt_target), None)
        return self.prompt_mode == "queue" or bool(
            target
            and (target["status"] in ("working", "awaiting_approval") or target.get("queued"))
        )

    def update_composer(self) -> None:
        if not self.prompt_mode:
            return
        target = next((r for r in self.rows if r["id"] == self.prompt_target), None)
        name = name_of(target) if target else short_id(self.prompt_target or "")
        if self.prompt_mode in ("send", "queue"):
            verb = "Queue" if self.prompt_mode == "queue" else "Reply"
            text = f"{verb} to {name} · {short_id(self.prompt_target or '')}"
            if self.submitting:
                text += "\nSending… draft preserved"
            else:
                text += "\nEnter queues" if self.composer_queues() else "\nEnter sends"
                text += " · Shift+Enter newline · Esc saves draft"
        else:
            text = (
                "Filter sessions · Enter applies · Esc clears"
                if self.prompt_mode == "filter"
                else f"Rename {name} · Enter saves · Esc cancels"
            )
        title = self.query_one("#composer-title", Static)
        title.add_class("show")
        if text != self.composer_shown:
            self.composer_shown = text
            title.update(Text(text, style="bold"))
        for reply in self.query("#replies Button").results(Button):
            reply.disabled = self.submitting

    def close_prompt(self, clear_filter: bool = False, discard: bool = False) -> None:
        inp = self.query_one("#prompt", Prompt)
        key = (self.prompt_mode or "", self.prompt_target or "")
        if self.prompt_mode in ("send", "queue"):
            if discard:
                self.drafts.pop(key, None)
            else:
                self.drafts[key] = inp.text
        if clear_filter:
            self.filter = ""
            self.rebuild()
            self.update_subtitle()
        self.prompt_mode, self.prompt_target = None, None
        self.screen.remove_class("composing")
        self.prompt_token += 1
        inp.submitted, inp.read_only = False, False
        inp.remove_class("show")
        self.query_one("#composer-title").remove_class("show")
        self.query_one("#replies", Horizontal).remove_class("show")
        focus = self.prompt_return_focus
        if focus == "log" and self.narrow and not self.screen.has_class("detail-view"):
            focus = "list"
        if focus == "files" and not self.query_one("#files").display:
            focus = "log" if self.screen.has_class("detail-view") else "list"
        self.query_one(f"#{focus}").focus()
        self.refresh_bindings()
        self.update_tapbar()

    def action_send(self) -> None:
        # Always open the box, even where sending will fail (the error says why and the text is
        # kept): otherwise what you type next would land on the list as commands (a, d, x, …).
        if self.selected:
            self.open_prompt(
                "send", f"message to {short_id(self.selected)} · enter sends · esc cancels"
            )

    @on(Button.Pressed, "#replies Button")
    def reply_pressed(self, event: Button.Pressed) -> None:
        inp = self.query_one("#prompt", Prompt)
        if self.submitting:
            return
        inp.insert(event.button.name or "")
        inp.focus()

    @work(exclusive=True, group="edited")
    async def load_edited(self, sid: str) -> None:
        with contextlib.suppress(Exception):
            self.query_one("#files", Files).set_edited(await self.rpc("edited", id=sid))

    @on(DirectoryTree.FileSelected, "#files")
    def file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self.open_diff(str(event.path))

    @work(exclusive=True, group="diff")
    async def open_diff(self, path: str) -> None:
        from agentctl.daemon.fleet import file_diff

        self.push_screen(Diff(path, await asyncio.to_thread(file_diff, path)))

    @work
    async def action_search(self) -> None:
        sid = await self.push_screen_wait(Search(self.rpc))
        if sid:
            self.filter = ""
            self.update_subtitle()
            self.opened = sid
            self.show_ended = True  # it may well be an ended one
            self.select(sid)  # before the list is rebuilt, so the highlight lands on it
            await self.poll()
            self.show_files(self.current())  # select() ran before its row was listed
            self.action_log()

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
        if self.submitting or not self.prompt_mode or event.token != self.prompt_token:
            return
        mode, target, text = self.prompt_mode, self.prompt_target, event.value
        inp = self.query_one("#prompt", Prompt)
        if mode in ("send", "queue") and (not text.strip() or not target):
            inp.submitted = False
            inp.focus()
            return
        if mode == "filter":
            self.filter = text
            self.rebuild()
            self.close_prompt()
            return
        self.submitting, inp.read_only = True, True
        self.prompt_token += 1
        inp.token = self.prompt_token
        self.update_composer()
        self.refresh_bindings()
        self.update_tapbar()
        self.deliver_prompt(mode, target, text)

    @work(group="send")
    async def deliver_prompt(self, mode: str, target: str | None, text: str) -> None:
        """Keep input and rendering responsive while the daemon acknowledges a delivery."""
        inp = self.query_one("#prompt", Prompt)
        if mode in ("send", "queue") and target:
            created = time.time()
            result = await self.control(mode, sid=target, text=text)
            ok = bool(result)
            at = datetime.now().strftime("%H:%M:%S")
            entry = {
                "at": at,
                "created": created,
                "text": text,
                "outcome": result.outcome,
                "error": result.error,
            }
            if result.command_id:
                entry["id"] = result.command_id
            entries = self.sent.setdefault(target, [])
            if not any(e.get("id") == result.command_id for e in entries if result.command_id):
                entries.append(entry)
            if not ok:
                self.drafts[(mode, target)] = text
        elif mode == "rename":
            ok = bool(await self.control("rename", sid=target, title=text))
        else:
            ok = True
        self.submitting, inp.read_only, inp.submitted = False, False, False
        if ok:
            self.close_prompt(discard=True)
        else:
            inp.focus()
            self.update_composer()
            self.refresh_bindings()
            self.update_tapbar()

    # --- control ---

    async def control(self, method: str, sid: str | None = None, **params: Any) -> ControlResult:
        sid = sid or self.selected
        if not sid:
            return ControlResult("failed", "No session selected")
        try:
            r = await self.rpc(method, id=sid, **params)
            if "queued" in r:
                method = "queue"
            result = ControlResult(
                "queued" if method == "queue" else "sent", command_id=r.get("command_id")
            )
            done = r.get("chosen") or {
                "send": "sent",
                "interrupt": "interrupted",
                "close": "closed",
                "rename": "renamed",
                "queue": f"busy: queued ({len(r.get('queued') or [])} waiting)",
            }.get(method, method)
            self.notify(f"{short_id(sid)} {done}", timeout=3)
        except Exception as e:
            self.notify(str(e), severity="error", timeout=8)
            result = ControlResult("failed", str(e))
        self.refresh_after_control()
        return result

    @work(exclusive=True, group="control-refresh")
    async def refresh_after_control(self) -> None:
        await self.poll()

    async def action_approve(self) -> None:
        if self.questions():
            self.action_answer()
        else:
            await self.control("approve", allow=True)

    def questions(self) -> list[dict[str, Any]]:
        r = self.current()
        appr = r and r["status"] == "awaiting_approval" and r["pending_approval"]
        return list(appr.get("questions") or []) if appr else []

    @work
    async def action_answer(self) -> None:
        """The agent asked (AskUserQuestion): answer it in a form, typed into its menu."""
        sid, qs, r = self.selected, self.questions(), self.current()
        if r and qs and not r["controllable"]:  # e.g. the Codex desktop app has it open
            self.notify("it's open in another app: answer it there", severity="warning")
        elif sid and qs and (answers := await self.push_screen_wait(Questions(qs))):
            await self.control("answer", sid=sid, answers=answers)

    async def action_deny(self) -> None:
        await self.control("approve", allow=False)

    async def action_choose(self, n: int) -> None:
        row = next((r for r in self.rows if r["id"] == self.selected), None)
        if self.questions():
            self.action_answer()
        elif row and row["status"] == "awaiting_approval":
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
            self.filter = ""
            self.update_subtitle()
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
        folders = list(dict.fromkeys(folders + [r["cwd"] for r in rows if r["cwd"]]))
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

    @work(exclusive=True, group="teams")
    async def action_teams(self) -> None:
        from agentctl.tui.teams import TeamsScreen

        while True:
            intent = await self.push_screen_wait(TeamsScreen(self.rpc))
            self.on_resize()  # the terminal may have resized while the dashboard was covered
            if not intent:
                return
            if intent.action == "new":
                await self.action_new().wait()
                continue
            if intent.session_id:
                await self.poll()
                self.select(intent.session_id)
                self.rebuild()
                if intent.action == "reply":
                    self.open_prompt(
                        "send",
                        "message to coordinator or worker · enter sends",
                        value=intent.text,
                        target=intent.session_id,
                    )
                elif intent.action == "transcript":
                    self.action_log()
                elif intent.action == "terminal":
                    await self.action_attach()
            return

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
        self.notify(f"{short_id(plan.session['id'])} is now in agentctl", timeout=5)
        await self.poll()

    @work
    async def action_hide(self) -> None:
        r = self.current()
        if not r:
            return
        sid = r["id"]
        if not await self.push_screen_wait(
            Confirm(
                "Hide this session? Its agent keeps running; new activity can restore it.",
                "Hide only",
            )
        ):
            return
        try:
            await self.rpc("hide", id=sid)
            self.removed = sid
            self.notify(f"Hidden {short_id(sid)} · z unhides it", timeout=6)
        except Exception as e:
            self.notify(str(e), severity="error", timeout=8)
        await self.poll()

    @work
    async def action_close(self) -> None:
        r = self.current()
        if not r:
            return
        name, sid = r["title"] or short_cwd(r["cwd"]), r["id"]
        if r["tmux_pane"]:
            q = f"Quit {name} and hide it? z only unhides it; o resumes the stopped chat."
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
            self.notify(f"Hidden {short_id(sid)} · z unhides it (does not restart it)", timeout=6)
        except Exception as e:
            self.notify(str(e), severity="error", timeout=8)
        await self.poll()

    async def action_undo_remove(self) -> None:
        sid, self.removed = self.removed, None
        if sid:
            try:
                await self.rpc("unhide", id=sid)
                self.notify(f"{short_id(sid)} is back", timeout=3)
            except Exception as e:
                self.notify(str(e), severity="error", timeout=8)
            await self.poll()
            self.select(sid)
            self.rebuild()

    @work
    async def action_results(self) -> None:
        row = self.current()
        if not row:
            return
        try:
            report = await self.rpc("review", id=row["id"])
        except Exception as e:
            if "unknown method" not in str(e) or "review" not in str(e):
                self.notify(str(e), severity="error")
                return
            # An editable install can update this UI while an older daemon is still running.
            # Its existing tail API supplies the same local evidence without restarting agents.
            from agentctl.daemon.fleet import review_turn
            from agentctl.models import Session, SessionEvent

            try:
                snapshot = await self.rpc("tail", id=row["id"], n=500)
                session = Session.model_validate(snapshot["session"])
                events = [SessionEvent.model_validate(ev) for ev in snapshot["events"]]
                report = await asyncio.to_thread(review_turn, session, events)
            except Exception as fallback_error:
                self.notify(str(fallback_error), severity="error")
                return
        self.push_screen(Review(name_of(row), report))

    async def refresh_history(self, sid: str) -> None:
        history = await self.rpc("history", id=sid)
        if isinstance(history, list):
            # Keep local connection failures which never reached the daemon.
            local = [e for e in self.sent.get(sid, []) if "id" not in e]
            self.sent[sid] = [*history, *local]

    @work(exclusive=True, group="history")
    async def load_history(self, sid: str) -> None:
        with contextlib.suppress(Exception):
            await self.refresh_history(sid)

    @work
    async def action_history(self) -> None:
        r = self.current()
        if not r:
            return
        arrived: set[str] = set()
        with contextlib.suppress(Exception):
            await self.refresh_history(r["id"])
        with contextlib.suppress(Exception):  # which sends show up in its transcript
            arrived = user_texts((await self.rpc("tail", id=r["id"], n=500))["events"])
        title = r["title"] or short_id(r["id"])
        text = await self.push_screen_wait(History(title, self.sent.get(r["id"], []), arrived))
        if text:
            self.open_prompt(
                "send", f"message to {short_id(r['id'])} · enter sends", text, target=r["id"]
            )

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
