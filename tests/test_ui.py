"""The command line's look and its interactive pieces, driven with fake keys."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from rich.console import Console
from rich.text import Text
from typer.testing import CliRunner

from agentctl import cli, ui, waiting


def row(sid: str, status: str = "idle", **kw: Any) -> dict[str, Any]:
    return {
        "id": sid, "source": "claude_code", "status": status, "title": kw.pop("title", None),
        "cwd": kw.pop("cwd", "/src/api"), "project": kw.pop("project", "api"),
        "last_activity": datetime.now(UTC).isoformat(), "controllable": True, "tmux_pane": "%1",
        "pending_approval": kw.pop("pending_approval", None), **kw,
    }  # fmt: skip


def keys(*names: str) -> Any:
    it: Iterator[str] = iter(names)
    return lambda: next(it)


def quiet() -> Console:
    return Console(file=open("/dev/null", "w"), force_terminal=True, width=100)  # noqa: SIM115


# --- picker ---


def test_fuzzy_matches_characters_in_order() -> None:
    assert ui.fuzzy("dmode", "Add dark mode to settings")
    assert ui.fuzzy("", "anything")
    assert not ui.fuzzy("zz", "dark mode")


def test_picker_filters_moves_and_picks() -> None:
    items = [("a", Text("api  fix login")), ("b", Text("web  dark mode")), ("c", Text("docs"))]
    assert ui.Picker("p", items, quiet(), keys("down", "enter")).run() == "b"
    assert ui.Picker("p", items, quiet(), keys("d", "o", "enter")).run() == "c"  # "do": docs
    assert ui.Picker("p", items, quiet(), keys("j", "j", "k", "enter")).run() == "b"
    assert ui.Picker("p", items, quiet(), keys("esc")).run() is None
    assert ui.Picker("p", items, quiet(), keys("z", "enter")).run() is None  # nothing matches
    assert ui.Picker("p", items, quiet(), keys("x", "backspace", "enter")).run() == "a"


def test_pick_session_and_option_return_ids_and_keys() -> None:
    rows = [row("aaaa1111", "working", title="fix login"), row("bbbb2222", title="dark mode")]
    assert ui.pick_session(rows, "read:", quiet(), keys("down", "enter")) == "bbbb2222"
    opts = {"1": "Yes", "2": "Yes, don't ask again", "3": "No, and tell Claude"}
    assert ui.pick_option(opts, quiet(), keys("down", "down", "enter")) == "3"


def test_suggest_finds_near_misses() -> None:
    rows = [row("aaaa1111", title="Add dark mode", project="web"), row("bbbb2222", title="Docs")]
    assert [r["id"] for r in ui.suggest("dakr", rows)] == ["aaaa1111"]  # a typo
    assert [r["id"] for r in ui.suggest("wbe", rows)] == ["aaaa1111"]
    assert ui.suggest("zzzzzz", rows) == []


# --- look ---


def test_styles_stay_on_their_own_part() -> None:
    ok = ui.success("Sent to aaaa1111", "2 lines")
    assert ok.style == "" and ok.plain == "✓ Sent to aaaa1111  2 lines"
    assert ui.failure("No session").style == ""
    line = ui.tree([Text("edited a.py")])[0]
    assert line.style == "" and line.plain == "  └ edited a.py"


def test_gauge_and_until() -> None:
    g = ui.gauge(50, 10)
    assert g.plain == "━" * 10 and g.spans[0].end == 5
    soon = (datetime.now(UTC) + timedelta(hours=2, minutes=5, seconds=30)).timestamp()
    assert ui.until(soon) == "2h 05m"


def test_session_line_puts_what_first_and_ids_last() -> None:
    r = row("aaaa1111", "awaiting_approval", title="Migrate orders", unread=2)
    line = ui.session_line(r, 80, None, None).plain
    assert line.startswith("▲ api  Migrate orders") and line.rstrip().endswith("aaaa1111")
    assert "2 new" in line and len(line) <= 80
    untitled = ui.session_line(row("bbbb2222"), 80, None, None).plain
    assert untitled.startswith("○ api  ") and "api  api" not in untitled


def test_details_explain_what_needs_a_look() -> None:
    r = row("aaaa1111", "awaiting_approval", pending_approval={"tool": "Bash: rm x"},
            queued=["then y"], conflicts=["bbbb also edited a.py"])  # fmt: skip
    text = "\n".join(t.plain for t in ui.session_details(r, stuck=12, full=91))
    for want in ("wants Bash: rm x", "no output for 12m", "91% of its context used: /compact",
                 "overlap: bbbb", "1 message queued"):  # fmt: skip
        assert want in text


# --- commands ---


def test_session_args_open_a_picker_only_on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [row("aaaa1111", "working"), row("bbbb2222", "awaiting_approval")]
    calls: list[tuple[str, dict[str, Any]]] = []

    def call(method: str, **p: Any) -> Any:
        calls.append((method, p))
        return rows if method == "ls" else {"session_id": p.get("id"), "chosen": "3. No"}

    monkeypatch.setattr(cli, "call", call)
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    r = CliRunner().invoke(cli.app, ["deny"])
    assert r.exit_code == 1 and "which session?" in r.output.lower()  # scripts must say
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    offered: list[list[str]] = []
    monkeypatch.setattr(
        cli,
        "choose_session",
        lambda prompt, rs: offered.append([x["id"] for x in rs]) or "bbbb2222",
    )
    r = CliRunner().invoke(cli.app, ["deny"])
    assert r.exit_code == 0 and offered == [["bbbb2222"]]  # only those waiting for approval
    assert ("approve", {"id": "bbbb2222", "allow": False}) in calls


def test_interactive_approve_shows_the_card_and_takes_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = row("bbbb2222", "awaiting_approval", title="Migrate",
                  pending_approval={"tool": "Bash: npm run migrate",
                                    "options": {"1": "Yes", "2": "No"}})  # fmt: skip
    calls: list[dict[str, Any]] = []

    def call(method: str, **p: Any) -> Any:
        if method == "ls":
            return [row("aaaa1111", "working"), pending]
        calls.append(p)
        return {"session_id": p["id"], "chosen": "2. No"}

    monkeypatch.setattr(cli, "call", call)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(ui, "pick_option", lambda opts, con, rk=None: "2")
    r = CliRunner().invoke(cli.app, ["approve"])
    assert r.exit_code == 0, r.output
    assert "Bash: npm run migrate" in r.output  # the card, before the choice
    assert calls == [{"id": "bbbb2222", "choice": 2, "allow": False}]  # the only one: no pick


def test_unknown_session_suggests_what_was_meant(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [row("aaaa1111", title="Add dark mode", project="web")]

    def call(method: str, **p: Any) -> Any:
        if method == "ls":
            return rows
        raise cli.DaemonError(f"no session matches {p['id']!r} (by id, name or folder)")

    monkeypatch.setattr(cli, "call", call)
    r = CliRunner().invoke(cli.app, ["tail", "dakr"])
    assert r.exit_code == 1
    assert "No session matches 'dakr'" in r.output and "did you mean" in r.output
    assert "aaaa1111" in r.output and "agentctl ls -a" in r.output
    r = CliRunner().invoke(cli.app, ["tail", "dakr", "--json"])
    assert r.output.strip() == '{"error": "no session matches \'dakr\' (by id, name or folder)"}'


def test_wait_reports_each_poll() -> None:
    seen: list[str] = []
    states = iter(["working", "working", "idle"])
    now = [0.0]

    def call(method: str, **p: Any) -> Any:
        if p.get("n"):
            return {"session": {"id": "a", "status": "idle"}, "events": []}
        return {"session": {"id": "a", "status": next(states, "idle")}, "events": []}

    def sleep(s: float) -> None:
        now[0] += s

    w = waiting.wait_turn(
        call, "a", 60, sleep, lambda: now[0], on_poll=lambda s: seen.append(s["status"])
    )
    assert w.outcome == "done" and seen == ["working", "working", "idle"]


def test_usage_draws_gauges(monkeypatch: pytest.MonkeyPatch) -> None:
    later = (datetime.now(UTC) + timedelta(hours=3)).timestamp()
    window = {"name": "5h", "used_percent": 62, "resets_at": later}
    u = {"claude": {"at": datetime.now(UTC).isoformat(), "windows": [window]}}
    monkeypatch.setattr(cli, "call", lambda m, **p: u)
    r = CliRunner().invoke(cli.app, ["usage"])
    assert r.exit_code == 0 and "━" in r.output and "62%" in r.output and "resets in 2h" in r.output
