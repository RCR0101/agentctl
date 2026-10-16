"""Start agents in agentctl's own tmux server (`tmux -L agentctl`), one tmux session per agent.

This runs inside the daemon, so agents inherit the daemon's login context. Under launchd that is
the GUI session, which can read the login Keychain (Claude's subscription login). A tmux server
started from an SSH/mosh shell can't, and Claude then shows "API Usage Billing". Commands go to
tmux as argv, never through a shell.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from agentctl import HOME, STATE_DIR

CONF = STATE_DIR / "tmux.conf"


def tmux_conf() -> str:
    # The dashboard command for the popup: this Python, so it works whatever PATH tmux has.
    watch = shlex.quote(f"{shlex.quote(sys.executable)} -m agentctl watch")
    return f"""\
# agentctl's private tmux server. Written by agentctl; edits are overwritten.
# one dim line, so it's clear how to get back out of an agent
set -g status on
set -g status-style "fg=colour244,bg=default"
set -g status-left ""
set -g window-status-format ""
set -g window-status-current-format ""
set -g status-right "ctrl-] d back · ctrl-] w all sessions "
set -g status-right-length 60
set -g prefix C-]
unbind C-b
bind C-] send-prefix
bind d detach-client
# C-] w: the agentctl dashboard over the current agent; q or Ctrl-C closes it
bind w display-popup -E -w 100% -h 100% {watch}
set -s escape-time 10
set -s set-clipboard on
set -g focus-events on
set -g mouse on
set -g history-limit 50000
set -g remain-on-exit failed
set -g default-terminal "screen-256color"
set -as terminal-features ",*:RGB"
"""


class LaunchError(Exception):
    pass


def socket_name() -> str:
    return os.environ.get("AGENTCTL_TMUX_SOCKET", "agentctl")


def socket_path() -> str:
    tmpdir = os.environ.get("TMUX_TMPDIR", "/private/tmp")
    return f"{tmpdir}/tmux-{os.getuid()}/{socket_name()}"


def need(tool: str, hint: str, path: str | None = None) -> str:
    found = shutil.which(tool, path=path)
    if not found:
        raise LaunchError(f"{tool} is required but not on PATH ({hint})")
    return found


# The Codex desktop app ships its own, usually newer, CLI. Threads it writes can use item types an
# older Homebrew `codex` can't read ("unknown variant"), so always use the newest one installed.
BUNDLED_CODEX = (
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
)
_versions: dict[str, tuple[int, ...]] = {}


def _version(exe: str) -> tuple[int, ...]:
    if exe not in _versions:
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
            m = re.search(r"(\d+)\.(\d+)\.(\d+)", out.stdout)
            _versions[exe] = tuple(int(x) for x in m.groups()) if m else ()
        except (OSError, subprocess.TimeoutExpired):
            _versions[exe] = ()
    return _versions[exe]


def no_daemon_flag(exe: str) -> list[str]:
    """Codex >= 0.16x runs TUI threads inside its shared background server when that's running,
    so the thread's holder is that server, not the TUI in our pane. `--no-daemon` keeps the
    thread in the TUI process, where agentctl can find and control it."""
    if exe not in _no_daemon:
        try:
            out = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=10)
            _no_daemon[exe] = "--no-daemon" in out.stdout
        except (OSError, subprocess.TimeoutExpired):
            _no_daemon[exe] = False
    return ["--no-daemon"] if _no_daemon[exe] else []


_no_daemon: dict[str, bool] = {}


def best_codex(path: str | None = None) -> str | None:
    found = [
        c for c in (shutil.which("codex", path=path), BUNDLED_CODEX) if c and os.access(c, os.X_OK)
    ]
    return max(found, key=_version, default=None)


# opencode merges $OPENCODE_CONFIG into your own config, so this loads agentctl's plugin into
# the opencodes agentctl launches without touching ~/.config/opencode.
OPENCODE_CONF = STATE_DIR / "opencode.json"
PLUGIN = Path(__file__).with_name("opencode_plugin.js")


def opencode_config() -> str:
    conf = json.dumps({"plugin": [PLUGIN.as_uri()]}, indent=2) + "\n"
    if not OPENCODE_CONF.exists() or OPENCODE_CONF.read_text() != conf:
        OPENCODE_CONF.write_text(conf)
    return str(OPENCODE_CONF)


def tmux(*args: str, path: str | None = None) -> str:
    exe = need("tmux", "brew install tmux", path)
    conf = tmux_conf()
    if not CONF.exists() or CONF.read_text() != conf:
        CONF.write_text(conf)
        # -f only applies when the server starts; push changes into a running one too
        subprocess.run([exe, "-L", socket_name(), "source-file", str(CONF)], capture_output=True)
    r = subprocess.run(
        [exe, "-L", socket_name(), "-f", str(CONF), *args], capture_output=True, text=True
    )
    if r.returncode:
        raise LaunchError(f"tmux {args[0]}: {r.stderr.strip()}")
    return r.stdout.strip()


def clean_env_prefix() -> list[str]:
    """`env -u ...` for Claude Code's per-session variables. If the daemon was started from
    inside a Claude Code session they would leak into agents and confuse their auth."""
    leaked = sorted(k for k in os.environ if k.startswith("CLAUDE_CODE_") or k == "CLAUDECODE")
    return ["/usr/bin/env", *(f"-u{k}" for k in leaked)] if leaked else []


def launch(
    agent: str,
    cwd: str,
    name: str | None,
    extra: list[str],
    path: str,
    resume: str | None = None,
) -> dict[str, Any]:
    """New tmux session running the agent. Claude gets a pre-assigned --session-id (or
    --resume, which keeps the id), so its pane is known before the first hook fires."""
    if not os.path.isdir(cwd):
        raise LaunchError(f"not a directory: {cwd}")
    sid, env = None, [f"PATH={path}"]
    if agent == "claude":
        sid = resume or str(uuid.uuid4())
        exe = need("claude", "install Claude Code", path)
        args = ["--resume", sid] if resume else ["--session-id", sid]
        args += ["--name", name] if name and not resume else []
    elif agent == "codex":
        exe = best_codex(path) or need("codex", "install Codex", path)
        sid, args = resume, no_daemon_flag(exe) + (["resume", resume] if resume else [])
    elif agent == "opencode":
        exe = shutil.which("opencode", path=path) or str(HOME / ".opencode/bin/opencode")
        if not os.access(exe, os.X_OK):
            raise LaunchError("opencode is required but not on PATH (install opencode)")
        sid, args = resume, ["--session", resume] if resume else []
        env.append(f"OPENCODE_CONFIG={opencode_config()}")
    else:
        raise LaunchError("agent must be claude, codex or opencode")
    label = re.sub(r"[^A-Za-z0-9_-]", "-", f"{agent}-{name or os.path.basename(cwd)}")[:30]
    label = f"{label}-{uuid.uuid4().hex[:4]}"
    out = tmux(
        "new-session", "-d", "-s", label, "-c", cwd, *(f"-e{e}" for e in env),
        "-P", "-F", "#{pane_id} #{socket_path}",
        "--", *clean_env_prefix(), exe, *args, *extra,
        path=path,
    )  # fmt: skip
    pane, sock = out.split(" ", 1)
    for _ in range(8):  # an agent that dies at once (bad flag, …) stays visible: report it
        time.sleep(0.15)
        if tmux("display-message", "-p", "-t", pane, "#{pane_dead}", path=path) == "1":
            time.sleep(0.3)  # its last output can land just after tmux marks the pane dead
            screen = [ln for ln in capture(sock, pane).splitlines() if ln.strip()]
            tmux("kill-session", "-t", label, path=path)
            tail = "\n".join(ln for ln in screen if not ln.startswith("Pane is dead"))[-600:]
            raise LaunchError(f"{agent} exited right away:\n{tail}")
    return {"session_id": sid, "tmux_session": label, "pane": pane, "socket": sock}


def capture(sock: str, pane: str) -> str:
    exe = shutil.which("tmux") or "tmux"
    cmd = [exe, "-S", sock, "capture-pane", "-p", "-J", "-S", "-", "-t", pane]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""
