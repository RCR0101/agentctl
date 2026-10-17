# On-disk formats (verified empirically)

Observed on 2026-10-15 against Claude Code 2.1.296 and 2.1.300 on macOS. Things marked
**unverified** are inferred and still need a live test.

## Claude Code

### `~/.claude/sessions/<pid>.json`: one file per running interactive process

Example (one line in the real file):

```json
{"pid":13746,"sessionId":"eca0d8dd-…","cwd":"/Users/…/IRProject","startedAt":1792055241680,
 "procStart":"Thu Oct 15 09:07:19 2026","version":"2.1.300","kind":"interactive",
 "entrypoint":"cli","name":"irproject-23","nameSource":"derived","status":"shell",
 "updatedAt":1792060705266,"statusUpdatedAt":1792060705266,
 "messagingSocketPath":"/tmp/cc-socks/13746.sock", "peerProtocol":1, "peerFeatures":[…]}
```

- Each `<pid>.json` has a sibling `<pid>.<hash>.key` (mode 0600). agentctl never reads it.
- `kind` ∈ `interactive | bg | daemon | daemon-worker` (enum found in the binary).
- `status` ∈ `busy | shell | idle | waiting`. The binary derives it as: `waiting` if a dialog,
  elicitation or sandbox request is pending (with `waitingFor` set), else `busy` while a query
  is loading, else `idle`. Known `waitingFor` strings: `dialog open`, `input needed`,
  `goal proposal`, `sandbox request`, `worker request`. The value for a tool-permission prompt
  is **unverified** (milestone 2 test). `shell` is **unverified**; it was seen on a live session.
- The file is rewritten on every status change, so FSEvents on this directory gives
  near-instant status without hooks.
- Headless `claude -p` runs write **no** PID file (verified). Whether desktop-app sessions
  write one is **unverified**: no desktop session was running during the survey.
- PID liveness: `kill(pid, 0)`. Stale files from dead processes linger, so liveness has to be
  checked rather than trusting file existence.

Mapping used: `busy`→working, `waiting`→awaiting_approval, `idle`→awaiting_input, `shell`→idle.

### `~/.claude/projects/<cwd-slug>/<sessionId>.jsonl`: transcript, append-only JSONL

- The slug is the cwd with `/`, `.`, `_` replaced by `-`. I don't decode it; `cwd` is on every
  message line.
- Subagent transcripts live in `<cwd-slug>/<sessionId>/subagents/*.jsonl` and are ignored.
- Size: 132 top-level transcripts, 1.5 GB total, 7 over 20 MB. So startup reads only the
  first 64 KB (metadata, first prompt) and the last 512 KB (recent events) of each file, then
  follows from EOF. Full startup scan takes 0.24 s.

Entry `type` counts across the 80 most recent files:

| type | notes |
|---|---|
| `assistant` | one line **per content block** (`thinking`, `text`, `tool_use`), each carrying `message.stop_reason` (`tool_use`, `end_turn`, `stop_sequence`) |
| `user` | `message.content` is a string (typed prompt) or a list of `tool_result` / `text` / `image` blocks. `isMeta: true` = injected context, `isCompactSummary` = compaction summary; both are skipped |
| `system` | `subtype` ∈ `turn_duration`, `stop_hook_summary`, `local_command`, `informational`, `compact_boundary`, …; `level: "error"` is surfaced |
| `attachment` | reminders, hook results, file edits. Not rendered |
| `ai-title` / `custom-title` / `agent-name` | `aiTitle` / `customTitle` / `agentName`. Rewritten often, so the tail always has the latest |
| `last-prompt`, `mode`, `permission-mode`, `queue-operation`, `file-history-*`, `cost-state`, `bridge-session`, `api-request*`, `frame-link`, … | ignored. **`api-request-blob` embeds full message lists**; parsing it would duplicate every turn |

Common fields on message lines: `uuid`, `parentUuid`, `timestamp` (ISO 8601, `Z`), `sessionId`,
`cwd`, `version`, `gitBranch`, `entrypoint`, `isSidechain`, `userType`.

- `entrypoint` values seen: `cli`, `claude-desktop`, `sdk-cli` (from `claude -p`).
  Mapped to origin `cli`, `desktop`, `sdk`.
- `isSidechain: true` lines are skipped. Lines with `agentId` (in-process subagents/teammates
  in the main file) are shown with an `agent_id` flag and don't affect status.
- A user `text` block `[Request interrupted by user]` marks an interrupt.
- Title precedence: `customTitle` > `aiTitle` > first non-meta string prompt not starting
  with `<` (slash-command wrappers) > PID file `name`.

Fallback status (no live PID file): last main-agent entry `end_turn` or interrupt →
awaiting_input; prompt/tool_use/tool_result → working; only while the file was written in
the last 120 s, else `ended`.

### Control (milestone 2, verified with 2.1.300 in tmux 3.8)

PID file additions seen on tmux-hosted sessions:
- `"tmux": "<session>:@<window>.%<pane>"`. Claude records its own pane, but not which tmux
  server. agentctl takes the socket from hooks (`$TMUX`) or the launcher, and before every
  control action it checks that the session's PID descends from the pane's `pane_pid`.
- `waitingFor: "permission prompt"` on a tool permission prompt.
- The PID file appears only **after** the folder-trust dialog is accepted, and is deleted on a
  clean `/exit`.
- `pane_current_command` is the binary name, which is the version (`2.1.300`), so it can't be
  used to recognise Claude.

Hook payloads (stdin JSON; every one has `session_id`, `transcript_path`, `cwd`,
`hook_event_name`, and usually `prompt_id`, `permission_mode`):

| event | extra fields | notes |
|---|---|---|
| SessionStart | `source` (`startup`, `resume`, …), `model`, `scratchpad_dir` | `$TMUX_PANE`, `$TMUX` in env |
| UserPromptSubmit | `prompt` | |
| PreToolUse | `tool_name`, `tool_input`, `tool_use_id` | fires before the permission check |
| PermissionRequest | `tool_name`, `tool_input`, `permission_suggestions` | immediately when the prompt shows |
| Notification | `message`, `notification_type` (`permission_prompt`, …) | ~6 s after the prompt shows |
| PostToolUse | `tool_name`, `tool_input`, `tool_response`, `duration_ms` | |
| Stop | `last_assistant_message`, `stop_hook_active`, `background_tasks` | **not** fired after an Escape interrupt or a denied permission |
| SessionEnd | `reason` (`prompt_input_exit`) | |

Because Stop is skipped on interrupt/deny, status precedence is: live PID file >
SessionEnd hook > other hooks (only for sessions without PID files) > transcript inference.

Keys:
- Permission prompt: numbered menu (`❯ 1. Yes` … `4. No`). Typing the digit selects
  immediately, with no Enter. Deny = the digit of the option starting with "No" (Escape also
  cancels). Long options wrap onto indented continuation lines.
- Interrupt: Escape. Claude then **puts the interrupted prompt back in the input box**, so
  `send` first clears the input: `C-u BSpace` ×10, four times, 50 ms apart. `C-u` clears one
  wrapped row; one long burst of these keys is taken for a paste and only partly applied (a
  ~790-char prompt kept 74 chars).
- Every message goes as `load-buffer -` + `paste-buffer -p`, then Enter after 150 ms.
  Measured on 2.1.302: typed `send-keys -l` text of 800 chars arrived intact; 1,500 and 3,500
  arrived as only their last ~450 chars; a typed tab is a keypress and vanishes. A bracketed
  paste arrived intact at every size tried (up to 3,500 chars, 40 lines).
- Claude wraps a large paste (~1,000+ chars or 10+ lines; 790 chars or 3 lines were not
  wrapped) in the user text as `\n\n<pasted_content id="2ef5">\n…\n</pasted_content
  id="2ef5">\n`; agentctl unwraps it when reading.
- Ctrl-J inserts a newline without submitting (an alternative to pasting for short text).
- `--session-id <uuid>` is honoured, and `--resume <id>` keeps the same id.
- Launching from inside a Claude Code session leaks `CLAUDE_CODE_*` env vars into tmux; the
  child then starts in "API Usage Billing" mode. The launcher strips them with `env -u`.

## Codex

Observed with `codex-cli 0.145.0` (our app-server) and the desktop app's bundled 0.159.2.
Protocol types: `codex app-server generate-ts --out ./codex-protocol` (git-ignored).

### app-server (stdio, newline-delimited JSON-RPC, both directions)

- Handshake: `initialize {clientInfo:{name,title,version}, capabilities:null}` then the
  `initialized` notification. Server request ids are their own sequence (they start at 0), so
  a message with `id` **and** `method` is a server request, and one with `id` only is a response.
- `thread/list {limit, cursor, sortKey:"updated_at", useStateDbOnly}` returns
  `{data: Thread[], nextCursor}`. The default `sourceKinds` covers interactive threads
  (38 here); passing every kind adds guardian/subagent threads (51 of the first 100).
- `Thread`: `id`, `name`, `preview` (first prompt), `cwd`, `source` (`"vscode"` for the desktop
  app, `"cli"` for the TUI), `updatedAt` (unix s), `path` (rollout), `status`
  (`notLoaded | idle | systemError | active{activeFlags: waitingOnApproval|waitingOnUserInput}`).
- **Status is per app-server process.** Threads open in the desktop app are `notLoaded` to us.
- `thread/read {includeTurns:true}` fails for `historyMode: "paginated"` threads (all desktop
  ones), so history comes from rollouts.
- Sending: `thread/resume {threadId}`, then `turn/start {threadId, input:[{type:"text", text,
  text_elements:[]}]}` (returns `turn.id`), or `turn/steer {…, expectedTurnId}` mid-turn.
  `turn/interrupt {threadId, turnId}`. `thread/unsubscribe` after the turn releases the thread.
- Notifications seen in a turn: `thread/status/changed`, `turn/started`, `item/started`,
  `item/completed` (`userMessage`, `reasoning`, `agentMessage` with `phase` commentary/final,
  `commandExecution {command, status, aggregatedOutput, exitCode}`, …), `serverRequest/resolved`,
  `turn/completed`.
- Approval: server request `item/commandExecution/requestApproval {threadId, turnId, itemId,
  command, cwd, reason}` (or `item/fileChange/requestApproval`), with status flag
  `waitingOnApproval` set at the same moment. Reply `{decision: "accept" | "acceptForSession" |
  "decline" | "cancel"}`. A declined command completes with item `status: "declined"`.
  Other server requests (`item/permissions/requestApproval`, `item/tool/requestUserInput`,
  MCP elicitation) have different reply shapes; agentctl surfaces them and can only deny.

### Rollouts: `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread id>.jsonl`

166 files, 590 MB. Line = `{timestamp, ordinal, type, payload}`. The first line is
`session_meta` with `id`, `cwd`, `source`, `originator` (`Codex Desktop`,
`codex_work_desktop`, `codex-tui`, or our `clientInfo.name`), `history_mode`.

Two formats, chosen by `history_mode`:
- **paginated** (desktop app): conversation in `event_msg/item_completed {item}`, where item
  uses PascalCase types and snake_case fields (`CommandExecution {command:[argv], exit_code,
  aggregated_output}`, `AgentMessage {content:[{type:"Text"}]}`, `Extension {kind:"web.search"}`).
  Tool calls are **also** in `response_item`, so only `item_completed` is used.
- **legacy** (threads created by our 0.145 app-server): `event_msg/user_message|agent_message
  {message}` and `response_item/custom_tool_call|function_call` plus `*_output`.
- Both: `event_msg/task_started`, `task_complete`, `turn_aborted` give turn state. Approval
  requests are **not** recorded.
- **FSEvents never fires for rollout writes** (verified: zero events during a full turn, even
  watching the day directory). Codex keeps the file open and appends. agentctl polls live and
  recent rollouts once per second instead.

### Who has a thread open: `~/.codex/thread-writer-locks/<thread id>.lock`

Every process with a thread loaded holds a lock on its file. `lsof -Fpn +D` lists the holders
in about 160 ms. On this machine the desktop app's app-server (pid 89079) held 8. agentctl treats a
thread held by any process but its own as live, and **read-only** unless the holder runs in a
tmux pane agentctl launched.

### Desktop-app live sync (milestone 3 risk), without touching the desktop UI

- The desktop app runs its own `codex app-server` over stdio (no `--listen`), so there is no
  endpoint to attach to. `codex app-server --listen unix://` / `ws://` and
  `app-server daemon|proxy` exist, but the desktop app doesn't use them (0.162 TUIs can;
  see "Taking over a session" below).
- **Threads made in the desktop app are discoverable** (`thread/list`, source `"vscode"`), with
  history from their paginated rollouts.
- **Concurrent use is guarded by Codex itself** through the writer locks above. agentctl refuses
  to send to a thread the desktop app holds ("open in another Codex process (pid N)") instead
  of testing what a second writer would do. Status of such threads comes from the rollout
  (`task_started` → working, `task_complete` → input). Their pending approvals are invisible.
- After its own turns agentctl unsubscribes, and the lock was confirmed released (0 holders), so
  the desktop app can reopen the thread.
- Not tested, per instruction: whether a turn sent from agentctl appears live in an open desktop
  window. Since the lock means at most one process has the thread loaded, I'd expect the desktop
  app to show agentctl's turn only after it reloads the thread.

### Codex TUI in tmux (`agentctl new codex`, verified with 0.145.0)

- First run in a folder: a trust prompt (persisted in Codex's state; the
  `-c projects.<path>.trust_level` override did not skip it), sometimes preceded by an update
  prompt where "Skip" is one-time. agentctl never answers these itself.
- The thread (and rollout) is created on the first prompt, not at launch.
- The 0.145 TUI holds **no** writer lock, but keeps its rollout open, as the desktop app does.
  So "who has thread X open" = `lsof -Fpn -c codex` (rollout files + lock files, ~55 ms). The
  holder pid is mapped to a pane by walking its ancestors to a `pane_pid` from
  `tmux list-panes -a`. That needs no registration, survives daemon restarts, and also catches
  TUIs started by hand.
- Typing: fast `send-keys -l` input is treated as a paste and an Enter right after it is
  swallowed. A 0.6 s pause before Enter fixes it. `C-u`/`BSpace` clears the input, as in
  Claude.
- Approval menu: `› 1. Yes, proceed (y)`, `2. Yes, and don't ask again … (p)`,
  `3. No, and tell Codex what to do differently (esc)`, "Press enter to confirm or esc to
  cancel". Each option's shortcut is pressed. Approvals are **not** in the rollout, so for
  threads in a pane agentctl reads this menu off the screen every 2 s.
- Interrupt: Escape ("Conversation interrupted").

## opencode (verified with 1.18.29, 2026-10-16)

### `~/.local/share/opencode/opencode.db`: SQLite (WAL), shared by every opencode process

agentctl opens it with `?mode=ro` and only ever reads it. `$XDG_DATA_HOME` moves it.

- `session`: `id` (`ses_` + 26 chars; the 8 after `ses_` are time-ordered and make the short
  id), `parent_id` (set for subagent sessions, e.g. title "… (@explore subagent)"),
  `directory`, `title`, `time_updated`, `time_archived` (ms). `title` stays
  `New session - <ISO date>` when title generation fails (it uses `small_model`), so agentctl
  falls back to the first prompt. `time_updated` does **not** move during a turn.
- `message`: `id`, `session_id`, `time_created`, `time_updated`, `data` JSON:
  `role` (user|assistant), `time.created`, `time.completed` (assistant, once done), `finish`
  (`tool-calls`|`stop`), `providerID`, `modelID`, `tokens` {`input`, `output`, `reasoning`,
  `cache` {`read`, `write`}}, `error` {`name`, `data.message`} (e.g. `MessageAbortedError`
  "Aborted" after an interrupt; `model '…' not found`).
- `part`: `id`, `message_id`, `session_id`, `time_created`, `time_updated`, `data` JSON with
  `type`: `text` (`text`, `synthetic` for injected context, `time.end` once streamed),
  `reasoning`, `tool` (`tool`, `callID`, `state` {`status` running|completed|error, `input`
  (camelCase, e.g. `filePath`), `output`, `error`}), `step-start`, `step-finish`, `patch`,
  `file`. A rejected permission ends as a tool part with `status: error`, `error: "The user
  rejected permission to use this specific tool call."`.
- **Pending permissions are not in the DB.** While the prompt is up the tool part just stays
  `running`. Nothing in the DB says which process has a session open either (every process
  holds the same DB file).

Context in use = the last assistant message's `input + cache.read + cache.write`. The
window comes from opencode's models.dev cache `~/.cache/opencode/models.json`:
`<provider>.models.<model>.limit.context`.

### Plugin events (how agentctl knows pane, status and approvals)

opencode merges `$OPENCODE_CONFIG` into the user's config, so
`agentctl new/adopt opencode` sets it to `~/.agentctl/opencode.json`, which only lists
agentctl's plugin (`agentctl/opencode_plugin.js`). `~/.config/opencode` is never touched. A
plugin's `event` hook gets every bus event; in the TUI process `process.env.TMUX_PANE` is the
pane. Events agentctl forwards (properties abbreviated):

- `session.status` {`sessionID`, `status.type`: busy|idle (busy repeats during a turn)}
- `session.idle` {`sessionID`}
- `permission.asked` {`id: per_…`, `sessionID`, `permission: "bash"`, `patterns: ["echo three > out3.txt"]`, `metadata.command`, `always: ["echo *"]`, `tool.callID`}
- `permission.replied` {`sessionID`, `requestID`, `reply: "once"`}

Subagent sessions send their own busy/idle events under their own id; agentctl ignores
those, but maps their permission requests to the root session, since they show in its pane.
Without the plugin (an opencode started by hand) status is inferred from the DB only.
Such a session counts as working if its newest message is a user message or an unfinished
assistant one written in the last 2 min; it's never controllable.

### TUI in tmux

- No TCP listener in the TUI by default (`lsof`); the session is created at the first prompt.
- `-m provider/model` is ignored when the model is unknown: it falls back to the config's
  `model` silently, and the error appears as a message error.
- Permission prompt: `△ Permission required`, `# Shell command`, `$ <cmd>`, then
  `Allow once   Allow always   Reject` with focus on the first, `⇆ select  enter confirm`.
  agentctl presses Enter, Right Enter, or Right Right Enter, after checking that the prompt
  is on screen.
- Interrupt: Escape arms it ("esc again to interrupt"), a second Escape aborts.
- Ctrl-C quits. Typed text plus Enter submits; a message sent while busy is queued.

## Taking over a session from another terminal (`adopt`, verified 2026-10-16)

- **Claude Code 2.1.301:** SIGINT to the TUI exits cleanly in ~1 s (rc 0). It prints
  "Resume this session with: claude --resume <id>", removes `~/.claude/sessions/<pid>.json`,
  and the transcript is complete (it ends with a `cost-state` line). `claude --resume` then
  has the full conversation.
- **Codex TUI 0.162:** SIGINT, SIGTERM and SIGHUP all kill it in ~0.5 s (rc 130/143/129)
  without restoring the terminal: it's left with `-icanon`. An interactive zsh restores tty
  modes when its foreground job dies, but other modes (kitty keyboard flags, bracketed paste,
  mouse, hidden cursor) can stay on. So agentctl writes their reset sequences to the old tty.
  The writer lock is released on exit, and `codex resume <id>` has the full history.
- **Codex's shared background server** (`codex app-server daemon`, under
  `~/.codex/packages/app-server-daemon/`, no tty): when it's running, a 0.162 TUI can run
  its thread *inside* it. The server then holds the rollout and writer lock, not the TUI, and
  keeps the thread loaded a while after the TUI quits. Such a thread can't be mapped to a
  pane, and stopping the TUI doesn't free it. `codex --no-daemon` ("Run without the shared
  background server, even if it is already running") keeps the thread in the TUI process, so
  agentctl passes it when `codex --help` lists it (0.145 doesn't). It goes before any
  subcommand: `codex --no-daemon resume <id>`.
- **Which processes `adopt` will stop:** only one whose command name is `claude` or `codex`
  *and* that has a terminal. Every desktop-app and background-server process shows `??` as
  its tty in `ps`.

## Claude Code status line input (verified in the 2.1.301 binary, 2026-10-16)

Claude Code pipes JSON to the `statusLine` command whenever it redraws the status bar. Its
schema is in the binary's own docs. Fields agentctl uses:

- `session_id`
- `context_window.context_window_size`: the exact window (the transcript lacks it, so it's
  otherwise guessed). Also `total_input_tokens` and `used_percentage`.
- `prompt_cache.expires_at`: Unix seconds when the cached prefix goes cold; null without
  cache tokens. Also `warm` and `ttl` ("5m" or "1h").
- `rate_limits.five_hour` and `rate_limits.seven_day`: `{used_percentage, resets_at}`.
  Subscribers only, present "only while the API reports it and its resets_at has not
  passed". This is the only local source of the plan limits: transcripts, `~/.claude/state`
  and `stats-cache.json` don't have them.

`agentctl hooks install` sets `statusLine.command` to `<agentctl> statusline` and saves your
previous command in `~/.agentctl/statusline.json`. The wrapper (stdlib only) forwards those
fields to agentctld, then runs your command through the shell with the same stdin. Its
output and exit code pass through unchanged. Nothing is sent anywhere, and no tokens are used.

## Codex plan limits

`event_msg` / `token_count` lines in rollouts carry
`rate_limits: {limit_id, primary: {used_percent, window_minutes: 300, resets_at},
secondary: {…, window_minutes: 10080, …}, plan_type: "plus"}`. That's the 5-hour and weekly
windows, as of that reply.
