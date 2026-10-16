"""Thin client: every command is one JSON-RPC call to agentctld over the Unix socket."""

import contextlib
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agentctl import HOME, LOG_DIR, SOCKET_PATH, hooks, launcher
from agentctl.models import short_id
from agentctl.setup import LABEL, PLIST

app = typer.Typer(no_args_is_help=True, add_completion=False, rich_markup_mode="rich")
daemon_app = typer.Typer(no_args_is_help=True, help="Manage the agentctld background daemon.")
hooks_app = typer.Typer(no_args_is_help=True, help="Manage Claude Code hooks in settings.json.")
app.add_typer(daemon_app, name="daemon")
app.add_typer(hooks_app, name="hooks")
out = Console(highlight=False)
err = Console(stderr=True, highlight=False)

JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]
IdArg = Annotated[str, typer.Argument(help="Session id or unique prefix.")]
SendIdArg = Annotated[
    str, typer.Argument(help="Session id or prefix, or the pane id (%N) `new` printed.")
]

STATUS_STYLE = {
    "awaiting_approval": "bold black on yellow",
    "awaiting_input": "bold cyan",
    "working": "green",
    "idle": "dim",
    "ended": "dim",
}
SRC = {"claude_code": "cc", "codex": "cx", "opencode": "oc"}
SOURCES = {"claude": "claude_code", "codex": "codex", "opencode": "opencode"}
STATUS_SHORT = {
    "awaiting_approval": "APPROVE",
    "awaiting_input": "input",
    "working": "working",
    "idle": "idle",
    "ended": "ended",
}


class DaemonError(Exception):
    pass


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
        raise DaemonError(reply["error"]["message"])
    return reply.get("result")


def call(method: str, autostart: bool = True, **params: Any) -> Any:
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
        fail(str(e), as_json)


def fail(msg: str, as_json: bool) -> NoReturn:
    if as_json:
        print(json.dumps({"error": msg}))
    else:
        err.print(Text("✗ ", style="bold red") + Text(msg))
    raise typer.Exit(1)


def ok(msg: str, as_json: bool, result: Any) -> None:
    if as_json:
        print(json.dumps(result))
    else:
        out.print(Text("✓ ", style="bold green") + Text(msg))


# --- formatting ---


def age(iso: str) -> str:
    secs = int((datetime.now(UTC) - datetime.fromisoformat(iso)).total_seconds())
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= n:
            return f"{secs // n}{unit}"
    return f"{max(secs, 0)}s"


def short_cwd(cwd: str) -> str:
    return "~" if cwd == str(HOME) else Path(cwd).name or cwd


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


def cache_text(s: dict[str, Any]) -> Text:
    """Time left before the prompt cache expires (Claude only; Codex doesn't expose it)."""
    exp = s.get("cache_expires_at")
    if not exp:
        return Text("")
    left = int((datetime.fromisoformat(exp) - datetime.now(UTC)).total_seconds())
    if left <= 0:
        return Text("expired", style="dim")
    m, sec = divmod(left, 60)
    return Text(f"{m}m" if m >= 5 else f"{m}m{sec:02d}s", style="yellow" if m < 5 else "green")


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
        title=Text(f" ⚠ {short} needs approval ", style="bold yellow"),
        title_align="left",
        border_style="yellow",
        expand=False,
    )


def session_notes(s: dict[str, Any]) -> list[Text]:
    """Lines about the session beyond its status: overlapping agents, queued follow-ups."""
    notes = [Text(f"⚠ {c}", style="yellow") for c in s.get("conflicts") or []]
    if s.get("queued"):
        n = len(s["queued"])
        t = Text(f"⏳ {n} follow-up{'s' if n > 1 else ''} queued for when it's free", style="cyan")
        if s.get("queue_error"):
            t.append(f" (can't send: {s['queue_error']})", style="red")
        notes.append(t)
    return notes


def compact_header(s: dict[str, Any]) -> Group:
    """Phone-width header: title, then status · source · control · context on one line."""
    title = Text(s["title"] or short_cwd(s["cwd"]), style="bold", no_wrap=True, overflow="ellipsis")
    meta = Text(no_wrap=True, overflow="ellipsis")
    meta.append_text(status_text(s["status"]))
    meta.append(f" {SRC[s['source']]} {short_id(s['id'])} ", style="dim")
    if s["tmux_pane"] or s["controllable"]:
        meta.append("● ", style="green")
    if s.get("context_tokens") is not None:
        meta.append_text(ctx_text(s, short=True)).append(" ")
    meta.append(short_cwd(s["cwd"]), style="blue")
    return Group(title, meta, *session_notes(s))


def session_header(s: dict[str, Any], compact: bool = False) -> Panel | Group:
    if compact:
        return compact_header(s)
    meta = Text()
    meta.append_text(status_text(s["status"])).append("  ")
    meta.append(f"{s['source']} · {s['origin']} · ", style="dim")
    if s["tmux_pane"]:
        meta.append("● controllable", style="green").append(" · in tmux", style="dim")
    elif s["controllable"]:
        meta.append("● controllable", style="green").append(" · via codex app-server", style="dim")
    else:
        meta.append("read-only", style="dim")
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
        Group(meta, *([usage] if usage.plain else []), path, *session_notes(s)),
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
        return Group(line.append("◆", style="bold green"), Markdown(p["text"]))
    elif kind == "message":
        line.append("◆ ", style="bold green").append(p["text"])
    elif kind == "tool_call":
        first, *rest = p["summary"].splitlines() or [""]
        more = " …" if rest else ""
        line.append(f"⚙ {p['name']} ", style="yellow").append(first[:160] + more, style="dim")
    elif kind == "tool_result":
        lines = p["text"].splitlines() or [""]
        style = "red" if p.get("is_error") else "dim"
        more = f"  (+{len(lines) - 1} lines)" if len(lines) > 1 else ""
        line.append("↳ ", style=style).append(lines[0][:200] + more, style=style)
    elif kind == "status_change":
        line.append("● ", style=STATUS_STYLE[p["to"]]).append_text(status_text(p["to"]))
    elif kind == "approval_request":
        line.append(f"⚠ approval asked: {p.get('tool') or '?'}", style="yellow")
    elif kind == "error":
        line.append(f"✗ {p.get('text', '')}", style="red")
    else:
        line.append(f"{kind} {json.dumps(p)[:200]}", style="dim")
    return line


# --- sessions ---


@app.command()
def ls(
    source: Annotated[str | None, typer.Option(help="claude, codex or opencode")] = None,
    status: Annotated[str | None, typer.Option(help="e.g. awaiting_approval")] = None,
    all_: Annotated[bool, typer.Option("--all", "-a", help="Include old ended sessions.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """List sessions, those needing attention first."""
    src = SOURCES.get(source or "", source)
    rows = rpc("ls", as_json, source=src, status=status, all=all_)
    if as_json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        out.print(Text("no sessions", style="dim"))
        return
    narrow = out.width < 70  # phone portrait: drop cwd and source, keep title
    t = Table(box=None, pad_edge=False, header_style="bold dim", expand=True)
    t.add_column("ID", no_wrap=True, min_width=8, style="bold")
    if not narrow:
        t.add_column("SRC", no_wrap=True, min_width=3)
    t.add_column("STATUS", no_wrap=True, min_width=7)
    t.add_column("AGE", no_wrap=True, justify="right", min_width=3, style="dim")
    if not narrow:
        t.add_column("CTX", no_wrap=True, justify="right")
        t.add_column("CACHE", no_wrap=True, justify="right")
        t.add_column("CWD", no_wrap=True, overflow="ellipsis", max_width=18, style="blue")
    t.add_column("TITLE", no_wrap=True, overflow="ellipsis", ratio=1)
    t.add_column("C", no_wrap=True, min_width=1)
    for r in rows:
        cells: list[str | Text] = [short_id(r["id"])]
        if not narrow:
            cells.append(SRC[r["source"]])
        cells += [status_text(r["status"]), age(r["last_activity"])]
        if not narrow:
            cells += [ctx_text(r, short=True), cache_text(r), short_cwd(r["cwd"])]
        title = Text()
        if r.get("unread"):
            title.append(f"{r['unread']} new ", style="bold cyan")
        if r.get("conflicts"):
            title.append("⚠ ", style="yellow")
        title.append(
            r["title"] or short_cwd(r["cwd"]), style="dim" if r["status"] == "ended" else ""
        )
        ctl = Text("●", style="green") if r["controllable"] else Text("·", style="dim")
        t.add_row(*cells, title, ctl)
    out.print(t)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary = Text()
    for st in ("awaiting_approval", "awaiting_input", "working"):
        if counts.get(st):
            summary.append(f"{counts[st]} {STATUS_SHORT[st]}  ", style=STATUS_STYLE[st])
    summary.append("● = controllable", style="dim")
    out.print(summary)
    for r in rows:
        for c in r.get("conflicts") or []:
            out.print(Text(f"⚠ {short_id(r['id'])} {c}", style="yellow"))
    for r in rows:
        if r["status"] == "awaiting_approval" and r["pending_approval"]:
            out.print(approval_panel(r["id"], r["pending_approval"], r["controllable"]))


@app.command()
def tail(
    id: IdArg,
    n: Annotated[int, typer.Option("-n", help="Number of recent events.")] = 50,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Keep streaming.")] = False,
    raw: Annotated[bool, typer.Option("--raw", help="Show replies as plain text.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Show a session's recent transcript, optionally live."""
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
        narrow = out.width < 70
        out.print(session_header(s, compact=narrow))
        for ev in first["events"]:
            out.print(render(ev, narrow=narrow, markdown=not raw))
        if s["status"] == "awaiting_approval" and s["pending_approval"]:
            out.print(approval_panel(s["id"], s["pending_approval"], s["controllable"]))
    if n:
        with contextlib.suppress(DaemonError):
            call("seen", id=s["id"])  # you've now read it
    try:
        for ev in it:
            if as_json:
                print(json.dumps(ev), flush=True)
            else:
                out.print(render(ev, live=True, narrow=out.width < 70, markdown=not raw))
    except KeyboardInterrupt:
        pass


@app.command()
def send(
    id: SendIdArg,
    message: Annotated[str, typer.Argument(help='Text to send, or "-" to read stdin.')],
    when_idle: Annotated[
        bool,
        typer.Option("--when-idle", "-l", help="Queue it; it's sent once the agent is free."),
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Type a message into a session and press Enter (or queue it for when it's free)."""
    text = sys.stdin.read().rstrip("\n") if message == "-" else message
    if not text.strip():
        fail("empty message", as_json)
    if when_idle:
        r = rpc("queue", as_json, id=id, text=text)
        n = len(r["queued"])
        ok(f"queued for {short_id(r['session_id'])} ({n} waiting)", as_json, r)
        return
    r = rpc("send", as_json, id=id, text=text)
    lines = text.count("\n") + 1
    ok(
        f"sent to {short_id(r['session_id'])}" + (f" ({lines} lines)" if lines > 1 else ""),
        as_json,
        r,
    )


@app.command()
def approve(
    id: IdArg,
    choice: Annotated[int | None, typer.Option(help="Pick option N instead of 1 (Yes).")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Answer a pending permission prompt (default: option 1, Yes)."""
    r = rpc("approve", as_json, id=id, choice=choice, allow=True)
    ok(f"{short_id(r['session_id'])} → {r['chosen']}", as_json, r)


@app.command()
def deny(id: IdArg, as_json: JsonOpt = False) -> None:
    """Reject a pending permission prompt."""
    r = rpc("approve", as_json, id=id, allow=False)
    ok(f"{short_id(r['session_id'])} → {r['chosen']}", as_json, r)


@app.command()
def interrupt(id: IdArg, as_json: JsonOpt = False) -> None:
    """Stop the current turn (sends Escape)."""
    r = rpc("interrupt", as_json, id=id)
    ok(f"interrupted {short_id(r['session_id'])}", as_json, r)


@app.command()
def attach(
    id: Annotated[str, typer.Argument(help="Session id/prefix, or a tmux pane id like %2.")],
) -> None:
    """Jump to the session's tmux pane (ctrl-] d detaches from agentctl's panes)."""
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
    body = Text()
    body.append("id      ", style="dim").append(ref, style="bold")
    answer = f"Answer it in `agentctl watch` (v shows its screen), or: agentctl attach {ref}"
    if state == "pending":
        body.append("\n\nSend its first message with:\n")
        body.append(f'agentctl send {ref} "…"', style="bold")
    elif state == "update":
        body.append("\n\nCodex is offering an update; pick Skip or update.\n", style="yellow")
        body.append(answer, style="bold")
    elif state == "trust":
        body.append("\n\nThe agent is asking whether to trust this folder.\n", style="yellow")
        body.append(answer, style="bold")
    elif state == "unknown":
        body.append("\n\nNot up yet; check with: ", style="yellow")
        body.append(f"agentctl attach {ref}", style="bold")
    else:
        body.append("\nstatus  ", style="dim").append_text(status_text(state))
    out.print(Panel(body, title=" ✓ launched ", title_align="left", border_style="green"))


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


# Undo what a TUI killed by a signal can leave on: kitty keyboard flags, bracketed paste,
# mouse reporting, hidden cursor. Unknown sequences are ignored by terminals.
TTY_RESET = "\x1b[<u\x1b[?2004l\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?25h"


def _proc(pid: int) -> tuple[str, str]:
    """(tty, command name) of a process; ("", "") if it's gone."""
    r = subprocess.run(["ps", "-o", "tty=,comm=", "-p", str(pid)], capture_output=True, text=True)
    tty, _, comm = r.stdout.strip().partition(" ")
    return tty, Path(comm.strip()).name


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
    tty, comm = _proc(pid)
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
        with contextlib.suppress(OSError):  # tell the old terminal where the session went
            with open(f"/dev/{plan.tty}", "w") as t:
                note = f"[agentctl] moved; open it with: agentctl attach {short}"
                t.write(f"{TTY_RESET}\r\n{note}\r\n")
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
    """Move a session into agentctl's tmux so it can be controlled: `claude --resume <id>`,
    `codex resume <id>` or `opencode -s <id>`. If it's open in another terminal, that process
    is stopped first (like Ctrl-C there; asks unless -y). Then `agentctl attach <id>`."""
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
    if not s["queued"]:
        out.print(Text("nothing queued", style="dim"))
    for i, t in enumerate(s["queued"], 1):
        out.print(Text(f"{i}. ", style="bold").append(t))
    if s.get("queue_error"):
        out.print(Text(f"can't send yet: {s['queue_error']}", style="red"))


@app.command()
def digest(as_json: JsonOpt = False) -> None:
    """What happened since you last looked: per session, new replies, files touched, and the
    start of its last reply. Opening a session (watch, or tail) marks it read."""
    rows = rpc("digest", as_json)
    if as_json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        out.print(Text("nothing new since you last looked", style="dim"))
    for r in rows:
        head = Text(f"{short_id(r['id'])} ", style="bold")
        head.append_text(status_text(r["status"])).append(f"  {r['title'] or ''}")
        body = Text(f"{r['new_replies']} new repl{'ies' if r['new_replies'] > 1 else 'y'}")
        if r["files"]:
            body.append(" · touched " + ", ".join(r["files"][:6]), style="blue")
            body.append(f" +{len(r['files']) - 6}" if len(r["files"]) > 6 else "")
        if r["last_reply"]:
            body.append("\n◆ ", style="green").append(r["last_reply"], style="dim")
        out.print(head)
        out.print(body, "")


def usage_text(u: dict[str, Any]) -> Text:
    """'cx 5h 15% · week 30%' for the header and `agentctl usage`."""
    t = Text()
    for key, label in (("claude", "cc"), ("codex", "cx")):
        if not u.get(key):
            continue
        t.append("  " if t else "").append(f"{label} ", style="dim")
        for i, w in enumerate(u[key]["windows"]):
            pct = round(w["used_percent"])
            style = "red" if pct >= 90 else "yellow" if pct >= 70 else ""
            t.append(" · " if i else "").append(f"{w['name']} ").append(f"{pct}%", style=style)
    return t


@app.command()
def usage(as_json: JsonOpt = False) -> None:
    """Plan usage limits: 5-hour and weekly windows. Claude Code reports them to its status line
    (agentctl wraps it); Codex writes them to its session files."""
    u = rpc("usage", as_json)
    if as_json:
        print(json.dumps(u, indent=2))
        return
    for key, name in (("claude", "Claude Code"), ("codex", "Codex")):
        lim = u.get(key)
        if not lim:
            how = "hooks install wraps its status line" if key == "claude" else "a Codex reply"
            out.print(Text(f"{name}: nothing reported yet (needs {how})", style="dim"))
            continue
        plan = f" ({lim['plan']})" if lim.get("plan") else ""
        out.print(Text(f"{name}{plan}", style="bold"))
        for w in lim["windows"]:
            when = datetime.fromtimestamp(w["resets_at"]).strftime("%a %H:%M")
            out.print(f"  {w['name']:<5} {round(w['used_percent']):>3}% used · resets {when}")
        out.print(Text(f"  as of {age(lim['at'])} ago", style="dim"))


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
    id: IdArg,
    force: Annotated[bool, typer.Option(help="Even mid-turn (aborts it).")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Quit a session's agent (like Ctrl-C in its terminal) and close agentctl's pane for it.
    The session stays resumable (`agentctl adopt <id>`)."""
    r = rpc("close", as_json, id=id, force=force)
    ok(f"closed {short_id(r['session_id'])}", as_json, r)


@app.command()
def watch(
    notify: Annotated[bool, typer.Option(help="macOS notification + bell on approval.")] = False,
) -> None:
    """Live dashboard. Works down to ~45 columns; `n` toggles notifications, `m` raw text."""
    rpc("ping", False)  # starts the daemon if needed
    from agentctl.tui import Watch

    Watch(notify=notify).run()


# --- setup ---

MARK = {"ok": ("✓", "green"), "warn": ("!", "yellow"), "fail": ("✗", "red"), "info": ("·", "dim")}


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
    """Set agentctl up, asking before each change: Claude hooks, the launchd daemon, shell
    aliases. Then checks phone access. Safe to re-run; `agentctl uninstall` undoes it."""
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
    out.print(Text("✓ tmux, agents: " + (", ".join(found) or "none found"), style="green"))
    if not found:
        fail("install claude, codex or opencode first", False)

    step(2, "Claude Code hooks")
    if "claude" not in found:
        out.print(Text("skipped: claude not installed", style="dim"))
    elif not hooks.planned(exe)[1]:
        out.print(Text("✓ installed", style="green"))
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
        out.print(Text(f"✓ running under launchd (pid {info['pid']})", style="green"))
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
            Text(f"✓ {', '.join(found)} already aliased in {su.tilde(str(rc))}", style="green")
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
    """Undo `agentctl setup`: remove the hooks (restoring settings.json), the launchd daemon
    and the alias block. Leaves ~/.agentctl and running agents alone."""
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
        t.add_row(Text("✓", style="green") if cmd else Text("✗", style="red"), ev)
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


@daemon_app.command("install")
def daemon_install(as_json: JsonOpt = False) -> None:
    """Run agentctld from launchd: starts at login, and in your desktop session, so agents
    it launches can read the Keychain (your Claude login) even when you're on SSH/mosh."""
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
