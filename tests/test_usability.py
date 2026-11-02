"""Finding sessions by name, waiting for turns, and the CLI/dashboard surface."""

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from typer.testing import CliRunner

from agentctl import cli, tui, waiting
from agentctl.daemon.bus import Bus
from agentctl.daemon.registry import Registry
from agentctl.models import Session

NOW = datetime.now(UTC)


def reg_with(*rows: tuple[str, str, str | None, str]) -> Registry:
    """(id, cwd, title, status) rows."""
    reg = Registry(Bus())
    for i, (sid, cwd, title, status) in enumerate(rows):
        reg.upsert(
            Session(
                id=sid,
                source="claude_code",
                cwd=cwd,
                title=title,
                status=status,  # type: ignore[arg-type]
                last_activity=NOW - timedelta(minutes=i),
            )
        )
    return reg


def test_resolve_by_name_title_folder_and_word() -> None:
    reg = reg_with(
        ("aaaa1111", "/Users/me/api", "Fix the login bug", "working"),
        ("bbbb2222", "/Users/me/web", None, "idle"),
    )
    reg.names["bbbb2222"] = "Frontend"
    assert reg.resolve("frontend").id == "bbbb2222"  # your rename, any case
    assert reg.resolve("fix the login bug").id == "aaaa1111"  # the agent's title
    assert reg.resolve("aaaa").id == "aaaa1111"  # id prefixes still work
    assert reg.resolve("api").id == "aaaa1111"  # folder name
    assert reg.resolve("login").id == "aaaa1111"  # a word in the title
    with pytest.raises(KeyError, match="no session matches"):
        reg.resolve("zzz")


def test_resolve_prefers_the_live_session_by_name_but_not_by_id() -> None:
    reg = reg_with(
        ("aaaa1111", "/Users/me/api", None, "working"),
        ("aaaa2222", "/Users/me/api", None, "ended"),
    )
    assert reg.resolve("api").id == "aaaa1111"  # the old ended one doesn't make it ambiguous
    with pytest.raises(KeyError, match="ambiguous 'aaaa'.*aaaa1111"):
        reg.resolve("aaaa")  # an id prefix must be unique: never guess where a send goes


def test_resolve_ambiguous_name_lists_the_choices() -> None:
    reg = reg_with(
        ("aaaa1111", "/Users/me/api", None, "working"),
        ("bbbb2222", "/Users/me/api", None, "idle"),
    )
    with pytest.raises(KeyError, match=r"ambiguous 'api': aaaa1111 \(api\), bbbb2222 \(api\)"):
        reg.resolve("api")


def test_exact_name_beats_id_prefix() -> None:
    reg = reg_with(("cafe1111", "/x", None, "idle"), ("dddd2222", "/y", None, "idle"))
    reg.names["dddd2222"] = "cafe"
    assert reg.resolve("cafe").id == "dddd2222"
    assert reg.resolve("cafe1").id == "cafe1111"


def test_at_is_the_session_waiting_on_you() -> None:
    reg = reg_with(
        ("aaaa1111", "/a", None, "working"),
        ("bbbb2222", "/b", None, "awaiting_input"),
        ("cccc3333", "/c", None, "awaiting_approval"),
    )
    assert reg.resolve("@").id == "cccc3333"  # approvals first, as in `ls`
    reg.sessions["cccc3333"].status = "idle"
    assert reg.resolve("@").id == "bbbb2222"
    reg.sessions["bbbb2222"].status = "idle"
    with pytest.raises(KeyError, match="no session is waiting"):
        reg.resolve("@")


# --- waiting ---


def fake_call(statuses: list[str], reply: str = "done!", **extra: Any) -> Any:
    it = iter(statuses)
    last = {"s": statuses[0]}

    def call(method: str, **p: Any) -> Any:
        if p.get("n"):
            ev = {"kind": "message", "ts": NOW.isoformat(),
                  "payload": {"role": "assistant", "text": reply}}  # fmt: skip
            return {"session": {"id": "aaaa1111", "status": last["s"]}, "events": [ev]}
        last["s"] = next(it, last["s"])
        row = {"id": "aaaa1111", "status": last["s"], "controllable": True} | extra
        return {"session": row, "events": []}

    return call


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def test_wait_turn_outcomes() -> None:
    c = Clock()
    w = waiting.wait_turn(fake_call(["working", "working", "idle"]), "a", 60, c.sleep, c)
    assert (w.outcome, w.reply) == ("done", "done!")
    c = Clock()
    w = waiting.wait_turn(fake_call(["working", "awaiting_approval"]), "a", 60, c.sleep, c)
    assert w.outcome == "approval"
    c = Clock()
    assert waiting.wait_turn(fake_call(["working"]), "a", 10, c.sleep, c).outcome == "timeout"
    c = Clock()
    assert waiting.wait_turn(fake_call(["idle"]), "a", 60, c.sleep, c).outcome == "idle"


def test_wait_command_exit_codes_and_reply_on_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(waiting.time, "sleep", lambda s: None)
    # the first status is used up by resolving "api" to its id
    monkeypatch.setattr(
        cli, "call", fake_call(["working", "working", "idle"], reply="# Done\nall good")
    )
    r = CliRunner().invoke(cli.app, ["wait", "api"])
    assert r.exit_code == 0 and "all good" in r.stdout
    monkeypatch.setattr(cli, "call", fake_call(["working", "awaiting_approval"]))
    r = CliRunner().invoke(cli.app, ["wait", "api", "--json"])
    assert r.exit_code == waiting.EXIT["approval"] and '"outcome": "approval"' in r.stdout


def test_stuck_minutes() -> None:
    now = datetime.now(UTC)
    old = (now - timedelta(minutes=14, seconds=20)).isoformat()
    assert waiting.stuck_minutes({"status": "working", "last_activity": old}) == 14
    assert waiting.stuck_minutes({"status": "idle", "last_activity": old}) is None
    fresh = now.isoformat()
    assert waiting.stuck_minutes({"status": "working", "last_activity": fresh}) is None


# --- interface surface ---


def test_every_top_level_command_has_a_help_panel() -> None:
    panels = {name for names in cli.HELP_PANELS.values() for name in names}
    commands = {c.name or c.callback.__name__ for c in cli.app.registered_commands if c.callback}
    groups = {g.name for g in cli.app.registered_groups}
    assert commands | groups <= panels  # nothing lands in "Other"


def test_command_summaries_are_one_line() -> None:
    """Typer keeps line breaks in the first paragraph, so they'd break `--help` mid-sentence."""
    for c in cli.app.registered_commands:
        doc = (c.callback.__doc__ or "") if c.callback else ""
        assert "\n" not in doc.split("\n\n")[0], c.callback


def test_dashboard_keys_are_bound_once() -> None:
    keys = Counter(
        k for b in tui.Watch.BINDINGS if not isinstance(b, tuple) for k in b.key.split(",")
    )
    assert [k for k, n in keys.items() if n > 1] == []


# --- picking, completion, summary, ls ---


def row(sid: str, status: str = "idle", **kw: Any) -> dict[str, Any]:
    return {
        "id": sid,
        "source": "claude_code",
        "status": status,
        "title": kw.pop("title", None),
        "cwd": kw.pop("cwd", "/Users/me/api"),
        "last_activity": kw.pop("last_activity", datetime.now(UTC).isoformat()),
        "controllable": True,
        "tmux_pane": "%1",
        "pending_approval": kw.pop("pending_approval", None),
        **kw,
    }


def test_ambiguous_name_offers_a_pick_on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    choices = [{"id": "aaaa1111", "label": "api", "status": "idle", "cwd": "/a"},
               {"id": "bbbb2222", "label": "api", "status": "working", "cwd": "/b"}]  # fmt: skip
    sent: list[dict[str, Any]] = []

    def call(method: str, **p: Any) -> Any:
        if p["id"] == "api":
            raise cli.DaemonError("ambiguous 'api'", choices)
        sent.append(p)
        return {"session_id": p["id"]}

    monkeypatch.setattr(cli, "call", call)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    offered: list[list[str]] = []

    def choose(prompt: str, rows: list[dict[str, Any]]) -> str:
        offered.append([r["id"] for r in rows])
        return "bbbb2222"

    monkeypatch.setattr(cli, "choose_session", choose)
    cli.rpc("send", False, id="api", text="hi")
    assert offered == [["aaaa1111", "bbbb2222"]]
    assert sent == [{"id": "bbbb2222", "text": "hi"}]


def test_ambiguous_name_is_an_error_for_scripts(monkeypatch: pytest.MonkeyPatch) -> None:
    def call(method: str, **p: Any) -> Any:
        raise cli.DaemonError("ambiguous 'api'", [{"id": "a", "label": "", "status": "idle"}])

    monkeypatch.setattr(cli, "call", call)
    monkeypatch.setattr(cli, "_interactive", lambda: False)
    r = CliRunner().invoke(cli.app, ["send", "api", "hi"])
    assert r.exit_code == 1 and "ambiguous" in r.output.lower()


def test_completion_offers_ids_names_and_at(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [row("aaaa1111", "awaiting_input", title="frontend"), row("bbbb2222", title="api")]
    monkeypatch.setattr(cli, "call", lambda m, **p: rows)
    assert [c for c, _ in cli.complete_session("")][:3] == ["@", "aaaa1111", "bbbb2222"]
    assert [c for c, _ in cli.complete_session("fr")] == ["frontend"]

    def down(m: str, **p: Any) -> Any:
        raise cli.DaemonError("agentctld is not running")

    monkeypatch.setattr(cli, "call", down)
    assert cli.complete_session("a") == []  # quiet, and never starts the daemon


def test_bare_agentctl_summarises_what_needs_you(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        row("aaaa1111", "awaiting_approval", title="api", pending_approval={"tool": "Bash: ls"}),
        row("bbbb2222", "working", title="web"),
    ]
    monkeypatch.setattr(cli, "call", lambda m, **p: rows)
    r = CliRunner().invoke(cli.app, [])
    assert r.exit_code == 0
    assert "1 need you" in r.output and "1 working" in r.output
    assert "agentctl approve @" in r.output and "wants Bash: ls" in r.output


def test_ls_here_keeps_sessions_inside_the_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [row("aaaa1111", cwd="/r/app"), row("bbbb2222", cwd="/r/app/pkg"),
            row("cccc3333", cwd="/r/application")]  # fmt: skip
    monkeypatch.setattr(cli, "call", lambda m, **p: rows)
    monkeypatch.setattr(cli, "_here_root", lambda: "/r/app")
    r = CliRunner().invoke(cli.app, ["ls", "--here", "--json"])
    assert [x["id"] for x in __import__("json").loads(r.stdout)] == ["aaaa1111", "bbbb2222"]


def test_attention_notes_for_stuck_and_full_context() -> None:
    old = (datetime.now(UTC) - timedelta(minutes=15)).isoformat()
    rows = [
        row("aaaa1111", "working", last_activity=old),
        row("bbbb2222", "idle", context_tokens=170_000, context_window=200_000),
        row("cccc3333", "idle", context_tokens=10_000, context_window=200_000),
    ]
    text = "\n".join(n.plain for n in cli.attention_notes(rows))
    assert "aaaa1111 working but no output for 15m" in text
    assert "bbbb2222 has used 85% of its context" in text and "/compact" in text
    assert "cccc3333" not in text


def test_compact_leads_the_quick_replies_when_context_is_full() -> None:
    full = row("aaaa1111", context_tokens=190_000, context_window=200_000)
    assert tui.quick_replies(full)[0] == "/compact"
    assert "/compact" not in tui.quick_replies(row("aaaa1111"))
    assert "/compact" not in tui.quick_replies(full | {"source": "codex"})


def _norm_key(k: str) -> str:
    k = k.strip().strip("`").lower().replace("ctrl-", "ctrl+").replace("–", "-")
    return {"question_mark": "?", "esc": "escape"}.get(k, k)


def test_every_dashboard_key_is_documented_in_help_and_readme() -> None:
    """The `?` screen and the README's key table list every key the dashboard binds, so they
    can't drift from the code (as `g` and `n` once did)."""
    import re
    from pathlib import Path

    from agentctl.tui.screens import HELP

    navigation = {"j", "k", "escape", "ctrl+enter", "ctrl+s"}  # moving, going back, submit
    bound = {
        "1-4" if k in {"1", "2", "3", "4"} else _norm_key(k)
        for b in tui.Watch.BINDINGS
        if not isinstance(b, tuple)
        for k in b.key.split(",")
        if not (len(k) == 1 and k.isupper())  # nocase's shifted copies
    } - navigation
    in_help = {_norm_key(part) for _, rows in HELP for key, _ in rows
               for part in re.split(r"\s{2,}", key)}  # fmt: skip
    readme = (Path(__file__).parent.parent / "README.md").read_text()
    table = readme[readme.index("| key | does |") :]
    table = table[: table.index("\n\n")].replace("`1`–`4`", "`1-4`")
    in_readme = {
        _norm_key(a or b)
        for line in table.splitlines()[2:]
        for a, b in re.findall(r"`([^`]+)`|(Enter|Space)", line.split("|")[1])
    }
    assert sorted(bound - in_help) == []
    assert sorted(bound - in_readme) == []
