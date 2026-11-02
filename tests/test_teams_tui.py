import asyncio
from pathlib import Path
from typing import Any

from test_tui import ROWS, Fake
from textual.widgets import Button, Input, Select, Static, TextArea, Tree

from agentctl.daemon.bus import Bus
from agentctl.daemon.history import CommandHistory
from agentctl.daemon.registry import Registry
from agentctl.daemon.teams import Teams
from agentctl.models import Session
from agentctl.tui.teams import TeamDetail, TeamForm, TeamsScreen


class TeamFake(Fake):
    def __init__(self, folder: Path) -> None:
        super().__init__()
        reg = Registry(Bus())
        for row in ROWS:
            reg.upsert(Session.model_validate(row))
        self.teams = Teams(reg, CommandHistory())
        self.folder = folder

    async def rpc(self, method: str, **params: Any) -> Any:
        if method.startswith("team_") or method == "teams_list":
            self.calls.append((method, params))
            return self.teams.rpc(method, params, self.folder)
        return await super().rpc(method, **params)


def test_create_connect_edit_graph_and_reply(tmp_path: Path) -> None:
    fake = TeamFake(tmp_path)

    async def run() -> None:
        app = fake.watch(start_teams=True)
        async with app.run_test(size=(120, 50)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, TeamsScreen)
            await pilot.click("#new")
            await pilot.pause()
            assert isinstance(app.screen, TeamForm)
            app.screen.query_one("#name", Input).value = "Release team"
            app.screen.query_one("#session", Select).value = "aaaa1111"
            app.screen.query_one("#scope", TextArea).load_text("Ship the release")
            app.screen.query_one("#permission-ceiling", Select).value = "delegate"
            await pilot.click("#save")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, TeamsScreen)
            assert screen.team["name"] == "Release team"
            await pilot.click("#connect")
            await pilot.pause()
            form = app.screen
            assert isinstance(form, TeamForm)
            assert form.query_one("#permission", Select).value == "propose"
            form.query_one("#name", Input).value = "Implementation"
            form.query_one("#session", Select).value = "bbbb2222"
            form.query_one("#scope", TextArea).load_text("Build UI")
            form.query_one("#model", Input).value = "gpt-terra"
            await pilot.click("#save")
            await pilot.pause()
            graph = screen.query_one("#team-graph", Tree)
            worker = graph.root.children[0].children[0]
            assert "PROPOSE" in str(worker.label)
            graph.focus()
            graph.select_node(graph.root)
            await pilot.press("down", "down")
            await pilot.pause()
            assert screen.worker(), (screen.selection, graph.cursor_node.data)
            assert screen.worker()["role"] == "Implementation"
            assert screen.worker()["model"] == "gpt-terra"
            await pilot.click("#edit")
            await pilot.pause()
            app.screen.query_one("#permission", Select).value = "delegate"
            await pilot.click("#save")
            await pilot.pause()
            assert screen.team["workers"][0]["permission"] == "delegate"
            # Refreshing under a modal and returning to the graph must preserve selection.
            await pilot.click("#setup")
            await pilot.pause()
            assert isinstance(app.screen, TeamDetail)
            await pilot.press("escape")
            await pilot.pause()
            await screen.action_reload()
            assert screen.worker()["role"] == "Implementation"
            assert screen.worker()["model"] == "gpt-terra"
            await pilot.click("#reply")
            await pilot.pause()
            assert app.prompt_target == "bbbb2222"
            assert app.prompt_mode == "send"
            assert not any(m in ("send", "launch") for m, _ in fake.calls)

    asyncio.run(run())


def test_phone_graph_and_full_assignment_reply(tmp_path: Path) -> None:
    fake = TeamFake(tmp_path)
    team = fake.teams.create({"name": "Phone", "coordinator": "aaaa1111", "goal": "Review"})
    fake.teams.edit_member(team, {"session_id": "bbbb2222", "role": "Reviewer"})

    async def run() -> None:
        app = fake.watch(start_teams=True)
        async with app.run_test(size=(45, 40)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, TeamsScreen) and screen.has_class("narrow")
            assert screen.query_one("#team-graph").region.width <= 45
            # Details never truncate the tail of a long reply.
            reply = "x" * 12000 + "THE END"
            detail = TeamDetail("Result", [("FULL REPLY", reply)])
            app.push_screen(detail)
            await pilot.pause()
            assert any(str(s.content).endswith("THE END") for s in detail.query(Static))
            await pilot.press("escape")
            await pilot.pause()
            await pilot.resize_terminal(100, 40)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is app.screen_stack[0]
            assert not app.screen.has_class("narrow")

    asyncio.run(run())


def test_assignment_approval_and_full_result_acceptance(tmp_path: Path) -> None:
    fake = TeamFake(tmp_path)
    svc = fake.teams
    t = svc.create({"name": "Review", "coordinator": "aaaa1111", "goal": "Check results"})
    svc.edit_member(t, {"session_id": "bbbb2222", "role": "Reviewer"})
    setup = svc.setup(t, tmp_path)
    svc.authenticate(t, Path(setup["args"][-1]).read_text().strip())
    svc.activate(t)
    assignment = svc.assign(t, {"member": t.workers[0].id, "text": "Review this task"}, True)

    async def run() -> None:
        app = fake.watch(start_teams=True)
        async with app.run_test(size=(100, 45)) as pilot:
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, TeamsScreen)
            graph = screen.query_one(Tree)
            graph.select_node(graph.root.children[1].children[0])
            await pilot.pause()
            await pilot.click("#inspect")
            await pilot.pause()
            assert isinstance(app.screen, TeamDetail)
            await pilot.click("#approve")
            await pilot.pause()
            assert assignment.status == "queued"
            assignment.status = "reported_complete"
            assignment.reply = "Full reply\n" + "x" * 12000 + "END OF REPLY"
            await screen.action_reload()
            await pilot.click("#inspect")
            await pilot.pause()
            detail = app.screen
            assert isinstance(detail, TeamDetail)
            assert any(str(s.content).endswith("END OF REPLY") for s in detail.query(Static))
            detail.query_one("#accept", Button).scroll_visible(animate=False, immediate=True)
            await pilot.pause()
            await pilot.click("#accept")
            await pilot.pause()
            assert assignment.status == "accepted"
            assert not any(method == "send" for method, _ in fake.calls)

    asyncio.run(run())
