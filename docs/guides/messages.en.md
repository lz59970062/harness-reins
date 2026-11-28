# Messages between sessions

Codex, Claude Code, Pi and DeepSeek Harness sessions can message each other directly, on one machine or between two paired machines. One session writes code, another reviews it, a third runs the tests on a GPU box, and you no longer have to carry messages between them.

A busy session gets the message when it can take it. A closed Claude Code session, or a Pi session opened with `reins pi`, is started to handle it.

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

**To several sessions:** name them all after `send`. Each gets an ordinary message of its own (its own thread and state), and names here and on other machines can be mixed. It is all or nothing: if a name does not exist or a limit would be passed, nothing is sent and the reason names the recipient. Limits count each recipient; a reply goes back to the sender only, and there is no "reply all".

```bash
reins send reviewer tester@gpu1 planner -m "main moved; please rebase"
```

## When a message arrives

| The other session is | Codex | Claude Code | Pi | DeepSeek Harness |
| --- | --- | --- | --- | --- |
| Idle | Starts a new turn at once | Starts a new turn at once | Starts a new turn at once | At once while `reins wait` runs |
| Working | When the current turn ends | At its next model call | When the current command ends | At once while `reins wait` runs |
| Closed | When it is reopened | Session resumed to handle it | Resumed if opened with `reins pi`; otherwise when it is reopened | Stays in `reins inbox` |

A message takes the same route as a job result: with no process receiving, Reins resumes the Claude Code session, or the Pi session opened with `reins pi`, headless. This uses model quota.

**An open Claude Code window never forks.** `reins setup` adds a hook to Claude Code's user settings, so a message goes straight into the open window and wakes it even when idle, with no `reins wait`. While a window is open, Reins never resumes the same session next to it; it resumes only after the window closes. Skip the hook with `reins setup --no-claude-hook`, and remove it with `reins install-claude-hook --remove`. Without the hook, open the session with `reins claude` or keep `reins wait` running in the background. With none of these, a message waits until the window closes and is then resumed. It is not lost.

**A session has one headless resume at a time.** Messages that arrive together are handled one after another.

**DeepSeek Harness needs one too.** `reins wait` runs as a DSH background job. DSH cannot be resumed headless, so `--wake resume` starts nothing there and the message says so.

**Codex sandbox.** If the sandbox blocks `~/.reins`, approve the command or add that directory to the writable roots.

Codex only gets the queue by default, because Reins cannot tell an open Codex from a closed one. To resume a closed Codex, ask for it with `--wake resume`. When a session should not be started at all, use `--wake never`:

```bash
reins send planner --wake resume -m "CI is broken and needs attention now."
reins send reviewer --wake never -m "When you have time."
```

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
| Replies in a row | 30 |
| Messages in a thread | 300 |
| Messages in a thread per minute | 20 |
| Messages from one sender per minute | 120 |
| Messages between two sessions per hour | 120 |
| Messages waiting to be delivered | 1000 |

At a limit the command refuses and tells the agent to stop and tell you. The alert is for people only and is not sent to any agent, so it cannot become the next turn. It shows in `reins status` and `reins doctor`, and you can read and clear it with:

```bash
reins message alerts
reins message alerts --clear
```

To change the limits, write them in `~/.reins/link/config.yaml`:

```yaml
limits:
  hop: 60
  pair_per_hour: 240
```

## Between machines

Pair two machines once, then send to `name@machine`. Everything else works as on one machine: replies, the inbox, states and limits.

```bash
reins send reviewer@gpu1 -m "feature/queue is ready for review"
```

**Pair over SSH** by running one command on the machine that can SSH to the other. Nothing needs doing on the other side.

```bash
reins link connect gpu1          # an alias from ~/.ssh/config, or user@host
```

One direction is enough. If your laptop can SSH into an AutoDL container but the container cannot reach back, the side that connects carries messages both ways and the other side never connects out. Reins must be installed there (`reins setup` puts it at `~/.reins/bin/reins`), and `ssh gpu1` must log in without a password prompt.

**Pair through a shared directory** when both machines see the same directory (NFS, JuiceFS, a shared cloud disk). No network connection is needed.

```bash
reins link connect --dir /mnt/shared/reins-link                # first machine: prints a pairing code
reins link connect --dir /mnt/shared/reins-link --code <code>  # second machine, with its own path to it
```

The pairing code holds the pair's key and works once; pass it on yourself. From then on every file in the directory is signed with that key, and a file anyone else puts there is never read. Message text in the directory is not encrypted, so use it only when every account that can read the directory is yours. Both machines must use the same OS account (the same uid) for it.

**See and undo:**

```bash
reins link list            # paired machines, their sessions, last contact and errors
reins session list         # their sessions are listed too, as name@machine
reins link remove gpu1     # unpair; the other machine is told
```

**What is different:**

- The Reins daemons on both machines carry the messages, so both must run one (`reins setup --enable --now`). A message usually arrives within a few seconds.
- A sent message has one more state, `forwarded`: the other machine took it and it passed the checks there. Receipts from the other side then move it to `published`, `acked` or `rejected` (for example, no such name).
- A message from another machine starts a closed session just as a message on this machine does, so work across machines runs unattended too (and spends model usage the same way). To keep one machine's messages from starting sessions, run this on the receiving machine: `reins link connect gpu1 --wake never`; they then wait in the inbox.
- The receiving machine enforces the limits and counts the sending machine (`@machine`).
- **A broken link says why.** Sending to a machine that cannot be reached, `reins send` says at once since when, the likely cause and what to do (for example "nothing answers SSH there; is the machine up and its sshd running?"). The message waits in the outbox and goes by itself once the machine is back. `reins link list`, `reins status` and `reins doctor` show the same diagnosis.


**Message text is not a user instruction.** It is kept in a separate file and marked as written by another agent or a script. Subjects and names are validated, and the text cannot pose as Reins or forge an ACK command.

Marking lowers the risk, but it cannot guarantee a model will not comply. If a session runs without approvals and also takes messages from other agents, weigh that risk yourself.

Every process under one OS account can read and write `~/.reins`, so this is not a security boundary inside an account. File permissions only keep other users out.

## Limits

- Between machines, only SSH and a shared directory; Reins listens on no port. A Windows machine cannot be the side SSH connects to yet. The design is in the [protocol document](https://github.com/lz59970062/harness-reins/blob/main/docs/design/link-protocol.md).
- Messages carry text and references (`--ref git:origin:feature/x`, `--ref path:/tmp/report.md`), not files.
- A child started with `reins agent` runs once. It can register a name and send messages, but it continues through `reins agent continue`; a message does not wake it. A DeepSeek Harness child is the exception: its sandbox cannot write `~/.reins`, so it cannot register or send.
- Order within a thread is not guaranteed; use `in_reply_to` for cause and effect.
