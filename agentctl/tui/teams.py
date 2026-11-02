"""Keyboard and mouse team graph, connection editor, and assignment review."""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Select, Static, TextArea, Tree

Rpc = Callable[..., Awaitable[Any]]
# Textual renamed its no-selection sentinel; keep the supported older releases working.
NO_SELECTION = getattr(Select, "NULL", Select.BLANK)


@dataclass(frozen=True)
class TeamIntent:
    action: str
    session_id: str | None = None
    text: str | None = None


class TeamForm(ModalScreen[dict[str, Any] | None]):
    DEFAULT_CLASSES = "teams-dialog"
    DEFAULT_CSS = """
    TeamForm { align: center middle; }
    TeamForm > VerticalScroll { width: 92%; max-width: 78; height: auto; max-height: 90%;
        padding: 1 2; background: $surface; border: round $accent; }
    TeamForm .caption { height: auto; margin-top: 1; text-style: bold; color: $text-muted; }
    TeamForm .form-title { text-align: center; color: $text; margin: 0 0 1 0; }
    TeamForm .hint { color: $text-muted; margin: 1 0; }
    TeamForm TextArea { height: 7; }
    TeamForm Horizontal { height: auto; align-horizontal: right; }
    TeamForm Button { min-width: 10; margin-left: 1; }
    """
    BINDINGS = [Binding("escape", "dismiss(None)", "cancel")]

    def __init__(
        self, kind: str, sessions: list[dict[str, Any]], values: dict[str, Any] | None = None
    ) -> None:
        super().__init__()
        self.kind, self.sessions, self.values = kind, sessions, values or {}

    def compose(self) -> ComposeResult:
        v = self.values
        worker = self.kind == "worker"
        with VerticalScroll():
            yield Static(
                "Connect worker" if worker else "Team details", classes="caption form-title"
            )
            yield Static("Role" if worker else "Name", classes="caption")
            yield Input(v.get("role" if worker else "name", ""), id="name")
            yield Static("Session" if worker else "Coordinator session", classes="caption")
            options = [
                (f"{s.get('title') or s['id'][:12]} [{s['source']}] {s['status']}", s["id"])
                for s in self.sessions
            ]
            selected = v.get("session_id" if worker else "coordinator")
            yield Select(
                options,
                value=selected if selected in [o[1] for o in options] else NO_SELECTION,
                prompt="Choose a session",
                id="session",
            )
            yield Static("Worker scope" if worker else "Goal", classes="caption")
            yield TextArea(v.get("brief" if worker else "goal", ""), id="scope")
            if worker:
                yield Static(
                    "Intended model for fresh assignments (blank: agent default)", classes="caption"
                )
                yield Input(v.get("model", ""), id="model")
                yield Static("Connection permission", classes="caption")
                yield Select(
                    [
                        ("Observe — read only", "observe"),
                        ("Propose — human approves each assignment", "propose"),
                        ("Delegate — coordinator can send work", "delegate"),
                    ],
                    value=v.get("permission", "propose"),
                    allow_blank=False,
                    id="permission",
                )
            else:
                yield Static("Maximum worker routing grant", classes="caption")
                yield Select(
                    [
                        ("Observe — read only", "observe"),
                        ("Propose — human approves assignments", "propose"),
                        ("Delegate — coordinator can send assignments", "delegate"),
                    ],
                    value=v.get("permission_ceiling", "propose"),
                    allow_blank=False,
                    id="permission-ceiling",
                )
            yield Static("Saving sends no messages. Active runs pause for review.", classes="hint")
            with Horizontal():
                yield Button("Save", id="save", variant="primary")
                yield Button("Cancel", id="cancel")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        name = self.query_one("#name", Input).value.strip()
        session = self.query_one("#session", Select).value
        scope = self.query_one("#scope", TextArea).text.strip()
        if not name or session is NO_SELECTION or (self.kind != "worker" and not scope):
            self.notify("Choose a session and fill in the name and goal", severity="warning")
            return
        if self.kind == "worker":
            self.dismiss(
                {
                    "role": name,
                    "session_id": session,
                    "brief": scope,
                    "model": self.query_one("#model", Input).value.strip(),
                    "permission": self.query_one("#permission", Select).value,
                }
            )
        else:
            self.dismiss(
                {
                    "name": name,
                    "coordinator": session,
                    "goal": scope,
                    "permission_ceiling": self.query_one("#permission-ceiling", Select).value,
                }
            )


class TeamDetail(ModalScreen[str | None]):
    DEFAULT_CLASSES = "teams-dialog"
    DEFAULT_CSS = """
    TeamDetail { align: center middle; }
    TeamDetail > VerticalScroll { width: 92%; max-width: 104; height: 90%;
        padding: 1 2; background: $surface; border: round $accent; }
    TeamDetail .caption { margin: 1 0; text-style: bold; color: $accent; }
    TeamDetail .detail-title { text-align: center; padding-bottom: 1;
        border-bottom: solid $panel; }
    TeamDetail Horizontal { height: auto; layout: grid; grid-size: 2;
        grid-gutter: 0 1; margin-top: 1; }
    TeamDetail Button { width: 1fr; min-width: 0; }
    TeamDetail TextArea { height: 12; }
    """
    BINDINGS = [Binding("escape", "dismiss(None)", "close")]

    def __init__(
        self,
        title: str,
        sections: list[tuple[str, str]],
        actions: list[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__()
        self.heading, self.sections, self.actions = title, sections, actions or []

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Static(Text(self.heading, style="bold"), classes="detail-title")
            for heading, content in self.sections:
                yield Static(heading, classes="caption")
                # Full selectable text, including the end of long replies.
                yield Static(Text(content))
            with Horizontal():
                for label, action in self.actions:
                    yield Button(label, id=action)
                yield Button("Close", id="close")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None if event.button.id == "close" else event.button.id)


class TeamsScreen(ModalScreen[TeamIntent | None]):
    """Tree edges carry grants; selection opens a complete inspector or editor."""

    DEFAULT_CLASSES = "teams-dialog"
    DEFAULT_CSS = """
    TeamsScreen { background: $background; align: center middle; }
    TeamsScreen #team-frame { width: 96%; max-width: 140; height: 94%;
        border: round $panel; background: $surface; padding: 0 1; }
    TeamsScreen #team-title { height: auto; padding-top: 1;
        text-style: bold; text-align: center; }
    TeamsScreen #team-subtitle { height: auto; text-align: center;
        color: $text-muted; margin-bottom: 1; }
    TeamsScreen #team-picker { margin-bottom: 1; }
    TeamsScreen #team-body { height: 1fr; margin-top: 1; border-top: solid $panel; }
    TeamsScreen #team-graph { width: 43%; border-right: solid $panel; padding: 1; }
    TeamsScreen #team-inspector { width: 1fr; padding: 1 2; }
    TeamsScreen #team-info { height: auto; margin-bottom: 1; }
    TeamsScreen .caption { text-style: bold; color: $accent; margin-top: 1; }
    TeamsScreen .toolbar { height: auto; layout: grid; grid-size: 4; grid-gutter: 0 1; }
    TeamsScreen #team-inspector .toolbar { grid-size: 2; }
    TeamsScreen Button { min-width: 0; width: 1fr; }
    TeamsScreen #team-error { height: auto; color: $error; padding: 0 1; }
    TeamsScreen.narrow #team-frame { width: 100%; height: 100%; border: none; padding: 0 1; }
    TeamsScreen.narrow #team-title { padding-top: 0; }
    TeamsScreen.narrow #team-subtitle { margin-bottom: 0; }
    TeamsScreen.narrow #team-picker { margin-bottom: 0; }
    TeamsScreen.narrow #team-body { margin-top: 0; }
    TeamsScreen.narrow #team-body { layout: vertical; }
    TeamsScreen.narrow #team-graph { width: 100%; height: 40%; border-right: none; padding: 0; }
    TeamsScreen.narrow #team-inspector { width: 100%; height: 1fr; padding: 1 0; }
    TeamsScreen.narrow .toolbar { grid-size: 2; }
    """
    BINDINGS = [
        Binding("escape", "dismiss(None)", "back"),
        Binding("r", "reload", "refresh"),
        Binding("n", "create", "new team"),
    ]

    def __init__(self, rpc: Rpc) -> None:
        super().__init__()
        self.rpc = rpc
        self.teams: list[dict[str, Any]] = []
        self.team: dict[str, Any] | None = None
        self.selection: tuple[str, str] = ("team", "")
        self.signature = ""
        self.busy = False

    def compose(self) -> ComposeResult:
        with Vertical(id="team-frame"):
            yield Static("Teams", id="team-title")
            yield Static(
                "One coordinator. Connected sessions. Clear permissions.", id="team-subtitle"
            )
            yield Select([], prompt="Choose a team or create one", id="team-picker")
            with Horizontal(classes="toolbar"):
                for label, action in [
                    ("New team", "new"),
                    ("Edit team", "edit-team"),
                    ("Connect worker", "connect"),
                    ("New session", "new-session"),
                    ("MCP setup", "setup"),
                    ("Start run", "start"),
                    ("Pause", "pause"),
                    ("End run", "end"),
                    ("Brief coordinator", "brief"),
                    ("Back", "back"),
                ]:
                    yield Button(
                        label, id=action, variant="primary" if action == "start" else "default"
                    )
            yield Static("", id="team-error")
            with Horizontal(id="team-body"):
                yield Tree("No team selected", id="team-graph")
                with VerticalScroll(id="team-inspector"):
                    yield Static(
                        "Create a team, choose its coordinator, then connect workers.",
                        id="team-info",
                    )
                    with Horizontal(classes="toolbar"):
                        for label, action in [
                            ("Inspect / Review", "inspect"),
                            ("Edit connection", "edit"),
                            ("Disconnect", "disconnect"),
                            ("Take over", "hold"),
                            ("Open transcript", "transcript"),
                            ("Reply", "reply"),
                            ("Open terminal", "terminal"),
                        ]:
                            yield Button(label, id=action)
        yield Footer()

    async def on_mount(self) -> None:
        self.set_class(self.size.width < 80, "narrow")
        await self.reload()
        self.set_interval(2, self.reload)

    def on_resize(self) -> None:
        self.set_class(self.size.width < 80, "narrow")

    async def reload(self) -> None:
        if self.busy or self.app.screen is not self:
            return
        try:
            teams = await self.rpc("teams_list")
            signature = json.dumps(teams, sort_keys=True)
            if signature == self.signature:
                return
            self.signature, self.teams = signature, teams
            old = self.team["id"] if self.team else None
            self.team = next((t for t in teams if t["id"] == old), teams[0] if teams else None)
            picker = self.query_one("#team-picker", Select)
            with picker.prevent(Select.Changed):
                picker.set_options([(t["name"], t["id"]) for t in teams])
                picker.value = self.team["id"] if self.team else NO_SELECTION
            self.draw()
            self.query_one("#team-error", Static).update("")
        except Exception as error:
            self.query_one("#team-error", Static).update(Text(str(error)))

    async def action_reload(self) -> None:
        self.signature = ""
        await self.reload()

    @on(Select.Changed, "#team-picker")
    def picked(self, event: Select.Changed) -> None:
        self.team = next((t for t in self.teams if t["id"] == event.value), None)
        self.selection = ("team", "")
        self.draw()

    def draw(self) -> None:
        tree = self.query_one("#team-graph", Tree)
        tree.clear()
        t = self.team
        self.update_controls()
        if not t:
            tree.root.set_label("No teams yet")
            return
        run = t.get("run")
        state = run["state"].upper() if run else "DRAFT"
        tree.root.set_label(Text(f"{t['name']} · {state}", style="bold"))
        tree.root.data = ("team", "")
        coordinator = t.get("coordinator_session") or {}
        root = tree.root.add(
            Text(f"Coordinator: {coordinator.get('title') or t['coordinator'][:12]}"),
            ("coordinator", t["coordinator"]),
            expand=True,
        )
        for m in t["workers"]:
            session = m.get("session") or {}
            status = "HUMAN CONTROL" if m["held"] else session.get("status", "unavailable")
            if not m["enabled"]:
                status = "disconnected"
            label = Text(f"{m['permission'].upper()} → {m['role']} · {status}")
            label.stylize(
                "yellow" if m["held"] else "cyan" if m["permission"] == "delegate" else ""
            )
            root.add_leaf(label, ("worker", m["id"]))
        assignments = tree.root.add("ASSIGNMENTS", ("team", ""), expand=True)
        for a in reversed(t["assignments"]):
            assignments.add_leaf(
                Text(f"{a['status'].upper()} · {a['text'].splitlines()[0][:60]}"),
                ("assignment", a["id"]),
            )
        proposals = [p for p in t.get("proposals", []) if p["status"] == "pending"]
        pending_nodes = []
        if proposals:
            pending = tree.root.add(
                f"SETUP PROPOSALS ({len(proposals)})", ("team", ""), expand=True
            )
            for proposal in proposals:
                pending_nodes.append(
                    pending.add_leaf(
                        Text(f"REVIEW · {len(proposal['changes'])} worker changes"),
                        ("proposal", proposal["id"]),
                    )
                )
        if self.selection[0] == "proposal" and all(
            self.selection[1] != proposal["id"] for proposal in proposals
        ):
            self.selection = ("team", "")
        tree.root.expand()
        for node in [tree.root, root, *root.children, *assignments.children, *pending_nodes]:
            if node.data == self.selection:
                tree.select_node(node)
                break
        self.inspect_summary()

    @on(Tree.NodeSelected, "#team-graph")
    @on(Tree.NodeHighlighted, "#team-graph")
    def selected(self, event: Tree.NodeSelected[Any] | Tree.NodeHighlighted[Any]) -> None:
        if event.node.data:
            self.selection = event.node.data
            self.inspect_summary()

    def worker(self) -> dict[str, Any] | None:
        return next(
            (
                m
                for m in (self.team or {}).get("workers", [])
                if self.selection == ("worker", m["id"])
            ),
            None,
        )

    def assignment(self) -> dict[str, Any] | None:
        return next(
            (
                a
                for a in (self.team or {}).get("assignments", [])
                if self.selection == ("assignment", a["id"])
            ),
            None,
        )

    def proposal(self) -> dict[str, Any] | None:
        return next(
            (
                proposal
                for proposal in (self.team or {}).get("proposals", [])
                if self.selection == ("proposal", proposal["id"])
            ),
            None,
        )

    def session_id(self) -> str | None:
        if self.selection[0] == "coordinator":
            return self.selection[1]
        m, a = self.worker(), self.assignment()
        if a:
            return str(a.get("delivery_session_id") or a["session_id"])
        if m:
            delivery = next(
                (
                    a.get("delivery_session_id")
                    for a in reversed((self.team or {}).get("assignments", []))
                    if a["member_id"] == m["id"]
                    and a["session_id"] == m["session_id"]
                    and a.get("delivery_session_id")
                ),
                None,
            )
            return str(delivery or m["session_id"])
        return None

    def inspect_summary(self) -> None:
        self.update_controls()
        t = self.team
        if not t:
            return
        m, a, proposal = self.worker(), self.assignment(), self.proposal()
        if m:
            session = m.get("session") or {}
            text = (
                f"CONNECTION · {m['role']}\n\n{m['id']}\nReference: {m['session_id']}\n"
                f"{session.get('source', 'unavailable')} · {session.get('title') or ''}\n"
                f"Control: {'available' if session.get('controllable') else 'read only'}\n"
                f"Permission: {m['permission'].upper()}\n"
                f"Intended model: {m.get('model') or 'agent default'} (effective unverified)\n"
                f"Human control: {'yes' if m['held'] else 'no'}\n\n{m['brief']}"
                "\n\nAssignments start a new agent chat. User conversations stay protected."
            )
        elif a:
            text = (
                f"ASSIGNMENT · {a['status'].upper()}\n\n{a['id']}\n\n{a['text']}\n\n"
                f"Requested model: {a.get('requested_model') or 'agent default'} "
                "(effective unverified)\n"
                f"{a['detail']}\n\nInspect / Review opens the full request and reply."
            )
        elif proposal:
            changes = "\n".join(
                f"{change['action'].upper()} · {change.get('role') or change.get('member')} · "
                f"{change.get('permission', '')} · {change.get('brief', '')}"
                for change in proposal["changes"]
            )
            text = (
                f"SETUP PROPOSAL · {proposal['status'].upper()}\n\n{proposal['id']}\n"
                f"{proposal['detail']}\n\n{changes}\n\n"
                "Approval applies the worker changes, checks the permission ceiling, and pauses "
                "the run for review."
            )
        else:
            run = t.get("run") or {}
            contact = "connected" if t.get("last_mcp_contact") else "setup required"
            events = "\n".join(f"{e['kind']}: {e['detail']}" for e in t["events"][:10])
            text = (
                f"GOAL\n{t['goal']}\n\nRUN · {run.get('state', 'draft').upper()}\n"
                f"{run.get('reason', '')}\nMCP: {contact}\nRevision: {t['revision']}\n"
                f"Worker permission ceiling: {t.get('permission_ceiling', 'delegate')}\n\n"
                "Start enables routing. Brief coordinator opens a message for you to send.\n"
                "Pause stops future sends; running agents keep working.\n\nACTIVITY\n" + events
            )
        rendered = Text(text)
        rendered.stylize("bold cyan", 0, len(text.splitlines()[0]))
        self.query_one("#team-info", Static).update(rendered)

    def update_controls(self) -> None:
        """Keep available actions and their labels tied to the inspected node."""
        t, m, a, proposal = self.team, self.worker(), self.assignment(), self.proposal()
        state = ((t or {}).get("run") or {}).get("state")
        for action in ("edit-team", "connect", "setup", "brief"):
            self.query_one(f"#{action}", Button).disabled = t is None
        start = self.query_one("#start", Button)
        start.label = "Resume run" if state == "paused" else "Start run"
        start.disabled = not t or state == "running"
        self.query_one("#pause", Button).disabled = state != "running"
        self.query_one("#end", Button).disabled = state not in ("running", "paused")
        for action in ("edit", "disconnect", "hold"):
            self.query_one(f"#{action}", Button).disabled = m is None
        self.query_one("#hold", Button).label = "Release worker" if m and m["held"] else "Take over"
        inspect = self.query_one("#inspect", Button)
        inspect.disabled = a is None and proposal is None
        inspect.label = "Review setup" if proposal else "Inspect / Review"
        for action in ("transcript", "reply", "terminal"):
            self.query_one(f"#{action}", Button).disabled = self.session_id() is None

    @work(group="team-control")
    async def action_create(self) -> None:
        await self.control("new")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.run_control(event.button.id or "")

    @work(group="team-control")
    async def run_control(self, action: str) -> None:
        await self.control(action)

    async def control(self, action: str) -> None:
        if self.busy:
            return
        self.busy = True
        try:
            await self.perform(action)
        except Exception as error:
            self.notify(str(error), severity="error", timeout=10)
        finally:
            self.busy = False
            self.signature = ""
            await self.reload()

    async def perform(self, action: str) -> None:
        if action == "back":
            self.dismiss(None)
            return
        if action == "new-session":
            self.dismiss(TeamIntent("new"))
            return
        if action == "new":
            values = await self.app.push_screen_wait(TeamForm("team", await self.rpc("ls")))
            if values:
                self.team = await self.rpc("team_create", **values)
            return
        t = self.team
        if not t:
            raise ValueError("Create or select a team first")

        async def call(method: str, **params: Any) -> Any:
            return await self.rpc(method, team_id=t["id"], **params)

        m, a = self.worker(), self.assignment()
        if action in ("transcript", "reply", "terminal"):
            sid = self.session_id()
            if not sid:
                raise ValueError("Select the coordinator or a worker")
            self.dismiss(TeamIntent(action, sid))
        elif action == "brief":
            brief = await call("team_brief")
            self.dismiss(TeamIntent("reply", brief["session_id"], brief["text"]))
        elif action in ("connect", "edit", "edit-team"):
            if action == "edit" and not m:
                raise ValueError("Select a worker connection to edit")
            kind = "team" if action == "edit-team" else "worker"
            sessions = await self.rpc("ls")
            if kind == "worker":
                sessions = [s for s in sessions if s["id"] != t["coordinator"]]
            values = await self.app.push_screen_wait(
                TeamForm(kind, sessions, t if kind == "team" else m if action == "edit" else None)
            )
            if values:
                method = "team_update" if kind == "team" else "team_edit_worker"
                await call(
                    method, **values, **({"member": m["id"]} if action == "edit" and m else {})
                )
        elif action in ("disconnect", "hold"):
            if not m:
                raise ValueError("Select a worker connection first")
            method = (
                "team_remove_worker"
                if action == "disconnect"
                else ("team_release" if m["held"] else "team_takeover")
            )
            await call(method, member=m["id"])
        elif action == "setup":
            setup = await call("team_mcp_setup")
            await self.app.push_screen_wait(
                TeamDetail(
                    "SCOPED MCP SETUP",
                    [
                        (
                            "Register in the coordinator's agent, then reconnect that session",
                            setup["note"],
                        ),
                        ("Stdio command", setup["stdio_command"]),
                        (
                            "MCP server configuration",
                            json.dumps(
                                {
                                    "mcpServers": {
                                        "agentctl-team": {
                                            "command": setup["command"],
                                            "args": setup["args"],
                                        }
                                    }
                                },
                                indent=2,
                            ),
                        ),
                        (
                            "Permissions",
                            "This connection exposes only this team's tools. "
                            "Configure it for the coordinator only. Team grants limit routing; "
                            "native shell, file, and tool permission prompts still belong to you. "
                            "Do not add --allow-write.",
                        ),
                    ],
                )
            )
        elif action in ("start", "pause", "end"):
            run = t.get("run") or {}
            method = (
                "team_resume"
                if action == "start" and run.get("state") == "paused"
                else ("team_start" if action == "start" else f"team_{action}")
            )
            await call(method)
        elif action == "inspect":
            proposal = self.proposal()
            if proposal:
                result = await self.app.push_screen_wait(
                    TeamDetail(
                        proposal["id"],
                        [
                            ("PROPOSED WORKER CHANGES", json.dumps(proposal["changes"], indent=2)),
                            ("PERMISSION CEILING", t.get("permission_ceiling", "delegate")),
                            ("REVIEW", "Approval applies these changes and pauses the run."),
                        ],
                        [("Approve setup", "approve"), ("Reject setup", "reject")],
                    )
                )
                if result:
                    await call("team_proposal_review", proposal=proposal["id"], action=result)
                return
            if not a:
                raise ValueError("Select an assignment or setup proposal to inspect")
            a = await call("team_result", assignment=a["id"])
            actions = []
            if a["status"] == "proposed":
                actions.append(("Approve delivery", "approve"))
            if a["status"] == "reported_complete":
                actions.append(("Accept result", "accept"))
            if a["status"] in ("proposed", "queued", "waiting", "blocked"):
                actions.append(("Cancel assignment", "cancel"))
            if a["status"] == "unknown":
                actions.append(("Inspected; close tracking", "resolve_unknown"))
            result = await self.app.push_screen_wait(
                TeamDetail(
                    a["id"],
                    [
                        ("STATUS", a["status"] + "\n" + a["detail"]),
                        ("REQUEST", a["text"]),
                        ("FULL REPORTED REPLY", a["reply"] or "No reply recorded yet"),
                    ],
                    actions,
                )
            )
            if result:
                await call("team_assignment_review", assignment=a["id"], action=result)
