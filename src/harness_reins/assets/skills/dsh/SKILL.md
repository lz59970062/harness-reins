---
name: harness-reins
description: Run long jobs (training, benchmarks, builds, test suites, deployments, data jobs, child agents) under Harness Reins (reins) so they outlive this turn, and get woken in this same DeepSeek Harness session when they finish through a background `reins wait` job. Use instead of sleeping, polling logs, or leaving a job owned by the foreground shell whenever a command may take about a minute or longer. Also covers handling an `[reins-status]` repair block and acknowledging `[reins]` results.
---

# Harness Reins for DeepSeek Harness

Reins hands a long command to an independent OS task owner (launchd on macOS, systemd on Linux,
Task Scheduler on Windows) and queues a callback when it exits. In DeepSeek Harness you receive
that callback **in this live session** by starting `reins wait` as a DSH background job: DSH
notifies the conversation when a background job finishes, and `reins wait` exits as soon as the
callback is ready.

The full reference (all flags, platforms, recovery and goals) is in [REFERENCE.md](REFERENCE.md).
Read it only when you need details beyond this page. It is shared with other agents; where it
differs from this page about callback delivery, this page applies to DeepSeek Harness.

## When to use it

- Reliably done within 60 seconds → run it in the foreground once. No Reins.
- About a minute or longer, uncertain duration, or might need a second check → `reins run` from the start.
- Never keep a job alive by polling it with `sleep`/`tail`, and never start a long job with a plain
  foreground Bash call: it blocks the conversation and dies with the turn.

## The workflow

1. **Submit** (foreground; returns in a second or two):

   ```bash
   reins run --cwd "$PWD" --task "train model" -- python train.py --config configs/exp.yaml
   ```

   stderr prints the task id, backend, log path, and a ready-made line:
   `reins: DeepSeek Harness live callback: run as a background job (run_in_background: true): .../reins wait --queue-dir ... --task <id>`

2. **Wait** — run exactly that `reins wait ... --task <id>` command with the Bash tool's
   `run_in_background: true` (the `bash` tool's `run_in_background` argument). Do not run it in
   the foreground (it blocks until the job ends), and do not poll its output. Then tell the user
   what is running and end your turn, or carry on with other work. Start it right after the
   submission; the queue keeps the callback until a waiter appears.

3. **Wake up** — DSH notifies this conversation when the background job finishes. Read that job's
   output with the `job_output` tool (use the job id from the notification). It contains:

   ```text
   [reins] <task-id>-c1
   Task: train model
   Result: exited 0 · 2h 3m 32s
   From: run on systemd-user
   Files: <task dir>/ (attempt-1.log)
   Details: <path> (as needed | read before acting)
   Session: <this session id> (only)
   ACK after inspection: reins ack <task-id>-c1
   ```

   Inspect the log and artifacts (read Details first when it says "read before acting"), decide
   `continue | stop_success | stop_blocked | ask_user`, then run the `reins ack ...` command from
   the callback. ACK means "received and inspected", not "goal done". An exit code is process
   status, not proof that tests passed.

4. **Next step** — if you launch follow-up work, submit it with `reins run` again and start a new
   background `reins wait --task <new-id>`. Inspect existing processes and outputs first: delivery
   is at-least-once.

## Other entry points

- **Fresh child agent** (independent review, test authoring, a parallel fix):

  ```bash
  reins agent dsh --cwd "$PWD" --name parser-review -- "Inspect parser.py and report concrete defects."
  reins agent codex --template test --name parser-tests -- "Test the contract in docs/parser.md."
  reins agent continue <task-or-callback-id> --name recheck -- "I fixed those; review parser.py again."
  ```

  Every child needs `--name`. `reins agent continue <id>` reuses the same child with what it already
  learned; `reins agent fork <id>` branches from it. `<id>` is the task ID, the callback ID, or the
  node's full name; prefer IDs. See `reins context show <node>` for summaries;
  never read a child's raw session.

  Then background `reins wait --task <id>` exactly as for `reins run`. A DSH child runs
  `dsh --profile headless`; that profile uses `DEEPSEEK_API_KEY` (or credentials restored in the
  headless profile), not the desktop app's saved sign-in. If it cannot authenticate, the child
  callback reports the error — do not retry blindly.

- **Work already owned elsewhere** (tmux, screen, Slurm, a script): add `reins done` at its end.

  ```bash
  set +e; python train.py; status=$?
  reins done --cwd "$PWD" --task "train model" --command "python train.py" --exit-code "$status"
  exit "$status"
  ```

  The callback id is not known in advance, so start a background `reins wait` with no `--task`: it
  delivers the next unacknowledged callback bound to this session.

## Rules

- **After `--` comes one command, not shell code**: for `;`, `&&`, pipes or redirection use
  `-- bash -c '...'` (`reins run --task check -- bash -c 'sleep 720; python3 report.py'`).
- **Heredoc scripts work**: input piped into `reins run` (`-- python3 - <<'EOF'`) becomes the task's
  stdin. Without piped input the task's stdin is empty.
- **Session binding is automatic** from `DSH_SESSION_ID`. Never invent a session id. `reins wait`
  refuses to run without a live session id.
- **One wait, one wake-up.** A wait exits after printing what it found; start a new one for the next
  task. Every unacknowledged callback of this session keeps coming back until you ACK it.
- **There is no headless fallback for DSH.** The daemon never opens a second writer for a DSH
  conversation, because DSH refuses to resume a desktop-preset session non-interactively. If this
  session closes before a waiter starts, the callback stays queued; it is delivered the next time
  this session runs `reins wait`, or you can inspect it with `reins status`.
- **Wait exit code 4 does not occur for DSH** (there is no headless resume to divert the callback).
  Exit code 3 means the wait timed out; the callback is still queued.
- **Do not** call `dsh --profile headless --session-id` yourself to pick up a callback, add
  `--via-daemon`, or invent launch flags. Callback problems never change the task's own exit code
  unless the user asks for `--strict`.
- **Long objectives with several stages**: use `reins goal start|check|ack` with a YAML plan file, see
  REFERENCE.md ("Goal acknowledgement"). Callback ACK never completes a goal.

## `[reins-status]` blocks

If `reins run`, `reins agent` or `reins done` prints JSON between `[reins-status]` and `[/reins-status]`, it is
addressed to you: fix the listed configuration issues yourself (do not hand routine setup back to the
user), run its `recheck_command`, then continue. Check `work.state` first — `task_persisted` or
`callback_queued` means the work already exists: repair around that id and **do not submit it again**.
For an explicit check: `reins doctor --agent dsh --session "$DSH_SESSION_ID"`.

## Install and verify

```bash
python3 -m pip install "git+https://github.com/lz59970062/harness-reins.git"
reins setup --with-dsh --force --enable --now
reins doctor --agent dsh --session "$DSH_SESSION_ID"
```

`reins setup --with-dsh` installs this skill under `${DSH_HOME:-~/.dsh}/skills/harness-reins` and
records the `dsh` launcher for the daemon. If the desktop app did not put `dsh` on `PATH`, pass
`--dsh-bin "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh"`.

Useful commands: `reins status` (unacknowledged callbacks), `reins cancel --id <id>`,
`reins retry --id <id>` (re-deliver a result without rerunning the task),
`tail -f <task log>` when the user asks to watch progress.
