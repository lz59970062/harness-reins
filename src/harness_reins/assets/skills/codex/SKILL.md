---
name: harness-reins
description: Run long jobs (training, benchmarks, builds, test suites, deployments, data jobs, child agents) under Harness Reins (reins) so they outlive this turn, then end the turn; Reins starts a new turn in this same Codex thread with the result. Use instead of sleeping, polling logs, waiting on a running exec session, or blocking a tool call whenever a command or a wait for time to pass may take about a minute or longer. Also covers handling an `[reins-status]` repair block and acknowledging `[reins]` results.
---

# Harness Reins for Codex

Reins hands a long command to an independent OS task owner (systemd on Linux, launchd on macOS,
Task Scheduler on Windows; `screen` as fallback) and queues a callback when it exits. When this
thread is idle, Reins delivers the callback **as a new turn in this same thread**: `codex exec
resume` for CLI sessions, the native `codex queue` command for normally started Codex Desktop. You do not wait for it; you end
your turn and the result comes back to you.

The full reference (all flags, platforms, Desktop modes, recovery and goals) is in
[REFERENCE.md](REFERENCE.md). Read it only when you need details beyond this page; where it
differs from this page about callback delivery, this page applies to Codex.

Reins resumes this thread with the permissions recorded when you submit (`workspace-write`,
`on-request` by default). If this session runs without a sandbox (for example
`--dangerously-bypass-approvals-and-sandbox` in a container), submit with
`--sandbox-mode danger-full-access --approval-policy never` (or have `REINS_SANDBOX_MODE` and
`REINS_APPROVAL_POLICY` set) so the resumed turn can still run commands and ACK.

## When to use it

- Reliably done within 60 seconds → run it in the foreground once. No Reins.
- About a minute or longer, uncertain duration, or might need a second check → `reins run` from the start.
- Waiting for time to pass (a real-time clock, a schedule, a cooldown) is a long job too: submit the
  wait, `reins run --cwd "$PWD" --task "wait for hour 12" -- sleep 900`.
- Never wait inside this turn: no `sleep` longer than about a minute, no loops of `sleep` plus a
  status or log check, no long `yield_time_ms` or repeated empty `write_stdin` on a running exec
  session, no `tail -f`/`tail --pid`, and no `reins wait` (that is for Claude Code background
  commands). Never start long jobs with `&`, `nohup` or `setsid`: Reins owns them.

## The workflow

1. **Submit** (returns in a second or two):

   ```bash
   reins run --cwd "$PWD" --task "train model" -- python train.py --config configs/exp.yaml
   ```

   Inside Codex, Reins binds the callback to this thread through `$CODEX_THREAD_ID`. stderr prints
   the task id, backend and log path. The command after `--` is argv, not shell code: use
   `-- bash -c '...'` for pipes, `&&` or redirection.

2. **End your turn.** Do any independent work first, then finish with a short message saying what
   is running and what you will do with the result. In `codex exec` this exits Codex; Reins resumes
   this thread when the task ends. Submitting several tasks before ending the turn is fine: each
   arrives as its own callback.

3. **Handle the callback** — it arrives as the user message of a new turn:

   ```text
   [reins] <task-id>-c1
   Task: train model
   Result: exited 0 · 2h 3m 32s
   From: run on systemd-user
   Files: <task dir>/ (attempt-1.log)
   Details: <path> (as needed | read before acting)
   Session: <this thread> (only)
   ACK after inspection: reins ack <task-id>-c1
   ```

   Inspect the log and artifacts (read Details first when it says "read before acting"), decide
   `continue | stop_success | stop_blocked | ask_user`, then run the `reins ack ...` command from
   the callback. ACK means "received and inspected", not "goal done". An exit code is process
   status, not proof that tests passed. Delivery is at-least-once: check existing processes and
   outputs before relaunching anything. ACK **every** callback. If more work is needed, submit it
   with `reins run` and end the turn again.

4. **Stop a task early** by killing its process (`pkill -f '<its command>'`); `reins cancel` cancels
   only the callback, not the workload. The callback still arrives with the exit status; ACK it.

## Other entry points

- `reins agent codex|claude|pi --cwd "$PWD" --name <name> -- "<prompt>"` — a child agent; its result
  comes back as a callback here. `reins agent continue <node>` or `fork <node>` reuses what that child
  learned; `reins context show <node>` gives summaries (never read its raw session).
- `reins done --cwd "$PWD" --task "..." --exit-code 0` — report work you are already supervising.
- `reins status --state pending --state running` — inspect tasks and callbacks once; do not poll it.

## `[reins-status]` blocks

Reins prints an `[reins-status]` JSON block when something needs attention. You own the repair: follow
`repair_command`, then `recheck_command`. `callback_queued` or `task_persisted` means the work
already exists — repair delivery around that id and **do not submit it again**. Do not run
`codex exec resume` yourself from a tool call; Reins owns delivery.
