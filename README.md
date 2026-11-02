# agentctl

Watch and drive your Claude Code, Codex and opencode sessions on a Mac, from the Mac or from
your phone over SSH. It lists every session with what it's doing (working, needs approval,
waiting for you), streams transcripts live, and lets you reply, approve, deny or interrupt.
The dashboard works down to about 45 columns, so a phone in portrait is fine.

It's local only: a background daemon talks to a Unix socket that only your user can open. It
has no network listener, no cloud and no telemetry. Remote access is plain SSH.

## Install

You need macOS, [uv](https://docs.astral.sh/uv/) and tmux, plus at least one of `claude`,
`codex` or `opencode`.

```sh
brew install uv tmux
git clone https://github.com/RCR0101/agentctl ~/src/agentctl
uv tool install --editable ~/src/agentctl
agentctl setup
```

`agentctl setup` asks before each change. It can:

1. **Add Claude Code hooks** to `~/.claude/settings.json`, backing the file up first.
   They make status updates faster, and let agentctl control Claude sessions you start in
   your own tmux. Setup also wraps your status line to read your plan's usage limits; it
   still shows as before.
2. **Run the daemon under launchd**, so it starts at login in your desktop session. Agents
   it launches can then use your Claude subscription login (in the Keychain) even when you're
   connected over SSH.
3. **Alias `claude`, `codex` and `opencode`** to `agentctl claude` etc. in `~/.zshrc`, in a
   marked block. Every session you start then runs inside agentctl and can be controlled from
   the phone. `command claude` bypasses the alias.

It finishes with `agentctl doctor`, which you can run any time. It checks the install,
daemon, hooks, aliases, file permissions and phone access, and prints a fix for anything
wrong.

`--editable` means edits to the clone take effect at once; after changing daemon code, run
`agentctl daemon restart`. It refuses to interrupt a daemon-owned Codex turn. Reopen the
dashboard after changing UI code. Without `--editable`, reinstall after pulling.

## Phone setup (iPhone + Termius, any network)

1. **Tailscale** on the Mac and the phone, same account. `agentctl doctor` prints the Mac's
   Tailscale address.
2. **Remote Login** on the Mac: System Settings → General → Sharing → Remote Login.
3. **A key for the phone.** In Termius: Keychain → Generate Key (ed25519) → Export to host.
   Or copy its public key into `~/.ssh/authorized_keys` on the Mac.
4. **Key-only SSH** (recommended). Put this in `/etc/ssh/sshd_config.d/100-hardening.conf`;
   macOS starts sshd per connection, so the next connection uses it:
   ```
   PasswordAuthentication no
   KbdInteractiveAuthentication no
   PermitRootLogin no
   ```
5. **A Termius host:** the Tailscale address, your Mac user name and that key. Connect and
   run `agentctl watch`.

## Using it

Start agents as usual: `claude`, `codex`, `opencode` (with the aliases), or
`agentctl new claude ~/proj` to start one in the background. Then, from anywhere:

```sh
agentctl watch                 # dashboard (phone friendly)
agentctl ls                    # sessions, what needs you first
agentctl tail <id> -f          # follow a transcript
agentctl send <id> "do X"      # reply ("-" reads stdin)
agentctl approve <id>          # option 1; --choice N for another
agentctl deny <id>
agentctl interrupt <id>
agentctl attach <id>           # open the session's terminal; Ctrl-] d to leave
agentctl send <id> -l "then run the tests"   # queued: sent once it's free
agentctl digest                # what happened since you last looked
agentctl usage                 # plan limits (Codex)
agentctl rename <id> "name"    # your own name for it
agentctl close <id>            # quit the agent (resumable with adopt)
```

Ids are 8-character prefixes as shown by `ls`.

### The dashboard (`agentctl watch`)

The overview shows approvals, unread sessions, running agents and overlapping work. Connection
status stays visible; if the daemon disconnects, the last view stays on screen with its age.
On a desktop, task names lead the session list and the transcript gets most of the space.
Project files are available with `v`. On desktop, Enter opens the agent's fullscreen terminal;
clicking a session selects its preview without attaching. While reading a transcript, Enter
stays in the transcript; `Ctrl+T` explicitly opens the terminal.
Space opens its fullscreen transcript, and Esc returns to the dashboard. On a phone, Enter
opens the fullscreen transcript. `Ctrl+T` opens the agent's terminal at any width.
`Ctrl+P` searches the available commands. Status groups have full-width colored headings and
distinct markers, so approvals, input requests and running sessions stand apart.

On a phone (under 80 columns) it's built for the phone rather than squeezed down: one
full-height page at a time, with large buttons along the bottom.

- **Sessions.** One card per session, with a coloured bar for its status: what it wants
  approved, the tool it's running, or the start of its last reply. The bottom bar starts a
  new session, searches, and shows how many sessions have something unread. When one needs
  you, **review N** takes you to it.
  Plan limits sit at the top.
- **A session.** Tap a card. Its transcript fills the screen; the bar has back, reply,
  files and stop. When it needs approval, what it wants to run shows above large buttons,
  one per option.
- **Files.** The session's project, with what it edited marked `●`. Tap a file for its diff.
- **more** holds the rest: reviewing the latest turn, commands, jumping into its terminal,
  queueing a message, rename, remove,
  grouping, theme, bell, and the key list.

While you type, both desktop and phone show the recipient, **cancel**, and **send** (or
**queue** when the agent is busy). Enter, Ctrl+Enter or Ctrl+S submits; Shift+Enter adds a
line, and Tab indents. Empty Enter keeps the editor open. Esc saves an unfinished reply as
a draft for that session while the dashboard stays open; `s` restores it. The editor stays
visible during delivery and retains the message on failure. A completed send or cancellation
returns focus to the view you were using. Up to three quick replies appear on a phone.

Esc goes back one view at a time: composer → files → fullscreen transcript → session list.
On the session list, Esc clears an applied filter. `Ctrl+Q` always exits the dashboard.

| key | does |
|---|---|
| Enter | desktop: open the agent's fullscreen terminal; phone: open its fullscreen transcript |
| Space | fullscreen transcript; Esc returns to the dashboard |
| `Ctrl+T` | open its terminal; `Ctrl-] d` returns |
| `Ctrl+P` | search available commands |
| `g` | go to a session needing approval |
| `w` | review the latest observed request, full reply, recorded tool outputs, tool-edited files and current Git changes. Git changes include all sessions and manual edits; recorded tool outcomes are shown without inferring whether the task succeeded |
| `v` | show or hide project files (a separate page on a phone) |
| `s` | send a message. Quick replies sit under the box: tap one to put it in, then Enter. Edit them in `~/.agentctl/replies`, one per line |
| `a` / `d` | approve / deny (or tap the option buttons). When an agent asks questions (Claude's AskUserQuestion, or Codex's request_user_input in Plan mode), `a` or the answer button opens them as a form: pick, tick several, or type your own answer; agentctl fills in the agent's own question menu for you. A thread open in the Codex desktop app shows its questions, but you answer them there. `d` cancels the question |
| `1`–`4` | pick an approval option |
| `i` | interrupt |
| `c` | copy the full last reply to your clipboard, even over SSH |
| `r` | rename (agentctl's own name; the agent's title is untouched) |
| `n` | new session: pick the agent and a folder (recent ones listed) |
| `u` | what's new since you last looked; pick one to open it |
| `p` | group the list by project (git repo) instead of by status |
| `l` | queue a message, sent once the agent is free |
| `o` | adopt a read-only session (moves it into agentctl) |
| `x` | hide a session; its agent keeps running (`z` unhides) |
| `Ctrl+X` | quit and hide an idle session; `z` only unhides, `o` resumes |
| `/` | filter the list by id, title or folder |
| `f` | search what was said in every session (your messages and the replies, not tool output); Enter on a match opens its session |
| `h` | messages sent through agentctl and their delivery states, saved across dashboard and daemon restarts; select one to reuse it |
| Enter on a file | its diff since the last commit. `●` marks files the session edited (with an edit tool; edits made through shell commands aren't seen), a dim `●` the folders holding them |
| `e` | show or hide ended sessions |
| `m` | raw text instead of rendered Markdown |
| `b` | bell and a Mac notification with the alerts below |
| `t` | theme, previewed as you move through the list; the pick is remembered |
| `?` | all keys |
| `q` | quit |

While attached to any agent in agentctl's tmux, `Ctrl-] w` opens this dashboard over it, and
`q` goes back.

Letter keys work with caps lock or shift too, and after a phone keyboard capitalises the
first letter.

**Alerts** (toasts; with `b` on, also the bell and a Mac notification):

- a session needs approval;
- a turn that ran 2 minutes or more finished;
- a Claude session waiting on you has under 2 minutes of prompt cache left, so replying
  before then avoids rebuilding the cache;
- a message you sent hasn't shown up in the session's transcript 30 s after the agent was
  free to take it (a short "has your message" toast confirms the ones that arrive).

**Themes:** the default `textual-dark` draws its own background, so it looks the same in any
truecolor terminal. `ansi-dark` and `ansi-light` use your terminal's own 16 colors and
background instead: pick one of these for a light or transparent terminal, a custom color
scheme, or a terminal without truecolor (macOS Terminal.app, some SSH clients). The other
built-in Textual themes (`nord`, `gruvbox`, `solarized-light`, `catppuccin-latte`, …) are
there too. Choose with `t` or `agentctl watch --theme ansi-light`; either is saved in
`~/.agentctl/theme`. `NO_COLOR=1` turns colors off.

Across sessions, the dashboard and `ls` show:

- **Unread:** cards say "3 new" for replies you haven't read. Opening a session (or
  `agentctl tail`) marks it read, and its transcript has a "── new since 09:52 ──" line.
- **Overlaps:** `!` marks two live agents working in the same git repo at once, or that edited
  the same file in the last two hours. A toast appears when a new overlap starts.
- **Queued follow-ups:** "N queued" marks messages waiting to be sent. Each goes out once the agent is
  waiting for input, one per finished turn.
- **Plan usage** (5-hour and weekly windows) for Claude Code and Codex in the title bar, and
  in `agentctl usage`. Claude Code reports these only to its status line, so the hooks install
  wraps yours: your status line still shows as before, and no tokens are used.

Each run of tool calls is folded into one line; tap it (or Tab to it and press Enter) to
expand. Drag to select text; Ctrl+C copies it to the clipboard of the terminal you're using.
Conversation messages are kept in full, including long replies, in both Markdown and raw
views. Tool output previews remain compact. Review, search, delivery tracking and the
dashboard use local data and make no additional model calls.

Sent-message history is stored locally in `~/.agentctl/state.db`. An **arrived** receipt means
the message appeared in the transcript after it was sent. It does not claim the agent acted
on it. A delivery interrupted by a daemon restart is marked **unknown**, so you can inspect
the transcript before deciding whether to resend.

### Which sessions you can control

`ls` marks controllable sessions with `●`. A session is controllable when:

- **agentctl started it:** through the aliases, `agentctl claude|codex|opencode`, or
  `agentctl new`. These run in agentctl's own tmux (`tmux -L agentctl`).
- **It's Claude Code in your own tmux,** with the hooks installed.
- **It's a Codex thread nothing else has open,** e.g. an idle desktop-app thread. agentctl
  drives it through its own `codex app-server`.

Everything else (the Claude desktop app, a Codex thread open in another app, an opencode
started without agentctl) is read-only. `agentctl adopt <id>` moves it into agentctl's tmux.
If it's open in another terminal, `claude` or `codex` there is stopped first (as if you'd
pressed Ctrl-C there, and the old terminal gets a note). The session then resumes in agentctl
with its full history. It asks first (`-y` skips the question), and refuses:

- **Mid-turn:** a working session, or one waiting for approval, unless you pass `--force`
  (that aborts the turn).
- **Outside a terminal:** a desktop app or Codex's shared background server is never stopped.
  Close the session there yourself.
- **opencode started by hand:** agentctl can't tell which process has the session, so quit it
  yourself first.

A new Codex or opencode session has no id of its own until its first message, so agentctl
gives it a temporary one (`new-a1b2`), and `send`, `attach` and the dashboard work with it.
After the first message it shows up under its real id, and the temporary id keeps working.

## Agents coordinating agents (MCP)

`agentctl mcp` is an MCP server, so one agent can see and coordinate your others, including
across vendors. For example, a Claude session can hand work to Codex.

```sh
claude mcp add -s user agentctl -- agentctl mcp                  # read-only
claude mcp add -s user agentctl -- agentctl mcp --allow-write    # also send, queue, start
codex mcp add agentctl -- agentctl mcp
```

| tool | what it gives the agent |
|---|---|
| `list_sessions` | every session, most urgent first, flagged when it needs approval, may be stuck, overlaps another, or has follow-ups queued |
| `read_transcript` | a session's recent messages and tool calls, labelled as untrusted |
| `whats_new` | replies you haven't read, and the files touched |
| `check_file` | before editing: who else edited this file recently, and who's working in its repo |
| `usage` | Claude Code and Codex plan limits |
| `wait_for` | blocks until a session finishes its turn, then returns its reply. It waits locally, so the agent spends no tokens polling |
| `send_message` · `queue_followup` · `start_session` | with `--allow-write` only; work starts in a fresh agent chat by default |

Agent messages use the target's agent type and project folder to create a **new chat**.
They do not append to your conversation or copy its transcript. Both message tools return
the new chat id; use that id when waiting for its answer. `new_chat=false` can continue only
a chat that agentctl positively recorded as agent-created. User-started and older chats with
unverified origins are protected, including when the agent tries to queue a follow-up.
Human replies in the dashboard and regular CLI keep their existing behavior.

The daemon saves chat provenance and checks it again at delivery. It identifies MCP callers
and shells descended from known agent processes; agents launched by agentctl also inherit
a caller marker for CLI requests. This policy governs agentctl messaging, rather than
sandboxing all shell access. Setup and permission prompts still require a human; pending
messages wait locally, and uncertain deliveries are not automatically replayed.
Older pending queues without a verified sender are held for review; cancel and resend that
work from the dashboard to authorize it as a human message.

Example: "Start a Codex session in ~/api with the migration task, wait for it, then tell the
frontend session the new endpoint."

**What it never does:**
- **Answer another agent's prompts.** Approve, deny, close and adopt aren't available to
  agents. A send is refused while the target has a permission or trust menu on screen, so
  typed text can't answer it.
- **Pass arbitrary flags.** `start_session` takes a model name, never flags.
- **Treat transcripts as instructions.** Other agents' transcripts are returned as data.

## Teams: one coordinator, several sessions

Run `agentctl teams`, press `g` in the dashboard, or choose **Teams** from Ctrl+P.
Create a team with a goal and coordinator session, then use **Connect worker** to add
sessions. The interactive graph shows each role, connection permission, and session state.
Select a node to edit its scope, rebind it, read its transcript, reply, or open its terminal.
**New session** uses the dashboard's existing session launcher.
Set the team's worker permission ceiling in **Team details**. The coordinator sees available
session candidates and current grants, and can propose worker setup changes, including before
the first run. New teams default to the **Propose** ceiling. Review these under **Setup
proposals**; approval applies the changes and pauses the run for review. The coordinator cannot
change the ceiling or apply its own proposals.

Each connection has one of three permissions:

| Permission | Coordinator can do |
|---|---|
| Observe | Read that worker's transcript |
| Propose (default) | Suggest assignments for human approval |
| Delegate | Start a fresh agent chat in that worker's project and queue the assignment |

Use **MCP setup** to get the private stdio command and server configuration. Register it
in the coordinator's agent and reconnect that session's tools. This registration is manual;
the app does not inject tools into a running agent. The scoped server exposes only
`get_team`, `read_worker`, `propose_team_setup`, `assign_worker`, `assignment_result`, and
`wait_for_assignment`.
Connection grants are checked by the daemon on every call. Keep the credential file private
and configure this server for the coordinator only. This scopes agentctl tools; it does not
sandbox an agent's shell or filesystem access. Permission ceilings control team routing grants;
native shell, file, and tool permission prompts still require human decisions. The existing
global MCP mode remains separate.

**Start / Resume** enables routing without sending a prompt. **Brief coordinator** opens
the goal and role instructions in the normal composer for you to review and send.
Creating, editing, inspecting, waiting, and routing run locally without model calls.
Messages that make an agent reason or work consume that agent's normal tokens.
Coordinators are not automatically awakened when results arrive; a running coordinator can
use the local wait tool, or you can reply to it yourself.

Select assignments in the graph to inspect the full request and reported reply, approve a
proposal, cancel pending work, or accept a result. A reported reply is not verification that
the task is correct. Permission prompts in worker sessions remain human decisions.
Worker connections are references for the agent type and project. Each assignment opens a
separate agent chat, and its delivery chat id is saved with the assignment. Opening an
assignment's transcript or reading its result follows that chat; the user's reference
conversation remains untouched. Include the necessary context in each assignment.
**Pause** stops future sends while running agents continue. Taking over a worker, replying
to it, queueing a human message, or opening its terminal holds future team sends until you
explicitly **Release** it. Connection edits pause the run and cancel undelivered assignments;
resume explicitly after reviewing them. A delegated worker can have one active coordinator.

Teams, runs, events and assignment receipts survive daemon restarts. Runs restart paused;
uncertain deliveries are never automatically retried. Inspect the worker transcript before
closing unknown delivery tracking. Native terminal input cannot guarantee exactly-once delivery.
This first version uses coordinator-to-worker trees; nested coordinators and worker dependency
edges are not supported.

For scripts, `agentctl teams --help` lists create/add/setup/start/pause/resume/assign/review
and other human controls. `agentctl teams brief <team>` prints the briefing without sending it.

## Uninstall

```sh
agentctl uninstall          # hooks (settings.json restored), launchd daemon, alias block
uv tool uninstall agentctl
rm -rf ~/.agentctl          # state, logs, backups
```

## Troubleshooting

- **Anything:** run `agentctl doctor`. Daemon logs are in `~/.agentctl/logs/`.
- **Claude shows "API Usage Billing"** in a session started over SSH: the daemon isn't running
  under launchd, so it can't read your Keychain login. Run `agentctl daemon install`.
- **`agentctl daemon stop` refuses:** a Codex turn started with `agentctl send` is still
  running, and stopping would abort it. Wait, or add `--force`.
- **A session stays read-only:** see "Which sessions you can control".

## How it works, and safety

- **The daemon** (`agentctld`) keeps all state. The CLI and dashboard are thin clients on
  `~/.agentctl/agentctl.sock`, which is mode 0600 and checks the caller's user id.
- **Claude Code:** read from `~/.claude/sessions/*.json` and the transcripts, plus hooks.
- **Codex:** read through `codex app-server` and its rollout files.
- **opencode:** read from its SQLite database (opened read-only), plus a small plugin loaded
  only into the opencodes agentctl starts. Your opencode config is never edited.
- **Agent data is read-only.** agentctl never writes to agent transcripts or databases.
- **Typing into sessions** uses tmux with arguments passed as arrays, never through a shell.
- **Message contents aren't logged** unless you run the daemon with `--debug`.

The formats agentctl relies on, as verified, are in [docs/formats.md](docs/formats.md).

## Development

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

## License

[MIT](LICENSE)

Fresh agent messages accept an explicit intended `model`; omitted models use the agent default. Team workers can save a default with `teams add --model`, and `teams assign --model` overrides it per assignment (an empty value requests the agent default). Assignment and message receipts record the requested/defaulted selection; they do not establish the effective runtime model. Model requests never resume user chats.

Watch shows ended sessions whose last activity is within 24 hours. `e` toggles those rows; `f` searches older history. Hidden rows remain hidden until unhidden or new activity restores them. Delivery holds show the reason beside queued work; inspect the terminal and sent history before retrying an uncertain send.
