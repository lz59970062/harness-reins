# Antigravity

Hand a long job off from a Google Antigravity CLI (`agy`) conversation, end the turn, and the result comes back to
**the same conversation** when the job finishes. With the window open, the agent wakes up in it; with the window
closed, Reins picks the conversation up in the background, and the agent still inspects the result, ACKs and moves on.

## Install

```bash
reins setup --with-agy --force --enable --now
```

`--with-agy` installs the skill in `~/.gemini/config/skills/harness-reins`, where every new Antigravity conversation
sees it. The daemon finds `agy` on its `PATH` or at `~/.local/bin/agy`; set `REINS_AGY_BIN` for another place. Check:

```bash
reins doctor --agent agy --session "$ANTIGRAVITY_CONVERSATION_ID"
```

## How a callback reaches the conversation

Antigravity sets `ANTIGRAVITY_CONVERSATION_ID` for every command its agent runs, so `reins run` binds the current
conversation by itself:

```bash
reins run --cwd "$PWD" --task "train model" -- python train.py
```

stderr prints the wait command:

```text
reins: Antigravity live callback: run as a background command (do not wait for it): reins wait --queue-dir … --task <id>
```

| The conversation | How the callback arrives |
| --- | --- |
| Open in a window | The agent sends `reins wait` to the background (`run_command` with `WaitMsBeforeAsync` 500) and ends its turn. The wait exits as soon as the callback is queued, and Antigravity wakes the agent in the same window, idle or not |
| Not open anywhere | The daemon runs `agy --resume <id> -p` and hands it the callback as one stream-json message, adding a turn. The queue directory is added as a workspace, so the agent can ACK |

The daemon **never resumes a conversation that is open**. A window and `agy -p` both hold an exclusive `flock` on
`~/.gemini/antigravity-cli/presence/<conversation id>.lock`, which the OS releases when the process exits; the daemon
resumes only while that lock is free, so it never writes to a conversation beside its window.

Inside a headless resume `reins wait` returns at once: `agy -p` waits for its background commands before it exits,
and the next callback resumes the conversation again after this turn.

## Messages

After `reins session register NAME`, other sessions reach this conversation with `reins send NAME`. With the window
open, messages arrive through the background `reins wait`; when nothing has the conversation open, a message resumes
it in the background (`--wake never` keeps it in the inbox instead).

## Sandbox

Antigravity's Auto mode runs commands in a terminal sandbox that hides your home directory, where Reins and its queue
live. The skill has the agent run Reins commands outside the sandbox from the first attempt.

## Verified, and limits

- Tested on Linux with `agy` 1.5.18: in an interactive window, submit, end the turn, wake while idle and ACK (no
  extra wake-ups during a 15-minute job); in an `agy -p` conversation, the background wait and ACK; after the window
  closed, a headless resume by the daemon and ACK; two conversations resumed in parallel and one conversation
  strictly in turn; a headless reply to a message; opening, jumping to and starting panes in `reins tui`.
- macOS uses the same `flock` check and has not been tried yet.
- On Windows the presence lock cannot be read, so callbacks arrive only through `reins wait`, never a headless resume.
- `reins tui` lists Antigravity conversations: open them in a pane, start new ones, message them, start a
  collaborator.
- Headless agy cannot ask for approval, so for that turn Reins allows its own ACK, reply, send and goal commands,
  keeping your permission rules; commands that start work, such as `reins run`, stay denied, and follow-up jobs wait
  for your window.
- A deleted conversation is never resumed: `agy --resume` starts a new conversation for an id it does not know, so
  the callback is marked blocked instead.
- Antigravity does not run as a child agent yet (`reins agent agy`).
