# Terminal UI: reins tui

One terminal and one login show every Claude Code, Codex and Pi session on this machine, with the tasks, goals and
messages they started, and open any session in a pane beside them in its own harness's interface. A session only
ever has one process, so nothing forks.

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

- **Sessions**: sessions grouped by directory, one row per project folder with its sessions indented under it, so
  you see at a glance which folder holds which sessions. Each session shows the Reins address you gave it
  (`@name`) beside the title its harness gave it, then where it runs, whether it is busy, how many of its tasks are
  running and its pending callbacks. Closed sessions that started tasks lately are listed too, so you can find them
  again.
- **Tasks**: every task with the session that started it and whether its callback was acknowledged.
- **Goals**: plans and progress.
- **Messages**: messages between sessions.

The panel opens the panes on the right: a session pane runs `claude attach`, `codex resume --remote` or `reins pi`
(see [Codex, Pi and DeepSeek Harness](#codex-pi-and-deepseek-harness)), a log pane runs `reins log`. A session
pane's title reads `<harness> <id> @address title`, such as `claude 08e8651e @lab motion plots`, the same names as
the panel; the harness's own terminal title does not replace it, and a pane you rename gets its name back within 3
seconds, since the panel finds a session's pane by that name. The panel
keeps its width: the first pane takes the hint's place on the right, and later sessions, logs and terminals open
below or to the right of the newest pane there (`d` switches which), instead of squeezing the panel.

## Keys

| Key | Does |
| --- | --- |
| `Enter` or double-click | Open the selected session, or jump to its pane if it is already open. On Tasks, open the session that started the task. A single click only selects |
| `n` | Start a new Claude Code, Codex or Pi session in a directory you choose; with more than one installed, it asks which. A Claude Code or Pi session also gets a name, `<folder>-<harness>` (such as `api-claude`) unless you type another or clear it. A Codex thread exists only after its first message, so name it from its menu then |
| `l` | Follow a task's output |
| `t` | Open a terminal in the selected session's directory, or the selected folder's |
| `d` | Switch whether new panes open below or to the right; the footer shows which |
| `m` | Send the selected session a message |
| `s` | Stop a Claude Code session, after a confirmation. The conversation stays, and `Enter` opens it again |
| `c` | Cancel a task's callback, after a confirmation |
| `o` or right-click | Open the selected session's menu (below); on a folder's row: a new session or a terminal there |
| `a` | Show or hide archived sessions |
| `1`–`4` | Switch tabs |

The panel refreshes itself every 3 seconds. Its top line shows what `reins doctor` finds: "Reins is ready", or one
line per problem with what to do.

A session that cannot open says so where you look: double-click a Claude Code session that is open in another
terminal, and a flashing red window tells you why and what to do; Enter closes it. Any other action that fails does
the same.

## The session menu

**Right-click** a session, or select it and press `o`, for its menu:

| Item | Does |
| --- | --- |
| Open below / Open to the right | Open it there, without switching `d` first |
| Fork into a new session | Start a new session (a new id) with a copy of this conversation; the original is untouched. Works on a session open in the desktop app too |
| Terminal in its directory | Same as `t` |
| Send a message... | Same as `m` |
| Start a collaborator... | Start a new session to work with this one (below) |
| Pin to the top / Unpin | Keep it at the top. Only the Reins panel changes, not Claude's own list |
| Copy address / Name it... / Rename address... | Copy, add or change its address in the Reins address book (below) |
| Copy Claude session id / Codex thread id / Pi session file | Copy the id the harness itself takes: `claude --resume`, `codex resume`, `pi --session` |
| Stop... | Same as `s`, after a confirmation |
| Archive / Stop and archive... / Unarchive | Fold it out of the list, stopping it first if it runs (after a confirmation). The conversation stays. It comes back by itself when it runs again, starts a task, or has a callback waiting; `a` shows archived sessions |

**Start a collaborator...** asks three things: the harness (skipped when only one is installed), the directory
(the session's own, so Enter is enough) and a first task (optional). Reins does the rest in one go: a session without
a Reins address gets one (the original from its title, the new one `<address>-<harness>`, with `-2` added when the
name is taken), the new session starts knowing its partner and how to reach it with `reins send`, the original gets a
message with the new address and task, and the new session's pane opens beside the others. The message never wakes a
closed session; it reads it when it next opens. A new Codex session is named once Codex lists its thread, which Reins
waits up to 30 seconds for. A DeepSeek Harness session can be the original (it reads the message while its `reins wait` runs) but cannot be
started as a collaborator.

Pins and archives live only in a file of Reins' own (`~/.reins/tui/sessions.json`); nothing is deleted. Claude Code
cannot rename a session or delete one conversation from outside it, so the menu has neither; rename a session with
`/rename` inside it. The menu shows only what a session's harness supports: Stop is for Claude Code, Fork for Claude
Code and Codex, and a DeepSeek Harness session has no Open.

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

## Selecting and copying

- **Drag to select** in Claude Code, Codex, Pi and terminal panes. The text is copied when you let go of the mouse,
  so just paste. **Do not press `Ctrl+C` afterwards**: it goes to the program in the pane, which Claude Code and
  Codex read as cancel (twice as quit) and a shell as an interrupt.
- **The panel's text does not select**: the panel uses the mouse to pick sessions. For a session's address or id, use
  the Copy items in its right-click menu (above).
- **The terminal's own selection** works everywhere, the panel included: hold `Shift` while you drag in Windows
  Terminal and most Linux terminals, hold ⌥ in iTerm2, or press ⌘R first in macOS Terminal. Then copy as you
  normally would; in Windows Terminal, `Ctrl+C` with a selection only copies. It ignores pane borders and picks up
  the next pane's text too, so press `Alt+f` to fullscreen the pane first.
- **If a paste comes out empty after letting go**, the terminal does not let programs write the clipboard (OSC 52),
  as in macOS Terminal. Use the terminal's own selection instead.

## Pane keys

Panes take a few `Alt` keys that act at once, with no modes to remember; every `Ctrl` key goes to Claude Code and
the terminal. Clicking a pane and scrolling with the wheel work too.

| Keys | Does | The session |
| --- | --- | --- |
| `Alt+←/→/↑/↓` | Move between panes | |
| `Alt+x` | Close the focused pane | Claude Code and Codex keep running in the background; a Pi session ends |
| `Alt+f` | Fullscreen the focused pane, or back | |
| `Alt+=` / `Alt+-` | Grow / shrink the focused pane | |
| `Alt+r` | Back to the panel; if it was closed, open it again on the left | Open panes keep running and move to its right |
| `Alt+d` | Leave for now; run `reins tui` to come back | Layout and panes stay |
| `Alt+q` | Asks first, with Detach preselected; choose Quit to end the whole screen | When ended, Claude Code and Codex keep running in the background; Pi sessions and your own shells in it end |

`Ctrl+Q` in the panel also leaves: choose to leave for now (as `Alt+d`) or end the whole screen (as `Alt+q`).

**On a Mac, Alt is the Option key**, but terminals do not send it as Alt until you say so, and until then no `Alt`
key works:

- macOS Terminal: Settings > Profiles > Keyboard > "Use Option as Meta key".
- iTerm2: Settings > Profiles > Keys > Left Option key: `Esc+`.

Until then, use `Ctrl+Q` in the panel to leave or end the screen.

If the panel's pane gets closed, press `Alt+r`: the panel opens again on the left, and the sessions, shells and Pi
sessions already open keep running and are rearranged to its right. Run inside the screen, `reins tui` says that you
are already there.

To stop a session, select it in the panel and press `s`: its process stops and the conversation stays.

## Codex, Pi and DeepSeek Harness

Every harness opens in its own interface, and Reins keeps the same promise for each: one process per session.

| Harness | Listed from | A session pane runs | When its pane closes | Callbacks while it is open |
| --- | --- | --- | --- | --- |
| Claude Code | `claude agents` | `claude attach`, a background session | It keeps running | The Reins hook in the session |
| Codex | Codex's app-server daemon | `codex resume --remote`, the thread on the daemon | It stays on the daemon, which unloads it once idle | A new turn in that thread on the daemon |
| Pi | Pi's session files | `reins pi`, Pi with the Reins extension and writer lease | Pi ends with its pane | The Reins Pi extension |
| DeepSeek Harness | The Reins address book and tasks | Nothing: it has no terminal interface | | Its live `reins wait` |

- **Codex** sessions open through Codex's shared app-server daemon (`codex app-server daemon`), so the Reins pane,
  Codex Desktop over SSH and `codex resume --remote` all show the same thread. Reins starts the daemon when it is not
  running. A Codex window opened without `--remote` writes its thread itself: the panel shows it as open in a
  terminal and does not open it again. A new Codex session appears in the list after its first message.
- A Codex callback keeps the thread's own settings and adds only the Reins queue and address book to its writable
  folders, so the agent can ACK without asking; the thread keeps that access while it is loaded.
- **Pi** has no daemon: its session lives in its pane. Pi sessions opened elsewhere with the Reins extension show as
  open in a terminal; one opened without it cannot be seen.
- **DeepSeek Harness** sessions that have a Reins name or started tasks are listed so you can message them, copy
  their address and follow their tasks. Open them in DeepSeek Harness's web or desktop app.

## What opening a session does

You only press `Enter` or double-click; Reins picks the way from the session's state. For a Claude Code session:

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
