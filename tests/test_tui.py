import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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

            assert await remove("bbbb2222") == ""  # mid-approval: quitting would abort it
            assert "Remove" in await remove("dddd4444")  # outside agentctl: just hidden
            assert ("hide", {"id": "dddd4444"}) in fake.calls
            assert ("close", {"id": "dddd4444"}) not in fake.calls
            await pilot.press("z")  # undo
            assert ("unhide", {"id": "dddd4444"}) in fake.calls and app.removed is None
            assert "Quit" in await remove("eeee5555")  # in agentctl: quit, then hidden
            calls = [c for c in fake.calls if c[1].get("id") == "eeee5555"]
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
            assert str(tree.path) == str(tmp_path) and tree.display
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
                "queued: and add tests  ✕",
                "queued: then commit  ✕",
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
    assert " 25% 42m t" in row_text(r).plain
    assert "idle" in row_text(r, status=True).plain and "idle" not in row_text(r).plain
    r["cache_expires_at"] = (now - timedelta(minutes=1)).isoformat()
    bare = {**r, "context_tokens": None}
    assert row_text(r).plain.index(" t") == row_text(bare).plain.index(" t")  # titles line up
