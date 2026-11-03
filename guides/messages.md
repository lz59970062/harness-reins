# Messages between sessions

Codex, Claude Code, Pi and DeepSeek Harness sessions on one machine can message each other directly. One session writes code, another reviews it, a third runs the tests, and you no longer have to carry messages between them.

A message is not lost when the other session is busy or closed, and it never quietly starts a process that spends your quota.

## Get started in three steps

Name the session that should receive messages:

```bash
reins session register reviewer
```

In another session, take a name too, then send:

```bash
reins session register planner
reins send reviewer --subject "Check the queue change" -m "Branch feature/queue is ready. Please focus on the retry logic."
```

The other session replies:

```bash
reins reply m-6ae8b07359c6e7f5 -m "Done. The retry on line 42 has no limit."
```

A reply also acknowledges the message.

You can leave this to the agents, for example: "Register as planner and send this change to reviewer for review." The skill installed by `reins setup` teaches them these commands.

## When a message arrives

| The other session is | Codex | Claude Code | Pi | DeepSeek Harness |
| --- | --- | --- | --- | --- |
| Idle | Starts a new turn at once | At once while `reins wait` runs | Starts a new turn at once | At once while `reins wait` runs |
| Working | When the current turn ends | At once while `reins wait` runs | When the current command ends | At once while `reins wait` runs |
| Closed | When it is reopened | Stays in `reins inbox` | When it is reopened | Stays in `reins inbox` |

By default, Reins starts no process for a message.

**Claude Code needs a waiting command.** Claude Code has no general way to receive a push from outside, so `reins wait` has to run in the background. The skill reminds it to do this, and to start it again after each batch of messages. Sessions opened with `reins claude` do not need this step.

**DeepSeek Harness needs one too.** `reins wait` runs as a DSH background job. DSH cannot be resumed headless, so `--wake resume` starts nothing there and the message says so.

**Codex sandbox.** If the sandbox blocks `~/.reins`, approve the command or add that directory to the writable roots.

To wake a closed session anyway, ask for it with `--wake resume`:

```bash
reins send reviewer --wake resume -m "CI is broken and needs attention now."
```

This resumes the other session headless and uses model quota. A thread can do this 3 times. After that, messages are still kept, but no process is started.

## See messages and their state

```bash
reins session list        # who is here, and how a message reaches them now
reins inbox               # unread messages for this session
reins inbox --sent        # what you sent and what became of it
reins inbox --thread <ID> # a whole thread
reins message show <ID>   # read one message
```

A state says only what is known:

| State | Meaning |
| --- | --- |
| `queued` | Saved, not yet handed to the other session |
| `published` | Handed to its channel, which does not mean it was read |
| `acked` | It confirmed seeing it, which does not mean it agreed or finished |
| `failed` | Delivery failed; after fixing the cause, resend with `reins retry --id <callback-id>` |
| `unknown` | Reins cannot tell whether it arrived and will not resend by itself; check the other session first |
| `expired` | Nobody read it within 7 days |
| `canceled` | Withdrawn with `reins cancel` |

## Agents that will not stop talking

Two agents replying to each other can go on indefinitely, so Reins sets limits for you:

| Limit | Default |
| --- | --- |
| Replies in a row | 8 |
| Messages in a thread | 100 |
| Messages in a thread per minute | 20 |
| Messages from one sender per minute | 120 |
| Messages between two sessions per hour | 60 |
| Messages waiting to be delivered | 1000 |

At a limit the command refuses and tells the agent to stop and tell you. The alert is for people only and is not sent to any agent, so it cannot become the next turn. It shows in `reins status` and `reins doctor`, and you can read and clear it with:

```bash
reins message alerts
reins message alerts --clear
```

To change the limits, write them in `~/.reins/link/config.yaml`:

```yaml
limits:
  hop: 4
  pair_per_hour: 30
```

## Security

**Message text is not a user instruction.** It is kept in a separate file and marked as written by another agent or a script. Subjects and names are validated, and the text cannot pose as Reins or forge an ACK command.

Marking lowers the risk, but it cannot guarantee a model will not comply. If a session runs without approvals and also takes messages from other agents, weigh that risk yourself.

Every process under one OS account can read and write `~/.reins`, so this is not a security boundary inside an account. File permissions only keep other users out.

## Limits

- One machine and one OS account. An address like `reviewer@gpu1` is refused with a clear error for now; the plan for multiple machines is in the [protocol document](https://github.com/lz59970062/harness-reins/blob/main/docs/design/link-protocol.md).
- Messages carry text and references (`--ref git:origin:feature/x`, `--ref path:/tmp/report.md`), not files.
- A child started with `reins agent` runs once. It can register a name and send messages, but it continues through `reins agent continue`; a message does not wake it. A DeepSeek Harness child is the exception: its sandbox cannot write `~/.reins`, so it cannot register or send.
- Order within a thread is not guaranteed; use `in_reply_to` for cause and effect.
