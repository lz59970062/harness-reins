# Claude Code integration

[← Back to README](../README.md)

Reins works with Claude Code in the CLI and in the Claude desktop app (Code tab). This page explains
how a callback reaches a Claude Code session and what is needed for each route.

## Two delivery routes

| Route | How it works | Needs |
| --- | --- | --- |
| **Live (`reins wait`)** — recommended | The session runs `reins wait --task <id>` as a background Bash command. Claude Code wakes the session when a background command exits; `reins wait` exits as soon as the callback is queued and prints it. | Nothing beyond Reins. Works in the desktop app and the terminal CLI. |
| **Headless resume** | When the session is closed, the daemon runs `claude -p --resume <session>` from the directory where the session started, and later callbacks go into that same process. | A standalone `claude` CLI that is signed in (`claude auth login`). |

A third route, the **Reins channel** (`reins claude`), pushes callbacks into a terminal session without
any waiter; see [below](#reins-channel-research-preview).

Why live delivery matters: a headless resume appends a turn to the session's transcript, but a
session that is already open in the app or terminal does not display it. `reins wait` instead hands
the callback to the session you are looking at.

## Workflow

```bash
# 1. Submit (returns immediately). stderr prints the exact wait command.
reins run --cwd "$PWD" --task "train model" -- python train.py

# 2. In Claude Code, run the printed command with run_in_background: true
reins wait --queue-dir ~/.reins/queue --task <task-id>

# 3. When it exits, the session wakes with the callback. Inspect, then:
reins ack <callback-id>
```

The installed Claude skill (`~/.claude/skills/harness-reins/SKILL.md`) teaches Claude this
workflow, so in practice you just ask Claude to run something long.

### `reins wait` reference

`reins wait` exists only for Claude Code. It refuses to run without `CLAUDE_CODE_SESSION_ID` (or an
explicit `--session`) and only ever touches callbacks bound to Claude Code; Codex and other agents
keep receiving callbacks from the daemon and are never told about this command.

| Option | Meaning |
| --- | --- |
| `--task <id>` | Only this managed task's callback. |
| *(no `--task`)* | Any unacknowledged callback bound to this session — use with `reins done`. |
| `--session <id>` | Session to watch (default: `CLAUDE_CODE_SESSION_ID`). |
| `--timeout <s>` | Give up after this many seconds (exit 3). Default: wait forever. |

One rule: a callback keeps coming back until it is ACKed. Exit codes: `0` callback printed (or the
task's callback is already acknowledged), `2` not a Claude session / unknown task, `3` timeout, `4`
the callback went to a headless resume of this session (see below) and is handled there.

### How it coordinates with the daemon

- A waiter holds a lock file under `<target-locks>/live-watchers/<session-hash>/`. While any waiter
  for a session is alive, the daemon does not start a headless resume for that session's Claude
  callbacks. Closing Claude Code kills the waiter, the OS drops the lock, and the daemon takes over.
- `reins run` returns before the session has started its waiter, and a task can end in that gap (a
  command that fails at once). So a submission made from inside the Claude session it targets
  (`CLAUDE_CODE_SESSION_ID` is that session; background tasks enabled, no channel, callback not
  manual) also writes a reservation file in the same directory. A script that names a session with
  `--session` from outside it gets no reservation: nothing there will start a waiter. Until it
  expires, 120 s later by default (`REINS_CLAUDE_WAIT_GRACE_SECONDS`, `0` turns it off), the daemon
  treats the session as live. A session that closed before starting its waiter is resumed once the
  reservation expires.
- The daemon holds the session's target lock for as long as a headless resume runs. It passes the
  resume `REINS_RESUMED_TARGET`, so a `reins wait` that the resumed session itself starts can still
  claim that session's callbacks instead of waiting for its own process to exit.
- A waiter outside that resume never waits silently on a callback the resume holds: after 3 s it
  prints a notice naming the callback and exits `4`. A `--task` waiter also exits `0` once that task's
  callback is ACKed, even while the resume that ACKed it is still running.
- The waiter claims a callback with the same atomic `pending → running` move the daemon uses, so a
  callback is never delivered by both at once.
- After printing, the callback returns to `pending` and the daemon waits 30 minutes for the ACK
  before resuming its normal retries, or less if the window closes first: then it is due at once.
  `reins ack` moves it straight to `done`.
- A waiter whose window has closed exits within seconds, so a shell the window left behind cannot
  keep the daemon away. Where Reins cannot see the window (no `~/.claude/sessions` record), the waiter
  runs until it is killed or finds a callback.
- All of this lives in `src/harness_reins/harness/claude/session.py`; the shared CLI only calls into it.

## The Reins hook

`reins setup` adds one entry to `SessionStart` and one to `Stop` in `${CLAUDE_CONFIG_DIR:-~/.claude}/settings.json`,
keeping everything else in that file. Each runs `reins _claude-hook` in the background with `asyncRewake`:
when a callback is ready it exits 2, and Claude Code wakes the model in that same window. An idle window gets it
within a second; during a turn it arrives when that turn ends, together with anything else that came meanwhile.
The next `Stop` starts it again. It needs no launch flag. It is verified in the terminal with Claude Code 2.1.318
and in the desktop app (Linux over SSH, CLI 2.1.309), where the app labels each delivery "Stop hook blocking error"
although nothing failed. Resuming a closed desktop session has not been tested yet.

- The hook holds the same live-watcher lock as `reins wait` (`hook-*.lock`). `reins wait` steps aside while it runs.
- A delivered callback is raised again only after 30 minutes without an ACK, at most twice, so a turn that does not
  ACK cannot wake the model in a loop. If it is still unacknowledged when the window closes, the daemon resumes it
  at once.
- It leaves at once in a headless run (`claude -p` does not exit while it lives) and when its window closes.
- Claude Code stops a hook after its one-week timeout, and only the next turn starts it again. Six hours before
  then the hook leaves. If a task started from that session is still running, or the session has a name, it wakes
  the window once with a note that asks for no reply, and that turn starts it again; otherwise it exits quietly.
- Where the hook is installed, `reins run` in an open window says the result arrives through the hook, and no
  `reins wait` is needed.
- `reins setup --no-claude-hook` skips it. `reins install-claude-hook [--remove]` adds or removes it on its own.
  Windows already open pick up the change after their next turn.

## Open windows are not resumed

Claude Code records each running session in `~/.claude/sessions/<pid>.json`. While a live process has the session
open, the daemon leaves its callbacks alone, because a resume beside it would fork the transcript. This holds for
job results and messages alike, with or without the hook. Without a hook, channel or waiter, the callback waits for
the window to close. Headless runs (`sdk-cli`) do not count as open, and a reused pid is told apart by its start time.

The daemon's target lock keeps it to one headless resume per session; other callbacks for that session wait for it to end.

## Headless resumes

A closed session has at most one `claude -p --resume` at a time, and a callback that comes due while it runs goes
into that process through Claude Code's streaming input. It arrives between model steps, or with the result of a
command that is still running. Nothing queues for a second process, so nothing forks.

- The process stays while a turn or a background task is running, and takes new callbacks until then. Reins then
  closes its stdin and it exits.
- A resume has no time limit by default, so unattended work can run for hours. `--resume-timeout SECONDS` on
  `reins daemon` or `reins setup` sets one. An upgrade keeps a limit already in a service definition until
  `reins setup` runs again.
- A stuck process is found from its stream instead and ended, and the callback is retried: no event for 30 minutes
  while it waits on the model or fails to exit after its input closes, or for 4 hours while a foreground tool runs.
  A background task may stay quiet as long as it runs. `--resume-stall SECONDS` changes the 30 minutes; 0 turns
  the check off.
- Sessions do not wait for each other, and all resumes count toward your usage. Up to eight deliveries wait for
  their ACK at once (`--max-resumes`, 0 for no cap); more results wait for a slot. A resume that has ACKed and keeps
  working takes no slot.
- A callback that went in but has no ACK when the process ends is due again at once, within the usual retry limit.
- A CLI that refuses the streaming flags gets the callback as a plain prompt, and later callbacks wait for it to
  exit. That path has no stall check, so set `--resume-timeout` if such a CLI can hang. `claude -p` drops channel
  events, so the channel plays no part here.
- Just before it starts `claude -p --resume`, the daemon checks once more that no window has the session open, so a
  window closed and opened again in between gets no resume beside it.

## Reins channel (research preview)

[Claude Code channels](https://code.claude.com/docs/en/channels) let a local MCP server push events
into a running interactive session. `reins claude` starts Claude Code with Reins's channel server:

```bash
reins claude                       # new session
reins claude -- --resume <id>      # anything after -- goes to claude
```

It runs `claude --mcp-config <reins server> --dangerously-load-development-channels server:reins`.
Claude Code asks you to confirm the development channel at every start (custom channels are not on
the preview allowlist), then shows `server:reins · no MCP server configured with that name` — that
notice is cosmetic for servers passed with `--mcp-config`; delivery works.

How it behaves:

- The server is a child of that Claude window and lives exactly as long as it. It reads the
  window's current session from `~/.claude/sessions/<claude-pid>.json`, so `/clear` and `/resume`
  are followed.
- While alive it holds the session's live-watcher lock (`channel-*.lock`), so the daemon never
  starts a headless resume for that session — reopening a session with `reins claude` cannot fork it.
- Each queued callback for the session is pushed once as `<channel source="reins" callback_id=…>`;
  Claude handles it even when the window is idle. It still needs `reins ack`.
- `reins run` prints "arrives through the reins channel" instead of a `reins wait` command, and a stray
  `reins wait` in that session exits instead of printing the callback a second time.
- Claude Code never acknowledges channel events and drops them silently when a channel is not
  registered. The server therefore only takes over when its parent `claude` carries the opt-in flag,
  and if a pushed callback is still unacknowledged after the 30-minute ACK grace it releases the lock
  so the daemon delivers it.

Limits: terminal CLI only — the desktop app runs Claude Code through the Agent SDK, which ignores the
development flag, so desktop sessions keep using `reins wait`. claude.ai Team/Enterprise organizations
must enable channels (`channelsEnabled`). The flags and protocol may change while channels are in
preview. Verified on Linux with Claude Code 2.1.296 (idle window woke, handled and ACKed the
callback; lock released when the window closed).

## Per-agent skills

Each agent gets its own installed skill, so Claude-specific usage never reaches other agents:

| Agent | Installed to | Contents |
| --- | --- | --- |
| Codex | `${CODEX_HOME:-~/.codex}/skills/harness-reins/` | `SKILL.md` (full skill, no `reins wait`) + `agents/openai.yaml` |
| Claude Code | `${CLAUDE_CONFIG_DIR:-~/.claude}/skills/harness-reins/` | `SKILL.md` (short, built around `reins wait`) + `REFERENCE.md` (the shared full skill) |

## Finding the Claude binary

The desktop app does not put `claude` on PATH, nvm installs live under a per-Node-version
directory, and launchd/systemd services see a minimal PATH. Reins looks for Claude in this order:

1. `REINS_CLAUDE_BIN` / `reins setup --claude-bin` (an explicit path or name is always honored)
2. `claude` on PATH
3. Standalone installs: `~/.local/bin`, `~/.claude/local`, `/opt/homebrew/bin`, `/usr/local/bin`, `~/.npm-global/bin`, `~/.bun/bin`, the newest `~/.nvm/versions/node/*/bin`
4. `CLAUDE_CODE_EXECPATH` from the launching session
5. On a Linux host used by the desktop app over SSH: the newest CLI under `~/.claude/remote/ccd-cli/`
6. On macOS: the newest desktop-app bundle under `~/Library/Application Support/Claude/claude-code/`

The macOS bundle is found last because it authenticates through the app and cannot run headless on
its own. The Linux SSH CLI is an ordinary binary that shares the host's `~/.claude` sign-in, so it
can run the headless fallback once `claude auth login` has been done on that host. Both are
versioned: a configured path to either that disappears after an app update is rediscovered.

## Resuming from the right directory

Claude Code stores each session under a slug of the directory it started in, and `--resume <id>`
looks the session up from the current directory. A session that later `cd`s elsewhere, or a task
submitted with a different `--cwd`, would otherwise fail to resume. Reins reads the session's first
recorded `cwd` from `~/.claude/projects/*/<session-id>.jsonl` (respecting `CLAUDE_CONFIG_DIR`) and
starts the headless resume there.

## Checking readiness

```bash
reins doctor --agent claude --session "$CLAUDE_CODE_SESSION_ID"
```

The JSON report contains a `claude_code` block with the binary that was found, whether this is a
desktop session, and whether the headless fallback can work. Only the desktop bundle being present
is not a configuration error — live delivery still works — but delegating to a Claude child with
`reins agent claude` is reported as `child_agent_desktop_only`, because the child cannot sign in.

## Platform status

| Platform | Status |
| --- | --- |
| macOS | Verified with the Claude desktop app (local sessions) and the CLI. |
| Linux | Verified end to end with the desktop app's SSH sessions (`CLAUDE_CODE_ENTRYPOINT=claude-desktop`, `screen` backend): `reins run` → background `reins wait` wakes the live session → daemon defers while the waiter lives and takes over once it exits → `reins ack`. The terminal CLI uses the same mechanism. |
| Windows | Untested. |

### Linux notes

- Without a systemd user bus (SSH-only hosts, containers) tasks run under GNU `screen`; install it
  with your package manager. Live delivery does not depend on the task backend.
- Run `reins setup` once on the Linux host so the Claude skill lands in that host's
  `~/.claude/skills/` — the desktop app's SSH sessions read skills from the remote host.
- The `reins wait` command printed by `reins run` uses the `reins` found on PATH; make sure it is the
  same install as the daemon (`reins --version`), or older installs will reject `wait`.
- If `claude` on PATH is a wrapper that cannot run outside your login shell, pin a working binary
  with `reins setup --claude-bin <path>` so the headless fallback can resume.
