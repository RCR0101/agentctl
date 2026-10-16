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
`agentctl daemon stop; agentctl daemon start`. Without `--editable`, reinstall after pulling.

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

On a phone it shows a list of cards. Tap a card (or Enter) to open the session; Esc goes back.

| key | does |
|---|---|
| `s` | send a message |
| `a` / `d` | approve / deny (or tap the option buttons) |
| `1`–`4` | pick an approval option |
| `i` | interrupt |
| `v` | the session's terminal, live, with tappable keys (↑ ↓ ⏎ esc 1 2 3 y n…), for prompts the transcript doesn't show: "trust this folder?", update menus, pickers |
| `c` | copy the last reply (in `v`, the screen) to your clipboard, even over SSH |
| `r` | rename (agentctl's own name; the agent's title is untouched) |
| `n` | new session: pick the agent and a folder (recent ones listed) |
| `u` | what's new since you last looked; pick one to open it |
| `p` | group the list by project (git repo) instead of by status |
| `l` | queue a message, sent once the agent is free |
| `o` | adopt a read-only session (moves it into agentctl) |
| `x` | close a session (quits the agent; it stays resumable) |
| `t` | attach to the session's terminal (`Ctrl-] d` to come back) |
| `/` | filter |
| `e` | show or hide ended sessions |
| `m` | raw text instead of rendered Markdown |
| `b` | bell (and a Mac notification) when something needs approval |
| `?` | all keys |
| `q` | quit |

While attached to any agent in agentctl's tmux, `Ctrl-] w` opens this dashboard over it, and
`q` goes back.

Across sessions, the dashboard and `ls` show:

- **Unread:** cards say "3 new" for replies you haven't read. Opening a session (or
  `agentctl tail`) marks it read, and its transcript has a "── new since 09:52 ──" line.
- **Overlaps:** ⚠ marks two live agents working in the same git repo at once, or that edited
  the same file in the last two hours. A toast appears when a new overlap starts.
- **Queued follow-ups:** ⏳ marks messages waiting to be sent. Each goes out once the agent is
  waiting for input, one per finished turn.
- **Plan usage** (5-hour and weekly windows) for Claude Code and Codex in the title bar, and
  in `agentctl usage`. Claude Code reports these only to its status line, so the hooks install
  wraps yours: your status line still shows as before, and no tokens are used.

Each run of tool calls is folded into one line; tap it (or Tab to it and press Enter) to
expand. Drag to select text; Ctrl+C copies it to the clipboard of the terminal you're using.

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
