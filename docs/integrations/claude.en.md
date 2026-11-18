# Claude Code

Follow the [installation guide](../installation.md), then run `reins setup` to install the Reins skill for Claude Code.

## Submit a task and receive the result

Ask Claude to submit this in the current conversation:

```bash
reins run --cwd "$PWD" --task "Run the tests" -- python -m pytest tests/
```

The submission output provides a `reins wait` command, for example:

```bash
reins wait --queue-dir ~/.reins/queue --task <task-id>
```

Ask Claude to execute the complete printed command through its Bash tool with `run_in_background: true`. When the result enters the queue, `reins wait` prints the callback and exits. Claude Code then handles the background command's completion message.

After reading the logs and artifacts, Claude runs the callback's `reins ack` command. This workflow works in the terminal CLI and local Code sessions in the Claude desktop app.

## Describe the work in plain language

After installing the skill, you can say:

> Run the full test suite through Reins. When the results arrive, read the failed-test logs, identify the causes, and suggest fixes.

The skill guides Claude through submission, the background waiter, reading results, and acknowledgement.

## Configure the CLI

To start Claude child Agents or resume a closed conversation through the CLI, sign into the CLI on this machine:

```bash
claude auth login
claude auth status
```

Check Reins from the current session's tool environment:

```bash
reins doctor --agent claude --session "$CLAUDE_CODE_SESSION_ID"
```

For SSH work, install Reins and its skill on the remote host where the task runs. See the [Claude Code implementation notes](https://github.com/lz59970062/harness-reins/blob/main/docs/claude-code.md) for executable discovery, session directories, and the channel integration.
