"""Claude Code hooks: the `agentctl hook <event>` fast path, the `agentctl statusline` wrapper,
and settings.json install/uninstall.

The fast path imports only the stdlib (~25 ms startup) and always exits 0 with no output, so it
can never block Claude Code or change a permission decision.
"""

import hashlib
import json
import os
import re
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Any

from agentctl import CLAUDE_DIR, SOCKET_PATH, STATE_DIR

EVENTS = (
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "Notification",
    "Stop",
    "PreToolUse",
    "PostToolUse",  # flips awaiting_approval back to working the moment a tool runs
    "PermissionRequest",  # fires ~6 s before the permission Notification, with tool details
)
SETTINGS = CLAUDE_DIR / "settings.json"
RECORD = STATE_DIR / "hooks-install.json"
OURS = re.compile(r"agentctl['\"]? hook [A-Za-z]+$")
OURS_STATUSLINE = re.compile(r"agentctl['\"]? statusline$")
# The status line command agentctl wraps: {"command": <your original, or null>}
STATUSLINE_REC = "statusline.json"  # next to RECORD


def _send(msg: dict[str, Any]) -> None:
    with socket.socket(socket.AF_UNIX) as s:
        s.settimeout(0.1)
        s.connect(str(SOCKET_PATH))
        s.sendall(json.dumps(msg).encode() + b"\n")


def run_hook(event: str) -> None:
    try:
        payload = json.loads(sys.stdin.buffer.read() or b"{}")
        for big in ("tool_response", "last_assistant_message"):
            payload.pop(big, None)
        msg = {
            "id": 0,
            "method": "hook",
            "params": {
                "event": event,
                "payload": payload,
                "pane": os.environ.get("TMUX_PANE"),
                "tmux": os.environ.get("TMUX"),
            },
        }
        _send(msg)
    except Exception:
        pass  # daemon down or bad payload: Claude Code must never notice
    os._exit(0)


def run_statusline() -> None:
    """Claude Code's status line command. Claude pipes it JSON with the session's plan limits
    (rate_limits), exact context window and prompt-cache expiry, which it reports nowhere
    else. Forward those to agentctld, then run your own status line command (saved at
    install) with the same input, so the status bar looks as before. Local only: no tokens."""
    data = sys.stdin.buffer.read()
    try:
        d = json.loads(data or b"{}")
        keep = ("session_id", "rate_limits", "context_window", "prompt_cache", "model")
        _send({"id": 0, "method": "statusline", "params": {k: d.get(k) for k in keep}})
    except Exception:
        pass
    try:
        command = json.loads((STATE_DIR / STATUSLINE_REC).read_text()).get("command")
    except (OSError, ValueError):
        command = None
    if command:
        import subprocess

        # your own status line, run the way Claude Code runs it (a shell command)
        rc = subprocess.run(command, shell=True, input=data).returncode
        sys.stdout.flush()
        os._exit(rc)
    os._exit(0)


# --- settings.json editing ---


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _is_ours(entry: dict[str, Any]) -> bool:
    return any(OURS.search(h.get("command", "")) for h in entry.get("hooks", []))


def _render(data: dict[str, Any]) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode()


def _write(path: Path, content: bytes) -> None:
    tmp = path.with_suffix(".json.agentctl-tmp")
    tmp.write_bytes(content)
    if path.exists():
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def planned(exe: str, path: Path = SETTINGS) -> tuple[dict[str, Any], list[str]]:
    """Settings with our hooks added (or re-pointed at `exe`, e.g. after a reinstall), and the
    events that changed."""
    data: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}
    hooks = data.setdefault("hooks", {})
    changed = []
    for ev in EVENTS:
        entries = hooks.setdefault(ev, [])
        cmd = f"{exe} hook {ev}"
        ours = [h for e in entries if _is_ours(e) for h in e["hooks"] if OURS.search(h["command"])]
        if not ours:
            entries.append({"matcher": "", "hooks": [{"type": "command", "command": cmd}]})
            changed.append(ev)
        elif any(h["command"] != cmd for h in ours):
            for h in ours:
                h["command"] = cmd
            changed.append(ev)
    sl = data.get("statusLine")
    want = f"{exe} statusline"
    if sl is None:
        data["statusLine"] = {"type": "command", "command": want}
        changed.append("statusLine")
    elif isinstance(sl, dict) and sl.get("type") == "command" and sl.get("command") != want:
        sl["command"] = want
        changed.append("statusLine")
    return data, changed


def install(exe: str, path: Path = SETTINGS, record: Path = RECORD) -> Path | None:
    """Back up, then add our hooks. Idempotent: returns None if nothing to do."""
    data, changed = planned(exe, path)
    if not changed:
        return None
    sl = (json.loads(path.read_text()) if path.exists() else {}).get("statusLine")
    cmd = sl.get("command") if isinstance(sl, dict) else None
    if "statusLine" in changed and not (cmd and OURS_STATUSLINE.search(cmd)):
        # remember your own status line; the wrapper runs it (null: you had none)
        record.parent.mkdir(mode=0o700, exist_ok=True)
        (record.parent / STATUSLINE_REC).write_text(json.dumps({"command": cmd}))
    existed = path.exists()
    original = path.read_bytes() if existed else b""
    new = _render(data)
    rec = json.loads(record.read_text()) if record.exists() else {}
    if rec and sha(original) == rec.get("written_sha256"):
        # a re-point of our own untouched write: keep the pre-agentctl backup for uninstall
        rec["written_sha256"] = sha(new)
        backup = Path(rec["backup"])
    elif rec:
        # edited since our install: keep the pre-agentctl backup on record, but uninstall can
        # no longer restore bytes without losing those edits, so it will remove our entries
        rec["written_sha256"] = None
        backup = Path(rec["backup"])
    else:
        backup = record.parent / "backups" / f"settings.json.{time.strftime('%Y%m%d-%H%M%S')}"
        STATE_DIR.mkdir(mode=0o700, exist_ok=True)  # parents=True would make it 0755
        backup.parent.mkdir(exist_ok=True, mode=0o700)
        backup.write_bytes(original)
        rec = {"backup": str(backup), "existed": existed, "written_sha256": sha(new)}
    _write(path, new)
    record.write_text(json.dumps(rec))
    return backup


def uninstall(path: Path = SETTINGS, record: Path = RECORD) -> str:
    """Restore the backup byte-for-byte if the file is untouched since install; else remove
    only our entries."""
    rec = json.loads(record.read_text()) if record.exists() else {}
    current = path.read_bytes() if path.exists() else b""
    if rec and sha(current) == rec.get("written_sha256"):
        if rec["existed"]:
            _write(path, Path(rec["backup"]).read_bytes())
        else:
            path.unlink()
        record.unlink()
        (record.parent / STATUSLINE_REC).unlink(missing_ok=True)
        return f"restored {path} from {rec['backup']}"
    data: dict[str, Any] = json.loads(current or b"{}")
    hooks = data.get("hooks", {})
    removed = 0
    for ev in list(hooks):
        keep = [e for e in hooks[ev] if not _is_ours(e)]
        removed += len(hooks[ev]) - len(keep)
        if keep:
            hooks[ev] = keep
        else:
            del hooks[ev]
    if "hooks" in data and not hooks:
        del data["hooks"]
    sl = data.get("statusLine")
    if isinstance(sl, dict) and OURS_STATUSLINE.search(sl.get("command", "")):
        try:
            original = json.loads((record.parent / STATUSLINE_REC).read_text()).get("command")
        except (OSError, ValueError):
            original = None
        if original:
            sl["command"] = original
        else:
            del data["statusLine"]
        removed += 1
    (record.parent / STATUSLINE_REC).unlink(missing_ok=True)
    if removed:
        _write(path, _render(data))
    record.unlink(missing_ok=True)
    return (
        f"removed {removed} agentctl hook entries (settings changed since install, kept the rest)"
    )


def status(path: Path = SETTINGS) -> dict[str, str | None]:
    data = json.loads(path.read_text()) if path.exists() else {}
    out: dict[str, str | None] = {}
    for ev in EVENTS:
        cmds = [
            h["command"]
            for e in data.get("hooks", {}).get(ev, [])
            if _is_ours(e)
            for h in e["hooks"]
        ]
        out[ev] = cmds[0] if cmds else None
    sl = data.get("statusLine")
    cmd = sl.get("command", "") if isinstance(sl, dict) else ""
    out["statusLine"] = cmd if OURS_STATUSLINE.search(cmd) else None
    return out
