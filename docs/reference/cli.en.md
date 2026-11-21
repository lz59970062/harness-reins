# CLI reference

Inspect the complete options with:

```bash
reins --help
reins run --help
reins agent --help
```

## Install and inspect

| Command | Purpose |
| --- | --- |
| `reins --version` | Print the current version |
| `reins setup --enable --now` | Install skills and start the coordinator |
| `reins setup --keep-skill --force --enable --now` | Update the coordinator and retain skill content |
| `reins doctor --agent codex --session <id>` | Inspect a session's runtime configuration |

## Terminal UI

| Command | Purpose |
| --- | --- |
| `reins tui` | Open the terminal UI: every session, task, goal and message, with sessions opened in panes beside them. See [Terminal UI](../guides/tui.md) |
| `reins tui <id>` | Open one session at once; a session, task or callback id works |

## Submit work

| Command | Purpose |
| --- | --- |
| `reins run --task <name> -- <command>` | Submit a command |
| `reins agent codex -- <prompt>` | Start a Codex child Agent |
| `reins agent claude -- <prompt>` | Start a Claude child Agent |
| `reins done --task <name> --exit-code <code>` | Report an external task result |
| `reins log <task-id>` | Print the last 10 lines of a task's output; on a terminal, follow it until the task ends, like `tail -f`. Piped or redirected output is printed without following. A unique prefix of the id is enough |
| `reins log <task-id> -n 100 --no-follow` | Print the last 100 lines and exit |
| `reins log <task-id> --all` | Print the whole log from its first line; on a terminal, then follow it |
| `reins owner` | List the tasks that have not ended and, for each, the Reins name of the session that started it; the longest-quiet log first |
| `reins owner <task-id>` | Which session started a task (its Reins name, harness and session id), how long it has run and when its log was last written. Add `--json` for machine-readable output |
| `reins stop <task-id> -m "why"` | Stop a task on purpose: every process it started ends, including ones it detached with `setsid` or `nohup`; a task that has not started never starts; the session that started it gets a callback saying you stopped it and not to run it again. On a task that already ended, it stops the processes it left behind. Add `--yes` without a terminal. Linux tasks run by systemd or screen, Docker included |

Put submission options before `--`:

| Option | Purpose |
| --- | --- |
| `--cwd <directory>` | Working directory |
| `--task <name>` | Task name shown in the callback |
| `--agent <type>` | Agent type receiving the callback |
| `--session <session>` | Session receiving the callback |
| `--queue-dir <directory>` | Callback queue location |
| `--backend auto` | Select the task execution backend automatically |
| `--goal-id <goal>` | Associate the task with a multi-stage goal |
| `--callback-format full` | Use the full callback text |

## Handle callbacks

| Command | Purpose |
| --- | --- |
| `reins status` | Inspect pending, running and failed callbacks |
| `reins status --state failed` | Inspect failed deliveries |
| `reins ack <callback-id>` | Confirm that you have read the result |
| `reins cancel --id <callback-id>` | Cancel delivery of this callback |
| `reins prompt-policy` | Inspect reminder intervals |

## Messages between sessions

| Command | Purpose |
| --- | --- |
| `reins session register <name>` | Name the current session |
| `reins install-claude-hook [--remove]` | Add or remove the Claude Code hook that delivers into open windows |
| `reins session list` | See the sessions, and how a message reaches each one now |
| `reins session unregister <name>` | Remove a name |
| `reins send <name> --subject <subject> -m <text>` | Send a message, starting a new thread |
| `reins reply <message-id> -m <text>` | Reply, which also acknowledges the message |
| `reins inbox` | See what is unread; `--all` includes read ones, `--sent` shows yours, `--thread <id>` a whole thread |
| `reins message show <message-id>` | Read one message and its state |
| `reins message alerts` | See threads stopped at a limit; `--clear` removes the alerts |

A Claude Code session with nothing listening, or a closed `reins pi` session, is resumed headless by default. Codex holds the message in its queue unless you pass `--wake resume`. `--wake never` starts nothing. See [Messages between sessions](../guides/messages.md).

## Preview commands

These commands use the `0.7.1` preview:

| Command | Purpose |
| --- | --- |
| `reins wait --task <task-id>` | Wait for and print a callback in Claude Code |
| `reins retry --id <callback-id>` | Redeliver a saved callback |
| `reins agent pi -- <prompt>` | Start a Pi child Agent |
| `reins pi` | Open a Reins-managed Pi session |
| `reins template list` | List registered templates |

See [Multi-stage goals](../guides/goals.md) for the goal command sequence.
