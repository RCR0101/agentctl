import os
from pathlib import Path

HOME = Path.home()
STATE_DIR = HOME / ".agentctl"
SOCKET_PATH = STATE_DIR / "agentctl.sock"
LOG_DIR = STATE_DIR / "logs"
CLAUDE_DIR = HOME / ".claude"
CODEX_DIR = HOME / ".codex"
# opencode follows XDG: $XDG_DATA_HOME/opencode, $XDG_CACHE_HOME/opencode
_XDG_DATA = Path(os.environ.get("XDG_DATA_HOME") or HOME / ".local" / "share")
_XDG_CACHE = Path(os.environ.get("XDG_CACHE_HOME") or HOME / ".cache")
OPENCODE_DB = _XDG_DATA / "opencode" / "opencode.db"
OPENCODE_MODELS = _XDG_CACHE / "opencode" / "models.json"
