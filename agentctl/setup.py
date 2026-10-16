"""`agentctl doctor` checks and the shell-alias edits `agentctl setup` makes."""

import json
import os
import plistlib
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

import agentctl
from agentctl import HOME, SOCKET_PATH, STATE_DIR, hooks, launcher

LABEL = "dev.sam.agentctld"
PLIST = HOME / "Library" / "LaunchAgents" / f"{LABEL}.plist"
AGENTS = ("claude", "codex", "opencode")
BEGIN, END = "# >>> agentctl aliases >>>", "# <<< agentctl aliases <<<"
TAILSCALE_APP = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail | info
    detail: str
    fix: str = ""


def run(*cmd: str, timeout: float = 5) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def first_line(s: str | None) -> str:
    return (s or "").splitlines()[0] if s else "?"


# --- install ---


def install_info() -> tuple[str, str | None]:
    """("uv tool" | "uv tool, editable from <dir>" | ".venv in <dir>" | ..., project dir)."""
    src = None
    try:
        direct = json.loads(metadata.distribution("agentctl").read_text("direct_url.json") or "{}")
        if direct.get("dir_info", {}).get("editable"):
            src = direct["url"].removeprefix("file://")
    except metadata.PackageNotFoundError:
        pass
    if not in_project_venv():
        return ("uv tool" + (f", editable from {tilde(src)}" if src else "")), src
    project = Path(agentctl.__file__).parent.parent
    return f".venv in {tilde(str(project))}", str(project)


def in_project_venv() -> bool:
    return not (Path(sys.prefix) / "uv-receipt.toml").exists()  # uv marks tool environments


def tilde(p: str) -> str:
    return p.replace(str(HOME), "~", 1)


def agent_exe(name: str) -> str | None:
    if name == "codex":
        return launcher.best_codex()
    if name == "opencode":
        fallback = HOME / ".opencode" / "bin" / "opencode"
        return shutil.which("opencode") or (str(fallback) if fallback.exists() else None)
    return shutil.which(name)


# --- daemon ---


def launchd_pid() -> int | None:
    out = run("launchctl", "print", f"gui/{os.getuid()}/{LABEL}")
    m = re.search(r"^\s*pid = (\d+)", out or "", re.M)
    return int(m[1]) if m else None


def plist_python() -> str | None:
    try:
        return str(plistlib.loads(PLIST.read_bytes())["ProgramArguments"][0])
    except (OSError, KeyError, IndexError, plistlib.InvalidFileException):
        return None


def newest_source() -> float:
    pkg = Path(agentctl.__file__).parent
    return max(p.stat().st_mtime for p in [*pkg.rglob("*.py"), *pkg.rglob("*.js")])


def ping() -> dict[str, float] | None:
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(2)
            s.connect(str(SOCKET_PATH))
            s.sendall(b'{"id": 1, "method": "ping"}\n')
            reply = json.loads(s.makefile("rb").readline())
        return dict(reply["result"])
    except (OSError, ValueError, KeyError):
        return None


# --- aliases ---


def rc_file() -> Path | None:
    shell = Path(os.environ.get("SHELL", "")).name
    return {"zsh": HOME / ".zshrc", "bash": HOME / ".bashrc"}.get(shell)


def aliased(rc: Path, name: str) -> bool:
    text = rc.read_text() if rc.exists() else ""
    return bool(re.search(rf"^\s*alias\s+{name}=", text, re.M))


def strip_block(text: str) -> str:
    return re.sub(rf"\n?{re.escape(BEGIN)}.*?{re.escape(END)}\n?", "\n", text, flags=re.S)


def add_aliases(rc: Path, names: list[str]) -> None:
    """Append (or replace) one marked block, so `uninstall` can remove exactly it."""
    text = rc.read_text() if rc.exists() else ""
    have = re.findall(r"^alias (\w+)='agentctl \1'$", text.split(BEGIN)[-1].split(END)[0], re.M)
    names = sorted(set(names) | (set(have) if BEGIN in text else set()))
    lines = "\n".join(f"alias {n}='agentctl {n}'" for n in names)
    note = "# these start inside agentctl, so they can be watched and controlled"
    block = f"{BEGIN}\n{note}\n{lines}\n{END}\n"
    body = strip_block(text).rstrip("\n")
    if rc.exists():
        STATE_DIR.mkdir(mode=0o700, exist_ok=True)
        (STATE_DIR / "backups").mkdir(exist_ok=True, mode=0o700)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = STATE_DIR / "backups" / f"{rc.name}.{stamp}"
        n = 1
        while backup.exists():  # never overwrite an earlier backup
            backup, n = backup.with_name(f"{rc.name}.{stamp}-{n}"), n + 1
        shutil.copy2(rc, backup)
    rc.write_text((body + "\n\n" if body else "") + block)


def remove_aliases(rc: Path) -> bool:
    if not rc.exists() or BEGIN not in rc.read_text():
        return False
    rc.write_text(strip_block(rc.read_text()).rstrip("\n") + "\n")
    return True


# --- remote access ---


def ssh_on() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 22), timeout=0.5):
            return True
    except OSError:
        return False


def tailscale_ip() -> str | None:
    exe = shutil.which("tailscale") or (TAILSCALE_APP if os.path.exists(TAILSCALE_APP) else None)
    return first_line(run(exe, "ip", "-4")) if exe and run(exe, "ip", "-4") else None


# --- doctor ---


def checks() -> list[Check]:
    out: list[Check] = []
    add = out.append

    kind, _ = install_info()
    version = metadata.version("agentctl")
    if not shutil.which("agentctl"):
        add(Check("agentctl", "fail", f"{version} ({kind}), not on PATH",
                  "uv tool update-shell, then open a new terminal"))  # fmt: skip
    elif in_project_venv():
        add(Check("agentctl", "warn", f"{version}, {kind}",
                  "moving that folder breaks hooks and the daemon: "
                  "uv tool install --editable <folder>, then agentctl setup"))  # fmt: skip
    else:
        add(Check("agentctl", "ok", f"{version}, {kind}"))

    tmux = shutil.which("tmux")
    add(Check("tmux", "ok", first_line(run(tmux, "-V")))
        if tmux else Check("tmux", "fail", "not installed", "brew install tmux"))  # fmt: skip

    found = {n: agent_exe(n) for n in AGENTS}
    for n, exe in found.items():
        if exe:
            add(Check(n, "ok", f"{first_line(run(exe, '--version'))}  {tilde(exe)}"))
        else:
            add(Check(n, "info", "not installed"))
    if not any(found.values()):
        out[-1] = Check("agents", "fail", "none of claude, codex, opencode found", "install one")

    info = ping()
    started = info.get("started") if info else None
    lpid = launchd_pid()
    py = plist_python()
    if not info:
        add(Check("daemon", "fail", "not running", "agentctl daemon install"))
    elif lpid != info["pid"]:
        add(Check("daemon", "warn", f"pid {info['pid']}, started by hand, not by launchd",
                  "agentctl daemon install: agents started from SSH can't read the Keychain "
                  "otherwise (Claude shows API Usage Billing)"))  # fmt: skip
    elif py and not os.path.exists(py):
        add(
            Check(
                "daemon", "fail", f"launchd runs a missing {tilde(py)}", "agentctl daemon install"
            )
        )
    elif py != sys.executable:
        add(Check("daemon", "warn", f"launchd runs another install: {tilde(py or '?')}",
                  "agentctl daemon install"))  # fmt: skip
    elif not started or started < newest_source():
        add(Check("daemon", "warn", f"pid {info['pid']} is running older code",
                  "agentctl daemon stop; agentctl daemon start"))  # fmt: skip
    else:
        add(Check("daemon", "ok", f"pid {info['pid']}, under launchd"))

    if found["claude"]:
        st = hooks.status()
        cmds = {c for c in st.values() if c}
        exes = {shlex.split(c)[0] for c in cmds}
        mine = shutil.which("agentctl") or sys.argv[0]
        if not cmds:
            add(Check("claude hooks", "warn", "not installed",
                      "agentctl hooks install: faster status, and control of Claude sessions "
                      "started in your own tmux"))  # fmt: skip
        elif any(not os.path.exists(e) for e in exes):
            add(Check("claude hooks", "fail", f"point at a missing {', '.join(exes)}",
                      "agentctl hooks install (re-points them)"))  # fmt: skip
        elif len(cmds) < len(st):
            add(Check("claude hooks", "warn", f"{len(cmds)}/{len(st)} events",
                      "agentctl hooks install"))  # fmt: skip
        elif os.path.realpath(mine) not in {os.path.realpath(e) for e in exes}:
            add(Check("claude hooks", "warn", f"point at another install: {', '.join(exes)}",
                      "agentctl hooks install (re-points them)"))  # fmt: skip
        else:
            add(Check("claude hooks", "ok", f"{len(st) - 1} hooks + status line wrapper"))

    rc = rc_file()
    names = [n for n in AGENTS if found[n]]
    if rc is None:
        add(Check("aliases", "info", f"shell {os.environ.get('SHELL')} not handled; "
                  "alias claude/codex/opencode to `agentctl <name>` yourself"))  # fmt: skip
    else:
        missing = [n for n in names if not aliased(rc, n)]
        if missing:
            add(Check("aliases", "warn",
                      f"{', '.join(missing)} not aliased in {tilde(str(rc))}: those start "
                      "outside agentctl and stay read-only", "agentctl setup"))  # fmt: skip
        else:
            add(Check("aliases", "ok", f"{', '.join(names)} in {tilde(str(rc))}"))

    bad = [
        f"{tilde(str(p))} is {oct(p.stat().st_mode & 0o777)}"
        for p, want in ((STATE_DIR, 0o700), (SOCKET_PATH, 0o600))
        if p.exists() and p.stat().st_mode & 0o777 != want
    ]
    add(Check("permissions", "fail", "; ".join(bad), "agentctl daemon stop; agentctl daemon start")
        if bad else Check("permissions", "ok", "~/.agentctl 0700, socket 0600"))  # fmt: skip

    add(Check("ssh", "ok", "Remote Login is on") if ssh_on() else
        Check("ssh", "warn", "Remote Login is off: the phone can't connect",
              "System Settings → General → Sharing → Remote Login"))  # fmt: skip
    ip = tailscale_ip()
    user = os.environ.get("USER", "you")
    add(Check("tailscale", "ok", f"{ip}: from the phone, ssh {user}@{ip}") if ip else
        Check("tailscale", "warn", "not running: the phone reaches this Mac only on the same LAN",
              "install Tailscale on the Mac and the phone (see README)"))  # fmt: skip
    return out
