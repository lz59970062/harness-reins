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

## Submit work

| Command | Purpose |
| --- | --- |
| `reins run --task <name> -- <command>` | Submit a command |
| `reins agent codex -- <prompt>` | Start a Codex child Agent |
| `reins agent claude -- <prompt>` | Start a Claude child Agent |
| `reins done --task <name> --exit-code <code>` | Report an external task result |

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
| `reins status` | Inspect outstanding callbacks |
| `reins status --state failed` | Inspect failed deliveries |
| `reins ack <callback-id>` | Confirm that you have read the result |
| `reins cancel --id <callback-id>` | Cancel delivery of this callback |
| `reins prompt-policy` | Inspect reminder intervals |

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
