"""The dashboard's pop-up screens: diffs, search, help, sent history, what's new, menus,
agent questions, confirmations and the new-session picker. `Watch` (in __init__) opens them."""

import asyncio
import os
from datetime import UTC, datetime
from typing import Any

from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import Suggester
from textual.widgets import (
    Button,
    Checkbox,
    Input,
    OptionList,
    RadioButton,
    RadioSet,
    Static,
)
from textual.widgets.option_list import Option

from agentctl.cli import age, status_text
from agentctl.models import short_id
from agentctl.tui.common import Rpc, nocase, sent_key


class Diff(ModalScreen[None]):
    """A file's changes since the last commit."""

    DEFAULT_CSS = """
    Diff { align: center middle; }
    Diff > VerticalScroll { width: 95%; height: 90%; border: round $accent; padding: 0 1;
                            background: $surface; }
    """
    BINDINGS = nocase([Binding("escape,q", "dismiss", "close")])

    def __init__(self, path: str, diff: str) -> None:
        super().__init__()
        self.path, self.diff = path, diff

    def compose(self) -> ComposeResult:
        body = Text()
        for line in self.diff.splitlines():
            if line.startswith(("diff ", "index ", "--- ", "+++ ", "new file")):
                style = "bold dim"
            elif line.startswith("@@"):
                style = "cyan"
            elif line.startswith("+"):
                style = "green"
            elif line.startswith("-"):
                style = "red"
            else:
                style = ""
            body.append(line + "\n", style=style)
        with VerticalScroll():
            yield Static(Text(tilde(self.path), style="bold"))
            yield Static(body)


class Search(ModalScreen[str | None]):
    """Search what was said in every session (messages and replies, not tool output); pick a
    match to open its session."""

    DEFAULT_CSS = """
    Search { align: center middle; }
    Search > Vertical { width: 95%; max-width: 110; height: auto; max-height: 90%;
                        border: round $accent; padding: 0 1; background: $surface; }
    Search Input { border: none; height: 1; padding: 0; }
    Search OptionList, Search OptionList:focus { height: auto; max-height: 30; border: none; }
    """
    BINDINGS = [Binding("escape", "dismiss(None)", "close")]

    def __init__(self, rpc: "Rpc") -> None:
        super().__init__()
        self.rpc = rpc
        self.hits: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Input(placeholder="search every session · enter searches · esc closes")
            yield Static(id="search-status")
            yield OptionList(id="hits")

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        self.run_search(event.value.strip())

    @work(exclusive=True)
    async def run_search(self, q: str) -> None:
        status = self.query_one("#search-status", Static)
        if not q:
            return
        status.update(Text("searching…", style="dim"))
        try:
            self.hits = await self.rpc("search", q=q)
        except Exception as e:
            status.update(Text(str(e), style="red"))
            return
        opts = []
        for i, h in enumerate(self.hits):
            t = Text(no_wrap=True, overflow="ellipsis")
            t.append(h["title"] or short_id(h["id"]), style="bold")
            t.append(f"  {short_id(h['id'])} {age(h['ts'])} ago · ", style="dim")
            t.append_text(status_text(h["status"]))
            line = Text("\n" + ("› " if h["role"] == "user" else "  ") + h["snippet"])
            line.highlight_words([q], style="reverse", case_sensitive=False)
            opts.append(Option(t + line, id=str(i)))
        self.query_one("#hits", OptionList).set_options(opts)
        n = len(self.hits)
        status.update(Text(f"{n} match{'es' if n != 1 else ''}, newest first", style="dim"))
        if opts:
            hits = self.query_one("#hits", OptionList)
            hits.highlighted = 0  # enter opens the newest match
            hits.focus()

    @on(OptionList.OptionSelected)
    def chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.hits[int(event.option.id or 0)]["id"])


def first_line(s: str) -> str:
    return (s.splitlines() or [""])[0]


def since(iso: str) -> str:
    secs = int((datetime.now(UTC) - datetime.fromisoformat(iso)).total_seconds())
    return f"{secs // 60}m {secs % 60:02d}s" if secs >= 60 else f"{max(secs, 0)}s"


def fmt_ts(iso: str, narrow: bool) -> str:
    return datetime.fromisoformat(iso).astimezone().strftime("%H:%M" if narrow else "%H:%M:%S")


HELP = [
    ("list", [
        ("enter", "desktop: fullscreen agent terminal; phone: fullscreen transcript"),
        ("space", "fullscreen transcript; Esc returns to the dashboard"),
        ("ctrl+t", "open its terminal at any width (ctrl-] d comes back)"),
        ("g", "go to a session that needs approval"),
        ("a  d  1-4", "approve, deny, pick an option; a also answers an agent's questions"),
        ("n", "new session"),
        ("u", "what's new since you last looked"),
        ("p", "group by project / by status"),
        ("/", "filter by id, title or folder"),
        ("f", "search what was said in every session"),
        ("e", "show everything: all idle sessions, and ended (last 24h)"),
        ("q", "quit"),
    ]),
    ("session", [
        ("s", "send a message (quick replies: one per line in ~/.agentctl/replies)"),
        ("l", "queue a message for when it's free"),
        ("h", "what you sent from here, and whether it arrived (↑ ↓ in the box)"),
        ("w", "review the latest turn and shared project changes"),
        ("v", "show or hide project files"),
        ("i", "interrupt the running turn"),
        ("c", "copy the last reply"),
        ("r", "rename"),
        ("o", "adopt: move it into agentctl"),
        ("x  z", "remove from the list (nothing is deleted) / bring it back"),
        ("ctrl+x", "quit an idle agent and hide it (o resumes it)"),
        ("m", "replies as raw text / Markdown"),
        ("enter", "on a file: its diff (● marks what this session edited)"),
        ("esc", "back"),
    ]),
    ("anywhere", [
        ("b", "bell + Mac notification: approvals, long turns done, caches expiring"),
        ("t", "theme (ansi-dark / ansi-light use your terminal's own colors)"),
        ("ctrl-r", "reload everything (it also reconnects by itself)"),
        ("?", "this list"),
        ("ctrl+q", "quit, from anywhere"),
        ("ctrl+p", "search all available commands"),
        ("ctrl+g", "teams: a coordinator and its worker sessions"),
        ("ctrl-] w", "this dashboard, from inside any agent"),
    ]),
]  # fmt: skip


def help_table() -> Table:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold", no_wrap=True)
    t.add_column()
    for i, (title, rows) in enumerate(HELP):
        t.add_row(Text(title.upper(), style="dim"))
        for key, what in rows:
            t.add_row(key, what)
        if i < len(HELP) - 1:
            t.add_row("", "")
    return t


class Help(ModalScreen[None]):
    DEFAULT_CSS = """
    Help { align: center middle; }
    Help > VerticalScroll { width: 95%; max-width: 92; height: auto; max-height: 95%;
                            border: round $accent; padding: 0 1; background: $surface; }
    """
    BINDINGS = nocase([Binding("escape,q,question_mark", "dismiss", "close")])

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(help_table())

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
    BINDINGS = nocase([Binding("escape,h", "dismiss(None)", "close")])

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
            elif s.get("settled") == "arrived" or (
                "id" not in s and sent_key(s["text"]) in self.arrived
            ):
                t.append("arrived ", style="green")
            elif s["outcome"] in ("cancelled", "unknown", "sending"):
                t.append(s["outcome"] + " ", style="yellow")
            else:
                t.append(f"{s['outcome']}, not in transcript yet ", style="yellow")
            t.append(first_line(s["text"]))
            if s.get("error"):
                t.append(f"\n  {s['error']}", style="red")
            opts.append(Option(t, id=str(len(self.sends) - 1 - i)))
        with Vertical():
            yield Static(Text(f"Sent to {self.head}", style="bold"))
            if opts:
                yield OptionList(*opts)
            else:
                yield Static(Text("No messages sent through agentctl yet", style="dim"))

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
    BINDINGS = nocase([Binding("escape,u", "dismiss(None)", "close")])

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


class Menu(ModalScreen[str | None]):
    """The phone's "more": the rest of what applies here, as rows to tap; returns the action."""

    DEFAULT_CSS = """
    Menu { align: center bottom; }
    Menu > OptionList { width: 100%; height: auto; max-height: 80%; border: round $accent;
                        background: $surface; }
    """
    BINDINGS = nocase([Binding("escape,q", "dismiss(None)", "close")])

    def __init__(self, items: list[tuple[str, str]]) -> None:
        super().__init__()
        self.items = items

    def compose(self) -> ComposeResult:
        yield OptionList(*[Option(label, id=action) for label, action in self.items])

    @on(OptionList.OptionSelected)
    def chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def on_click(self, event: events.Click) -> None:
        if event.widget is self:  # a tap outside the rows
            self.dismiss(None)


class Questions(ModalScreen[list[dict[str, Any]] | None]):
    """An agent's AskUserQuestion as a form: each question's options (pick one, or tick
    several) and a box for your own answer, which replaces a single choice."""

    DEFAULT_CSS = """
    Questions { align: center middle; }
    Questions > Vertical { width: 95%; max-width: 90; height: auto; max-height: 95%;
                           border: round $accent; background: $surface; padding: 0 1; }
    Questions VerticalScroll { height: auto; max-height: 1fr; }
    Questions VerticalScroll > Vertical { height: auto; }
    Questions .question { margin-top: 1; }
    Questions RadioSet { border: none; width: 100%; }
    Questions Checkbox { border: none; width: 100%; }
    Questions #q-buttons { height: 3; margin-top: 1; }
    Questions #q-buttons Button { width: 1fr; }
    """
    BINDINGS = [Binding("escape", "dismiss(None)", "cancel")]  # letters go into the boxes

    def __init__(self, questions: list[dict[str, Any]]) -> None:
        super().__init__()
        self.questions = questions

    def compose(self) -> ComposeResult:
        with Vertical():
            with VerticalScroll():
                for i, q in enumerate(self.questions):
                    head = Text(f"{q.get('header') or ''}  ", style="dim")
                    head.append(str(q.get("question") or ""), style="bold")
                    if q.get("multiSelect"):
                        head.append("  (any)", style="dim")
                    yield Static(head, classes="question")
                    labels = [option_label(o) for o in q.get("options") or []]
                    if q.get("multiSelect"):
                        with Vertical(id=f"q{i}"):
                            yield from (Checkbox(label) for label in labels)
                    elif labels:
                        yield RadioSet(*(RadioButton(label) for label in labels), id=f"q{i}")
                    typing = Input(placeholder="or type your own answer", id=f"t{i}")
                    if not labels:
                        typing.placeholder = "your answer"
                    typing.display = q.get("isOther") is not False
                    yield typing
            with Horizontal(id="q-buttons"):
                yield Button("send answers", variant="primary", id="q-send")
                yield Button("cancel", id="q-cancel")

    @on(Button.Pressed, "#q-cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#q-send")
    def send(self) -> None:
        answers = []
        for i, q in enumerate(self.questions):
            typed = self.query_one(f"#t{i}", Input).value.strip()
            if q.get("multiSelect"):
                boxes = self.query(f"#q{i} Checkbox").results(Checkbox)
                picks = [k + 1 for k, box in enumerate(boxes) if box.value]
            else:
                sets = self.query(f"#q{i}").results(RadioSet)
                pressed = next((rs.pressed_index for rs in sets), -1)
                picks = [] if typed or pressed < 0 else [pressed + 1]
            if not picks and not typed:
                self.notify(f"no answer yet: {q.get('question')}", severity="warning")
                return
            answers.append({"choices": picks, "text": typed})
        self.dismiss(answers)


def option_label(o: dict[str, Any]) -> Text:
    label = Text(str(o.get("label") or ""))
    if o.get("description") and o["description"] != o.get("label"):
        label.append(f"  {o['description']}", style="dim")
    return label


class Confirm(ModalScreen[bool]):
    """Yes/no dialog with big tap targets."""

    DEFAULT_CSS = """
    Confirm { align: center middle; }
    Confirm > Vertical { width: 90%; max-width: 60; height: auto; border: round $warning;
                         padding: 1; background: $surface; }
    Confirm Horizontal { height: auto; margin-top: 1; }
    Confirm Button { width: 1fr; min-width: 0; }
    """
    BINDINGS = nocase(
        [Binding("escape", "dismiss(False)", "cancel"), Binding("y", "dismiss(True)")]
    )

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
