"""tmux plumbing. All user text goes through argv (`send-keys -l --`) or stdin (`load-buffer -`)."""

import asyncio
import itertools
import os
import re
import shutil

from agentctl.adapters.base import ControlError

_buf_ids = itertools.count()
OPTION_RE = re.compile(r"^\s*(?:[❯›]\s*)?(\d+)\.\s+(.+?)\s*$")  # Claude ❯, Codex ›
# C-u clears one wrapped row, BSpace joins the previous one. A long burst of them is taken
# for a paste and partly dropped, so they go in short rounds.
# ponytail: 4 rounds clear 40 rows; a longer leftover prompt survives
CLEAR_KEYS = ["C-u", "BSpace"] * 10
_pane_locks: dict[tuple[str | None, str], asyncio.Lock] = {}


def tmux_bin() -> str:
    path = shutil.which("tmux")
    if not path:
        raise ControlError("tmux is required but not installed (brew install tmux)")
    return path


def argv(sock: str | None, *args: str) -> list[str]:
    return [tmux_bin(), *(["-S", sock] if sock else []), *args]


async def run(sock: str | None, *args: str, stdin: bytes | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        *argv(sock, *args),
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(stdin)
    if proc.returncode:
        raise ControlError(f"tmux {args[0]}: {err.decode().strip()}")
    return out.decode()


async def send_text(
    sock: str | None, pane: str, text: str, clear: bool = True, settle: float = 0.15
) -> None:
    """Always a bracketed paste: typed keys over ~800 chars are taken for an unbracketed
    paste and Claude keeps only the tail, and a typed tab is a keypress. One send per pane
    at a time, or two messages interleave in the input box and go as one."""
    async with _pane_locks.setdefault((sock, pane), asyncio.Lock()):
        if clear:  # Escape-interrupts put the old prompt back in the input box
            for _ in range(4):
                await run(sock, "send-keys", "-t", pane, *CLEAR_KEYS)
                await asyncio.sleep(0.05)
        buf = f"agentctl-{os.getpid()}-{next(_buf_ids)}"
        await run(sock, "load-buffer", "-b", buf, "-", stdin=text.encode())
        await run(sock, "paste-buffer", "-p", "-d", "-b", buf, "-t", pane)
        await asyncio.sleep(settle)  # let the app finish the paste before Enter
        await run(sock, "send-keys", "-t", pane, "Enter")


async def keys(sock: str | None, pane: str, *k: str) -> None:
    await run(sock, "send-keys", "-t", pane, *k)


async def literal(sock: str | None, pane: str, text: str) -> None:
    await run(sock, "send-keys", "-t", pane, "-l", "--", text)


async def capture(sock: str | None, pane: str) -> str:
    return await run(sock, "capture-pane", "-p", "-J", "-t", pane)


async def pane_pid(sock: str | None, pane: str) -> int | None:
    try:
        out = await run(sock, "display-message", "-p", "-t", pane, "#{pane_pid}")
    except ControlError:
        return None
    return int(out) if out.strip().isdigit() else None


async def parents() -> dict[int, int]:
    proc = await asyncio.create_subprocess_exec(
        "ps", "-axo", "pid=,ppid=", stdout=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    parent: dict[int, int] = {}
    for ln in out.decode().splitlines():
        a, b = ln.split()
        parent[int(a)] = int(b)
    return parent


async def all_panes(sock: str | None) -> list[tuple[str, int]]:
    """(pane id, pane pid) for every pane on a server; [] if it isn't running."""
    try:
        out = await run(sock, "list-panes", "-a", "-F", "#{pane_id} #{pane_pid}")
    except ControlError:
        return []
    return [(a, int(b)) for a, b in (ln.split() for ln in out.splitlines() if ln.strip())]


async def is_descendant(pid: int, ancestor: int, parent: dict[int, int] | None = None) -> bool:
    parent = parent if parent is not None else await parents()
    for _ in range(10):
        if pid == ancestor:
            return True
        pid = parent.get(pid, 1)
        if pid <= 1:
            return False
    return False


def parse_options(screen: str) -> dict[str, str]:
    """Numbered choices of the last menu on screen, e.g. {"1": "Yes", ..., "4": "No"}."""
    block: dict[str, str] = {}
    key, col = "", 0
    for line in screen.splitlines():
        m = OPTION_RE.match(line)
        if m:
            if m[1] == "1":
                block = {}
            key, col = m[1], m.start(2)
            block[key] = m[2]
        elif key and line.strip() and len(line) - len(line.lstrip()) >= col:
            block[key] += " " + line.strip()  # long option wrapped by the app itself
        else:
            key = ""
    return block
