# Changelog

## 0.13.1 — 2026-11-06

- `reins setup --now` restarts the daemon when the service now runs a different
  command, as after an upgrade into another installation or with new daemon flags.
  A reload re-executes the running daemon's own interpreter and arguments, so
  upgrading to 0.13.0 and running `reins setup` kept the old installation and the old
  `--resume-timeout 3600`. Linux compares the running process with the unit, macOS
  with the LaunchAgent. While a callback is being delivered the daemon is not
  restarted: Linux setup says so and leaves it, macOS reloads it and asks you to run
  `reins setup --now` again later.

## 0.13.0 — 2026-11-06 (tagged; publish 0.13.1 instead)

- A callback that comes due while a headless Claude Code resume is running now goes
  into that process through Claude Code's streaming input, instead of waiting for it
  to exit. There is still one process per session and no fork. Verified with Claude
  Code 2.1.318 on Linux.
- A long resume no longer holds up other sessions, or the daemon's own work: each
  resume runs on its own. Up to eight wait for their ACK at once (`--max-resumes`, 0
  for no cap), so a burst of results does not start a model session for each at the
  same moment; the rest wait for a slot. A resume that has ACKed and works on takes no
  slot, and ACKed callbacks are still finished at once.
- Resumes have no time limit by default, for Codex and Pi as well as Claude, so
  unattended work is not killed at one hour. `--resume-timeout` still sets a limit.
  A service installed earlier keeps its limit until `reins setup` runs again. Waiting
  for a Pi or Codex process that Reins did not start still gives up after one hour.
- A streamed Claude resume that stops making progress is ended and retried: no stream
  event for 30 minutes while it waits on the model or fails to exit after its input
  closes, or 4 hours while a foreground tool runs (`--resume-stall`, 0 to turn it off).
  A background task may run quiet as long as it needs. Codex and Pi resumes, and a
  Claude CLI too old for streaming input, have no such check; give them
  `--resume-timeout` if one can hang.
- A callback that went into a resume without being ACKed is due again as soon as that
  resume ends, rather than after 30 minutes. The same holds for one the hook or
  `reins wait` showed to a window that then closed. The daemon checks for an open
  window again just before it resumes, so a window closed and reopened in between
  is not forked.
- The hook raises a callback that stays unACKed in an open window again after 30
  minutes, at most twice.
- A background `reins wait` whose window has closed exits within seconds. Before, a
  shell left behind by the window kept it alive, its lock kept the daemon from
  resuming the session, and the callback reached no one.
- The Claude Code skill forbids starting long work with `nohup ... &`, `setsid` or
  `disown`, which nothing tracks: such a job ends unseen.
- Claude Code stops a hook after a week, and only the window's next turn starts it
  again. The hook now leaves before then: if a task from that session is still running,
  it has an active goal or it has a name, it wakes the window once with a note that needs no reply,
  which starts it again; otherwise it exits quietly.
- A goal's 3-hour inactivity reminder now reaches the session the way its job results
  do: a Codex Desktop thread through its queue, a Pi session through its channel. It
  always used the CLI route, which an open Desktop thread refuses. Goals started
  earlier keep the CLI route.
- The skills ask agents to start a goal before the first job of any request that needs
  several jobs or turns, or runs unattended, and Codex and Pi now explain goals at all.
- `reins status` lists failed callbacks too, with the retry and cancel commands. They
  are where automatic delivery gave up, and were hidden before.
- `reins run` and message envelopes no longer ask for `reins wait` where the hook is
  installed and the session is an open window.
- Windows: a read of a task or callback record that lands while it is being replaced,
  and a replace that lands while it is being read, are retried for up to 2 seconds
  instead of failing with `PermissionError`.

## 0.12.0 — 2026-11-05 (tagged, not published to npm)

- DSH is a native callback parent and child worker. `DSH_SESSION_ID` binds the
  exact conversation, `reins wait` runs as a DSH background job, and its job
  completion notification wakes the live session. DSH callbacks are live-only:
  the desktop preset cannot be resumed by `dsh --profile headless`, and Reins
  never opens a second writer, so an unclaimed callback stays queued until the
  same session waits again.
- `reins agent dsh` runs `dsh --profile headless` with the prompt on stdin and
  captures the final answer from stdout. It strips the parent DSH markers and
  keeps provider credentials. DSH children write no `report.json`: the headless
  sandbox cannot reach the task directory and has no approval channel.
- `reins setup --with-dsh` installs the focused DSH skill under
  `${DSH_HOME:-~/.dsh}/skills/harness-reins` and records `--dsh-bin` for the
  coordinator. `install-skill --target dsh` and `install-skill --target all`
  include DSH; `reins doctor --agent dsh` explains the live-only route.
- DSH sessions can use the messages from 0.11.0: `reins wait` delivers them, and
  `--wake resume` starts nothing and tells the receiver why.
- Real DSH sessions are verified on macOS desktop and on Linux (`dsh` 0.2.1-rc.5,
  ACP and web UI). Windows has unit tests only.
- A message now reaches a Claude Code session or a `reins pi` session the way a job
  result does: with nothing listening, the session is resumed headless to handle
  it, which costs model usage. `--wake never` starts nothing. Codex stays on its
  queue by default, since Reins cannot tell an open Codex from a closed one;
  `--wake resume` still resumes it. Other Pi sessions get it when they reopen, and
  `--wake resume` no longer fails on them. The per-thread resume limit is gone,
  along with `thread_resumes`.
- A live session that just registered, sent a message or received a batch gets 2
  minutes to start `reins wait` before a resume is tried next to it.
- An open Claude Code window is no longer forked. While a live process has the
  session open, the daemon does not resume it, for job results as well as messages;
  it does so once the window closes. This changes job results: without a hook,
  channel or waiter, one now waits for the window instead of being handled beside it.
- `reins setup` adds a Claude Code hook that delivers into the open window and wakes
  it when idle, with no `reins wait`. Verified in the terminal with Claude Code
  2.1.318 and in the desktop app (Linux over SSH, CLI 2.1.309): results and
  messages reach the open window within a second, with no headless resume and no
  fork. Resuming a closed desktop session has not been tested yet.
  `--no-claude-hook` skips it; `reins install-claude-hook [--remove]` manages it.

## 0.11.0 — 2026-11-03

- Sessions on one machine can now message each other, across Codex, Claude Code
  and Pi. Name a session with `reins session register NAME`, then use `reins send`,
  `reins reply` and `reins inbox`. A message is durable, belongs to a thread, and
  shows what happened to it (`queued`, `published`, `acked`, `failed`, `unknown`,
  `expired`). See the new messaging guide.
- The same rule on every harness: a message never starts a process unless the
  sender asks with `--wake resume`. A busy session gets it when it can take it. A
  closed session gets it when it next opens: Codex holds it in its queue, Pi and
  Claude Code keep it in the inbox. In Claude Code, `reins wait` now also delivers
  messages.
- Agents cannot run up a bill talking to each other. Replies in a row, messages
  per thread and per minute, and messages per hour between two sessions all have
  limits, set in `~/.reins/link/config.yaml`. At a limit the command refuses, the
  agent is told to stop, and a person sees an alert in `reins status`,
  `reins message alerts` and `reins doctor`.
- Message text stays out of the model's envelope and is marked as written by
  another agent, so it cannot pose as Reins or as the user. Subjects and names are
  validated, and only the receiving side generates the ACK command.
- A headless resume can now also write to `~/.reins/link`, so a resumed session
  can answer messages from inside its sandbox.
- Multi-machine messaging is designed but not included: `name@machine` is refused
  with a clear error. The direction and the contributor rules are in
  `docs/design/link-protocol.md` and `CONTRIBUTING.md`.
- Upgrade note: run `reins setup --force --enable --now`. It restarts the
  coordinator and updates the skills, so agents learn the new commands. Open
  sessions read the updated skill the next time they load it.

## 0.10.1 — 2026-11-02

- `reins setup` no longer generates the Codex Desktop launcher on macOS or
  Windows, and the shared Core behind it is gone. Open Desktop normally; callbacks
  use `codex-queue`. An old launcher now prints this advice and exits, and setup
  names any leftover launcher files you can delete.
- A saved `--callback-mode desktop` callback keeps its result. On macOS and
  Windows it is held in failed with the exact command to move it: `reins retry
  --id ID --callback-mode codex-queue`. The task is not run again. On Linux these
  records keep delivering through the local App Server socket.
- New work cannot select `--callback-mode desktop`; the error points to
  `codex-queue`. `reins desktop`, `--desktop-app`, `--desktop-launch-mode` and
  `reins-desktop-core` are removed. `--no-desktop-launcher` is accepted and ignored,
  so existing install scripts keep working.

## 0.10.0 — 2026-11-01

- Codex Desktop callbacks work on a normally started Desktop. `auto` now selects
  the new `codex-queue` route, which publishes once through the public
  `codex queue --thread --message` command; the running Codex picks the message
  up by itself, including when the chat is busy. No launcher, private MCP pipe
  or `--resume` is involved. The shared Core launcher and `--callback-mode
  desktop` remain only for records that already use them.
- A queued callback stays pending until the chat ACKs and is never sent twice
  automatically. A missing ACK does not hold back later callbacks for that chat,
  and `reins status` shows what is waiting.
- If `codex queue` reports an error, the callback can be repaired with `reins
  retry`. A killed command or an unreadable response is an unknown outcome and
  is not retried. The command runs with the submitting session's environment, so
  a service coordinator whose PATH lacks `node` can still run an npm Codex.
- New opt-in `--callback-mode app-server --app-server-socket PATH` sends native
  tool output to a thread already loaded on an explicit App Server (macOS/Linux).
- Upgrade note: restart the coordinator with `reins setup`; an older one leaves
  `codex-queue` records untouched rather than misrouting them.

## 0.9.3 — 2026-10-30

- `reins wait` and the `reins claude` channel work on Windows. Registering a
  live waiter locked a staging file and then renamed it into place; Windows
  cannot rename (or delete) a file held open with share mode 0, so every wait
  failed at start with `PermissionError: [WinError 32]` and callbacks only
  arrived through a headless resume after the session closed. On Windows the
  waiter file is now created already locked under its final name, and it is
  closed before it is deleted.
- The Pi extension resolves a `REINS_PI_CHANNEL_ROOT` that is reached through a
  symlink, or does not exist yet, the way Reins does. It used the path as
  given, so the owner record it wrote never matched the route Reins froze in
  the callback ("Pi callback owner does not match the bound session") and Pi
  callbacks could not be delivered live. Existing Pi processes pick up the
  fix after `reins setup` reinstalls the extension and Pi reloads.
- The Pi, template and channel tests no longer assume the temporary directory
  has no symlinks in its path; on macOS `/var` leads to `/private/var`. The Pi
  live-callback tests now run through a symlinked directory on every platform.
- The `reins claude` channel recognizes its opt-in on Windows. It reads the
  parent Claude process's command line from `/proc` or `ps`, neither of which
  exists on Windows, so it never pushed a callback there; it now asks CIM
  (`Win32_Process`) through PowerShell.
- Claude desktop CLI paths (the macOS bundle, the SSH-session `ccd-cli`) are
  recognized with Windows path separators too.
- The whole CI matrix passes for the first time: Linux, macOS (with real
  launchd), Windows (with Task Scheduler and the native coordinator lifecycle)
  and the container job. Cleared test environments keep a Windows home
  (`USERPROFILE`), POSIX-only expectations (paths, shell quoting, mode bits,
  newline translation, case) follow the platform, and stale fixtures were
  repaired: coordinator programs that never imported the module they called,
  a launchd test reading the pre-`reins.result/1` exit code, a container test
  expecting the pre-canonical-launcher daemon command, and a stderr file left
  open while its directory was removed.
- Known limitation: on native Windows, input piped into `reins run` (a heredoc
  script) is not handed to the task yet; its test is skipped there.

## 0.9.2 — 2026-10-29

- The live-waiter reservation from 0.9.1 applies only to a submission made
  from inside the session it targets (`CLAUDE_CODE_SESSION_ID` equals the
  bound session). 0.9.1 also held callbacks for 120 s when a script named a
  Claude session with `--session` from outside it, where no waiter will ever
  start; the npm package smoke test caught the delay.

## 0.9.1 — 2026-10-29

- A Claude Code session no longer gets a headless twin when a task ends before
  the session has started `reins wait`. `reins run` returns first and the
  waiter starts a few seconds later; a task that failed at once (for example a
  Codex child refusing a directory outside git) queued its callback in that
  gap, the daemon saw no waiter and ran `claude -p --resume` on the session
  that was still open. A submission from a live session now reserves its
  callbacks for the waiter for 120 s (`REINS_CLAUDE_WAIT_GRACE_SECONDS`, `0`
  turns it off); a session that closed before waiting is still resumed after.
- `reins wait` no longer hangs while the daemon resumes its session headless.
  A waiter started inside that resume can claim its own session's callbacks
  (the daemon sets `REINS_RESUMED_TARGET`), where it used to wait on the lock
  held for its own process. A waiter elsewhere prints which callback went to
  the headless resume and exits 4 instead of waiting in silence, and a
  `--task` waiter returns once that task's callback is ACKed even while the
  resume that ACKed it is still running.
- `reins agent codex` passes `--skip-git-repo-check`, so a child given a
  `--cwd` outside a git repository runs instead of exiting within a second.

## 0.9.0 — 2026-10-26 (Harness Reins)

- Rename Agent Traffic Control (ATC) to Harness Reins: a harness for your
  harnesses. Codex, Claude Code and Pi are harnesses; Reins dispatches work to
  any of them and brings the result back to the session that asked.

  | | ATC 0.8 | Reins 0.9 |
  |---|---|---|
  | command | `atc` | `reins` |
  | npm | `agent-atc`, `@agent-atc/linux-x64` | `harness-reins`, `@harness-reins/linux-x64` |
  | Python | `agent-atc`, `import agent_atc` | `harness-reins`, `import harness_reins` |
  | state | `~/.atc`, `ATC_*` | `~/.reins`, `REINS_*` |
  | service, skill | `agent-atc` | `harness-reins` |
  | records | `atc.callback/1` … | `reins.callback/1` … |
  | envelope | `[atc]`, `atc-compact/1`, `[atc-status]` | `[reins]`, `reins-compact/1`, `[reins-status]` |

  Record structures are unchanged; only the names move.
- `reins migrate` retires ATC as well as Long Task Callback. Each generation
  moves on its own: one that still has callbacks to deliver or tasks running is
  reported and left alone, so a busy LTC no longer blocks an idle ATC.
  `--only atc|ltc` moves one. ATC's systemd drop-ins (such as a
  `NoNewPrivileges=no` override) are copied to `harness-reins.service.d`, and
  its `experts.yaml` moves to `~/.reins` with the `reins.experts/1` schema.
  State directories are archived, never deleted. A callback that is already
  acknowledged but still filed under running does not count as live work.
- `reins setup` restarts the coordinator instead of hot-reloading it when the
  running process lacks something only a restart applies: a changed
  `NoNewPrivileges` (so `--allow-privilege-escalation` takes effect at once) or
  `Environment=` lines such as agent binary paths and PATH. While callbacks are
  being delivered it leaves the daemon alone and says what to restart; `reins
  doctor` reports `coordinator_restart_required`.
- `reins doctor` looks for the callback agent CLI where the coordinator unit
  records it (`--codex-bin`, its PATH), not on the shell's PATH.
- Runs on Python 3.9 as declared: a module evaluated `str | None` at import,
  and before 3.12 argparse could not parse `reins agent continue|fork|expert
  <name> --name … -- <prompt>`.
- The Codex skill and troubleshooting cover resumed Codex turns that need the
  same permissions as the first one (`--sandbox-mode`, `--approval-policy`,
  `REINS_SANDBOX_MODE`, `REINS_APPROVAL_POLICY`).

## 0.8.2 — 2026-10-16

- `atc doctor` and `atc setup` honor systemd drop-ins (`<unit>.d/*.conf`,
  applied in filename order) when checking agent CLIs against
  `NoNewPrivileges`. 0.8.1 read only the unit file and reported
  `agent_cli_blocked_by_service_sandbox` on machines where a drop-in had
  already turned the hardening off, although resumes worked.

## 0.8.1 — 2026-10-16

- `atc setup` (systemd) and `atc doctor` start each agent CLI recorded in the
  unit with `--version` under the unit's `NoNewPrivileges=yes`
  (`systemd-run --user --wait --pipe`). A CLI that only fails with it, such
  as a launcher that re-executes through sudo, is reported as
  `agent_cli_blocked_by_service_sandbox` with the binary and its stderr line,
  instead of doctor saying ready while every headless callback fails.
- `atc setup --allow-privilege-escalation` (and `install-systemd`) writes the
  unit without `NoNewPrivileges=yes` for such launchers. The hardening is kept
  by default and doctor never adds the flag on its own unless the installed
  unit already opted out.
- A headless resume runs with the environment of the session that submitted
  the work. `atc run`, `atc agent` and `atc done` keep a private copy
  (`<queue>/environments/<callback id>.json`, mode 600) and the delivery worker
  starts `claude -p --resume`, `codex exec resume` and the like with the
  coordinator's environment overlaid by it, so model endpoints and keys set as
  variables, proxies and PATH reach the resumed session as they reach the task.
  Session markers (`CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID`,
  Pi's session variables, Claude's entrypoint, service markers) never pass on,
  and the coordinator's `ATC_HOME` and lock directory win. The copy is removed
  once the callback is acknowledged, done or canceled; a failed callback keeps
  it for `atc retry`. Callbacks without a copy resume as before.

## 0.8.0 — 2026-10-16 (Agent Traffic Control)

- Rename Long Task Callback (LTC) to Agent Traffic Control (ATC). The command is
  `atc`, the package `agent-atc`, the Python import `agent_atc`, the state home
  `${ATC_HOME:-~/.atc}`, services and skills `agent-atc`, and the callback tag
  `[atc]`. Environment variables `CODEX_LONG_TASK_WAKEUP_*`, `LONG_TASK_WAKEUP_*`
  and `LTC_*` become `ATC_*`.
- Give every callback a root and a kind suffix: a task's completion callback
  is `<task id>-c1` instead of the bare task ID, an `atc done` callback gets a
  fresh root (`<root>-c1`), and goal reminders are `g-<goal id>-c<N>` with the
  ordinal reserved in the goal record. Generated goal IDs are 8 hex digits.
- Callback envelopes use `atc-compact/1`: `Result: exited 0 · 2h 3m 32s`
  folds the exit code and runtime together, a new `From:` line names who did
  the work (`run on systemd-user`, `agent claude, model …`, or an external
  job), and the ACK is one line. `atc ack` takes the callback ID positionally
  (`atc ack <id>`; `--id` still works) and the generated command names the
  queue only when it is not the default one.
- Callback records carry `schema: atc.callback/1` and a `requires` list of
  delivery capabilities (`deliver.cli-resume`, `deliver.codex-desktop`,
  `deliver.pi-steer`, `deliver.pi-follow-up`) instead of versions 1–4. A
  tower leaves a callback that needs an unknown capability untouched in the
  queue. Records without the schema are refused with a hint to run
  `atc migrate`.
- Task results are `atc.result/1`: `observed` holds what ATC saw (state,
  exit code, attempt, RFC 3339 times, backend, log), kept apart from `report`,
  the child agent's own claims. `atc agent` asks the child to write a JSON
  report to `$ATC_REPORT_FILE` (opt out with `--no-report`); it is validated
  and bounded, and the envelope shows it as `Report: … (claimed, unverified)`.
  A missing report never fails a task.
  Interrupted, launch-failed and unrecoverable tasks also get a result,
  with `observed.state` and the recovery reason; an observed exit is never
  overwritten.
- Callback and task records name their target as `{"kind": "session",
  "session": …}` and record who submitted the work in `origin` (harness,
  session, directory and binding source), replacing `target_source`.
- Add `atc migrate`: it shows the plan by default and changes nothing;
  `--apply` archives the LTC state directory unchanged and removes the old
  coordinator service, skills, Pi extension and shell hook. It refuses while
  LTC still has callbacks or tasks in flight (`--wait` waits for them).
- `atc setup` makes the running installation the canonical ATC: it writes
  `~/.atc/bin/atc` and `~/.atc/install.json` (a frozen build copies itself to
  `~/.atc/versions/<version>-<build>/`), points services and pasted ACK
  commands at that launcher, and other installations forward their commands
  to it. `atc --version` shows a build ID (a digest of the package, or the
  commit for release builds) and `atc doctor` reports a coordinator running a
  different build.
- Child agents are named nodes that keep their sessions. `atc agent continue
  <node>` reuses a finished child with what it learned; `atc agent fork <node>`
  branches from any finished node and sees exactly what it knew then. Each node
  forks its parent's session, so finished nodes never change. Names are
  required (`--name`) and compose along the lineage (`research/review`).
  `atc context list|show|archive` shows chains as summaries and never prints a
  raw session.
- Add expert agents: `~/.atc/experts.yaml` (`atc.experts/1`) names presets of
  harness, model, reasoning effort and instructions. `atc agent expert <name>`
  runs one; `atc expert list|show|export|import` manage the file, and export
  inlines instruction files so the file moves to another machine unchanged.
- Remove `--last`. ATC only resumes an explicitly bound session and never
  falls back to "the most recent session"; stored callbacks must name one.
- Remove the `codex-long-task-wakeup` command alias and the automatic adoption
  of `${CODEX_HOME}/long-task-wakeup` state. Moving from LTC will be an explicit
  `atc migrate`.
- Input piped into `atc run` (a heredoc script, `-- python3 - <<'EOF'`) becomes
  the task's stdin; without piped input a task reads an empty stdin and never
  waits on a terminal.
- When Claude Code runs without background tasks
  (`CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`), `atc run` tells the agent to end
  its turn instead of running `atc wait`; ATC resumes the session with the
  callback.
- `atc agent continue|fork|expert` parse their options before the prompt
  (`--name` after the node name was folded into the prompt).
- Manual callbacks no longer report `callback_agent_unavailable`.
- The skills say that the command after `--` is argv, not shell code (use
  `bash -c` for sequences). The protocol notes that Claude Code's auto memory
  is shared by every session in a project, so a fork can see what later nodes
  saved there.

Entries below are from Long Task Callback and keep its original names.

## 0.7.1a2 — unreleased

- Move LTC's own state (queue, tasks, logs, callback hook, reminder policy,
  target locks) out of the Codex profile to `${LTC_HOME:-~/.ltc}`. On first run
  without `LTC_HOME`, an existing `${CODEX_HOME:-~/.codex}/long-task-wakeup` is
  moved there and replaced by a symlink, so installed services and queued
  callbacks that name the old path keep working. Codex Desktop launcher and
  bridge files stay in the Codex profile.
- Automatically deliver new live Pi callbacks through native steering, after
  the current tool-call batch and before the next model request; idle Pi starts
  a normal turn. Keep the existing CLI without delivery flags. Preserve older
  follow-up records, same-process ownership, cancellation and ACK rules; require
  a capable extension and use task v5/callback v4/mailbox v2 to prevent silent
  downgrade by older coordinators. Test receipt during a continuing tool loop
  using real Pi with a local provider, without paid model calls.
- Include measured task runtime in compact callbacks, for example
  `Duration: 2m 5.5s`, using recorded execution timestamps rather than callback
  delivery time. Keep the duration in the callback record across retries.
- Synchronize the MIT license and GitHub issue templates from `main`.

## 0.7.1a1 — unreleased

### Claude Code

- Add `ltc wait [--task ID]` for Claude Code only. Run in the background from a Claude Code session,
  it prints that session's unacknowledged callback when it is queued and exits, so Claude Code wakes
  the conversation that is actually open. While a waiter is alive the daemon leaves that session's
  Claude callbacks alone; after a live delivery it waits 30 minutes for the ACK before its normal
  retries. It refuses to run outside Claude Code and never touches other agents' callbacks. All
  Claude-specific logic lives in `claude_code.py`.
- Find Claude Code when `claude` is not on PATH (Claude desktop app, launchd/systemd services):
  standalone install locations, `CLAUDE_CODE_EXECPATH`, and the newest desktop-app bundle. A
  configured desktop-bundle path that disappears after an app update is rediscovered.
- Start headless `claude -p --resume` in the session's original directory, read from the session
  transcript, instead of the task's `--cwd`.
- Install a separate skill per agent: Claude gets a focused `SKILL.md` built around `ltc wait` plus
  the shared full skill as `REFERENCE.md`; the Codex skill does not mention `ltc wait`.
- `doctor`/health checks no longer report Claude as unavailable when only discovery finds it; the
  report gains a `claude_code` block describing the live and headless routes, and a Claude child
  backed only by the desktop bundle is reported as `child_agent_desktop_only`.
- `ltc run`/`ltc agent` from Claude Code print the exact background `ltc wait` command.
- Fix callback ACK commands when `ltc` is not on the worker's PATH: use the interpreter plus private
  entry point instead of a non-executable script path.
- Add `ltc claude [-- claude args]` (research preview): starts interactive Claude Code with LTC as a
  development channel. The channel server lives as long as the window, follows its current session,
  holds the session's live-watcher lock so the daemon never forks it with a headless resume, and
  pushes each callback into the session as a `<channel source="ltc">` event, even when it is idle.
  `ltc run` then skips the `ltc wait` hint and a stray `ltc wait` exits instead of duplicating the
  callback. Terminal CLI only; the desktop app keeps using `ltc wait`.
- On Linux, also find the newest nvm install (`~/.nvm/versions/node/*/bin/claude`) and the Claude
  desktop app's SSH-session CLI (`~/.claude/remote/ccd-cli/<version>`, source `desktop-remote`).
  The SSH CLI shares the host's `~/.claude` sign-in, so it is not treated as desktop-only; a
  configured path to it that disappears after an app update is rediscovered.
- Verified end to end on macOS with the Claude desktop app (launchd task, live wakeup, late waiter
  takeover from headless retries) and on Linux with the desktop app over SSH (`screen` task, live
  wakeup, daemon deferral while the waiter lives, daemon takeover without one, ACK). Windows not
  yet verified.

### Pi Agent

- Publishing a callback to a live Pi no longer blocks the daemon until the ACK. The delivery worker
  returns "published; awaiting ACK" (122) and the daemon keeps launching tasks and delivering other
  sessions' callbacks; the session's retained lease still orders its own callbacks, and `ltc ack`
  finalizes it. Each loop moves a published, unacknowledged callback to `failed` for manual
  recovery (never replay) when its Pi exits or closes the mailbox, the session is reopened by
  another process, or `--resume-timeout` passes; `last_error` says whether Pi had admitted it.
  Previously a live delivery held the coordinator for up to the resume timeout, delaying unrelated
  task starts.
- Give Pi its own installed skill (`${PI_CODING_AGENT_DIR:-~/.pi/agent}/skills/long-task-callback`):
  a focused `SKILL.md` about steered callbacks plus the shared reference as `REFERENCE.md`. The
  shared Codex/Claude skill no longer carries Pi-session instructions. `install-skill --target`
  and `setup --skill-target` accept `pi` and `all`; `setup --with-pi-extension` installs all three
  and `install-pi-extension` installs the Pi skill too. `doctor` checks the Pi skill and its repair
  command targets it.
- Add `docs/pi.md`.
- Add Pi Agent as a child worker with `ltc agent pi`, fresh nonpersistent text
  execution, submission-time environment/configuration and private result capture.
  Support Pi model/thinking overrides, per-Pi template defaults, executable
  selection via `LONG_TASK_WAKEUP_PI_BIN` and
  `doctor --operation agent --agent-worker pi`. Pi permissions/extensions follow
  its own noninteractive configuration; native Windows/macOS Pi launch remains
  unverified in this session.
- Deliver Pi callbacks to the original live process through a private mailbox and
  native extension message API; busy Pi defers until idle. Add
  `ltc install-pi-extension`, optional `setup --with-pi-extension`, and the managed
  `ltc pi` launcher that leases an absolute session file before startup. Only
  verified terminated managed owners permit print-mode recovery. Pin managed
  sessions; restart the launcher to switch/fork/reload resources. Freeze profile
  routes, reject ID/`--last` fallbacks, and retain unknown publication outcomes
  without automatic replay. Keep the existing ACK protocol. Separate ordinary
  online-only Pi from managed offline recovery and preserve personal settings.
  Ship isolated real Pi/local-provider tests; native Windows/macOS Pi is unverified.
- Add Pi-only `--system-prompt-file` and template `pi.system_prompt_file` defaults.
  Pass the snapshot to Pi's `--system-prompt` to replace its built-in base prompt
  while retaining normal project context, skills and appended instructions.
  Snapshot nonempty UTF-8 contents privately at submission; retain the source
  path for review and keep queued tasks independent of later source changes.
  Resolve CLI paths from the submitting shell and template defaults from their
  YAML directory; dry-run reports the source without creating task files.
- Add `ltc template register|list|unregister` for reusable child-task discovery.
  Accept optional YAML `worker`/`description` metadata and CLI overrides; copy
  templates and system prompts into the user template directory, generate short
  `ltc-NAME` routing skills and maintain an installed LTC skill's managed index.
  Pin each route to its installed absolute template path and emit Codex UI
  metadata. Preserve configuration/home overrides and unrelated skills/aliases;
  use standard Codex aliases only for the default Unix profile. Keep
  `setup --keep-skill` text unchanged. Unregister removes managed discovery files
  while retaining templates, prompts and extra user files, and refuses edited routes.

### Callback modes and Desktop

- Separate CLI, Desktop shared Core and manual callback modes; freeze the mode
  with each task and report callback capability separately from local readiness.
- Refuse new Desktop work before admission when its shared connection is
  unavailable; preserve external `done` results and explicit manual callbacks.
- Retain definite App Server ownership/bridge failures without blind retries;
  add callback-only `retry` with existing target/lease/ACK safeguards.
- Add an opt-in Mac Desktop launcher, private Unix peer PID/UID and creation
  identity verification, and Desktop-owned Core cleanup on stdio EOF.
- Generate Desktop launchers during standard `ltc setup` on both Mac and
  Windows, using the current profile and installed Python runtime. Ship launcher
  resources in the wheel, without a checkout, `.venv` or `examples` dependency.
  Mac produces `.command`; Windows produces `.cmd`, `.ps1` and private
  `desktop.json` settings through the common `desktop prepare|launch|status`
  commands. Retain existing selections and support `--desktop-app` and
  `--no-desktop-launcher` on both platforms. CLI setup and launcher generation
  remain available before Desktop is installed.
- Discover Windows Store Desktop through its manifest and select a matching
  Core dynamically. Save an explicit `--desktop-launch-mode package-context`
  choice; keep direct launch as the default without automatic fallback.
  `desktop launch --check-only` retains the suspended native creation probe;
  setup/prepare generate files without launching Desktop or running that probe.
  Store direct-launch error 5 and the experimental package-context limitations
  remain. This installation change has not received native Windows acceptance;
  earlier Windows callback success does not validate the new setup path.
- Extend the shell Git installation helper to run setup with the same Python
  after pip installation, with `LTC_PYTHON` and setup arguments after `--`.
  Add native Windows `scripts/install_from_git.ps1` with `-RepoUrl`, `-Python`,
  optional `-Subdirectory` and `-SetupArguments` for the same pip-then-setup flow.
  Direct pip installs continue to require an explicit setup command. These
  preview changes are on `codex/macos-desktop-callback` and are absent from stable `v0.7.0`.
- Use task record v3 and callback record v2 for new explicit mode intent, with
  backward reads and deliberate rejection by older coordinators.
- Real Mac two-client protocol/cleanup probe passed with an empty profile and
  no model calls. Live Desktop original-session receipt/ACK remains pending.

## 0.7.0 — 2026-09-27

Release the shared task lifecycle and native Linux, macOS and Windows execution
backends as stable. The optional Windows Desktop package-context bridge remains
experimental and retains its documented installation and transport limitations.
PI/DSH adapters and npm/frozen distribution are not included.

### Shared lifecycle and Linux

- Promote the Linux architecture preview: independent systemd task ownership,
  screen compatibility, and non-systemd container deployment.
- Separate platform backends, Agent adapters, private storage, runtime workers and
  callback rendering while preserving legacy screen task records.
- Default new task callbacks to compact envelopes with private detail artifacts;
  retain full-format callbacks and the existing 4/3 reminder policy.
- Add Agent-owned environment/configuration/runtime checks and repair guidance;
  preserve unknown outcomes and inspect durable results before recovery.
- Keep task ownership independent from coordinator hosting, with no automatic
  replay of ambiguous native attempts. Drain and upgrade 0.6 coordinators before
  submitting native task records, or use an isolated queue and Agent profile.

### Windows

- Add an explicit experimental Desktop bridge with native same-user TCP peer
  verification and a private authenticated upstream; two real App Server clients
  can resume the same owned test thread. The explicit package-context route passed Desktop startup, App Tools and
  original-session receipt/ACK on the tested Windows installation.
- Add a Desktop-launched stdio adapter to retain Desktop's injected tool
  environment and Core arguments while sharing that server with LTC.
- Persist Desktop submission intent before sending a turn so parent timeout or
  worker loss cannot cause duplicate delivery; handle WebSocket pong frames.

- Add independent per-attempt Windows Task Scheduler runners and a per-user
  coordinator supervisor, with no automatic business replay or execution limit.
- Add private Windows ACLs, atomic file publication, transferable process-held
  leases, native process/boot identities and runner-owned Job Object containment.
- Support Windows Python virtual environments, recognized npm/batch CLI shims,
  literal PowerShell ACK commands, safe coordinator drain/replacement and removal.
- Cover Windows lifecycle and shared queue contracts with native process tests;
  add Windows installation guidance and Python 3.9/3.12 CI configuration.
- Record the successful native 120-second workload and subsequent original-session
  callback ACK without restarting the workload; default CLI writer contention remains.
- Preserve partial WebSocket frames and fragmented messages across completion-poll
  timeouts on all platforms, including interleaved ping frames.

### macOS

- Add independent one-shot launchd task jobs and a persistent user LaunchAgent
  coordinator, selected automatically on macOS; add `install-launchd --print`.
- Preserve recovery without replay across ambiguous launch outcomes, coordinator
  replacement and reboot; collect exited task registrations after reconciliation.
- Use native macOS host, boot and process identities, identity-bound reload requests
  and UNIX socket peer credentials; support Apple's bundled screen logging.
- Add macOS setup documentation, platform/lifecycle tests and macOS CI coverage.
- Verify a real 120-second original-session Codex callback and durable ACK on macOS;
  add a Windows development handoff with implementation entry points and acceptance criteria.

## 0.7.0a1 — Linux architecture preview

- Add automatic local configuration checks to task commands and `ltc doctor`.
  Failed checks emit an Agent-owned repair/recheck plan with persisted-work state;
  `setup --keep-skill` preserves existing skill customizations during repair.
- Check the observed task/result/callback chain through saved backend identities;
  distinguish missing owners, unknown observations and unresolved handoffs, with
  unsupported-platform short circuits and separate configuration/runtime results.
- Add an independent systemd user-service execution backend, selected per new task;
  retain legacy screen records and an explicit screen fallback.
- Support non-systemd Linux containers with a screen backend and explicit
  standalone/Supervisor hosting; add Docker and AutoDL deployment examples.
- Verify standalone coordinator identity before reloading, including PID namespace
  and process start time; stale PID files cannot authorize signals or prevent startup.
- Separate Agent adapters, Linux/POSIX primitives, atomic storage, runtime worker
  launching and callback rendering for future platform/Agent handoff.
- Default new task-completion callbacks to short envelopes with private full-detail artifacts;
  retain 4/3 reminder cadence, custom handoff instructions and a full-format option.
- Preserve unknown launch outcomes without automatic duplicate execution, and
  validate persisted result identity before completion recovery.
- Document Linux guarantees and future macOS/Windows/PI/DSH extension requirements.
  These future targets are not implemented or advertised as supported.

## 0.6.6 — 2026-09-21

- Add independent standard/user callback reminder intervals, defaulting to 4/3,
  configured with `ltc prompt-policy`. First reminders are always shown.
- Persist per-conversation callback numbers and reminder decisions atomically;
  retries/restarts do not double-count. Keep task data, routing and ACK instructions
  in compact callbacks, and retain full reminders on invalid state/configuration.
- Move always-applicable callback responsibilities into the skill's core rules.

- Use 8-character random hexadecimal IDs for new managed tasks, with atomic
  directory reservation and retry on collision. Existing task IDs remain valid.

## 0.6.5 — 2026-09-14

- Fix Codex child startup on CLI 0.153.4: use explicit automatic-review and
  approval-policy configuration alongside the requested sandbox, instead of the
  mutually exclusive `--approve-for-me` / `--sandbox` combination.
- Promote durable `ltc agent codex|claude` execution from preview to stable.
- Add `--template test`: an independent child authors requirement-based tests and
  a reviewable handoff; the parent executes tests and reports actual outcomes.
- Add user YAML templates in `~/.config/ltc/templates/` and `--template-file`.
  Configure prompts, per-agent model defaults and parent handoff instructions
  without reinstalling. User templates can override built-ins; submission freezes
  the template source and handoff alongside the expanded prompt and configuration.
- Default Codex test authors to `gpt-5.6-luna` with `max` reasoning. Add child-only
  `--model` and Codex `--reasoning-effort` overrides. Claude inherits its own model
  unless overridden; generic agent commands retain existing defaults.
- Persist template version, resolved model/effort, expanded prompt and child argv
  at submission; include profile metadata and execution responsibilities in callbacks.
- Clarify failed/empty/partial authoring handoffs, requirement gaps, and the limits
  of prompt-level role separation in a shared workspace.

Validation uses independent contract tests plus the existing callback/worker suite
and installed-wheel template loading. Generated test quality still requires parent
review; this release does not enforce a tests-only filesystem or execution sandbox.
