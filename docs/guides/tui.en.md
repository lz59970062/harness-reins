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

The panel opens the panes on the right: a session pane runs `claude attach`, a log pane runs `reins log`. A
session pane's title reads `claude <id> @address title`, the same names as the panel; Claude Code's own terminal
title does not replace it. The panel
keeps its width: the first pane takes the hint's place on the right, and later sessions, logs and terminals open
below or to the right of the newest pane there (`d` switches which), instead of squeezing the panel.

## Keys

| Key | Does |
| --- | --- |
| `Enter` or double-click | Open the selected session, or jump to its pane if it is already open. On Tasks, open the session that started the task. A single click only selects |
| `n` | Start a new Claude Code session in a directory you choose |
| `l` | Follow a task's output |
| `t` | Open a terminal in the selected session's directory |
| `d` | Switch whether new panes open below or to the right; the footer shows which |
| `m` | Send the selected session a message |
| `s` | Stop a session, after a confirmation. The conversation stays, and `Enter` opens it again |
| `c` | Cancel a task's callback, after a confirmation |
| `o` or right-click | Open the selected session's menu (below) |
| `a` | Show or hide archived sessions |
| `1`–`4` | Switch tabs |

The panel refreshes itself every 3 seconds. Its top line shows what `reins doctor` finds: "Reins is ready", or one
line per problem with what to do.

## The session menu

**Right-click** a session, or select it and press `o`, for its menu:

| Item | Does |
| --- | --- |
| Open below / Open to the right | Open it there, without switching `d` first |
| Fork into a new session | Start a new session (a new id) with a copy of this conversation; the original is untouched. Works on a session open in the desktop app too |
| Terminal in its directory | Same as `t` |
| Send a message... | Same as `m` |
| Pin to the top / Unpin | Keep it at the top. Only the Reins panel changes, not Claude's own list |
| Copy address / Name it... / Rename address... | Copy, add or change its address in the Reins address book (below) |
| Copy Claude session id | Copy Claude Code's session id, the one `claude attach` and `claude --resume` take |
| Stop... | Same as `s`, after a confirmation |
| Archive / Stop and archive... / Unarchive | Fold it out of the list, stopping it first if it runs (after a confirmation). The conversation stays. It comes back by itself when it runs again, starts a task, or has a callback waiting; `a` shows archived sessions |

Pins and archives live only in a file of Reins' own (`~/.reins/tui/sessions.json`); nothing is deleted. Claude Code
cannot rename a session or delete one conversation from outside it, so the menu has neither; rename a session with
`/rename` inside it.

### Copying a session's address

The address is the session's name in the Reins address book. Copy it, then in another conversation ask the agent to
send to that address and it uses `reins send`. A session that has a name shows it as `@name` before its title; a
pinned one shows `★`.

- Named: choose "Copy address". "Rename address..." gives it a new name and drops the old one; a name another session
  holds is refused, never taken over.
- Not named yet: choose "Name it and copy its address...", accept or change the suggested name, and the panel adds it
  to the address book and copies it.

A copy goes straight to your own computer's clipboard through the terminal's clipboard protocol (OSC 52), and a
small window shows the copied text on a line of its own. If it pastes, press `Enter` to close it. If a paste comes out
empty, your terminal does not let programs write the clipboard (macOS Terminal and the terminal in Claude Code
Desktop do not); copy the line from the window by hand:

- macOS Terminal: press ⌘R to stop mouse reporting, drag over the line, ⌘C, then ⌘R again.
- iTerm2: hold ⌥ while you drag, then ⌘C.

For copies that just work, use iTerm2 (Settings > General > Selection > "Applications in terminal may access
clipboard") or Ghostty.

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

`Ctrl+Q` in the panel also leaves: choose to leave for now (as `Alt+d`) or end the whole screen (as `Alt+q`).

**On a Mac, Alt is the Option key**, but terminals do not send it as Alt until you say so, and until then no `Alt`
key works:

- macOS Terminal: Settings > Profiles > Keyboard > "Use Option as Meta key".
- iTerm2: Settings > Profiles > Keys > Left Option key: `Esc+`.

Until then, use `Ctrl+Q` in the panel to leave or end the screen.

If the panel's pane gets closed, run `reins tui` again: it offers to start the screen again. Claude sessions keep
running, and a double click in the new panel opens them again. Run inside the screen, `reins tui` says that you are
already there.

To stop a session, select it in the panel and press `s`: its process stops and the conversation stays.

## What opening a session does

You only press `Enter` or double-click; Reins picks the way from the session's state:

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

Reins runs Zellij with its own configuration in `~/.reins/zellij` and leaves yours alone. Its session index lives in a local temporary directory (`/tmp/reins-zellij-<uid>`), so a `~/.cache` on a network filesystem such as JuiceFS or NFS cannot stall it; programs in its panes keep your own `XDG_CACHE_HOME`. `reins tui` is for people:
inside an agent session it refuses to start.
