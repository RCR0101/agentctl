"""Thin client: every command is one JSON-RPC call to agentctld over the Unix socket."""

import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agentctl import HOME, LOG_DIR, SOCKET_PATH, hooks, launcher, ui
from agentctl.ids import short_id
from agentctl.setup import LABEL, PLIST
from agentctl.teams_cli import app as teams_app
from agentctl.waiting import EXIT, context_full, stuck_minutes, wait_turn

app = typer.Typer(add_completion=True, rich_markup_mode="rich")
daemon_app = typer.Typer(no_args_is_help=True, help="Manage the agentctld background daemon.")
hooks_app = typer.Typer(no_args_is_help=True, help="Manage Claude Code hooks in settings.json.")
app.add_typer(daemon_app, name="daemon")
app.add_typer(hooks_app, name="hooks")
app.add_typer(teams_app, name="teams")
rules_app = typer.Typer(
    invoke_without_command=True,
    help="Approval rules: answer matching permission prompts for you.",
)
app.add_typer(rules_app, name="rules")
out = Console(highlight=False)
err = Console(stderr=True, highlight=False)


def complete_session(incomplete: str) -> list[tuple[str, str]]:
    """Shell completion: session ids (with their names as the description) and names. Never
    starts the daemon, and stays quiet if it's down."""
    try:
        rows = call("ls", autostart=False)
    except (DaemonError, OSError, ValueError):
        return []
    want = incomplete.casefold()
    out: list[tuple[str, str]] = []
    if "@".startswith(incomplete) and any(
        r["status"] in ("awaiting_approval", "awaiting_input") for r in rows
    ):
        out.append(("@", "the session waiting on you"))
    for r in rows:
        name = r["title"] or short_cwd(r["cwd"])
        sid = short_id(r["id"])
        if sid.startswith(incomplete):
            out.append((sid, f"{STATUS_SHORT[r['status']]} · {name}"))
        elif want and name.casefold().startswith(want) and " " not in name:
            out.append((name, f"{STATUS_SHORT[r['status']]} · {sid}"))
    return out


JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]
IdArg = Annotated[
    str,
    typer.Argument(
        help="Session id prefix, name, title or folder; @ for the one waiting on you.",
        autocompletion=complete_session,
    ),
]
OptIdArg = Annotated[
    str | None,
    typer.Argument(
        help="Session id prefix, name or folder; @ for the one waiting on you; none: pick one.",
        autocompletion=complete_session,
        show_default=False,
    ),
]


SendIdArg = Annotated[
    str,
    typer.Argument(
        help="Session id prefix, name or folder (@: the one waiting on you), or a %N pane id.",
        autocompletion=complete_session,
    ),
]

STATUS_STYLE = {
    "awaiting_approval": "bold black on yellow",
    "awaiting_input": "bold cyan",
    "working": "green",
    "idle": "dim",
    "ended": "dim",
}
SRC = {"claude_code": "cc", "codex": "cx", "opencode": "oc"}
AGENT = {"claude_code": "Claude Code", "codex": "Codex", "opencode": "opencode"}
ORIGIN = {"desktop": ("app", "desktop app"), "sdk": ("sdk", "SDK")}


def agent_label(r: dict[str, Any]) -> str:
    """Short agent label: "cc", or "cc app" for the desktop app, "cc sdk" for SDK runs."""
    tag = ORIGIN.get(r.get("origin") or "")
    return f"{SRC[r['source']]} {tag[0]}" if tag else SRC[r["source"]]


def agent_name(r: dict[str, Any]) -> str:
    tag = ORIGIN.get(r.get("origin") or "")
    return f"{AGENT[r['source']]} {tag[1]}" if tag else AGENT[r["source"]]


SOURCES = {"claude": "claude_code", "codex": "codex", "opencode": "opencode"}
STATUS_SHORT = {
    "awaiting_approval": "APPROVE",
    "awaiting_input": "input",
    "working": "working",
    "idle": "idle",
    "ended": "ended",
}


class DaemonError(Exception):
    def __init__(self, message: str, choices: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.choices = choices or []  # set when the id matched several sessions


# --- daemon client ---


def _connect(autostart: bool) -> socket.socket:
    deadline = time.monotonic() + 15
    started = False
    while True:
        s = socket.socket(socket.AF_UNIX)
        try:
            s.connect(str(SOCKET_PATH))
            return s
        except OSError:  # no socket, nobody listening, path too long, ...
            s.close()
            if not autostart:
                raise DaemonError("agentctld is not running") from None
            if not started:
                _spawn_daemon()
                started = True
            if time.monotonic() > deadline:
                raise DaemonError("agentctld did not start; see ~/.agentctl/logs/") from None
            time.sleep(0.1)


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _spawn_daemon() -> None:
    if PLIST.exists():  # start in the GUI login session even when asked from SSH/mosh
        domain = f"gui/{os.getuid()}"
        if _launchctl("kickstart", f"{domain}/{LABEL}").returncode != 0:
            _launchctl("bootstrap", domain, str(PLIST))
        return
    subprocess.Popen(
        [sys.executable, "-m", "agentctl.daemon"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _check(reply: dict[str, Any]) -> Any:
    if "error" in reply:
        raise DaemonError(reply["error"]["message"], reply["error"].get("choices"))
    return reply.get("result")


def call(method: str, autostart: bool = True, **params: Any) -> Any:
    if os.environ.get("AGENTCTL_CALLER") == "agent":
        if method in ("send", "queue"):
            method, params = "agent_message", {"id": params["id"], "text": params["text"]}
        params = params | {"_actor": "agent"}
    with _connect(autostart) as s:
        s.sendall(json.dumps({"id": 1, "method": method, "params": params}).encode() + b"\n")
        line = s.makefile("rb").readline()
    if not line:
        raise DaemonError("daemon closed the connection")
    return _check(json.loads(line))


def stream(method: str, **params: Any) -> Iterator[dict[str, Any]]:
    """First yield is the call's result, then each notification's params."""
    with _connect(True) as s:
        s.sendall(json.dumps({"id": 1, "method": method, "params": params}).encode() + b"\n")
        f = s.makefile("rb")
        yield _check(json.loads(f.readline()))
        for line in f:
            yield json.loads(line)["params"]


def rpc(method: str, as_json: bool, **params: Any) -> Any:
    try:
        return call(method, **params)
    except DaemonError as e:
        if e.choices and "id" in params and not as_json and _interactive():
            picked = choose_session(f"{params['id']!r} matches several:", e.choices)
            if picked is None:
                raise typer.Exit(1) from None
            return rpc(method, as_json, **(params | {"id": picked}))
        if str(e).startswith("no session matches") and "id" in params and not as_json:
            fail(str(e), as_json, did_you_mean(str(params["id"])))
        fail(str(e), as_json)


def did_you_mean(ref: str) -> list[Text]:
    """After a miss: the sessions that look like what was typed, and how to see them all."""
    try:
        rows = call("ls", autostart=False, all=True)
    except (DaemonError, OSError):
        return []
    lines = [
        Text().append("did you mean ", style=ui.DIM) + ui.glyph(r["status"])
        + Text().append(f" {ui.project(r)}", style=ui.ACCENT) + Text(f"  {ui.title(r)}  ")
        + Text(short_id(r["id"]), style=ui.DIM)
        for r in ui.suggest(ref, rows)
    ]  # fmt: skip
    return [*lines, ui.hint("agentctl ls -a", "agentctl  (pick from a list)")]


def resolve(ref: str, as_json: bool) -> str:
    """The full id for what was typed (asking which one, on a terminal, if several match)."""
    if ref.startswith("%"):
        return ref
    return str(rpc("tail", as_json, id=ref, n=0)["session"]["id"])


def _interactive() -> bool:
    return ui.interactive()


def choose_session(prompt: str, rows: list[dict[str, Any]]) -> str | None:
    """The inline picker over these sessions (the daemon's ambiguity choices carry a label,
    status and folder; `ls` rows carry everything)."""
    full = [
        r if "source" in r else {
            "id": r["id"], "status": r["status"], "title": r.get("label"), "cwd": r.get("cwd", ""),
            "source": "claude_code", "last_activity": datetime.now(UTC).isoformat(),
        }
        for r in rows
    ]  # fmt: skip
    return ui.pick_session(full, prompt, err)


def pick_or_fail(
    id: str | None, as_json: bool, prompt: str, want: Callable[[dict[str, Any]], bool]
) -> str:
    """The session named, or on a terminal a pick from those `want` keeps (the most urgent
    first). Scripts must name one."""
    if id is not None:
        return id
    if as_json or not _interactive():
        fail("which session? give an id, name or folder, or @ for the one waiting on you", as_json)
    rows = [r for r in rpc("ls", as_json) if want(r)]
    if not rows:
        fail("no session to choose from here", as_json, [ui.hint("agentctl ls -a")])
    picked = choose_session(prompt, rows)
    if picked is None:
        raise typer.Exit(1)
    return picked


def fail(msg: str, as_json: bool, hints: list[Text] | None = None) -> NoReturn:
    if as_json:
        print(json.dumps({"error": msg}))
    else:
        err.print(ui.failure(msg[:1].upper() + msg[1:]))
        for h in hints or []:
            err.print(Text("  ") + h)
    raise typer.Exit(1)


def ok(msg: str, as_json: bool, result: Any, detail: str = "") -> None:
    if as_json:
        print(json.dumps(result))
    else:
        out.print(ui.success(msg[:1].upper() + msg[1:], detail))


# --- formatting ---


def age(iso: str) -> str:
    secs = int((datetime.now(UTC) - datetime.fromisoformat(iso)).total_seconds())
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{secs // n}{unit}"
    return f"{max(secs, 0)}s"


def short_cwd(cwd: str) -> str:
    return "~" if cwd == str(HOME) else Path(cwd).name or cwd


def project_display(s: dict[str, Any]) -> str:
    """A readable project label that distinguishes repositories with the same folder name."""
    project = str(s.get("project") or short_cwd(s["cwd"]))
    parent = Path(str(s.get("project_path") or s["cwd"])).parent.name
    return f"{project} · {parent}" if parent and parent != project else project


def tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    return f"{round(n / 1000)}k" if n >= 1000 else str(n)


def ctx_text(s: dict[str, Any], short: bool = False) -> Text:
    used, window = s.get("context_tokens"), s.get("context_window")
    if used is None:
        return Text("")
    if not window:
        return Text(tokens(used))
    pct = used * 100 // window
    style = "red" if pct >= 80 else "yellow" if pct >= 60 else ""
    approx = "" if s.get("context_window_exact") else "~"
    if short:
        return Text(f"{approx}{pct}%", style=style)
    return Text(f"{tokens(used)} / {approx}{tokens(window)} ({pct}%)", style=style)


def cache_text(s: dict[str, Any], short: bool = False) -> Text:
    """Time left before the prompt cache expires (Claude only; Codex doesn't expose it).
    short: whole minutes, blank once expired, for the dense watch list."""
    exp = s.get("cache_expires_at")
    if not exp:
        return Text("")
    left = int((datetime.fromisoformat(exp) - datetime.now(UTC)).total_seconds())
    if left <= 0:
        return Text("" if short else "expired", style="dim")
    m, sec = divmod(left, 60)
    text = f"{m}m" if m >= 5 or short else f"{m}m{sec:02d}s"
    return Text(text, style="yellow" if m < 5 else "green")


def status_text(status: str) -> Text:
    return Text(STATUS_SHORT[status], style=STATUS_STYLE[status])


def approval_panel(
    sid: str, appr: dict[str, Any], controllable: bool, hint: str | None = None
) -> Panel:
    body = Text(appr.get("tool") or "(tool unknown)", style="bold")
    for k, v in (appr.get("options") or {}).items():
        style = "red" if v.lower().startswith(("no", "reject")) else "green" if k == "1" else ""
        body.append(f"\n {k}. ", style="bold").append(v, style=style)
    short = short_id(sid)
    if not controllable:
        hint = f"read-only: agentctl adopt {short} to control it"
    elif hint is None:
        hint = f"agentctl approve {short} [--choice N]\nagentctl deny {short}"
    return Panel(
        Group(body, Text(hint, style="dim")),
        title=Text(f" {short} needs approval ", style="bold yellow"),
        title_align="left",
        border_style="yellow",
        expand=False,
    )


def session_notes(s: dict[str, Any]) -> list[Text]:
    """Lines about the session beyond its status: overlapping agents, queued follow-ups."""
    notes = [Text(f"! {c}", style="yellow") for c in s.get("conflicts") or []]
    if s.get("status_reason"):
        notes.append(
            Text(
                f"Lifecycle: {s['status_reason']} · observed {s.get('status_observed_at')}",
                style="dim",
            )
        )
    if not s.get("controllable") and s.get("control_reason"):
        notes.append(Text(s["control_reason"], style="dim"))
    if s.get("queued"):
        n = len(s["queued"])
        t = Text(f"{n} follow-up{'s' if n > 1 else ''} queued for when it's free", style="cyan")
        if s.get("queue_error"):
            t.append(f" (can't send: {s['queue_error']})", style="red")
        notes.append(t)
    return notes


def compact_header(s: dict[str, Any]) -> Group:
    """Phone-width header: title, then status · agent · context · folder on one line."""
    title = Text(s["title"] or short_cwd(s["cwd"]), style="bold", no_wrap=True, overflow="ellipsis")
    meta = Text(no_wrap=True, overflow="ellipsis")
    meta.append_text(status_text(s["status"]))
    meta.append(f" {agent_label(s)} {short_id(s['id'])} ", style="dim")
    if s.get("context_tokens") is not None:
        meta.append_text(ctx_text(s, short=True)).append(" ")
    meta.append(project_display(s), style="bold cyan")
    return Group(title, meta, *session_notes(s))


def session_header(s: dict[str, Any], compact: bool = False) -> Panel | Group:
    if compact:
        return compact_header(s)
    meta = Text()
    meta.append_text(status_text(s["status"])).append("  ")
    meta.append(f"{agent_name(s)} · ", style="dim")
    if s["tmux_pane"]:
        meta.append("in agentctl", style="dim")
    elif s["controllable"]:
        meta.append("via codex app-server", style="dim")
    else:
        meta.append("outside agentctl", style="dim")
    cwd = s["cwd"].replace(str(HOME), "~", 1)
    path = Text(cwd, style="dim", no_wrap=True, overflow="ellipsis")
    usage = Text()
    if s.get("context_tokens") is not None:
        usage.append("context ", style="dim").append_text(ctx_text(s))
        if s["source"] == "claude_code":
            usage.append(" · cache ", style="dim")
            usage.append_text(cache_text(s) or Text("unknown", style="dim"))
            if cache_text(s).plain not in ("", "expired"):
                usage.append(" left", style="dim")
        else:
            usage.append(" · cache expiry not exposed", style="dim")
    return Panel(
        Group(
            meta,
            *([usage] if usage.plain else []),
            Text(f"project · {project_display(s)}", style="bold cyan"),
            path,
            *session_notes(s),
        ),
        title=Text(f" {s['title'] or short_id(s['id'])} ", style="bold"),
        title_align="left",
        subtitle=Text(f" {s['id']} ", style="dim"),
        subtitle_align="right",
        border_style="blue",
    )


def render(
    ev: dict[str, Any],
    live: bool = False,
    hint: str | None = None,
    narrow: bool = False,
    markdown: bool = True,
) -> Text | Panel | Group:
    fmt = "%H:%M" if narrow else "%H:%M:%S"
    ts = datetime.fromisoformat(ev["ts"]).astimezone().strftime(fmt)
    p, kind = ev["payload"], ev["kind"]
    if kind == "approval_request" and live:
        return approval_panel(ev["session_id"], p, True, hint)
    line = Text(f"{ts} ", style="dim")
    if p.get("agent_id"):
        line.append("[sub] ", style="magenta")
    if kind == "message" and p["role"] == "user":
        line.append("› ", style="bold cyan").append(p["text"], style="bold")
    elif kind == "message" and markdown:  # agents reply in Markdown: render it
        from rich.markdown import Markdown  # ~50 ms to import: only when there's a reply

        return Group(line.append("◆", style="bold green"), Markdown(p["text"]))
    elif kind == "message":
        line.append("◆ ", style="bold green").append(p["text"])
    elif kind == "tool_call":
        first, *rest = p["summary"].splitlines() or [""]
        more = " …" if rest else ""
        line.append(f"▸ {p['name']} ", style="yellow").append(first[:160] + more, style="dim")
    elif kind == "tool_result":
        lines = p["text"].splitlines() or [""]
        style = "red" if p.get("is_error") else "dim"
        more = f"  (+{len(lines) - 1} lines)" if len(lines) > 1 else ""
        line.append("↳ ", style=style).append(lines[0][:200] + more, style=style)
    elif kind == "status_change":
        line.append("● ", style=STATUS_STYLE[p["to"]]).append_text(status_text(p["to"]))
    elif kind == "approval_request":
        line.append(f"approval asked: {p.get('tool') or '?'}", style="yellow")
    elif kind == "error":
        line.append(f"error: {p.get('text', '')}", style="red")
    elif kind == "notice":
        line.append(f"note: {p.get('text', '')}", style="cyan")
    else:
        line.append(f"{kind} {json.dumps(p)[:200]}", style="dim")
    return line


# --- sessions ---


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Watch and drive your Claude Code, Codex and opencode sessions.

    Run with no command for a short summary of what needs you."""
    if ctx.invoked_subcommand is None:
        out.print(summary_view(rpc("ls", False), out.width))


def headline(rows: list[dict[str, Any]]) -> Text:
    """`2 need you · 2 working · 2 unread · 7 sessions`: the counts that matter, coloured only
    when they're not zero."""
    needs = sum(r["status"] in ("awaiting_approval", "awaiting_input") for r in rows)
    working = sum(r["status"] == "working" for r in rows)
    unread = sum(1 for r in rows if r.get("unread"))
    t = Text()
    for i, (n, word, style) in enumerate(
        ((needs, "need you", "bold yellow"), (working, "working", "green"),
         (unread, "unread", "cyan"), (len(rows), "sessions", ""))
    ):  # fmt: skip
        if i:
            t.append("  ·  ", style=ui.DIM)
        t.append(f"{n} {word}", style=style if n else ui.DIM)
    return t


def summary_view(rows: list[dict[str, Any]], width: int = 100) -> Group:
    """What `agentctl` alone prints: the counts, what needs you (with how to answer it), what
    looks wrong, and what to type next."""
    if not rows:
        return Group(
            Text("No sessions yet.", style="bold"),
            ui.hint("agentctl new claude .", "or run claude, codex or opencode as usual"),
        )
    parts: list[Any] = [headline(rows), Text("")]
    needs = [r for r in rows if r["status"] in ("awaiting_approval", "awaiting_input")]
    for r in needs[:5]:
        parts.append(ui.session_line(r, width, stuck_minutes(r), context_full(r)))
        parts += ui.tree(ui.session_details(r, stuck_minutes(r), context_full(r)))
    if len(needs) > 5:
        parts.append(ui.note(f"and {len(needs) - 5} more · agentctl ls -m"))
    trouble = [r for r in rows if r not in needs and (stuck_minutes(r) or context_full(r))]
    for r in trouble[:5]:
        parts.append(ui.session_line(r, width, stuck_minutes(r), context_full(r)))
        parts += ui.tree(ui.session_details(r, stuck_minutes(r), context_full(r)))
    if not needs and not trouble:
        parts.append(ui.note("Nothing needs you right now.", style="green"))
    parts.append(Text(""))
    first = needs[0] if needs else None
    if first and first["status"] == "awaiting_approval":
        parts.append(ui.hint("agentctl approve @", "agentctl approve  (choose)", "agentctl watch"))
    elif first:
        parts.append(ui.hint(f"agentctl tail {short_id(first['id'])}", "agentctl watch"))
    else:
        parts.append(ui.hint("agentctl watch", "agentctl ls", "agentctl --help"))
    return Group(*parts)


@app.command()
def ls(
    source: Annotated[str | None, typer.Option(help="claude, codex or opencode")] = None,
    status: Annotated[str | None, typer.Option(help="e.g. awaiting_approval")] = None,
    all_: Annotated[bool, typer.Option("--all", "-a", help="Include old ended sessions.")] = False,
    needs_me: Annotated[
        bool, typer.Option("--needs-me", "-m", help="Only those waiting for approval or input.")
    ] = False,
    here: Annotated[
        bool, typer.Option("--here", help="Only sessions in this folder's git repo (or folder).")
    ] = False,
    watch_: Annotated[
        bool, typer.Option("--watch", "-w", help="Keep the list up to date until Ctrl-C.")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """List sessions, those needing attention first."""
    src = SOURCES.get(source or "", source)
    root = _here_root() if here else None

    def rows() -> list[dict[str, Any]]:
        got: list[dict[str, Any]] = rpc("ls", as_json, source=src, status=status, all=all_)
        if needs_me:
            got = [r for r in got if r["status"] in ("awaiting_approval", "awaiting_input")]
        if root:
            got = [r for r in got if _within(r["cwd"], root)]
        return got

    if as_json:
        print(json.dumps(rows(), indent=2))
        return
    if not watch_:
        out.print(ls_view(rows(), out.width))
        return
    from rich.live import Live

    try:
        with Live(ls_view(rows(), out.width), console=out, screen=True, auto_refresh=False) as live:
            while True:
                time.sleep(2)
                stamp = Text(f"\nupdated {datetime.now():%H:%M:%S} · Ctrl-C to stop", style="dim")
                live.update(Group(ls_view(rows(), out.width), stamp), refresh=True)
    except KeyboardInterrupt:
        pass


def _here_root() -> str:
    cwd = os.getcwd()
    r = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else cwd


def _within(cwd: str, root: str) -> bool:
    return bool(cwd) and (cwd == root or cwd.startswith(root.rstrip("/") + "/"))


LS_GROUPS = (
    ("Needs you", ("awaiting_approval", "awaiting_input")),
    ("Working", ("working",)),
    ("Idle", ("idle",)),
    ("Ended", ("ended",)),
)


def ls_view(rows: list[dict[str, Any]], width: int) -> Group:
    """Sessions grouped by what they need from you, one line each, with a tree of details
    (what it wants, what's wrong) only where there's something to say."""
    if not rows:
        return Group(Text("No sessions.", style="bold"), ui.hint("agentctl ls -a", "agentctl new"))
    parts: list[Any] = [headline(rows)]
    for label, statuses in LS_GROUPS:
        group = [r for r in rows if r["status"] in statuses]
        if not group:
            continue
        parts += [Text(""), ui.section(label, len(group), style="bold")]
        for r in group:
            stuck, full = stuck_minutes(r), context_full(r)
            parts.append(ui.session_line(r, width, stuck, full))
            parts += ui.tree(ui.session_details(r, stuck, full))
    parts.append(Text(""))
    if any(r["status"] == "awaiting_approval" for r in rows):
        parts.append(ui.hint("agentctl approve", "agentctl tail <name>", "agentctl watch"))
    else:
        parts.append(ui.hint("agentctl tail <name>", 'agentctl send <name> "…"', "agentctl watch"))
    return Group(*parts)


def attention_notes(rows: list[dict[str, Any]]) -> list[Text]:
    """One line per session that looks stuck or is running out of context, with what to do."""
    notes = []
    for r in rows:
        ref = short_id(r["id"])
        if (stuck := stuck_minutes(r)) is not None:
            notes.append(
                Text().append(f"? {ref} working but no output for {stuck}m: ", style="yellow")
                + Text(f"agentctl attach {ref}, or agentctl interrupt {ref}", style="dim")
            )
        if (pct := context_full(r)) is not None:
            hint = "/compact" if r["source"] == "claude_code" else "a fresh session"
            notes.append(
                Text().append(f"? {ref} has used {pct}% of its context: ", style="yellow")
                + Text(f"consider {hint} before the next big task", style="dim")
            )
    return notes


@app.command()
def tail(
    id: OptIdArg = None,
    n: Annotated[int, typer.Option("-n", help="Number of recent events.")] = 50,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Keep streaming.")] = False,
    raw: Annotated[bool, typer.Option("--raw", help="Show replies as plain text.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Show a session's recent transcript, optionally live.

    With no session on a terminal, pick one from a list."""
    id = resolve(pick_or_fail(id, as_json, "read:", lambda r: True), as_json)
    try:
        it = stream("follow", id=id, n=n) if follow else iter([call("tail", id=id, n=n)])
        first = next(it)
    except DaemonError as e:
        fail(str(e), as_json)
    s = first["session"]
    if as_json:
        for ev in first["events"]:
            print(json.dumps(ev), flush=True)
    else:
        out.print(ui.session_header(s))
        for ev in first["events"]:
            if (shown := ui.event(ev, not raw, out.width, ui.AGENT[s["source"]])) is not None:
                out.print(shown)
        if s["status"] == "awaiting_approval" and s["pending_approval"]:
            out.print()
            out.print(ui.approval_card(s))
        elif not follow:
            ref = short_id(s["id"])
            out.print()
            out.print(ui.hint(f'agentctl send {ref} "…"', f"agentctl tail {ref} -f"))
    if n:
        with contextlib.suppress(DaemonError):
            call("seen", id=s["id"])  # you've now read it
    try:
        for ev in it:
            if as_json:
                print(json.dumps(ev), flush=True)
            elif ev["kind"] == "approval_request":
                with contextlib.suppress(DaemonError):
                    out.print()
                    out.print(ui.approval_card(call("tail", id=s["id"], n=0)["session"]))
            elif (shown := ui.event(ev, not raw, out.width, ui.AGENT[s["source"]])) is not None:
                out.print(shown)
    except KeyboardInterrupt:
        pass


@app.command()
def send(
    id: SendIdArg,
    message: Annotated[
        str | None,
        typer.Argument(
            help='Text to send, or "-" to read stdin. Left out on a terminal: asks for it.',
            show_default=False,
        ),
    ] = None,
    when_idle: Annotated[
        bool,
        typer.Option("--when-idle", "-l", help="Queue it even if the agent is free now."),
    ] = False,
    now: Annotated[
        bool,
        typer.Option(
            "--now",
            help="Send even mid-turn (Claude: queued in its own input; "
            "Codex: steers the running turn).",
        ),
    ] = False,
    wait_: Annotated[
        bool,
        typer.Option("--wait", "-w", help="Then wait for the reply, like `agentctl wait`."),
    ] = False,
    timeout: Annotated[
        int, typer.Option(help="With --wait: give up after this many seconds.")
    ] = 600,
    as_json: JsonOpt = False,
) -> None:
    """Type a message into a session and press Enter.

    If it's mid-turn, the message is queued and sent once the agent is free (--now sends it
    anyway)."""
    if message is None:
        if as_json or not _interactive():
            fail('what to send? give the text, or "-" to read it from stdin', as_json)
        target = rpc("tail", False, id=id, n=0)["session"]
        id = target["id"]
        who = ui.glyph(target["status"]).append(f" {ui.title(target)}", style="bold")
        err.print(who.append(f"  {ui.project(target)} · {short_id(id)}", style=ui.DIM))
        from rich.prompt import Prompt

        message = Prompt.ask(Text("message", style=f"bold {ui.ACCENT}"), console=err)
    text = sys.stdin.read().rstrip("\n") if message == "-" else message
    if not text.strip():
        fail("empty message", as_json)
    if when_idle:
        r = rpc("queue", as_json, id=id, text=text)
        n = len(r["queued"])
        ok(
            f"queued for {short_id(r['session_id'])}",
            as_json,
            r,
            f"{n} waiting · sent once it's free",
        )
    else:
        r = rpc("send", as_json, id=id, text=text, now=now)
        if "queued" in r:
            n = len(r["queued"])
            ok(f"queued for {short_id(r['session_id'])}", as_json, r, f"it's busy · {n} waiting")
        else:
            lines = text.count("\n") + 1
            more = f"{lines} lines" if lines > 1 else ""
            ok(f"sent to {short_id(r['session_id'])}", as_json, r, more)
    if wait_:
        _wait(r["session_id"], timeout, quiet=False, as_json=as_json)


@app.command()
def wait(
    id: OptIdArg = None,
    timeout: Annotated[int, typer.Option(help="Give up after this many seconds.")] = 600,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Don't print the reply.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Wait for a session to finish its turn, then print its last reply.

    Waits through queued follow-ups too. Exit status: 0 done, 2 stopped for approval,
    3 timed out, 4 nothing was running, 5 delivery uncertain or undeliverable. The reply goes
    to stdout and the status to stderr, so `agentctl wait api > reply.md` keeps just the reply.
    Polls the local daemon only; no tokens are spent waiting. With no session on a terminal,
    pick one of those working."""
    busy = lambda r: r["status"] in ("working", "awaiting_approval")  # noqa: E731
    _wait(pick_or_fail(id, as_json, "wait for:", busy), timeout, quiet, as_json)


def _wait(ref: str, timeout: int, quiet: bool, as_json: bool) -> None:
    ref = resolve(ref, as_json)
    started = time.monotonic()
    status = None
    if err.is_terminal and not as_json:
        status = err.status("waiting…", spinner="dots", spinner_style=ui.ACCENT)
        status.start()

    def show(s: dict[str, Any]) -> None:
        if status is not None:
            took = int(time.monotonic() - started)
            line = Text().append(f"{ui.title(s)}  ", style="bold") + ui.status_label(s["status"])
            line.append(f"  {took // 60}:{took % 60:02d}", style=ui.DIM)
            if s.get("queued"):
                line.append(f"  · {len(s['queued'])} queued", style="cyan")
            line.append("  · ctrl-c stops waiting (not the agent)", style=ui.DIM)
            status.update(line)

    try:
        w = wait_turn(call, ref, max(timeout, 1), on_poll=show)
    except DaemonError as e:
        fail(str(e), as_json)
    except KeyboardInterrupt:
        raise typer.Exit(130) from None
    finally:
        if status is not None:
            status.stop()
    s, short = w.session, short_id(w.session["id"])
    if as_json:
        print(json.dumps({"outcome": w.outcome, "session_id": s["id"], "status": s["status"],
                          "reply": w.reply, "detail": w.detail}))  # fmt: skip
        raise typer.Exit(EXIT[w.outcome])
    if w.outcome == "done":
        took = int(time.monotonic() - started)
        err.print(ui.success(f"{ui.title(s)} finished", f"{short} · {took // 60}:{took % 60:02d}"))
        if w.reply and not quiet:
            if out.is_terminal:
                from rich.markdown import Markdown

                out.print(Markdown(w.reply))
            else:  # piped: the reply exactly as written, unwrapped
                print(w.reply)
        return
    if w.outcome == "approval":
        err.print(ui.approval_card(s))
    message = {
        "approval": f"{short} is waiting for your approval of {w.detail}",
        "timeout": f"timed out after {timeout}s; {short} is still {s['status']}",
        "idle": f"{short} isn't working on anything ({s['status']})",
        "uncertain": f"{short}: delivery uncertain; check `agentctl tail {short}` before resending",
        "undelivered": f"{short} has undelivered work: {w.detail}",
    }[w.outcome]
    err.print(ui.failure(message))
    raise typer.Exit(EXIT[w.outcome])


@app.command()
def approve(
    id: OptIdArg = None,
    choice: Annotated[int | None, typer.Option(help="Pick option N instead of 1 (Yes).")] = None,
    all_: Annotated[
        bool, typer.Option("--all", help="Every pending request matching --tool, in any session.")
    ] = False,
    tool: Annotated[
        str | None,
        typer.Option(help="With --all: which requests, as a glob, e.g. 'Read*' or 'Bash: npm *'."),
    ] = None,
    here: Annotated[bool, typer.Option("--here", help="With --all: only in this repo.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="With --all: don't ask first.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Answer a pending permission prompt (default: option 1, Yes).

    With no session on a terminal: pick one of those waiting (or take the only one), see what
    it wants, and choose the answer. `--all --tool PATTERN` answers every matching request at
    once, after showing you the list (skip that with -y). There's no blanket approve: say
    `--tool '*'` if you mean it."""
    if all_ or tool:
        _answer_all(True, tool, here, yes, as_json)
        return
    if id is None and choice is None and not as_json and _interactive():
        _approve_interactively()
        return
    if id is None:
        fail("which session? an id, name or folder, or @ for the one waiting on you", as_json)
    r = rpc("approve", as_json, id=id, choice=choice, allow=True)
    ok(f"answered {short_id(r['session_id'])}: {r['chosen']}", as_json, r)


def _approve_interactively() -> None:
    pending = [
        r for r in rpc("ls", False)
        if r["status"] == "awaiting_approval" and r.get("pending_approval")
    ]  # fmt: skip
    if not pending:
        out.print(ui.success("Nothing is waiting for approval."))
        return
    sid = pending[0]["id"] if len(pending) == 1 else choose_session("approve:", pending)
    if sid is None:
        raise typer.Exit(1)
    r = next(x for x in pending if x["id"] == sid)
    err.print(ui.approval_card(r, show_hint=False))
    appr = r["pending_approval"]
    if appr.get("questions"):
        fail("it's asking questions: answer them in agentctl watch (a)", False)
    if not r.get("controllable"):
        fail("read-only here", False, [ui.hint(f"agentctl adopt {short_id(sid)}")])
    options = appr.get("options") or {"1": "Yes", "2": "No"}
    key = ui.pick_option(options, err)
    if key is None:
        raise typer.Exit(1)
    no = options[key].lower().startswith(("no", "reject", "deny"))
    choice = int(key) if key.isdigit() else None
    res = rpc("approve", False, id=sid, choice=choice, allow=not no)
    ok(f"answered {short_id(sid)}: {res['chosen']}", False, res, ui.title(r))


def _answer_all(allow: bool, tool: str | None, here: bool, yes: bool, as_json: bool) -> None:
    verb = "approve" if allow else "deny"
    if not tool:
        fail(f"say which requests to {verb}: --tool 'Read*' (or --tool '*' for every one)", as_json)
    folder = _here_root() if here else None
    params = {"tool": tool, "allow": allow, "folder": folder}
    preview = rpc("approve_all", as_json, **params, dry_run=True)["matched"]
    todo = [m for m in preview if "error" not in m]
    if not todo:
        if as_json:
            print(json.dumps({"matched": preview}))
            return
        out.print(Text(f"nothing pending matches {tool!r}", style="dim"))
        for m in preview:
            out.print(Text(f"  {short_id(m['session_id'])} {m['tool']}: {m['error']}", style="dim"))
        return
    if not as_json:
        out.print(
            Text(f"{len(todo)} request{'s' if len(todo) > 1 else ''} to {verb}:", style="bold")
        )
        for m in todo:
            out.print(
                Text().append(f"  {short_id(m['session_id'])} ", style="bold")
                + Text(m["tool"])
                + Text(f"  {short_cwd(m['cwd'])}", style="blue")
            )
    if not yes and not as_json:
        if not _interactive():
            fail("pass --yes to answer these without a terminal to confirm on", as_json)
        if not typer.confirm(f"{verb.capitalize()} all of these?", default=False):
            raise typer.Exit(1)
    only = [{"id": m["session_id"], "tool": m["tool"]} for m in todo]
    done = rpc("approve_all", as_json, **params, only=only)["matched"]
    if as_json:
        print(json.dumps({"matched": done}))
        return
    for m in done:
        if "error" in m:
            err.print(Text(f"error: {short_id(m['session_id'])} {m['error']}", style="red"))
        else:
            ok(f"{short_id(m['session_id'])} → {m['chosen']}", False, m)


@app.command()
def deny(
    id: OptIdArg = None,
    all_: Annotated[
        bool, typer.Option("--all", help="Every pending request matching --tool, in any session.")
    ] = False,
    tool: Annotated[str | None, typer.Option(help="With --all: which requests, as a glob.")] = None,
    here: Annotated[bool, typer.Option("--here", help="With --all: only in this repo.")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="With --all: don't ask first.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Reject a pending permission prompt.

    `--all --tool PATTERN` for several. With no session on a terminal, pick one of those
    waiting."""
    if all_ or tool:
        _answer_all(False, tool, here, yes, as_json)
        return
    waiting = lambda r: r["status"] == "awaiting_approval"  # noqa: E731
    r = rpc("approve", as_json, id=pick_or_fail(id, as_json, "deny:", waiting), allow=False)
    ok(f"denied {short_id(r['session_id'])}: {r['chosen']}", as_json, r)


@app.command()
def interrupt(id: OptIdArg = None, as_json: JsonOpt = False) -> None:
    """Stop the current turn (sends Escape). With no session on a terminal, pick one."""
    busy = lambda r: r["status"] in ("working", "awaiting_approval") and r["controllable"]  # noqa: E731
    r = rpc("interrupt", as_json, id=pick_or_fail(id, as_json, "interrupt:", busy))
    ok(f"interrupted {short_id(r['session_id'])}", as_json, r)


@app.command()
def attach(
    id: Annotated[
        str | None,
        typer.Argument(
            help="Session id prefix, name or folder, or a tmux pane id like %2; none: pick one.",
            autocompletion=complete_session,
            show_default=False,
        ),
    ] = None,
) -> None:
    """Jump to the session's tmux pane (ctrl-] d detaches from agentctl's panes)."""
    id = pick_or_fail(id, False, "attach:", lambda r: bool(r.get("tmux_pane")))
    if id.startswith("%"):  # a pane `new`/`adopt` opened, before its session exists
        _attach(launcher.socket_path(), id)
        return
    r = rpc("pane", False, id=id)
    _attach(r["socket"], r["pane"])


def _attach(sock: str | None, pane: str) -> None:
    """Attach this terminal to a pane and return when the user detaches or the agent exits.
    Inside the same tmux server: switch to it. Inside another one (e.g. yours): nest."""
    tmux = [launcher.need("tmux", "brew install tmux"), *(["-S", sock] if sock else [])]
    subprocess.run([*tmux, "select-window", "-t", pane], check=False, capture_output=True)
    subprocess.run([*tmux, "select-pane", "-t", pane], check=False, capture_output=True)
    current = os.environ.get("TMUX", "").split(",")[0]
    if current and (sock is None or current == sock):
        subprocess.run([*tmux, "switch-client", "-t", pane], check=False)
        return
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    subprocess.run([*tmux, "attach-session", "-t", pane], check=False, env=env)


def _launch(
    agent: str,
    cwd: str,
    name: str | None,
    extra: list[str],
    as_json: bool,
    resume: str | None = None,
) -> dict[str, Any]:
    """Ask the daemon to start the agent, so it runs in the daemon's login context."""
    path = os.environ.get("PATH", "")
    params = {"agent": agent, "cwd": os.path.abspath(cwd), "name": name, "extra": extra}
    return dict(rpc("launch", as_json, **params, path=path, resume=resume))


def _await_start(sid: str, sock: str, pane: str) -> str:
    """Wait for the new session to come up. Returns its status, or a hint if it's stuck."""
    for _ in range(40):
        rows = call("ls", all=True)
        # the process in *our* pane: for `adopt` another process may hold the session too
        hit = next((r for r in rows if r["id"] == sid and r["tmux_pane"] == pane), None)
        if hit and hit["status"] != "ended":
            return str(hit["status"])
        screen = launcher.capture(sock, pane)
        if "trust this folder" in screen or "Do you trust the contents" in screen:
            return "trust"
        if "Update available" in screen:
            return "update"
        time.sleep(0.25)
    return "unknown"


def _launched(r: dict[str, Any], state: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps(r | {"state": state}))
        return
    ref = short_id(r["session_id"])
    head = f"Started {ref}" + (f" in {ui.tilde(r['cwd'])}" if r.get("cwd") else "")
    if state in ui.STATUS:
        out.print(ui.success(head) + Text("  ") + ui.status_label(state))
        out.print(ui.hint(f"agentctl attach {ref}", f"agentctl tail {ref} -f", "agentctl watch"))
        return
    out.print(ui.success(head))
    say = {
        "pending": ("It's waiting for its first message.", f'agentctl send {ref} "…"'),
        "update": ("Codex is offering an update: pick Skip or update.", f"agentctl attach {ref}"),
        "trust": ("It's asking whether to trust this folder.", f"agentctl attach {ref}"),
        "unknown": ("It isn't up yet.", f"agentctl attach {ref}"),
    }
    line, command = say.get(state, say["unknown"])
    style = ui.DIM if state == "pending" else "yellow"
    for ln in ui.tree([Text(line, style=style)]):
        out.print(ln)
    out.print(ui.hint(command, "agentctl watch"))


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def new(
    ctx: typer.Context,
    agent: Annotated[str, typer.Argument(help="claude, codex or opencode")],
    directory: Annotated[Path, typer.Argument(help="Working directory.")] = Path("."),
    name: Annotated[str | None, typer.Option("--name", help="Session name.")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Start an agent in the background (agentctl's tmux). Args after -- go to the agent."""
    if agent not in SOURCES:
        fail("agent must be claude, codex or opencode", as_json)
    r = _launch(agent, str(directory), name, list(ctx.args), as_json)
    state = (
        "pending" if agent != "claude" else _await_start(r["session_id"], r["socket"], r["pane"])
    )
    _launched(r, state, as_json)


def _foreground(agent: str, args: list[str]) -> None:
    r = _launch(agent, ".", None, args, False)
    _attach(r["socket"], r["pane"])
    tmux = launcher.need("tmux", "brew install tmux")
    check = [tmux, "-S", r["socket"], "has-session", "-t", r["pane"]]
    if subprocess.run(check, capture_output=True).returncode == 0:  # detached, not exited
        ref = short_id(r["session_id"])
        out.print(
            Text(f"detached; {agent} keeps running · back with: agentctl attach {ref}", style="dim")
        )


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
def claude(ctx: typer.Context) -> None:
    """Run Claude Code here, controllable by agentctl. Args go to claude. ctrl-] d detaches."""
    _foreground("claude", list(ctx.args))


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
def codex(ctx: typer.Context) -> None:
    """Run the Codex TUI here, controllable by agentctl. Args go to codex. ctrl-] d detaches."""
    _foreground("codex", list(ctx.args))


@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
def opencode(ctx: typer.Context) -> None:
    """Run opencode here, controllable by agentctl. Args go to opencode. ctrl-] d detaches."""
    _foreground("opencode", list(ctx.args))


# Undo what a TUI killed by a signal can leave on: kitty keyboard flags, mouse reporting,
# hidden cursor. Unknown sequences are ignored by terminals. Bracketed paste stays on: this is
# written after the shell's prompt, and zsh/bash turn it on for that prompt themselves.
TTY_RESET = "\x1b[<u\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?25h"


def _proc(pid: int) -> tuple[str, str, str]:
    """(tty, command name, the macOS app it ships in) of a process; blanks if it's gone."""
    r = subprocess.run(["ps", "-o", "tty=,comm=", "-p", str(pid)], capture_output=True, text=True)
    tty, _, comm = r.stdout.strip().partition(" ")
    app = re.search(r"/([^/]+)\.app/", comm)  # the outermost bundle: ChatGPT, not CodexCLI
    return tty, Path(comm.strip()).name, app[1] if app else ""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, but isn't ours
        return True


class AdoptError(Exception):
    pass


@dataclass
class AdoptPlan:
    session: dict[str, Any]
    pid: int | None = None  # the terminal process to stop first, if any
    tty: str = ""
    comm: str = ""

    @property
    def question(self) -> str:
        short = short_id(self.session["id"])
        if self.pid:
            return (
                f"{short} is running in {self.comm} on {self.tty} (pid {self.pid}). "
                "Stop it there and move it here?"
            )
        return f"Reopen {short} in agentctl?"


def check_holder(s: dict[str, Any], pid: int, force: bool) -> tuple[str, str]:
    """(tty, command) of the process holding a session, if agentctl may stop it."""
    short = short_id(s["id"])
    tty, comm, app = _proc(pid)
    if app and tty in ("", "??"):  # a desktop app keeps running (and its turns) once closed
        raise AdoptError(
            f"{short} is open in the {app} app (pid {pid}), which keeps running after its window "
            f"closes. Quit {app} (⌘Q), after any turn running there finishes, then retry."
        )
    if comm not in ("claude", "codex") or tty in ("", "??"):
        raise AdoptError(
            f"{short} is open in pid {pid} ({comm or 'gone'}), which isn't running in a terminal "
            "(the desktop app, or Codex's shared background server). agentctl won't stop that; "
            "close it there first."
        )
    if s["status"] in ("working", "awaiting_approval") and not force:
        raise AdoptError(
            f"{short} is {STATUS_SHORT[s['status']]} in {comm} (pid {pid}); moving it now would "
            "abort that turn. Wait until it's done, or --force."
        )
    return tty, comm


def adopt_plan(id: str, force: bool) -> AdoptPlan:
    """What `adopt` would do, or AdoptError saying why it mustn't. Changes nothing."""
    s = call("tail", id=id, n=0)["session"]
    short = short_id(s["id"])
    if s["tmux_pane"]:
        raise AdoptError(f"{short} is already controllable: agentctl attach {short}")
    pid = call("holder", id=s["id"])["pid"]
    if pid:
        return AdoptPlan(s, pid, *check_holder(s, pid, force))
    if s["source"] == "codex":
        # Codex lets one process write a thread; never open a second one, --force or not.
        if not s["controllable"]:
            raise AdoptError(f"{short} is open in another Codex process; close it there first")
        if s["status"] == "working":
            raise AdoptError(f"{short} has a turn running via agentctl; wait for it to finish")
    elif s["status"] != "ended" and not force:
        # recent activity but no process agentctl can see (e.g. opencode started by hand)
        raise AdoptError(
            f"{short} looks {STATUS_SHORT[s['status']]} in a process agentctl can't see; two "
            "processes writing one session will conflict. Quit it there first, or --force."
        )
    return AdoptPlan(s)


def adopt_run(plan: AdoptPlan) -> dict[str, Any]:
    """Stop the old process (like Ctrl-C in its terminal), then resume the session in
    agentctl's tmux. Returns the launch result."""
    s, pid = plan.session, plan.pid
    short = short_id(s["id"])
    if pid:
        for sig, wait in ((signal.SIGINT, 5.0), (signal.SIGTERM, 3.0)):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, sig)  # Claude exits cleanly on SIGINT (verified); Codex just exits
            deadline = time.monotonic() + wait
            while _alive(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            if not _alive(pid):
                break
        else:
            raise AdoptError(f"pid {pid} is still running; stop it yourself, then retry")
        time.sleep(0.3)  # let the shell print its prompt
        with contextlib.suppress(OSError):  # tell the old terminal where the session went
            with open(f"/dev/{plan.tty}", "w") as t:
                # Claude/Codex clear their screen on exit, so the line above the fresh prompt is
                # the command that started them. Put the note there and leave the cursor at the
                # prompt: written below it, the terminal looked like it was still waiting.
                note = f"[agentctl] {short} moved into agentctl: agentctl attach {short}"
                t.write(f"{TTY_RESET}\0337\033[A\r\033[2K{note}\0338")
        deadline = time.monotonic() + 6
        while call("holder", id=s["id"])["pid"] and time.monotonic() < deadline:
            time.sleep(0.2)  # until the daemon sees it's released (Codex: its writer lock)
    agent = {"claude_code": "claude"}.get(s["source"], s["source"])
    name = (s["title"] or short_cwd(s["cwd"]))[:20]
    path = os.environ.get("PATH", "")
    params = {"agent": agent, "cwd": s["cwd"], "name": name, "extra": [], "path": path}
    return dict(call("launch", **params, resume=s["id"]))


@app.command()
def adopt(
    id: IdArg,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Stop it in its terminal without asking.")
    ] = False,
    force: Annotated[
        bool, typer.Option(help="Even mid-turn (aborts it), or when it only looks live.")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Move a read-only session into agentctl so it can be controlled.

    Resumes it with `claude --resume <id>`, `codex resume <id>` or `opencode -s <id>`. If it's
    open in another terminal, that process is stopped first (like Ctrl-C there; asks unless
    -y). Then `agentctl attach <id>`."""
    id = resolve(id, as_json)
    try:
        plan = adopt_plan(id, force)
        if plan.pid and not yes:
            if as_json:
                fail(plan.question.split(".")[0] + "; pass --yes", as_json)
            if not typer.confirm(plan.question, default=True):
                raise typer.Exit(1)
        r = adopt_run(plan)
    except (AdoptError, DaemonError) as e:
        fail(str(e), as_json)
    _launched(r, _await_start(plan.session["id"], r["socket"], r["pane"]), as_json)


@app.command()
def queue(
    id: IdArg,
    clear: Annotated[bool, typer.Option("--clear", help="Drop everything queued.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Show (or --clear) the follow-ups queued with `send --when-idle`."""
    if clear:
        r = rpc("unqueue", as_json, id=id)
        ok(f"cleared the queue for {short_id(r['session_id'])}", as_json, r)
        return
    s = rpc("tail", as_json, id=id, n=0)["session"]
    if as_json:
        print(json.dumps({"session_id": s["id"], "queued": s["queued"]}))
        return
    ref = short_id(s["id"])
    if not s["queued"]:
        out.print(ui.success(f"Nothing queued for {ui.title(s)}"))
        return
    n = len(s["queued"])
    out.print(Text(f"{n} queued for {ui.title(s)}", style="bold")
              + Text().append("  sent one at a time, once it's free", style=ui.DIM))  # fmt: skip
    for i, t in enumerate(s["queued"], 1):
        out.print(Text().append(f"  {i:>2}  ", style=ui.DIM).append(ui.first_line(t, 90)))
    if s.get("queue_error"):
        out.print(ui.failure(f"can't send yet: {s['queue_error']}"))
    out.print()
    out.print(ui.hint(f"agentctl queue {ref} --clear", f'agentctl send {ref} -l "…"'))


@app.command()
def digest(as_json: JsonOpt = False) -> None:
    """What happened since you last looked.

    Per session: new replies, files touched, and the start of its last reply. Opening a
    session (watch, or tail) marks it read."""
    rows = rpc("digest", as_json)
    if as_json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        out.print(ui.success("Nothing new since you last looked."))
        return
    known = {x["id"]: x for x in rpc("ls", False, all=True)}
    total = sum(r["new_replies"] for r in rows)
    out.print(
        Text().append(f"{total} new repl{'ies' if total != 1 else 'y'}", style="bold cyan")
        + Text(f" in {len(rows)} session{'s' if len(rows) != 1 else ''}", style=ui.DIM)
    )
    for r in rows:
        k = known.get(r["id"], {})
        head = Text("\n") + ui.glyph(r["status"]) + Text(" ")
        head.append(ui.project(k) if k else "", style=f"bold {ui.ACCENT}")
        head.append(f"  {r['title'] or ''}", style="bold").append(
            f"  {r['new_replies']} new  {short_id(r['id'])}", style=ui.DIM
        )
        out.print(head)
        lines: list[Text] = []
        if r["last_reply"]:
            lines.append(Text().append("◆ ", style="green") + Text(r["last_reply"]))
        for e in r.get("errors") or []:
            lines.append(Text().append("✗ ", style="red") + Text(e, style="red"))
        if r.get("failed_tools"):
            n = r["failed_tools"]
            lines.append(Text(f"{n} tool call{'s' if n > 1 else ''} failed", style="red"))
        if r.get("stuck_minutes") is not None:
            lines.append(Text(f"no output for {r['stuck_minutes']}m", style="yellow"))
        if r["files"]:
            more = f"  +{len(r['files']) - 6}" if len(r["files"]) > 6 else ""
            lines.append(
                Text().append("edited ", style=ui.DIM) + Text(", ".join(r["files"][:6]) + more)
            )
        for ln in ui.tree(lines):
            out.print(ln)
    first = short_id(rows[0]["id"])
    out.print()
    out.print(ui.hint(f"agentctl tail {first}", "agentctl watch"))


def reset_time(ts: float) -> str:
    """When a limit window resets: "16:30" within a day, else "Wed 20:30"."""
    return datetime.fromtimestamp(ts).strftime("%H:%M" if ts - time.time() < 86400 else "%a %H:%M")


def usage_text(u: dict[str, Any], resets: bool = False) -> Text:
    """'cx 5h 15% · week 30%'. With resets, one line per agent and when each window resets:
    'cx 5h 15% (16:30) · week 30% (Wed 20:30)'."""
    t = Text()
    for key, label in (("claude", "cc"), ("codex", "cx")):
        if not u.get(key):
            continue
        t.append(("\n" if resets else "  ") if t else "").append(f"{label} ", style="dim")
        for i, w in enumerate(u[key]["windows"]):
            pct = round(w["used_percent"])
            style = "red" if pct >= 90 else "yellow" if pct >= 70 else ""
            t.append(" · " if i else "").append(f"{w['name']} ").append(f"{pct}%", style=style)
            if resets and w.get("resets_at", 0) > time.time():
                t.append(f" ({reset_time(w['resets_at'])})", style="dim")
    return t


@app.command()
def usage(as_json: JsonOpt = False) -> None:
    """Plan usage limits: 5-hour and weekly windows.

    Claude Code reports them to its status line (agentctl wraps it); Codex writes them to its
    session files."""
    u = rpc("usage", as_json)
    if as_json:
        print(json.dumps(u, indent=2))
        return
    width = 24 if out.width >= 70 else 12
    for i, (key, name) in enumerate((("claude", "Claude Code"), ("codex", "Codex"))):
        lim = u.get(key)
        if i:
            out.print()
        if not lim:
            how = "a Claude session's status line redraws" if key == "claude" else "Codex replies"
            out.print(
                Text().append(name, style="bold")
                + Text(f"  nothing yet · updates when {how}", ui.DIM)
            )
            continue
        head = Text(name, style="bold")
        if lim.get("plan"):
            head.append(f"  {lim['plan']}", style=ui.DIM)
        head.append(f"  as of {age(lim['at'])} ago", style=ui.DIM)
        out.print(head)
        for w in lim["windows"]:
            pct = round(w["used_percent"])
            line = Text(f"  {w['name']:<7}") + ui.gauge(pct, width)
            colour = "red" if pct >= 85 else "yellow" if pct >= 60 else ""
            line.append(f" {pct:>3}%", style=f"bold {colour}".strip())
            when = reset_time(w["resets_at"])
            line.append(f"  resets in {ui.until(w['resets_at'])}", style=ui.DIM)
            line.append(f" ({when})", style=ui.DIM)
            out.print(line)


@app.command()
def rename(
    id: IdArg,
    title: Annotated[str, typer.Argument(help="New name; empty restores the agent's own.")] = "",
    as_json: JsonOpt = False,
) -> None:
    """Give a session your own name in agentctl (the agent's own title is untouched)."""
    r = rpc("rename", as_json, id=id, title=title)
    ok(f"{short_id(r['session_id'])} → {r['title'] or 'its own title'}", as_json, r)


@app.command()
def close(
    id: OptIdArg = None,
    force: Annotated[bool, typer.Option(help="Even mid-turn (aborts it).")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Quit a session's agent and close agentctl's pane for it.

    Like Ctrl-C in its terminal. The session stays resumable (`agentctl adopt <id>`)."""
    live = lambda r: r["status"] != "ended" and r["controllable"]  # noqa: E731
    r = rpc("close", as_json, id=pick_or_fail(id, as_json, "close:", live), force=force)
    ok(f"closed {short_id(r['session_id'])}", as_json, r)


@app.command()
def mcp(
    allow_write: Annotated[
        bool,
        typer.Option("--allow-write", help="Also let the agent send, queue and start sessions."),
    ] = False,
    team: Annotated[str | None, typer.Option(help="Expose only this team's scoped tools.")] = None,
    token_file: Annotated[Path | None, typer.Option(help="Private team credential file.")] = None,
) -> None:
    """MCP server (stdio) so an agent can see and coordinate your other agents.

    Read-only unless --allow-write. Never exposes approve/deny/close: those stay yours. Add it
    with e.g. `claude mcp add -s user agentctl -- agentctl mcp`."""
    from agentctl.mcp import serve

    if bool(team) != bool(token_file):
        fail("Use --team and --token-file together", False)
    token = None
    if token_file:
        try:
            if token_file.stat().st_mode & 0o077:
                fail("Team credential file must be private (chmod 600)", False)
            token = token_file.read_text().strip()
        except OSError as error:
            fail(str(error), False)
    serve(allow_write, team_id=team, team_token=token)


@app.command()
def watch(
    notify: Annotated[bool, typer.Option(help="macOS notification + bell on approval.")] = False,
    theme: Annotated[
        str | None,
        typer.Option(
            help="e.g. textual-light, nord; ansi-dark / ansi-light use your terminal's own "
            "colors. Remembered, like a pick with T.",
        ),
    ] = None,
) -> None:
    """Live dashboard. Works down to ~45 columns; `?` lists its keys."""
    from textual.theme import BUILTIN_THEMES

    if theme and theme not in BUILTIN_THEMES:
        fail(f"no theme {theme!r}; one of: {', '.join(sorted(BUILTIN_THEMES))}", False)
    rpc("ping", False)  # starts the daemon if needed
    from agentctl.tui import Watch

    Watch(notify=notify, theme=theme).run()


# --- setup ---

MARK = {"ok": ("ok", "green"), "warn": ("!", "yellow"), "fail": ("x", "red"), "info": ("·", "dim")}


def show_checks(rows: list[Any]) -> None:
    t = Table(box=None, show_header=False, pad_edge=False)
    t.add_column(no_wrap=True)
    t.add_column(no_wrap=True, style="bold")
    t.add_column(ratio=1)
    for c in rows:
        sym, style = MARK[c.status]
        detail = Text(c.detail, style="dim" if c.status == "info" else "")
        if c.fix:
            detail.append(f"\n→ {c.fix}", style=style)
        t.add_row(Text(sym, style=f"bold {style}"), c.name, detail)
    out.print(t)


@app.command()
def doctor(as_json: JsonOpt = False) -> None:
    """Check the install, daemon, hooks, aliases and phone access; print fixes."""
    from dataclasses import asdict

    from agentctl.setup import checks

    rows = checks()
    if as_json:
        print(json.dumps([asdict(c) for c in rows], indent=2))
    else:
        show_checks(rows)
    failed = [c for c in rows if c.status == "fail"]
    if not as_json:
        warned = sum(c.status == "warn" for c in rows)
        verdict = (
            "all good"
            if not failed and not warned
            else f"{len(failed)} problems, {warned} warnings"
        )
        out.print(
            Text(verdict, style="bold red" if failed else "yellow" if warned else "bold green")
        )
    raise typer.Exit(1 if failed else 0)


def step(n: int, title: str) -> None:
    out.print(Text(f"\n{n}. {title}", style="bold blue"))


def ask(question: str, yes: bool) -> bool:
    return yes or typer.confirm(question, default=True)


@app.command()
def setup(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Accept every step.")] = False,
) -> None:
    """Set agentctl up, asking before each change.

    Installs Claude hooks, the launchd daemon and shell aliases, then checks phone access.
    Safe to re-run; `agentctl uninstall` undoes it."""
    from agentctl import setup as su

    exe = _exe()
    kind, src = su.install_info()
    if su.in_project_venv():
        out.print(Panel(
            Text(f"agentctl is running from {kind}. Hooks and the daemon would point into that "
                 "folder, so moving or deleting it breaks them. Install it as a tool first:\n\n")
            .append(f"uv tool install --editable {shlex.quote(src or '.')}\n", style="bold")
            .append("agentctl setup", style="bold"),
            title=" ! not installed as a tool ", title_align="left", border_style="yellow",
        ))  # fmt: skip
        if not yes and not typer.confirm("Continue with this install anyway?", default=False):
            raise typer.Exit(1)

    step(1, "Requirements")
    if not shutil.which("tmux"):
        fail("tmux is required: brew install tmux", False)
    found = [n for n in su.AGENTS if su.agent_exe(n)]
    out.print(Text("ok: tmux, agents: " + (", ".join(found) or "none found"), style="green"))
    if not found:
        fail("install claude, codex or opencode first", False)

    step(2, "Claude Code hooks")
    if "claude" not in found:
        out.print(Text("skipped: claude not installed", style="dim"))
    elif not hooks.planned(exe)[1]:
        out.print(Text("ok: installed", style="green"))
    else:
        out.print(f"Adds or updates 8 hook entries in {hooks.SETTINGS} and wraps your status "
                  "line (backed up first; `agentctl hooks uninstall` restores it). They give "
                  "faster status, control of Claude sessions in your own tmux, and your plan's "
                  "usage limits. Your status line looks the same; no tokens are used.")  # fmt: skip
        if ask("Install the hooks?", yes):
            ok(f"hooks installed; backup at {hooks.install(exe)}", False, None)

    step(3, "Background daemon (launchd)")
    info = su.ping()
    if info and su.launchd_pid() == info["pid"] and su.plist_python() == sys.executable:
        out.print(Text(f"ok: running under launchd (pid {info['pid']})", style="green"))
    else:
        out.print("Starts agentctld at login, in your desktop session, so agents it launches "
                  "can use your Claude login even when you're connected over SSH.")  # fmt: skip
        if ask("Install the launchd daemon?", yes):
            info = install_daemon(False)
            ok(f"agentctld running under launchd (pid {info['pid']})", False, info)

    step(4, "Shell aliases")
    rc = su.rc_file()
    missing = [n for n in found if rc and not su.aliased(rc, n)]
    if rc is None:
        out.print(Text(f"skipped: alias {', '.join(found)} to `agentctl <name>` in your "
                       "shell's config yourself", style="dim"))  # fmt: skip
    elif not missing:
        out.print(
            Text(f"ok: {', '.join(found)} already aliased in {su.tilde(str(rc))}", style="green")
        )
    else:
        out.print(f"Makes {', '.join(missing)} start inside agentctl, so you can watch and answer "
                  f"them from your phone. Adds a marked block to {su.tilde(str(rc))}. "
                  f"Bypass with `command {missing[0]}`.")  # fmt: skip
        if ask("Add the aliases?", yes):
            su.add_aliases(rc, missing)
            ok(f"added; open a new terminal or run: source {su.tilde(str(rc))}", False, None)

    step(5, "Check")
    rows = su.checks()
    show_checks(rows)
    if any(c.status != "ok" and c.name in ("ssh", "tailscale") for c in rows):
        out.print(Text("Phone access: see “Phone setup” in the README.", style="dim"))


@app.command()
def uninstall(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask.")] = False,
) -> None:
    """Undo `agentctl setup`.

    Removes the hooks (restoring settings.json), the launchd daemon and the alias block.
    Leaves ~/.agentctl and running agents alone."""
    from agentctl import setup as su

    if not ask("Remove agentctl's hooks, daemon and aliases?", yes):
        raise typer.Exit(1)
    try:
        call("shutdown", autostart=False)
    except DaemonError as e:
        if "not running" not in str(e):
            fail(str(e), False)  # e.g. a Codex turn agentctl started is still running
    if any(hooks.status().values()) or hooks.RECORD.exists():
        ok(hooks.uninstall(), False, None)
    if PLIST.exists():
        _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
        PLIST.unlink()
        ok(f"removed {PLIST}", False, None)
    rc = su.rc_file()
    if rc and su.remove_aliases(rc):
        ok(f"removed the alias block from {su.tilde(str(rc))}", False, None)
    for n in su.AGENTS:
        if rc and su.aliased(rc, n):
            out.print(Text(f"! {su.tilde(str(rc))} still aliases {n} outside agentctl's "
                           "block; remove that line yourself", style="yellow"))  # fmt: skip
    out.print(Text("Left in place: ~/.agentctl (state, logs, backups) and agents running in "
                   "agentctl's tmux. Remove the program with: uv tool uninstall agentctl",
                   style="dim"))  # fmt: skip


# --- hooks ---


def _exe() -> str:
    return shlex.quote(str(Path(sys.argv[0]).absolute()))


@hooks_app.command("install")
def hooks_install(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Don't ask for confirmation.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Add agentctl hooks to ~/.claude/settings.json (backed up first)."""
    _, added = hooks.planned(_exe())
    if not added:
        ok("hooks already installed", as_json, {"added": []})
        return
    if not as_json:
        body = Text()
        body.append("file     ", style="dim").append(str(hooks.SETTINGS) + "\n")
        body.append("command  ", style="dim").append(f"{_exe()} hook <event>\n")
        body.append("events   ", style="dim").append(", ".join(added), style="bold")
        body.append("\n\nA backup is saved first; `agentctl hooks uninstall` restores it.")
        out.print(Panel(body, title=" hooks install ", title_align="left", border_style="blue"))
    if not yes and not typer.confirm("Write these hooks?"):
        raise typer.Exit(1)
    backup = hooks.install(_exe())
    ok(
        f"installed {len(added)} hooks; backup at {backup}",
        as_json,
        {"added": added, "backup": str(backup)},
    )


@hooks_app.command("uninstall")
def hooks_uninstall(as_json: JsonOpt = False) -> None:
    """Remove agentctl hooks, restoring the backup byte-for-byte when possible."""
    msg = hooks.uninstall()
    ok(msg, as_json, {"result": msg})


@hooks_app.command("status")
def hooks_status(as_json: JsonOpt = False) -> None:
    st = hooks.status()
    if as_json:
        print(json.dumps(st))
        return
    t = Table(box=None, show_header=False)
    for ev, cmd in st.items():
        t.add_row(Text("ok", style="green") if cmd else Text("x", style="red"), ev)
    out.print(t)
    cmds = {c.rsplit(" hook ", 1)[0] for c in st.values() if c}
    for c in cmds:
        out.print(Text(f"command: {c} hook <event>", style="dim"))


# --- daemon ---


@daemon_app.command("start")
def daemon_start(as_json: JsonOpt = False) -> None:
    """Start agentctld in the background (other commands do this automatically)."""
    info = rpc("ping", as_json)
    ok(f"agentctld running (pid {info['pid']})", as_json, info)


@daemon_app.command("stop")
def daemon_stop(
    force: Annotated[bool, typer.Option(help="Stop even if a Codex turn is running.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Stop agentctld. Refuses while a Codex turn started with `send` is running."""
    try:
        call("shutdown", autostart=False, force=force)
        ok("agentctld stopped", as_json, {"stopped": True})
    except DaemonError as e:
        if "not running" in str(e):
            ok(str(e), as_json, {"stopped": False})
        else:
            fail(str(e), as_json)


@daemon_app.command("status")
def daemon_status(as_json: JsonOpt = False) -> None:
    try:
        info = call("ping", autostart=False)
    except DaemonError:
        info = None
    if as_json:
        print(json.dumps({"running": info is not None, **(info or {})}))
    elif info:
        ok(f"running (pid {info['pid']})", False, info)
    else:
        out.print(Text("○ not running", style="dim"))
    raise typer.Exit(0 if info else 1)


def restart_daemon(force: bool = False) -> dict[str, Any]:
    """Wait for the old listener to leave before starting its replacement."""
    try:
        previous = call("ping", autostart=False)
    except DaemonError:
        return dict(call("ping"))
    # Keep the daemon's active-turn safeguard; a refused shutdown must not start a replacement.
    call("shutdown", autostart=False, force=force)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            current = call("ping", autostart=False)
        except DaemonError:
            return dict(call("ping"))
        if current["pid"] != previous["pid"]:
            return dict(current)
        time.sleep(0.1)
    raise DaemonError("daemon has not stopped yet; check agentctl daemon status")


@daemon_app.command("restart")
def daemon_restart(
    force: Annotated[bool, typer.Option(help="Restart even if a Codex turn is running.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Reload daemon code. Refuses while a daemon-owned Codex turn is running."""
    try:
        info = restart_daemon(force)
    except DaemonError as e:
        fail(str(e), as_json)
    ok(f"agentctld running with refreshed code (pid {info['pid']})", as_json, info)


@daemon_app.command("install")
def daemon_install(as_json: JsonOpt = False) -> None:
    """Run agentctld from launchd, starting at login.

    It runs in your desktop session, so agents it launches can read the Keychain (your Claude
    login) even when you're on SSH/mosh."""
    info = install_daemon(as_json)
    ok(f"installed {PLIST}; agentctld running under launchd (pid {info['pid']})", as_json, info)


def install_daemon(as_json: bool) -> dict[str, Any]:
    import plistlib

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "agentctl.daemon"],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},  # restart on crash, not after `daemon stop`
        "ProcessType": "Interactive",
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        "StandardOutPath": str(LOG_DIR / "launchd.log"),
        "StandardErrorPath": str(LOG_DIR / "launchd.log"),
    }
    try:
        call("shutdown", autostart=False)  # hand over from a hand-started daemon
        time.sleep(0.5)
    except DaemonError as e:
        if "not running" not in str(e):  # e.g. a Codex turn would be aborted
            fail(str(e), as_json)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps(plist))
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", f"{domain}/{LABEL}")  # idempotent reinstall
    r = _launchctl("bootstrap", domain, str(PLIST))
    if r.returncode:
        fail(f"launchctl bootstrap failed: {r.stderr.strip()}", as_json)
    return dict(call("ping"))


@daemon_app.command("uninstall")
def daemon_uninstall(as_json: JsonOpt = False) -> None:
    """Remove the launchd agent (stops the daemon; other commands start it again by hand)."""
    _launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    existed = PLIST.exists()
    PLIST.unlink(missing_ok=True)
    ok(f"removed {PLIST}" if existed else "not installed", as_json, {"removed": existed})


@daemon_app.command("run", hidden=True)
def daemon_run(debug: Annotated[bool, typer.Option("--debug")] = False) -> None:
    """Run agentctld in the foreground."""
    import asyncio

    from agentctl.daemon.server import main

    asyncio.run(main(debug=debug))


# --- approval rules ---

RULES_HELP = """\
A rule answers matching permission prompts for you, in sessions agentctl controls:
  agentctl rules add "allow Read*"
  agentctl rules add "allow Bash: npm test*" --in ~/src/api
  agentctl rules add "deny Bash: rm -rf *"
Patterns are globs matched against the request as `ls` shows it. Deny rules win. Questions
are never answered by a rule. Each answer is noted in the session's transcript."""


def _print_rules(rules: list[str]) -> None:
    if not rules:
        out.print(Text("no approval rules: every prompt waits for you", style="dim"))
        out.print(Text(RULES_HELP, style="dim"))
        return
    out.print(
        Text().append(f"{len(rules)} approval rule{'s' if len(rules) != 1 else ''}", style="bold")
        + Text("  · deny rules win · each answer is noted in the transcript", ui.DIM)
    )
    for i, r in enumerate(rules, 1):
        action, _, rest = r.partition(" ")
        pattern, _, folder = rest.partition("  in ")
        style = "bold red" if action == "deny" else "bold green"
        line = Text().append(f"  {i:>2}  ", style=ui.DIM) + Text(f"{action:<5}", style=style)
        line.append(f" {pattern.strip()}")
        if folder:
            line.append(f"  in {ui.tilde(folder.strip())}", style=ui.DIM)
        out.print(line)
    out.print()
    out.print(ui.hint('agentctl rules add "allow …"', "agentctl rules rm N", "agentctl rules test"))


@rules_app.callback()
def rules_main(ctx: typer.Context) -> None:
    """Approval rules: answer matching permission prompts for you.

    Changing them is for you only: the daemon refuses rule changes from agents."""
    if ctx.invoked_subcommand is None:
        _print_rules(rpc("rules", False, op="list")["rules"])


@rules_app.command("ls")
def rules_ls(as_json: JsonOpt = False) -> None:
    """List the rules, numbered."""
    r = rpc("rules", as_json, op="list")
    if as_json:
        print(json.dumps(r))
    else:
        _print_rules(r["rules"])


@rules_app.command("add")
def rules_add(
    rule: Annotated[str, typer.Argument(help='e.g. "allow Read*" or "deny Bash: rm *"')],
    in_: Annotated[
        Path | None, typer.Option("--in", help="Only for sessions in this folder (or below).")
    ] = None,
    as_json: JsonOpt = False,
) -> None:
    """Add a rule."""
    line = rule + (f" in {in_.expanduser().resolve()}" if in_ else "")
    r = rpc("rules", as_json, op="add", rule=line)
    ok(f"{len(r['rules'])} rule{'s' if len(r['rules']) != 1 else ''}; added: {line}", as_json, r)


@rules_app.command("rm")
def rules_rm(
    index: Annotated[int, typer.Argument(help="Its number in `agentctl rules`.")],
    as_json: JsonOpt = False,
) -> None:
    """Remove a rule by number."""
    r = rpc("rules", as_json, op="remove", index=index)
    ok(f"removed rule {index}; {len(r['rules'])} left", as_json, r)


@rules_app.command("test")
def rules_test(
    request: Annotated[str, typer.Argument(help='A request as `ls` shows it: "Bash: npm test"')],
    in_: Annotated[Path, typer.Option("--in", help="The session's folder.")] = Path("."),
    as_json: JsonOpt = False,
) -> None:
    """Which rule, if any, would answer this request."""
    r = rpc("rules", as_json, op="test", tool=request, cwd=str(in_.expanduser().resolve()))
    if as_json:
        print(json.dumps(r))
    elif r["decided_by"]:
        style = "red" if r["decided_by"].startswith("deny") else "green"
        out.print(Text("answered by: ") + Text(r["decided_by"], style=style))
    else:
        out.print(Text("no rule matches: it would wait for you", style="dim"))


# --- help layout: group the commands in `agentctl --help` ---

HELP_PANELS = {
    "Sessions": (
        "ls", "tail", "send", "wait", "approve", "deny", "interrupt", "attach", "rename", "close",
    ),
    "Start agents": ("new", "claude", "codex", "opencode", "adopt"),
    "Catch up": ("watch", "digest", "queue", "usage"),
    "Coordinate": ("teams", "mcp", "rules"),
    "Setup": ("setup", "doctor", "daemon", "hooks", "uninstall"),
}  # fmt: skip


def _group_help() -> None:
    panel = {name: title for title, names in HELP_PANELS.items() for name in names}
    for c in app.registered_commands:
        name = c.name or (c.callback.__name__ if c.callback else "")
        c.rich_help_panel = panel.get(name, "Other")
    for g in app.registered_groups:
        g.rich_help_panel = panel.get(g.name or "", "Other")


_group_help()
