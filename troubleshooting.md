# Troubleshooting

## The terminal cannot find reins

Activate the virtual environment where you installed Reins, then check the command:

```bash
source /path/to/harness-reins/.venv/bin/activate
reins --version
```

On Windows, call `.venv\Scripts\reins.exe` inside the installation directory. The coordinator also uses the Python path saved during setup. Run `reins setup` again after moving the environment.

## Submission reports missing session information

Ask the Agent to run `reins run` through its current session's tool environment. When submitting from a separate terminal or job script, pass `--agent` and `--session`:

```bash
reins run --agent codex --session "YOUR_SESSION_ID" --task "Build" -- bash scripts/build.sh
```

See [Codex CLI](integrations/codex.md) and [External jobs](guides/external-jobs.md) for session binding.

## The task finished, but the callback has not arrived

Inspect the callback queue and the session configuration:

```bash
reins status --state pending --state running --state failed
reins doctor --agent codex --session <session-id>
```

Use the report to choose the next step:

| Finding | Next step |
| --- | --- |
| The coordinator has not started | Run the `setup` command suggested in the report |
| A Claude Code session is open | Ask Claude to run the printed `reins wait` command as a background Bash task |
| Codex Desktop did not get a callback | Keep the chat open and check `reins status`; for saved older callbacks see [Codex Desktop](integrations/desktop.md#upgrading-from-the-old-launcher) |
| The callback is in `failed` | Read the error record, fix the cause, and use `reins retry --id <callback-id>` in the preview version |

The original submission output contains the task's log path. Open the log and artifacts to inspect the task result directly.

## Every headless callback fails on Linux and doctor reports agent_cli_blocked_by_service_sandbox

The systemd user service runs with `NoNewPrivileges=yes`. If an agent CLI recorded in the unit (for example `REINS_CLAUDE_BIN=~/.local/bin/claude`) is an administrator launcher that re-executes through `sudo` or another setuid helper, it works in a login shell but fails inside the service, for example:

```text
sudo: effective uid is not 0, is /usr/bin/sudo on a file system with the 'nosuid' option set ...
```

Every `claude -p --resume ...` callback then exits 1 and moves to `failed` after 3 retries. `reins setup` and `reins doctor` start each CLI recorded in the unit with `--version` under the same property and report `agent_cli_blocked_by_service_sandbox` with the binary path and its stderr line. To reproduce it by hand:

```bash
systemd-run --user --wait --pipe -p NoNewPrivileges=yes ~/.local/bin/claude --version
```

To fix it:

1. Prefer a CLI that needs no privilege escalation, such as the real program the launcher wraps: `reins setup --force --now --claude-bin /real/path/claude` (`--codex-bin` and `--pi-bin` for Codex and Pi).
2. If the launcher is required, run `reins setup --force --now --allow-privilege-escalation`. The unit is written without `NoNewPrivileges=yes`, so agent CLIs started by the service can gain privileges through sudo and similar helpers. Only do this if you accept that.

After the fix, run `reins retry --id <callback-id>` for the failed callbacks. The business task does not need to run again.

## The same callback appears repeatedly

After reading the result, run the complete ACK command from the callback. Check its ID and `--queue-dir` so the acknowledgement reaches the same queue.

## Resuming training after a restart

Find the task log, result file, and latest checkpoint. Ask the Agent to inspect the artifacts, then submit a new task using the training program's resume option:

```bash
reins run --cwd "$PWD" --task "Resume training" -- python train.py --resume outputs/checkpoint.pt
```

Here, `--resume` belongs to the example training program. Use the option and checkpoint path for your own framework.

## Reporting a problem

Open a [GitHub issue](https://github.com/lz59970062/harness-reins/issues) with your operating system, Reins version, submission command, error text, and relevant log excerpts. Include a small reproduction command to help locate the failing step.
