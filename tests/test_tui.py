import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from textual import events
from textual.widgets import Button, OptionList, Static

from agentctl.tui import IDLE_SHOWN, Watch

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
        app.by_project = False  # These legacy fixtures exercise status grouping explicitly.

        async def jump(sock: str | None, pane: str) -> None:
            self.calls.append(("jump", {"socket": sock, "pane": pane}))

        app.jump = jump  # type: ignore[method-assign]
        return app

    async def rpc(self, method: str, **params: Any) -> Any:
        self.calls.append((method, params))
        if method == "ls":
            return sorted(
                ROWS, key=lambda r: {"awaiting_approval": 0, "working": 2, "ended": 4}[r["status"]]
            )
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

            bar = lambda: [str(b.label) for b in app.query("#tapbar Button").results(Button)]  # noqa: E731
            assert bar() == ["+ new", "review 1", "more"]  # nothing unread: no unread button
            assert not app.query_one("Footer").display  # the tap bar replaces it
            await pilot.press("enter")  # a phone opens the session's page, not its pane
            assert app.query_one("#detail").display and not app.query_one("#left").display
            assert bar() == ["‹ back", "reply", "files", "more"]
            approving = app.query_one("#actions Static", Static).render()
            assert "! Bash: rm -rf build" in str(approving)  # what the buttons approve
            await app.run_action("menu")  # jumping in is under more
            await pilot.pause()
            menu = app.screen.query_one(OptionList)
            assert menu.get_option_at_index(0).id == "attach"
            await pilot.press("enter")
            await pilot.pause()
            assert ("jump", {"socket": "/tmp/agentctl-sock", "pane": "%1"}) in fake.calls
            assert app.selected == "bbbb2222"  # the menu's rows never select a session
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
            await pilot.press("s", *"tapped")  # a phone keyboard has no esc, enter isn't obvious
            assert bar() == ["cancel", "queue"]  # the selected agent is working
            await pilot.click(app.query("#tapbar Button").last())
            assert ("send", {"id": "aaaa1111", "text": "tapped"}) in fake.calls
            assert bar() == ["+ new", "review 1", "more"]

            await pilot.press("slash", *"old")
            ids = [ol.get_option_at_index(i).id for i in range(ol.option_count)]
            assert ids == [None, "cccc3333"]  # header + the one match
            await pilot.press("escape")  # clears the filter
            assert ol.option_count == 5  # ENDED folds to its header on its own
            await pilot.press("e")
            assert ol.option_count == 6
            await pilot.click(app.query("#tapbar Button")[1])  # review: the one waiting on you
            assert app.selected == "bbbb2222" and app.screen.has_class("detail-view")

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
            stops = set()
            for _ in range(6):  # Tab moves between panes, never onto the `▸` lines
                await pilot.press("tab")
                stops.add(type(app.focused).__name__)
            assert stops <= {"OptionList", "Files", "Transcript"}, stops
            app.screen.selections = {w: SELECT_ALL for w in app.query("#log Static")}
            copied = app.screen.get_selected_text() or ""
            assert "fix the tests" in copied and "pytest -q" in copied

    asyncio.run(run())


def test_close_and_new_with_folder_completion(tmp_path: Any) -> None:
    from textual.widgets import Input

    for d in ("alpha", "alpine", "beta", ".alps"):
        (tmp_path / d).mkdir()

    outside = row("dddd4444", "awaiting_input") | {"tmux_pane": None, "controllable": False}
    rows = [ROWS[1], row("eeee5555", "awaiting_input"), outside]

    class Daemon(Fake):
        async def rpc(self, method: str, **params: Any) -> Any:
            if method == "ls":
                return rows
            if method == "launch":
                self.calls.append((method, params))
                return {"session_id": "new-abcd", "socket": "/s", "pane": "%9"}
            return await super().rpc(method, **params)

        async def follow(self, sid: str) -> AsyncIterator[dict[str, Any]]:
            yield {"session": next(r for r in rows if r["id"] == sid), "events": []}
            await asyncio.Event().wait()

    fake = Daemon()

    async def run() -> None:
        from agentctl.tui import Confirm

        app = fake.watch(agents=["claude", "codex"])
        async with app.run_test(size=(45, 30)) as pilot:
            await pilot.pause()

            async def remove(sid: str) -> str:
                app.select(sid)
                app.rebuild()
                await pilot.press("x")
                await pilot.pause()
                if not isinstance(app.screen, Confirm):
                    return ""
                q = app.screen.question
                await pilot.click("#yes")
                await pilot.pause()
                return q

            assert "Hide" in await remove("bbbb2222")  # hiding never aborts a working agent
            assert "Hide" in await remove("dddd4444")  # outside agentctl: just hidden
            assert ("hide", {"id": "dddd4444"}) in fake.calls
            assert ("close", {"id": "dddd4444"}) not in fake.calls
            await pilot.press("z")  # undo
            assert ("unhide", {"id": "dddd4444"}) in fake.calls and app.removed is None
            app.select("eeee5555")
            app.rebuild()
            await pilot.press("ctrl+x")
            await pilot.pause()
            assert isinstance(app.screen, Confirm) and "Quit" in app.screen.question
            await pilot.click("#yes")
            await pilot.pause()
            calls = [
                c for c in fake.calls if c[1].get("id") == "eeee5555" and c[0] in ("close", "hide")
            ]
            assert calls[-2:] == [("close", {"id": "eeee5555"}), ("hide", {"id": "eeee5555"})]

            await pilot.press("n")
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
            await pilot.pause()
            assert isinstance(app.screen, Help)
            box = app.screen.query_one("VerticalScroll").size  # it once collapsed to nothing
            assert box.width > 30 and box.height > 20
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
                soon = time.time() + 3600
                return {
                    "codex": {"windows": [{"name": "5h", "used_percent": 15, "resets_at": soon}]}
                }
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
            usage = str(app.query_one("#usage", Static).render())
            assert "cx 5h 15% (" in usage  # Codex usage under the list, with its reset time
            ol = app.query_one("#list", OptionList)
            await pilot.press("p")
            heads = [str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count)]
            # the parent folder only appears when two projects share a name
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


def test_files_of_the_selected_session(tmp_path: Any) -> None:
    from agentctl.tui import Files

    for d in ("src", "node_modules", ".git", "__pycache__"):
        (tmp_path / d).mkdir()
    (tmp_path / "README.md").write_text("hi")
    rows = [row("aaaa1111", "working") | {"cwd": str(tmp_path)}]

    class Daemon(Fake):
        async def rpc(self, method: str, **params: Any) -> Any:
            return rows if method == "ls" else await super().rpc(method, **params)

        async def follow(self, sid: str) -> AsyncIterator[dict[str, Any]]:
            yield {"session": rows[0], "events": []}
            await asyncio.Event().wait()

    async def run() -> None:
        app = Daemon().watch()
        async with app.run_test(size=(45, 30)) as pilot:
            await pilot.pause(0.5)
            tree = app.query_one("#files", Files)
            assert str(tree.path) == str(tmp_path) and not tree.display  # a phone: its own page
            await pilot.press("enter")
            await app.run_action("files")
            await pilot.pause()
            assert tree.display and not app.query_one("#detail").display
            names = [str(n.label) for n in tree.root.children]
            assert names == ["src", "README.md"]  # no dot-folders, caches or dependencies
            assert str(tmp_path.name) in str(app.query_one("#files-title", Static).render())

    asyncio.run(run())


def test_running_timer_and_queued_messages() -> None:
    rows = [row("aaaa1111", "working") | {"queued": ["and add tests", "then commit"]}]
    started = (datetime.now(UTC) - timedelta(seconds=75)).isoformat()
    call = {"session_id": "aaaa1111", "kind": "tool_call", "ts": started,
            "payload": {"id": "t1", "name": "Bash", "summary": "pytest -q"}}  # fmt: skip

    class Daemon(Fake):
        async def rpc(self, method: str, **params: Any) -> Any:
            if method == "ls":
                return rows
            return await super().rpc(method, **params)

        async def follow(self, sid: str) -> AsyncIterator[dict[str, Any]]:
            yield {"session": rows[0], "events": [call]}
            await asyncio.Event().wait()

    fake = Daemon()

    async def run() -> None:
        app = fake.watch()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(0.5)
            running = str(app.query_one("#running", Static).render())
            assert "Bash pytest -q · running 1m 1" in running  # ticks while the call runs
            buttons = list(app.query("#queued Button").results(Button))
            assert [str(b.label) for b in buttons] == [
                "queued: and add tests · cancel",
                "queued: then commit · cancel",
            ]
            await pilot.click(buttons[1])  # tap to cancel that one
            assert ("unqueue", {"id": "aaaa1111", "index": 1}) in fake.calls

    asyncio.run(run())


def test_send_never_loses_text_or_changes_target() -> None:
    from agentctl.tui import Prompt

    class Daemon(Fake):
        fail = True

        async def rpc(self, method: str, **params: Any) -> Any:
            if method == "send" and self.fail:
                self.calls.append((method, params))
                raise RuntimeError("daemon: connection refused")
            return await super().rpc(method, **params)

    fake = Daemon()

    async def run() -> None:
        app = fake.watch()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.select("aaaa1111")
            await pilot.press("s", *"ship it")
            app.select("bbbb2222")  # the highlight moves while typing (a rebuild, a tap…)
            await pilot.press("enter")
            await pilot.pause()
            assert ("send", {"id": "aaaa1111", "text": "ship it"}) in fake.calls  # its own target
            prompt = app.query_one("#prompt", Prompt)
            assert app.prompt_mode == "send" and prompt.text == "ship it"  # failed: text kept
            fake.fail = False
            await pilot.press("enter")
            await pilot.pause()
            sends = [p for m, p in fake.calls if m == "send"]
            assert app.prompt_mode is None and sends == [{"id": "aaaa1111", "text": "ship it"}] * 2

            app.select("cccc3333")  # ended: s still opens the box, so keys can't leak
            await pilot.press("s", "a", "d")
            assert app.prompt_mode == "send" and prompt.text == "ad"
            assert not any(m == "approve" for m, _ in fake.calls)

    asyncio.run(run())


def test_transcript_reconnects_when_the_stream_drops() -> None:
    from agentctl.tui import Transcript

    class Daemon(Fake):
        tries = 0

        async def follow(self, sid: str) -> AsyncIterator[dict[str, Any]]:
            self.tries += 1
            if self.tries == 1:  # first stream dies at once, like a daemon restart
                raise OSError("connection reset")
            ev = {"session_id": sid, "kind": "message", "ts": NOW,
                  "payload": {"role": "user", "text": "after restart"}}  # fmt: skip
            yield {"session": ROWS[0], "events": [ev]}
            await asyncio.Event().wait()

    fake = Daemon()

    async def run() -> None:
        app = fake.watch()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause(1.6)
            texts = [str(w.render()) for w in app.query_one("#log", Transcript).query(Static)]
            assert fake.tries >= 2 and any("after restart" in t for t in texts)

    asyncio.run(run())


def test_send_history_per_session() -> None:
    from agentctl.tui import History, Prompt

    class Daemon(Fake):
        async def rpc(self, method: str, **params: Any) -> Any:
            if method == "send" and params["text"] == "boom":
                raise RuntimeError("connection refused")
            if method == "tail":  # only "first" shows up in its transcript
                ev = {"kind": "message", "payload": {"role": "user", "text": "first"}}
                return {"session": ROWS[0], "events": [ev]}
            return await super().rpc(method, **params)

    fake = Daemon()

    async def run() -> None:
        app = fake.watch()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.select("aaaa1111")
            for text in ("first", "second"):
                await pilot.press("s", *text, "enter")
                await pilot.pause()
            await pilot.press("s", *"boom", "enter")
            await pilot.pause()
            await pilot.press("escape")  # the failed one stays in the box; close it
            assert [s["outcome"] for s in app.sent["aaaa1111"]] == ["sent", "sent", "failed"]

            await pilot.press("s", "up")  # shell-like recall, newest first
            prompt = app.query_one("#prompt", Prompt)
            assert prompt.text == "boom"
            await pilot.press("up", "up", "down")
            assert prompt.text == "second"
            await pilot.press("escape")

            await pilot.press("h")
            await pilot.pause()
            assert isinstance(app.screen, History)
            ol = app.screen.query_one(OptionList)
            lines = [str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count)]
            assert "failed" in lines[0] and "connection refused" in lines[0]
            assert "not in transcript yet" in lines[1] and "second" in lines[1]
            assert "arrived" in lines[2] and "first" in lines[2]
            await pilot.press("down", "enter")  # pick "second": back in the send box
            await pilot.pause()
            assert app.prompt_mode == "send" and prompt.text == "second"

    asyncio.run(run())


def test_row_shows_ls_columns() -> None:
    from datetime import UTC, datetime, timedelta

    from agentctl.tui import row_text

    now = datetime.now(UTC)
    r = {
        "id": "abcdef12-0000", "source": "claude_code", "origin": "cli", "status": "idle",
        "last_activity": now.isoformat(), "title": "t", "cwd": "/x",
        "context_tokens": 50_000, "context_window": 200_000, "context_window_exact": True,
        "cache_expires_at": (now + timedelta(minutes=42, seconds=30)).isoformat(),
    }  # fmt: skip
    assert row_text(r).plain.splitlines() == ["t", "x · cc · 0s"]  # quiet when all is well
    assert "abcdef12" not in row_text(r).plain  # the id is in the selected session's header
    assert "idle" in row_text(r, status=True).plain and "idle" not in row_text(r).plain
    full = r | {"context_tokens": 170_000, "status": "awaiting_input"}
    full["cache_expires_at"] = (now + timedelta(minutes=3, seconds=30)).isoformat()
    assert row_text(full).plain.splitlines()[1] == "x · cc · 0s · 85% · cache 3m"
    assert row_text(r, project="x · work").plain.splitlines()[1].startswith("x · work · ")


def test_phone_card_says_what_it_needs() -> None:
    from agentctl.tui import card_text

    r = row("aaaa1111", "awaiting_input", title="fix parser") | {
        "preview": {"kind": "assistant", "text": "I fixed **the** [parser](http://x) " * 9}
    }
    lines = card_text(r, width=40).plain.splitlines()
    assert lines[0] == "▌ fix parser" and lines[1].startswith("▌ proj · cc · ")
    assert "input" not in lines[1]  # its group heading says so
    assert lines[2] == "▌ ◆ I fixed the parser I fixed the"  # Markdown's marks dropped
    assert len(lines) == 4 and lines[3].endswith("…") and all(len(x) <= 40 for x in lines)
    appr = card_text(row("bbbb2222", "awaiting_approval")).plain.splitlines()
    assert appr[2] == "▌ ! Bash: rm -rf build"  # what it wants approved, not its last words


def test_questions_are_answered_in_a_form() -> None:
    from textual.widgets import Checkbox, Input, RadioButton

    from agentctl.tui import Questions, card_text

    qs = [
        {"question": "Colour?", "header": "C", "options": [{"label": "red"}, {"label": "blue"}]},
        {"question": "Pets?", "header": "P", "multiSelect": True,
         "options": [{"label": "cat"}, {"label": "dog"}, {"label": "fish"}]},
    ]  # fmt: skip
    asking = row("aaaa1111", "awaiting_approval") | {
        "pending_approval": {"tool": "AskUserQuestion", "options": {}, "questions": qs}
    }
    assert "? Colour? (+1 more)" in card_text(asking).plain

    class Asking(Fake):
        async def rpc(self, method: str, **params: Any) -> Any:
            return [asking] if method == "ls" else await super().rpc(method, **params)

    fake = Asking()

    async def run() -> None:
        app = fake.watch()
        async with app.run_test(size=(45, 44)) as pilot:
            await pilot.pause()
            assert ("review 1", "review") in app.tapbar_shown
            await pilot.press("a")
            await pilot.pause()
            form = app.screen
            assert isinstance(form, Questions)
            await pilot.click("#q-send")  # nothing picked: it says so, and stays
            assert isinstance(app.screen, Questions)
            list(form.query("#q0 RadioButton").results(RadioButton))[1].value = True
            boxes = list(form.query("#q1 Checkbox").results(Checkbox))
            boxes[0].value = boxes[2].value = True
            form.query_one("#t1", Input).value = "a tortoise"
            form.query_one("#q-send", Button).press()  # the warning toast covers it now
            await pilot.pause()
            answers = [{"choices": [2], "text": ""}, {"choices": [1, 3], "text": "a tortoise"}]
            assert ("answer", {"id": "aaaa1111", "answers": answers}) in fake.calls

    asyncio.run(run())


def test_question_without_options_is_a_text_box() -> None:
    from textual.app import App
    from textual.widgets import Input

    from agentctl.tui import Questions

    got: list[object] = []

    class Host(App[None]):
        def on_mount(self) -> None:
            self.push_screen(Questions([{"question": "Name?", "options": []}]), got.append)

    async def run() -> None:
        app = Host()
        async with app.run_test(size=(45, 30)) as pilot:
            await pilot.pause()
            assert not app.screen.query("RadioSet")  # Codex: free text only
            app.screen.query_one("#t0", Input).value = "sam"
            app.screen.query_one("#q-send", Button).press()
            await pilot.pause()
            assert got == [[{"choices": [], "text": "sam"}]]

    asyncio.run(run())


def test_theme_remembered_only_when_picked(tmp_path: Path, monkeypatch: Any) -> None:
    import agentctl.tui as tui

    f = tmp_path / "theme"
    monkeypatch.setattr(tui, "THEME_FILE", f)
    app = tui.Watch(rpc=Fake().rpc)
    assert app.theme == tui.DEFAULT_THEME and not f.exists()  # the default isn't a choice
    app.theme = "ansi-light"
    assert f.read_text().strip() == "ansi-light"
    assert tui.Watch(rpc=Fake().rpc).theme == "ansi-light"  # comes back next start
    assert tui.Watch(rpc=Fake().rpc, theme="nord").theme == "nord"  # --theme wins
    f.write_text("no-such-theme")
    assert tui.saved_theme() == tui.DEFAULT_THEME


def test_keys_ignore_case() -> None:
    fake = Fake()

    async def run() -> None:
        app = fake.watch()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("A")  # caps lock / shift: still approve
            assert ("approve", {"id": "bbbb2222", "allow": True}) in fake.calls
            await pilot.press("J")
            assert app.selected == "aaaa1111"
            await pilot.press("S")
            assert app.prompt_mode == "send"

    asyncio.run(run())


def test_long_turn_and_cache_alerts() -> None:
    fake = Fake()

    async def run() -> None:
        app = fake.watch()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            said: list[str] = []
            app.notify = lambda msg, **kw: said.append(msg)  # type: ignore[assignment,method-assign,misc]
            app.working_since["aaaa1111"] = time.time() - 300
            app.announce([row("aaaa1111", "awaiting_input")])
            assert said and "finished after 5 min" in said[-1]
            app.working_since["aaaa1111"] = time.time() - 30  # short turns stay quiet
            said.clear()
            app.announce([row("aaaa1111", "awaiting_input")])
            assert not said
            soon = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
            waiting = row("aaaa1111", "awaiting_input") | {"cache_expires_at": soon}
            app.announce([waiting])
            app.announce([waiting])  # once per expiry, not every second
            assert len(said) == 1 and "cache expires in" in said[0]
            app.announce([waiting | {"status": "working"}])  # busy: not your move
            assert len(said) == 1

    asyncio.run(run())


def test_stuck_and_context_alerts_fire_once() -> None:
    fake = Fake()

    async def run() -> None:
        app = fake.watch()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            said: list[str] = []
            app.notify = lambda msg, **kw: said.append(msg)  # type: ignore[assignment,method-assign,misc]
            quiet = (datetime.now(UTC) - timedelta(minutes=12)).isoformat()
            stuck = row("aaaa1111", "working") | {"last_activity": quiet}
            app.announce([stuck])
            app.announce([stuck])  # once per silence, not every poll
            assert len(said) == 1 and "no output for 12 min" in said[0]
            said.clear()
            full = row("bbbb2222", "idle") | {"context_tokens": 180_000, "context_window": 200_000}
            app.announce([full])
            app.announce([full])
            assert len(said) == 1 and "90% of its context" in said[0] and "/compact" in said[0]
            app.announce([full | {"context_tokens": 20_000}])  # compacted: re-arms
            app.announce([full])
            assert len(said) == 2

    asyncio.run(run())


def test_list_keeps_refreshing_after_a_stuck_poll(monkeypatch: Any) -> None:
    import agentctl.tui as tui

    monkeypatch.setattr(tui, "STALL", 2)
    rows = [row("aaaa1111", "working")]
    hang = asyncio.Event()

    class Stuck(Fake):
        async def rpc(self, method: str, **params: Any) -> Any:
            if method == "ls":
                if hang.is_set():
                    hang.clear()
                    await asyncio.Event().wait()  # a reply that never comes
                return rows
            return await super().rpc(method, **params)

    async def run() -> None:
        app = Stuck().watch()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause(1.2)
            said: list[str] = []
            app.notify = lambda msg, **kw: said.append(msg)  # type: ignore[assignment,method-assign,misc]
            hang.set()
            await pilot.pause(1.2)
            rows[0] = row("aaaa1111", "awaiting_input")
            await pilot.pause(4)
            assert app.current() and app.current()["status"] == "awaiting_input"
            assert said == ["the list had stopped refreshing; restarted it"]
            app.poll_timer.stop()  # the timer itself gone (a cancelled poll ends it)
            rows[0] = row("aaaa1111", "working")
            await pilot.pause(4.5)
            assert app.current() and app.current()["status"] == "working"

    asyncio.run(run())


def test_message_arrival_confirmed_or_flagged() -> None:
    class Daemon(Fake):
        transcript: list[str] = []

        async def rpc(self, method: str, **params: Any) -> Any:
            if method == "tail":
                evs = [{"kind": "message", "payload": {"role": "user", "text": t}} for t in
                       self.transcript]  # fmt: skip
                return {"session": ROWS[0], "events": evs}
            return await super().rpc(method, **params)

    fake = Daemon()

    async def run() -> None:
        app = fake.watch()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            said: list[str] = []
            app.notify = lambda msg, **kw: said.append(msg)  # type: ignore[assignment,method-assign,misc]
            arrived = {"text": "fix\tit  now", "outcome": "sent"}
            lost = {"text": "where did this go", "outcome": "sent"}
            app.sent["aaaa1111"] = [arrived, lost]
            fake.transcript = ["fix    it now"]  # Claude turned the tab into spaces
            free = [row("aaaa1111", "awaiting_input")]
            await app.check_arrivals(free)
            assert arrived["settled"] == "arrived" and "has your message" in said[0]
            assert "settled" not in lost  # free only just now: give it time
            lost["free_at"] = time.time() - 31
            await app.check_arrivals(free)
            assert lost["settled"] == "missing" and "still hasn't got" in said[-1]

    asyncio.run(run())


def test_quick_replies_fill_the_send_box(tmp_path: Path, monkeypatch: Any) -> None:
    import agentctl.tui as tui

    (tmp_path / "replies").write_text("go on\n\nrun the linter\n")
    monkeypatch.setattr(tui, "REPLIES_FILE", tmp_path / "replies")
    fake = Fake()

    async def run() -> None:
        app = fake.watch()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("j", "s")
            labels = [str(b.label) for b in app.query("#replies Button").results(Button)]
            assert labels == ["go on", "run the linter"]
            await pilot.click("#replies Button")
            await pilot.press("enter")
            assert ("send", {"id": "aaaa1111", "text": "go on"}) in fake.calls
            assert not app.query_one("#replies").has_class("show")

    asyncio.run(run())


def test_search_opens_the_session() -> None:
    class Daemon(Fake):
        async def rpc(self, method: str, **params: Any) -> Any:
            if method == "search":
                self.calls.append((method, params))
                return [{"id": "cccc3333", "ts": NOW, "role": "assistant", "title": "old thing",
                         "status": "ended", "snippet": "we set up Caddy here"}]  # fmt: skip
            return await super().rpc(method, **params)

    fake = Daemon()

    async def run() -> None:
        app = fake.watch()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("f")
            await pilot.pause()  # the screen opens from a worker
            await pilot.press(*"caddy", "enter")
            await pilot.pause()
            assert ("search", {"q": "caddy"}) in fake.calls
            await pilot.press("enter")  # the first match
            await pilot.pause()
            assert app.selected == "cccc3333" and app.show_ended

    asyncio.run(run())


def test_files_marks_edits_and_their_folders() -> None:
    from agentctl.tui import Files

    async def run() -> None:
        app = Fake().watch()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            f = app.query_one("#files", Files)
            f.set_edited(["/w/proj/src/a.py"])
            assert "/w/proj/src/a.py" in f.edited
            assert {"/w/proj/src", "/w/proj", "/w"} <= f.edited_dirs

    asyncio.run(run())


def test_usage_shows_when_limits_reset() -> None:
    from agentctl.cli import usage_text

    now = time.time()
    u = {"claude": {"windows": [
        {"name": "5h", "used_percent": 22, "resets_at": now + 2 * 3600},
        {"name": "week", "used_percent": 25, "resets_at": now + 3 * 86400},
        {"name": "old", "used_percent": 0, "resets_at": now - 60},  # already reset: no time
    ]}}  # fmt: skip
    line = usage_text(u, resets=True).plain
    soon = time.strftime("%H:%M", time.localtime(now + 2 * 3600))
    later = time.strftime("%a %H:%M", time.localtime(now + 3 * 86400))
    assert line == f"cc 5h 22% ({soon}) · week 25% ({later}) · old 0%"
    assert usage_text(u).plain == "cc 5h 22% · week 25% · old 0%"  # compact form unchanged


def test_preview_changes_refresh_without_a_status_or_age_change() -> None:
    rows = [
        row("aaaa1111", "working")
        | {"preview": {"kind": "tool", "text": "pytest first", "ts": NOW}}
    ]

    class Daemon(Fake):
        async def rpc(self, method: str, **params: Any) -> Any:
            return rows if method == "ls" else await super().rpc(method, **params)

    async def run() -> None:
        for width in (45, 120):
            app = Daemon().watch()
            async with app.run_test(size=(width, 30)) as pilot:
                await pilot.pause()
                rows[0]["preview"]["text"] = "pytest second"
                await app.poll()
                listing = app.query_one("#list", OptionList)
                assert "pytest second" in str(listing.get_option("aaaa1111").prompt)
                rows[0]["preview"]["text"] = "pytest first"

    asyncio.run(run())


def test_desktop_navigation_files_palette_and_local_review(tmp_path: Path) -> None:
    from agentctl.tui import Review

    rows = [row("aaaa1111", "awaiting_input") | {"cwd": str(tmp_path), "unread": 1}]

    class Daemon(Fake):
        async def rpc(self, method: str, **params: Any) -> Any:
            if method == "ls":
                return rows
            if method == "review":
                self.calls.append((method, params))
                return {"request": "fix parser", "reply": "Fixed the parser.", "files": []}
            return await super().rpc(method, **params)

    async def run() -> None:
        fake = Daemon()
        app = fake.watch()
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            assert not app.query_one("#files").display
            await pilot.press("enter")
            assert any(method == "jump" for method, _ in fake.calls)
            assert not app.screen.has_class("detail-view")
            await pilot.press("space")
            assert app.focused == app.query_one("#log")
            assert not app.query_one("#left").display
            assert app.query_one("#detail").size.width == app.size.width
            await pilot.press("escape")
            assert app.query_one("#left").display and app.query_one("#detail").display
            await pilot.press("v")
            await pilot.pause()
            assert app.query_one("#files").display
            await pilot.press("v")
            assert not app.query_one("#files").display
            titles = {command.title for command in app.get_system_commands(app.screen)}
            assert {"Review latest turn", "Sent history", "Open terminal"} <= titles
            await pilot.press("ctrl+p")
            await pilot.pause()
            assert app.screen.__class__.__name__ == "CommandPalette"
            assert all(not icon.display for icon in app.screen.query("SearchIcon"))
            await pilot.press("escape", "w")
            await pilot.pause()
            assert isinstance(app.screen, Review)
            assert app.screen.report["reply"] == "Fixed the parser."
            assert ("review", {"id": "aaaa1111"}) in fake.calls
            assert not any(method in ("send", "launch") for method, _ in fake.calls)

    asyncio.run(run())


def test_disconnection_keeps_last_data_and_shows_its_age() -> None:
    class Daemon(Fake):
        offline = False

        async def rpc(self, method: str, **params: Any) -> Any:
            if method == "ls" and self.offline:
                raise OSError("daemon unavailable")
            return await super().rpc(method, **params)

    async def run() -> None:
        fake = Daemon()
        app = fake.watch()
        async with app.run_test(size=(45, 30)) as pilot:
            await pilot.pause()
            fake.offline = True
            await app.poll()
            connection = app.query_one("#connection", Static)
            assert connection.has_class("degraded")
            assert "last update" in str(connection.render())
            assert len(app.rows) == len(ROWS)
            fake.offline = False
            await app.poll()
            assert not connection.has_class("degraded")
            assert "Live" in str(connection.render())

    asyncio.run(run())


def test_full_long_reply_is_rendered_and_copied_in_markdown_and_raw_modes() -> None:
    from agentctl.tui import Transcript

    reply = "A complete paragraph.\n\n" * 300 + "THE FINAL ENDING"

    class Daemon(Fake):
        async def follow(self, sid: str) -> AsyncIterator[dict[str, Any]]:
            yield {
                "session": ROWS[1],
                "events": [
                    {
                        "session_id": sid,
                        "kind": "message",
                        "ts": NOW,
                        "payload": {"role": "assistant", "text": reply},
                    }
                ],
            }
            await asyncio.Event().wait()

    async def run() -> None:
        app = Daemon().watch()
        copied: list[str] = []
        app.copy_to_clipboard = copied.append  # type: ignore[method-assign]
        async with app.run_test(size=(45, 30)) as pilot:
            await pilot.pause()
            await pilot.press("enter", "c")
            await pilot.pause()
            log = app.query_one("#log", Transcript)
            assert log.last_reply == reply and copied[-1] == reply
            log.scroll_end(animate=False)
            await pilot.pause()
            assert "THE FINAL ENDING" in "\n".join(
                strip.text for strip in app.screen._compositor.render_strips()
            )
            await pilot.press("m")
            await pilot.pause()
            assert log.last_reply == reply
            assert any("THE FINAL ENDING" in str(w.render()) for w in log.query(Static))

    asyncio.run(run())


def test_history_loaded_when_dashboard_reopens() -> None:
    receipt = {
        "id": "persisted",
        "at": "2026-10-18 12:00",
        "text": "earlier message",
        "outcome": "sent",
        "settled": "arrived",
        "error": "",
    }

    class Daemon(Fake):
        async def rpc(self, method: str, **params: Any) -> Any:
            return [receipt] if method == "history" else await super().rpc(method, **params)

    async def run() -> None:
        app = Daemon().watch()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert app.sent["bbbb2222"][0]["text"] == "earlier message"
            await pilot.press("s", "up")
            assert app.query_one("#prompt").text == "earlier message"  # type: ignore[attr-defined]
            await pilot.press("escape", "h")
            await pilot.pause()
            assert "arrived" in str(app.screen.query_one(OptionList).get_option_at_index(0).prompt)

    asyncio.run(run())


def test_read_only_approval_and_empty_filter_cannot_control_a_hidden_session() -> None:
    rows = [row("bbbb2222", "awaiting_approval") | {"controllable": False, "tmux_pane": None}]

    class Daemon(Fake):
        async def rpc(self, method: str, **params: Any) -> Any:
            return rows if method == "ls" else await super().rpc(method, **params)

    async def run() -> None:
        fake = Daemon()
        app = fake.watch()
        async with app.run_test(size=(45, 30)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            assert all(b.disabled for b in app.query("#actions Button").results(Button))
            await pilot.press("a", "d")
            assert not any(method == "approve" for method, _ in fake.calls)
            await pilot.press("escape")
            app.filter = "no such session"
            app.rebuild()
            await pilot.pause()
            assert app.selected is None and app.current() is None
            assert app.query_one("#empty").display
            await pilot.press("s")
            assert app.prompt_mode is None

    asyncio.run(run())


def test_review_works_with_a_daemon_without_the_review_method() -> None:
    from agentctl.tui import Review

    class OldDaemon(Fake):
        async def rpc(self, method: str, **params: Any) -> Any:
            if method == "review":
                raise RuntimeError("unknown method 'review'")
            if method == "tail":
                return {
                    "session": ROWS[1],
                    "events": [
                        {
                            "session_id": "bbbb2222",
                            "kind": "message",
                            "ts": NOW,
                            "payload": {"role": "user", "text": "Fix the parser"},
                        },
                        {
                            "session_id": "bbbb2222",
                            "kind": "message",
                            "ts": NOW,
                            "payload": {
                                "role": "assistant",
                                "text": "Fixed, including the ending.",
                            },
                        },
                    ],
                }
            return await super().rpc(method, **params)

    async def run() -> None:
        app = OldDaemon().watch()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("w")
            await pilot.pause()
            assert isinstance(app.screen, Review)
            assert app.screen.report["request"] == "Fix the parser"
            assert app.screen.report["reply"] == "Fixed, including the ending."

    asyncio.run(run())


def test_status_headings_are_distinct_full_width_banners() -> None:
    from agentctl.tui import GROUPS
    from agentctl.tui.presentation import group_heading

    styles = []
    for status, label in GROUPS:
        heading = group_heading(status, label, 2, 45)
        assert label in heading.plain and heading.cell_len == 45
        assert heading.plain.startswith(" [")
        styles.append(heading.style)
    assert len(set(styles)) == len(GROUPS)


def test_composer_preserves_session_drafts_and_returns_focus() -> None:
    from agentctl.tui import Prompt

    async def run() -> None:
        for width in (45, 120):
            fake = Fake()
            app = fake.watch()
            async with app.run_test(size=(width, 35)) as pilot:
                await pilot.pause()
                app.select("aaaa1111")
                app.action_log()
                await pilot.press("s", *"first draft", "escape")
                assert app.focused is app.query_one("#log")
                app.select("bbbb2222")
                await pilot.press("s", "enter")  # empty Enter doesn't discard the composer
                assert app.prompt_mode == "send"
                await pilot.press(*"second draft", "escape")
                app.select("aaaa1111")
                await pilot.press("s")
                prompt = app.query_one("#prompt", Prompt)
                assert prompt.text == "first draft"
                assert "aaaa1111" in str(app.query_one("#composer-title", Static).render())
                assert app.query_one("#tapbar").display  # same buttons at both widths
                await pilot.press("ctrl+s")
                await pilot.pause()
                assert ("send", {"id": "aaaa1111", "text": "first draft"}) in fake.calls
                assert app.prompt_mode is None and app.focused is app.query_one("#log")
                assert ("send", "aaaa1111") not in app.drafts
                assert app.drafts[("send", "bbbb2222")] == "second draft"
                await pilot.press("enter")  # reading the transcript must not attach a terminal
                assert not any(method == "jump" for method, _ in fake.calls)

    asyncio.run(run())


def test_slow_send_keeps_the_editor_and_submits_only_once() -> None:
    from agentctl.tui import Prompt

    async def run() -> None:
        started, release = asyncio.Event(), asyncio.Event()

        class Slow(Fake):
            async def rpc(self, method: str, **params: Any) -> Any:
                if method == "send":
                    self.calls.append((method, params))
                    started.set()
                    await release.wait()
                    return {"session_id": params["id"], "command_id": "receipt-one"}
                return await super().rpc(method, **params)

        fake = Slow()
        app = fake.watch()
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            app.select("aaaa1111")
            await pilot.press("s", *"one message", "ctrl+enter")
            await asyncio.wait_for(started.wait(), 2)
            prompt = app.query_one("#prompt", Prompt)
            assert app.submitting and prompt.read_only and prompt.display
            assert prompt.text == "one message"
            assert all(b.disabled for b in app.query("#tapbar Button").results(Button))
            app.select("bbbb2222")
            app.rebuild()
            assert "aaaa1111" in str(app.query_one("#composer-title", Static).render())
            app.action_submit()
            await pilot.press("enter", "shift+enter", "s", "a")
            assert prompt.text == "one message"
            assert len([call for call in fake.calls if call[0] == "send"]) == 1
            assert not any(call[0] in ("jump", "approve") for call in fake.calls)
            release.set()
            await pilot.pause()
            assert not app.submitting and app.prompt_mode is None
            assert app.sent["aaaa1111"][0]["id"] == "receipt-one"
            assert not app.sent.get("bbbb2222")

    asyncio.run(run())


def test_escape_clears_applied_filter_and_restores_the_files_parent_view(tmp_path: Path) -> None:
    class Daemon(Fake):
        async def rpc(self, method: str, **params: Any) -> Any:
            if method == "ls":
                return [dict(r, cwd=str(tmp_path)) for r in ROWS]
            return await super().rpc(method, **params)

    async def run() -> None:
        for width in (45, 120):
            app = Daemon().watch()
            async with app.run_test(size=(width, 35)) as pilot:
                await pilot.pause()
                await pilot.press("slash", *"old", "enter")
                assert app.filter == "old" and app.prompt_mode is None
                await pilot.press("escape")
                assert app.filter == ""
                app.action_log()
                await pilot.press("v")
                await pilot.pause()
                assert app.query_one("#files").display
                await pilot.press("s", *"draft from files", "escape")
                assert app.focused is app.query_one("#files")
                await pilot.press("escape")
                assert app.screen.has_class("detail-view")
                assert app.focused is app.query_one("#log")
                await pilot.press("escape")
                assert app.query_one("#left").display
                assert not app.screen.has_class("detail-view")

    asyncio.run(run())


def test_tab_and_shortcut_letters_stay_inside_the_editor() -> None:
    from agentctl.tui import Prompt

    async def run() -> None:
        fake = Fake()
        app = fake.watch()
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            await pilot.press("s", "tab", *"ajkdxsq", "shift+tab")
            prompt = app.query_one("#prompt", Prompt)
            assert prompt.has_focus and prompt.text == " " * prompt.indent_width + "ajkdxsq"
            assert not any(
                method in ("approve", "close", "jump", "send") for method, _ in fake.calls
            )

    asyncio.run(run())


def test_desktop_click_selects_a_preview_and_enter_opens_the_terminal() -> None:
    async def run() -> None:
        fake = Fake()
        app = fake.watch()
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            await pilot.click("#list", offset=(2, 5))  # the working session's title
            await pilot.pause()
            assert app.selected == "aaaa1111"
            assert not any(method == "jump" for method, _ in fake.calls)
            await pilot.press("enter")
            assert any(method == "jump" for method, _ in fake.calls)

    asyncio.run(run())


def test_typing_after_a_transcript_click_continues_the_same_draft() -> None:
    from agentctl.tui import Prompt

    async def run() -> None:
        fake = Fake()
        app = fake.watch()
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            await pilot.press("s", *"first")
            app.query_one("#log").focus()
            await pilot.press(*" next", "ctrl+s")
            await pilot.pause()
            assert ("send", {"id": "bbbb2222", "text": "first next"}) in fake.calls
            assert app.query_one("#prompt", Prompt).text == "first next"

    asyncio.run(run())


def test_rpc_timeout_keeps_delivery_uncertainty_explicit(monkeypatch: Any) -> None:
    import pytest

    import agentctl.tui as tui

    async def stuck(method: str, **params: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(tui, "_rpc", stuck)
    monkeypatch.setattr(tui, "RPC_TIMEOUT", 0.01)

    async def run() -> None:
        with pytest.raises(RuntimeError, match="check sent history before retrying"):
            await tui.rpc("send", id="s", text="message")

    asyncio.run(run())


def test_hide_only_quit_guard_undo_and_delivery_guidance() -> None:
    from agentctl.tui import Confirm

    fake = Fake()

    async def run() -> None:
        app = fake.watch()
        async with app.run_test(size=(45, 30)) as pilot:
            await pilot.pause()
            app.select("aaaa1111")
            app.rebuild()
            assert app.check_action("close", ()) is False
            assert app.check_action("hide", ()) is True
            await pilot.press("x")
            await pilot.pause()
            assert isinstance(app.screen, Confirm)
            await pilot.click("#yes")
            await pilot.pause()
            assert ("hide", {"id": "aaaa1111"}) in fake.calls
            assert not any(method == "close" for method, _ in fake.calls)
            await app.action_undo_remove()
            assert ("unhide", {"id": "aaaa1111"}) in fake.calls
            # An idle pane still has a separately guarded quit-and-hide action.
            app.rows = [row("idle", "idle")]
            app.select("idle")
            app.rebuild()
            await pilot.press("ctrl+x")
            await pilot.pause()
            assert isinstance(app.screen, Confirm)
            assert "z only unhides" in app.screen.question
            await pilot.click("#yes")
            await pilot.pause()
            calls = [method for method, p in fake.calls if p.get("id") == "idle"]
            assert calls.index("close") < calls.index("hide")
            app.update_queued({"queued": ["work"], "queue_error": "Needs human setup"})
            await pilot.pause()
            message = str(app.query_one("#queued Static", Static).render())
            assert "Needs human setup" in message and "Inspect" in message
            app.update_queued({"queued": ["work"], "queue_error": "Delivery uncertain"})
            await pilot.pause()
            assert "uncertain" in str(app.query_one("#queued Static", Static).render())

    asyncio.run(run())


def test_ended_only_empty_state_explains_recent_boundary_in_both_groups() -> None:
    fake = Fake()

    async def run() -> None:
        app = fake.watch()
        async with app.run_test(size=(45, 30)) as pilot:
            await pilot.pause()
            for grouped in (True, False):
                app.by_project = grouped
                app.rows = [row("recent-ended", "ended")]
                app.show_ended = False
                app.rebuild()
                empty = app.query_one("#empty", Static)
                assert empty.has_class("show")
                assert "24h" in str(empty.render()) and "older history" in str(empty.render())
                app.action_toggle_ended()
                assert not empty.has_class("show")
                assert app.query_one("#list", OptionList).get_option("recent-ended")

    asyncio.run(run())


def test_project_labels_add_the_parent_only_on_a_clash() -> None:
    from agentctl.tui.presentation import project_labels

    rows = [
        row("aaaa1111", "idle") | {"cwd": "/w/work/api", "project": "api"},
        row("bbbb2222", "idle") | {"cwd": "/w/home/api", "project": "api"},
        row("cccc3333", "idle") | {"cwd": "/w/work/web", "project": "web"},
    ]
    assert project_labels(rows) == {
        "aaaa1111": "api · work", "bbbb2222": "api · home", "cccc3333": "web"
    }  # fmt: skip


def test_long_idle_list_folds_and_e_shows_it() -> None:
    idle = [
        row(f"{i:04d}0000", "idle", title=f"task {i}")
        | {"last_activity": (datetime.now(UTC) - timedelta(minutes=i)).isoformat()}
        for i in range(9)
    ]

    class Many(Fake):
        async def rpc(self, method: str, **params: Any) -> Any:
            if method == "ls":
                return [row("aaaa1111", "working"), *idle]
            return await super().rpc(method, **params)

    async def run() -> None:
        app = Many().watch()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            ol = app.query_one("#list", OptionList)
            texts = [str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count)]
            assert any("IDLE (9)" in t for t in texts)  # the heading counts them all
            assert sum("task " in t for t in texts) == IDLE_SHOWN  # the latest few
            assert any("+5 more idle · e shows all" in t for t in texts)
            await pilot.press("e")
            await pilot.pause()
            texts = [str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count)]
            assert sum("task " in t for t in texts) == 9 and not any(
                "more idle" in t for t in texts
            )

    asyncio.run(run())
