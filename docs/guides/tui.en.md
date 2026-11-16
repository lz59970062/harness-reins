# Terminal UI: reins tui

One terminal and one login show every Claude Code session on this machine, with the tasks, goals and messages they
started, and open any session in a pane beside them in Claude Code's own interface. A session only ever has one
process, so nothing forks.

```bash
reins tui
```

The layout survives a dropped SSH connection. Reconnect, run `reins tui` again, and every pane is still there.

## The screen

```text
┌─ Reins ──────────────────────────────┬─ claude 08e8651e motion plots ─────┐
│ Sessions  Tasks  Goals  Messages     │                                    │
│ ●  here        working  motion plots │  Claude Code itself (claude attach)│
│ ○  Desktop     idle     ppt draft    │                                    │
│ ◌  closed               old callback ├─ log cf408bf5 ─────────────────────┤
│ opened motion plots                  │  the task's output, followed       │
└──────────────────────────────────────┴────────────────────────────────────┘
```

The Reins panel on the left has four tabs:

- **Sessions**: each session's title, where it runs, whether it is busy, how many of its tasks are running, its
  pending callbacks and its goal. Closed sessions that started tasks lately are listed too, so you can find them again.
- **Tasks**: every task with the session that started it and whether its callback was acknowledged.
- **Goals**: plans and progress.
- **Messages**: messages between sessions.

The panel opens the panes on the right: a session pane runs `claude attach`, a log pane runs `reins log`. The panel
keeps its width: the first pane takes the hint's place on the right, and later sessions, logs and terminals open
below or to the right of the newest pane there (`d` switches which), instead of squeezing the panel.

## Keys

| Key | Does |
| --- | --- |
| `Enter` | Open the selected session, or jump to its pane if it is already open. On Tasks, open the session that started the task |
| `n` | Start a new Claude Code session in a directory you choose |
| `l` | Follow a task's output |
| `t` | Open a terminal in the selected session's directory |
| `d` | Switch whether new panes open below or to the right; the footer shows which |
| `m` | Send the selected session a message |
| `s` | Stop a session, after a confirmation. The conversation stays, and `Enter` opens it again |
| `c` | Cancel a task's callback, after a confirmation |
| `1`–`4` | Switch tabs |

The panel refreshes itself every 3 seconds. Its top line shows what `reins doctor` finds: "Reins is ready", or one
line per problem with what to do.

## Copying a session's address

**Right-click** a session to copy its address in the Reins address book; in another conversation, ask the agent to
send to that address and it uses `reins send`. A session that has a name shows it as `@name` before its title.

- Named: choose "Copy address".
- Not named yet: choose "Name it and copy its address...", accept or change the suggested name, and the panel adds it
  to the address book and copies it.
- "Copy session id" copies the id instead.

Copying uses the terminal's clipboard protocol (OSC 52). If a paste comes out empty, the terminal does not let
programs write the clipboard (in iTerm2, enable "Applications in terminal may access clipboard"); the copied text is
also shown at the bottom of the panel, where you can select it.

## Pane keys

Panes take a few `Alt` keys that act at once, with no modes to remember; every `Ctrl` key goes to Claude Code and
the terminal. Clicking a pane and scrolling with the wheel work too.

| Keys | Does | The session |
| --- | --- | --- |
| `Alt+←/→/↑/↓` | Move between panes | |
| `Alt+x` | Close the focused pane | Keeps running in the background |
| `Alt+f` | Fullscreen the focused pane, or back | |
| `Alt+=` / `Alt+-` | Grow / shrink the focused pane | |
| `Alt+d` | Leave for now; run `reins tui` to come back | Layout and panes stay |
| `Alt+q` | End the whole screen | Claude sessions keep running in the background |

To stop a session, select it in the panel and press `s`: its process stops and the conversation stays.

## What opening a session does

You only press `Enter`; Reins picks the way from the session's state:

| Session | Reins |
| --- | --- |
| Already in a pane | Jumps to that pane |
| Running in the background, or stopped | Opens a pane with `claude attach`; a stopped session starts again |
| Closed | Turns it into a background session with `claude --bg --resume`, then attaches. Its id and conversation stay the same |
| Open in the desktop app or another terminal | Does not open it and says where it is open; messages and task logs still work. Once it closes there, it opens here |

To open one session straight away, give its session id, or the id of a task or callback it started:

```bash
reins tui cf408bf5
```

## With the desktop app

Claude Code's desktop app cannot open a session that runs in the background ("That session is running in the
background"). Select it in the panel and press `s`, or run `claude stop <id>`, and the app opens it.

## Installing

- **npm**: Zellij and the panel are included.
- **pip**: `pip install 'harness-reins[tui]'`, then install [Zellij](https://zellij.dev) from your package manager or
  point `REINS_ZELLIJ` at it.
- **Windows**: Zellij does not run on Windows; use `reins status` and `reins log`.

Reins runs Zellij with its own configuration in `~/.reins/zellij` and leaves yours alone. `reins tui` is for people:
inside an agent session it refuses to start.
