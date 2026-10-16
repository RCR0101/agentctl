import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentctl import hooks, tmux
from agentctl.adapters.claude_code import ClaudeCodeAdapter
from agentctl.daemon.bus import Bus
from agentctl.daemon.registry import Registry, Store

REPL = "import sys\nfor line in sys.stdin:\n    print('GOT:' + line.rstrip('\\n'), flush=True)\n"


@pytest.fixture
def server(tmp_path: Path):  # type: ignore[no-untyped-def]
    # private server, like `tmux -L agentctl-test`; short path (sun_path is 104 bytes on macOS)
    sock = f"/tmp/agentctl-test-{os.getpid()}.sock"
    (tmp_path / "repl.py").write_text(REPL)
    pane = subprocess.run(
        [tmux.tmux_bin(), "-S", sock, "-f", "/dev/null", "new-session", "-d", "-P", "-F",
         "#{pane_id}", "-x", "120", "-y", "40", "--", sys.executable, str(tmp_path / "repl.py")],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    yield sock, pane
    subprocess.run([tmux.tmux_bin(), "-S", sock, "kill-server"], check=False)
    Path(sock).unlink(missing_ok=True)


def screen_after(sock: str, pane: str, want: str) -> str:
    s = ""
    for _ in range(50):
        s = asyncio.run(tmux.capture(sock, pane))
        if want in s:
            return s
        time.sleep(0.05)
    return s


def test_send_single_and_multiline(server: tuple[str, str]) -> None:
    sock, pane = server
    asyncio.run(tmux.send_text(sock, pane, "-starts with dash; $(echo no) 'quotes'", clear=False))
    s = screen_after(sock, pane, "GOT:-starts")
    assert "GOT:-starts with dash; $(echo no) 'quotes'" in s
    asyncio.run(tmux.send_text(sock, pane, "first line\nsecond line", clear=False))
    s = screen_after(sock, pane, "GOT:second line")
    assert "GOT:first line" in s and "GOT:second line" in s


def test_pane_pid_and_descendant(server: tuple[str, str]) -> None:
    sock, pane = server
    root = asyncio.run(tmux.pane_pid(sock, pane))
    assert root and asyncio.run(tmux.is_descendant(root, root))
    assert not asyncio.run(tmux.is_descendant(os.getpid(), root))
    assert asyncio.run(tmux.pane_pid(sock, "%999")) is None


def test_parse_options() -> None:
    screen = """⏺ Here is a plan:
  1. read files
  2. edit
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and always allow access to /private/tmp/claude-501/-Users-me-Documents-projects-agen
      tctl/scratchpad/proj from this project
   3. Yes, and switch to auto mode · auto mode handles these prompts for you
   4. No
 Esc to cancel · Tab to amend"""
    opts = tmux.parse_options(screen)
    assert opts["1"] == "Yes" and opts["4"] == "No" and len(opts) == 4
    assert opts["2"].endswith("-agen tctl/scratchpad/proj from this project")


def test_hooks_install_uninstall_byte_identical(tmp_path: Path) -> None:
    settings, record = tmp_path / "settings.json", tmp_path / "rec.json"
    original = b'{\n    "model": "opus",\n    "hooks": {"Stop": [{"hooks": []}]}\n}'  # odd format
    settings.write_bytes(original)
    assert hooks.install("/bin/agentctl", settings, record)
    data = json.loads(settings.read_text())
    assert len(data["hooks"]["Stop"]) == 2 and set(hooks.EVENTS) <= set(data["hooks"])
    assert hooks.install("/bin/agentctl", settings, record) is None  # idempotent
    assert hooks.install("/new/agentctl", settings, record)  # reinstalled elsewhere: re-point
    data = json.loads(settings.read_text())
    assert data["hooks"]["Stop"][1]["hooks"][0]["command"] == "/new/agentctl hook Stop"
    assert len(data["hooks"]["Stop"]) == 2
    hooks.uninstall(settings, record)
    assert settings.read_bytes() == original  # still the pre-agentctl bytes


def test_hooks_uninstall_after_user_edit(tmp_path: Path) -> None:
    settings, record = tmp_path / "settings.json", tmp_path / "rec.json"
    settings.write_text('{\n  "model": "opus"\n}\n')
    hooks.install("/bin/agentctl", settings, record)
    data = json.loads(settings.read_text())
    data["theme"] = "dark"  # user changes something after install
    settings.write_text(json.dumps(data, indent=2) + "\n")
    hooks.install("/new/agentctl", settings, record)  # re-point after the edit
    hooks.uninstall(settings, record)
    assert settings.read_text() == '{\n  "model": "opus",\n  "theme": "dark"\n}\n'


def test_hook_fast_path_daemon_down(tmp_path: Path) -> None:
    env = os.environ | {"HOME": str(tmp_path)}  # no socket there
    t = time.monotonic()
    r = subprocess.run(
        [sys.executable, "-m", "agentctl", "hook", "Stop"],
        input=b'{"session_id": "x"}', env=env, capture_output=True,
    )  # fmt: skip
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b""
    assert time.monotonic() - t < 0.5  # ~30 ms in practice; generous for CI


def test_hooks_drive_status_and_pane(tmp_path: Path) -> None:
    root = tmp_path / ".claude"
    (root / "projects").mkdir(parents=True)
    (root / "sessions").mkdir()
    store = Store(str(tmp_path / "state.db"))
    reg = Registry(Bus())
    a = ClaudeCodeAdapter(reg, root=root, store=store)
    base = {"session_id": "s1", "cwd": "/w"}
    a.on_hook("SessionStart", base, "%7", "/tmp/tmux-501/default,1,0")
    s = reg.sessions["s1"]
    s.origin = "desktop"  # hook-only liveness applies to sessions without PID files
    perm = base | {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    a.on_hook("PermissionRequest", perm, None, None)
    assert s.status == "awaiting_approval" and s.pending_approval
    assert s.pending_approval.tool == "Bash: ls"
    a.on_hook("PostToolUse", base, None, None)
    assert s.status == "working"
    a.on_hook("Stop", base, None, None)
    assert s.status == "awaiting_input"
    assert not s.controllable  # no live claude process
    s.origin = "cli"  # SessionEnd ends a session whatever its origin
    a.on_hook("SessionEnd", base | {"reason": "prompt_input_exit"}, None, None)
    assert s.status == "ended"
    # pane mapping persisted across restarts
    b = ClaudeCodeAdapter(Registry(Bus()), root=root, store=Store(str(tmp_path / "state.db")))
    assert b.state["s1"]["pane"] == "%7" and b.state["s1"]["socket"] == "/tmp/tmux-501/default"
    assert b.target("s1") == (None, None)  # a remembered pane alone doesn't make it controllable


def test_two_processes_one_session(tmp_path: Path) -> None:
    """`adopt --force` of a live session: control goes to the tmux process only once its own
    PID file says it's in the pane; a registered-but-unconfirmed pane is not controllable."""
    root = tmp_path / ".claude"
    (root / "projects").mkdir(parents=True)
    (root / "sessions").mkdir()
    reg = Registry(Bus())
    a = ClaudeCodeAdapter(reg, root=root)
    base = {"sessionId": "s1", "cwd": "/w", "entrypoint": "cli", "status": "busy"}
    (root / "sessions" / "1.json").write_text(json.dumps(base | {"pid": os.getpid()}))
    a.scan_pids()
    a.register_pane("s1", "/tmp/sock", "%1")  # adopt opened a pane; it's at a trust prompt
    s = reg.sessions["s1"]
    assert not s.controllable and s.tmux_pane is None
    tmux_proc = base | {
        "pid": os.getppid(),
        "tmux": "agents:@1.%1",
        "status": "idle",
        "updatedAt": 1,
    }
    (root / "sessions" / "2.json").write_text(json.dumps(tmux_proc))
    a.scan_pids()
    assert s.controllable and a.target("s1") == ("/tmp/sock", "%1")


def test_launcher_own_server_one_session_per_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentctl import launcher

    name = f"agentctl-test-{os.getpid()}"
    monkeypatch.setenv("AGENTCTL_TMUX_SOCKET", name)
    monkeypatch.setattr(launcher, "CONF", tmp_path / "tmux.conf")
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\necho "ARGS: $*"\nexec sleep 30\n')
    fake.chmod(0o755)
    path = f"{fake.parent}:{os.path.dirname(tmux.tmux_bin())}:/usr/bin:/bin"
    try:
        r = launcher.launch("claude", str(tmp_path), "demo", ["--model", "haiku"], path)
        assert r["socket"] == launcher.socket_path() and r["tmux_session"].startswith(
            "claude-demo-"
        )
        r2 = launcher.launch("claude", str(tmp_path), None, [], path, resume="abc")
        assert r2["tmux_session"] != r["tmux_session"]  # one tmux session per agent
        screen = screen_after(r["socket"], r["pane"], "ARGS:")
        assert f"ARGS: --session-id {r['session_id']} --name demo --model haiku" in screen
        assert "ARGS: --resume abc" in screen_after(r2["socket"], r2["pane"], "ARGS:")
        opts = launcher.tmux("show-options", "-s", "escape-time")  # Esc must reach the agent fast
        assert opts.endswith(" 10")
        fake.write_text('#!/bin/sh\necho "error: unexpected argument --bogus" >&2\nexit 2\n')
        with pytest.raises(launcher.LaunchError, match="unexpected argument --bogus"):
            launcher.launch("claude", str(tmp_path), None, ["--bogus"], path)
    finally:
        subprocess.run([tmux.tmux_bin(), "-L", name, "kill-server"], check=False)
        Path(launcher.socket_path()).unlink(missing_ok=True)


def test_crashed_watcher_is_restarted() -> None:
    from agentctl.daemon.server import supervise

    calls = []

    async def watch() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise IndexError("a bug in one tick")

    asyncio.run(supervise("test", watch))
    assert len(calls) == 2


def test_adopt_never_stops_a_non_terminal_process() -> None:
    from agentctl import cli

    s = {"id": "s1", "status": "awaiting_input"}
    with pytest.raises(cli.AdoptError, match="isn't running in a terminal"):
        cli.check_holder(s, 1, force=True)  # launchd: no tty, not an agent
    assert cli._alive(1)


def test_titles_persist(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "state.db"))
    store.set_title("s1", "deploy fix")
    assert Store(str(tmp_path / "state.db")).titles() == {"s1": "deploy fix"}
    store.set_title("s1", None)
    assert store.titles() == {}


def test_statusline_wrapped_and_restored(tmp_path: Path) -> None:
    settings, record = tmp_path / "settings.json", tmp_path / "rec.json"
    original = (
        b'{\n  "statusLine": {"type": "command", "command": "bash ~/my-line.sh", "padding": 1}\n}'
    )
    settings.write_bytes(original)
    hooks.install("/bin/agentctl", settings, record)
    sl = json.loads(settings.read_text())["statusLine"]
    assert sl == {"type": "command", "command": "/bin/agentctl statusline", "padding": 1}
    saved = json.loads((tmp_path / hooks.STATUSLINE_REC).read_text())
    assert saved == {"command": "bash ~/my-line.sh"}
    hooks.install("/new/agentctl", settings, record)  # re-point keeps your original
    assert json.loads((tmp_path / hooks.STATUSLINE_REC).read_text()) == saved
    data = json.loads(settings.read_text())
    data["theme"] = "dark"  # edited since: uninstall can't restore bytes, unwraps instead
    settings.write_text(json.dumps(data))
    hooks.uninstall(settings, record)
    after = json.loads(settings.read_text())
    assert after["statusLine"]["command"] == "bash ~/my-line.sh" and after["theme"] == "dark"
    assert "hooks" not in after and not (tmp_path / hooks.STATUSLINE_REC).exists()


def test_statusline_fast_path_runs_your_command(tmp_path: Path) -> None:
    (tmp_path / ".agentctl").mkdir()
    (tmp_path / ".agentctl" / hooks.STATUSLINE_REC).write_text(
        json.dumps({"command": "tr a-z A-Z; exit 3"})
    )
    env = os.environ | {"HOME": str(tmp_path)}  # no daemon there: forwarding is skipped
    t = time.monotonic()
    r = subprocess.run(
        [sys.executable, "-m", "agentctl", "statusline"],
        input=b'{"session_id": "s1", "model": {"display_name": "opus"}}', env=env,
        capture_output=True,
    )  # fmt: skip
    assert r.stdout == b'{"SESSION_ID": "S1", "MODEL": {"DISPLAY_NAME": "OPUS"}}'
    assert r.returncode == 3  # your command's exit code passes through
    assert time.monotonic() - t < 1.0


def test_statusline_gives_exact_numbers_and_limits(tmp_path: Path) -> None:
    root = tmp_path / ".claude"
    (root / "projects").mkdir(parents=True)
    (root / "sessions").mkdir()
    reg = Registry(Bus())
    a = ClaudeCodeAdapter(reg, root=root)
    a.ensure_session("s1", "/w")
    soon = int(time.time()) + 3600
    a.on_statusline({
        "session_id": "s1",
        "context_window": {"context_window_size": 1_000_000},
        "prompt_cache": {"expires_at": soon},
        "rate_limits": {"five_hour": {"used_percentage": 41.5, "resets_at": soon},
                        "seven_day": {"used_percentage": 12, "resets_at": 1}},
    })  # fmt: skip
    s = reg.sessions["s1"]
    assert s.context_window == 1_000_000 and s.context_window_exact
    assert s.cache_expires_at and int(s.cache_expires_at.timestamp()) == soon
    u = a.usage()
    assert u and [(w["name"], w["used_percent"]) for w in u["windows"]] == [
        ("5h", 41.5),
        ("week", 0.0),  # its reset time has passed
    ]


def test_claude_limits_survive_a_restart(tmp_path: Path) -> None:
    root = tmp_path / ".claude"
    (root / "projects").mkdir(parents=True)
    (root / "sessions").mkdir()
    db = str(tmp_path / "state.db")
    soon = int(time.time()) + 3600
    a = ClaudeCodeAdapter(Registry(Bus()), root=root, store=Store(db))
    a.on_statusline({"rate_limits": {"five_hour": {"used_percentage": 18, "resets_at": soon}}})
    b = ClaudeCodeAdapter(Registry(Bus()), root=root, store=Store(db))  # daemon restarted
    u = b.usage()
    assert u and u["windows"][0]["used_percent"] == 18
