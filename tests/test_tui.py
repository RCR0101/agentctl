import asyncio
from collections.abc import AsyncIterator
from typing import Any

from textual import events
from textual.widgets import Button, OptionList, Static

from agentctl.tui import Watch

NOW = "2026-10-15T12:00:00+00:00"


def row(sid: str, status: str, source: str = "claude_code", title: str = "t") -> dict[str, Any]:
    approval = {"tool": "Bash: rm -rf build", "options": {"1": "Yes", "2": "No"}}
    return {
        "id": sid, "source": source, "origin": "cli", "cwd": "/w/proj", "title": title,
        "status": status, "controllable": True, "tmux_pane": "%1", "last_activity": NOW,
        "pending_approval": approval if status == "awaiting_approval" else None,
    }  # fmt: skip


ROWS = [
    row("aaaa1111", "working", title="refactor parser"),
    row("bbbb2222", "awaiting_approval", "codex", "deploy script"),
    row("cccc3333", "ended", title="old thing"),
]


class Fake:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def watch(self, **kw: Any) -> Watch:
        """The app, with jumping into a pane recorded instead of done."""
        app = Watch(rpc=self.rpc, follow=self.follow, **kw)

        async def jump(sock: str | None, pane: str) -> None:
            self.calls.append(("jump", {"socket": sock, "pane": pane}))

        app.jump = jump  # type: ignore[method-assign]
        return app

    async def rpc(self, method: str, **params: Any) -> Any:
        self.calls.append((method, params))
        if method == "ls":
            return ROWS
        if method == "feed":
            return [{"id": "aaaa1111", "title": "refactor parser", "ts": NOW, "kind": "reply",
                     "text": "Split the lexer."}]  # fmt: skip
        if method == "pane":
            return {"session_id": params["id"], "socket": "/tmp/agentctl-sock", "pane": "%1"}
        return {"session_id": params.get("id"), "chosen": "1. Yes"}

    async def follow(self, sid: str) -> AsyncIterator[dict[str, Any]]:
        s = next(r for r in ROWS if r["id"] == sid)
        ev = {
            "session_id": sid,
            "kind": "message",
            "ts": NOW,
            "payload": {"role": "user", "text": "hi"},
        }
        yield {"session": s, "events": [ev]}
        await asyncio.Event().wait()  # stream stays open


def test_narrow_layout_and_keys() -> None:
    fake = Fake()

    async def run() -> None:
        app = fake.watch()
        async with app.run_test(size=(45, 30)) as pilot:
            await pilot.pause()
            ol = app.query_one("#list", OptionList)
            first = ol.get_option_at_index(0)
            assert first.disabled and "NEEDS APPROVAL" in str(first.prompt)  # pinned on top
            assert app.selected == "bbbb2222"  # highlight starts on the approval
            assert app.screen.has_class("narrow")
            assert app.query_one("#left").display and not app.query_one("#detail").display

            await pilot.press("enter")  # a live session: straight into its pane
            assert ("jump", {"socket": "/tmp/agentctl-sock", "pane": "%1"}) in fake.calls
            await pilot.press("space")  # list -> transcript
            assert app.query_one("#detail").display and not app.query_one("#left").display
            await pilot.press("a")
            assert ("approve", {"id": "bbbb2222", "allow": True}) in fake.calls
            buttons = [str(b.label) for b in app.query("#actions Button").results(Button)]
            assert buttons == ["1. Yes", "2. No"]  # tappable, one per option
            await pilot.click("#opt2")
            assert ("approve", {"id": "bbbb2222", "allow": True, "choice": 2}) in fake.calls
            await pilot.press("escape")  # detail -> list
            assert app.query_one("#left").display

            await pilot.press("j")  # moves past the WORKING header to aaaa1111
            assert app.selected == "aaaa1111"
            await pilot.press("s", *"ship it", "enter")  # straight from the list
            assert app.query_one("#left").display  # still on the list
            assert ("send", {"id": "aaaa1111", "text": "ship it"}) in fake.calls
            await pilot.press("s")  # a multi-line paste arrives whole, then typing continues
            app.post_message(events.Paste("line one\nline two\n"))
            await pilot.pause()
            await pilot.press(*"bug?", "enter")
            assert ("send", {"id": "aaaa1111", "text": "line one\nline two\nbug?"}) in fake.calls
            await pilot.press("s", "a", "shift+enter", "b", "ctrl+j", "c")
            await pilot.press("backslash", "enter", "d")  # `\` + enter: new line on a phone
            await pilot.press("ctrl+w", "e", "enter")  # ctrl+w deletes a word, as in a shell
            assert ("send", {"id": "aaaa1111", "text": "a\nb\nc\ne"}) in fake.calls
            await pilot.press("escape")

            feed = app.query_one("#feed", OptionList)
            assert "Split the lexer." in str(feed.get_option_at_index(0).prompt)
            await pilot.click("#feed", offset=(5, 0))  # tap a feed line: read that session
            await pilot.pause()
            assert app.selected == "aaaa1111" and app.screen.has_class("detail-view")
            await pilot.press("escape")

            await pilot.press("slash", *"old")
            ids = [ol.get_option_at_index(i).id for i in range(ol.option_count)]
            assert ids == [None, "cccc3333"]  # header + the one match
            await pilot.press("escape")  # clears the filter
            assert ol.option_count == 5  # ENDED folds to its header on its own
            await pilot.press("e")
            assert ol.option_count == 6

    asyncio.run(run())


def test_wide_layout_shows_both_panes() -> None:
    fake = Fake()

    async def run() -> None:
        app = fake.watch()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert not app.screen.has_class("narrow")
            assert app.query_one("#left").display and app.query_one("#detail").display

    asyncio.run(run())


def test_replies_render_as_markdown() -> None:
    from rich.console import Console

    from agentctl.cli import render

    text = "**bold** and a table:\n\n| a | b |\n|---|---|\n| 1 | 2 |"
    ev = {
        "session_id": "x",
        "kind": "message",
        "ts": NOW,
        "payload": {"role": "assistant", "text": text},
    }
    con = Console(width=45, record=True)
    con.print(render(ev, narrow=True))
    shown = con.export_text()
    assert "**" not in shown and "|---" not in shown and "bold and a table" in shown
    con.print(render(ev, markdown=False))
    assert "**bold**" in con.export_text()


def test_transcript_folds_tools_and_is_selectable() -> None:
    from textual.selection import SELECT_ALL
    from textual.widgets import Collapsible, Markdown

    def ev(kind: str, **payload: Any) -> dict[str, Any]:
        return {"session_id": "aaaa1111", "kind": kind, "ts": NOW, "payload": payload}

    events = [
        ev("message", role="user", text="fix the tests"),
        ev("tool_call", name="bash", summary="pytest -q"),
        ev("tool_result", text="2 failed", is_error=True),
        ev("tool_call", name="read", summary="/w/a.py"),
        ev("tool_result", text="x = 1"),
        ev("tool_call", name="bash", summary="pytest -q"),
        ev("tool_result", text="ok"),
        ev("message", role="assistant", text="**Fixed** both tests."),
        ev("tool_call", name="bash", summary="git status"),
        ev("tool_result", text="clean"),
    ]

    class OneSession(Fake):
        async def follow(self, sid: str) -> AsyncIterator[dict[str, Any]]:
            yield {"session": ROWS[0], "events": events}
            await asyncio.Event().wait()

    fake = OneSession()

    async def run() -> None:
        app = fake.watch()
        async with app.run_test(size=(45, 40)) as pilot:
            await pilot.pause()
            await pilot.press("j", "space")
            await pilot.pause()
            groups = list(app.query(Collapsible).results(Collapsible))
            assert len(groups) == 2 and all(g.collapsed for g in groups)
            title = str(groups[0].title)
            assert "3 tools · last bash pytest -q" in title and "1 failed" in title
            assert "bash git status" in str(groups[1].title)  # a lone call shows its command
            assert len(app.query(Markdown)) == 1
            await pilot.click(groups[0].query_one("CollapsibleTitle"))
            assert not groups[0].collapsed  # tap expands
            app.screen.selections = {w: SELECT_ALL for w in app.query("#log Static")}
            copied = app.screen.get_selected_text() or ""
            assert "fix the tests" in copied and "pytest -q" in copied

    asyncio.run(run())


def test_close_and_new_with_folder_completion(tmp_path: Any) -> None:
    from textual.widgets import Input

    for d in ("alpha", "alpine", "beta", ".alps"):
        (tmp_path / d).mkdir()

    class Daemon(Fake):
        async def rpc(self, method: str, **params: Any) -> Any:
            if method == "launch":
                self.calls.append((method, params))
                return {"session_id": "new-abcd", "socket": "/s", "pane": "%9"}
            return await super().rpc(method, **params)

    fake = Daemon()

    async def run() -> None:
        app = fake.watch(agents=["claude", "codex"])
        async with app.run_test(size=(45, 30)) as pilot:
            await pilot.pause()
            await pilot.press("space", "x")
            await pilot.pause()
            await pilot.click("#yes")  # confirm dialog
            await pilot.pause()
            assert ("close", {"id": "bbbb2222"}) in fake.calls

            await pilot.press("escape", "n")
            await pilot.pause()
            folder = app.screen.query_one("#folder", Input)
            assert folder.has_focus  # type the folder straight away
            await pilot.press("ctrl+u", *f"{tmp_path}/al")
            await pilot.pause(0.3)
            ol = app.screen.query_one("#recent", OptionList)
            shown = [str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count)]
            assert shown == [f"{tmp_path}/alpha", f"{tmp_path}/alpine"]  # no dot-folders
            await pilot.press("tab")  # takes the completion and goes into it
            assert folder.value == f"{tmp_path}/alpha/"
            await pilot.click("#agent-codex")
            await pilot.click("#start")
            await pilot.pause()
            launch = next(p for m, p in fake.calls if m == "launch")
            assert launch["agent"] == "codex" and launch["cwd"] == f"{tmp_path}/alpha"
            assert ("jump", {"socket": "/s", "pane": "%9"}) in fake.calls  # opened right away

    asyncio.run(run())


def test_copy_rename_and_help() -> None:
    from agentctl.tui import Help

    fake = Fake()

    async def run() -> None:
        app = fake.watch()
        async with app.run_test(size=(45, 30)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            assert isinstance(app.screen, Help)
            await pilot.press("escape")
            await pilot.press("space", "c")
            assert app.clipboard == ""  # the fake transcript has no reply yet
            app.query_one("#log").last_reply = "**done**"  # type: ignore[attr-defined]
            await pilot.press("c")
            assert app.clipboard == "**done**"  # raw Markdown, as the agent wrote it
            await pilot.press("r", "ctrl+u", *"deploy", "enter")
            assert ("rename", {"id": "bbbb2222", "title": "deploy"}) in fake.calls

    asyncio.run(run())


def test_projects_digest_queue_and_divider() -> None:
    from agentctl.tui import Digest, Transcript

    rows = [
        row("aaaa1111", "working", title="refactor parser")
        | {"project": "api", "unread": 2, "seen_at": "2026-10-15T11:59:00+00:00"},
        row("bbbb2222", "awaiting_input", title="fix css")
        | {"project": "web", "conflicts": ["x.py also edited by aaaa1111"]},
    ]
    digest = [
        {"id": "aaaa1111", "title": "refactor parser", "status": "working", "new_replies": 2,
         "files": ["parser.py"], "last_reply": "Split the lexer."}
    ]  # fmt: skip

    class Daemon(Fake):
        async def rpc(self, method: str, **params: Any) -> Any:
            self.calls.append((method, params))
            if method == "ls":
                return rows
            if method == "digest":
                return digest
            if method == "usage":
                return {"codex": {"windows": [{"name": "5h", "used_percent": 15}]}}
            return {"session_id": params.get("id")}

        async def follow(self, sid: str) -> AsyncIterator[dict[str, Any]]:
            old = {"session_id": sid, "kind": "message", "ts": "2026-10-15T11:58:00+00:00",
                   "payload": {"role": "assistant", "text": "old"}}  # fmt: skip
            new = old | {"ts": NOW, "payload": {"role": "assistant", "text": "new"}}
            yield {"session": rows[0], "events": [old, new]}
            await asyncio.Event().wait()

    fake = Daemon()

    async def run() -> None:
        app = fake.watch()
        async with app.run_test(size=(45, 30)) as pilot:
            await pilot.pause()
            assert "cx 5h 15%" in app.sub_title  # Codex usage in the title bar
            ol = app.query_one("#list", OptionList)
            await pilot.press("p")
            heads = [str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count)]
            assert heads[0].startswith(" api (1)") and any(h.startswith(" web (1)") for h in heads)
            assert "2 new" in heads[1]  # unread badge on the card

            await pilot.press("u")
            await pilot.pause()
            assert isinstance(app.screen, Digest)
            await pilot.press("enter")  # open the one with news
            await pilot.pause()
            assert app.selected == "aaaa1111" and app.screen.has_class("detail-view")
            log = app.query_one("#log", Transcript)
            texts = [str(w.render()) for w in log.query(Static)]
            assert any("new since" in t for t in texts)  # divider between old and new
            await pilot.pause(1.2)  # next poll: it's on screen, so it's marked read
            assert ("seen", {"id": "aaaa1111"}) in fake.calls

            await pilot.press("l", *"then commit", "enter")
            assert ("queue", {"id": "aaaa1111", "text": "then commit"}) in fake.calls

    asyncio.run(run())
