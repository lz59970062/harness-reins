"""`reins tui [id]`: one Zellij session named "reins", the Reins panel on its left (design section 7).

Zellij keeps the layout when the terminal or SSH connection goes away, so
running `reins tui` again comes back to the same panes. Reins uses its own
Zellij configuration under ~/.reins/zellij and leaves the user's alone.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Mapping, Optional

from ...core import sessions
from ...harness import AGENTS
from ...harness.claude import agents as claude_agents
from ...harness.codex import app_server as codex_app_server
from ...harness.codex import threads as codex_threads
from ...harness.pi import sessions as pi_sessions
from ...runtime import worker_command
from ...store import layout
from .live import (CACHE_ENV, HINT_TITLE, LAUNCH_ENV, ensure_zellij_cache_dir, read_record, session_of,
                   zellij_cache_dir, zellij_environment)

# Another name keeps a second Reins TUI (a test, another queue) out of the user's own session.
SESSION_NAME = os.environ.get("REINS_TUI_SESSION", "").strip() or "reins"
ZELLIJ_ENV = "REINS_ZELLIJ"
# The first start on a slow or CPU-capped machine (a container) can keep Zellij busy for a while.
READY_SECONDS = 90.0
NOTICE_SECONDS = 3.0


def zellij_executable(environment: Mapping[str, str] = os.environ) -> Optional[str]:
    """The Zellij Reins runs: REINS_ZELLIJ, the one shipped beside a packaged reins, or one on PATH."""
    configured = environment.get(ZELLIJ_ENV, "").strip()
    if configured:
        return configured if os.access(configured, os.X_OK) else None
    if getattr(sys, "frozen", False):
        bundled = Path(sys.executable).resolve().parent / "zellij"
        if os.access(bundled, os.X_OK):
            return str(bundled)
    return shutil.which("zellij", path=environment.get("PATH"))


def config_dir() -> Path:
    return layout.reins_home() / "zellij"


def _kdl(text: str) -> str:
    # KDL quotes and escapes like JSON, except that it has no \uXXXX escape: non-ASCII stays as it is.
    return json.dumps(text, ensure_ascii=False)


# A share, not a column count: Zellij creates the session before any terminal attaches, at a small
# default size, and a fixed width that does not fit then stops the session from starting.
PANEL_WIDTH = "38%"


def write_config(directory: Path, panel: list[str], hint: Optional[list[str]] = None) -> bool:
    """Reins's Zellij configuration, rewritten each time so an upgrade takes effect.

    Returns whether the layout changed. A running session keeps the layout it
    was made with, so a change only reaches it once the session is recreated.
    """
    layout_path = directory / "layouts" / "reins.kdl"
    try:
        before = layout_path.read_text(encoding="utf-8")
    except OSError:
        before = None
    (directory / "layouts").mkdir(parents=True, exist_ok=True)
    (directory / "config.kdl").write_text("\n".join([
        "// Written by `reins tui`; changes here are overwritten.",
        "show_startup_tips false",
        "show_release_notes false",
        "mouse_mode true",
        "pane_frames true",
        'default_layout "reins"',
        # Reins rebuilds the panes from its own state; a resurrected session would rerun stale commands.
        "session_serialization false",
        'on_force_close "detach"',
        # Alt+q asks before ending the screen, with Detach preselected: ending it stops the Pi sessions and
        # shells in its panes, and Enter alone should not do that.
        'on_quit "ask_detach"',
        # Zellij's own right-click menu (Paste, New pane, Close, ...) opened on every pane that does not
        # take the mouse itself, such as a Claude pane, and looked like Reins' session menu.
        "context_menu_enabled false",
        # Zellij's own keys are modes (Ctrl+p, Ctrl+o, ...) that swallow what is typed next, which reads
        # as a hang, and Claude Code uses some of the same keys. A few Alt keys replace them all.
        "keybinds clear-defaults=true {",
        "    shared {",
        '        bind "Alt Left" "Alt h" { MoveFocus "Left"; }',
        '        bind "Alt Right" "Alt l" { MoveFocus "Right"; }',
        '        bind "Alt Up" "Alt k" { MoveFocus "Up"; }',
        '        bind "Alt Down" "Alt j" { MoveFocus "Down"; }',
        '        bind "Alt x" { CloseFocus; }',
        '        bind "Alt f" { ToggleFocusFullscreen; }',
        '        bind "Alt =" "Alt +" { Resize "Increase"; }',
        '        bind "Alt -" { Resize "Decrease"; }',
        '        bind "Alt d" { Detach; }',
        '        bind "Alt q" { Quit; }',
        "    }",
        "}",
        "",
    ]), encoding="utf-8")
    hint = hint or [panel[0], "_tui-hint"]

    def command_pane(name: str, argv: list[str], size: Optional[str] = None) -> list[str]:
        sized = f" size={_kdl(size)}" if size else ""
        return [f"        pane name={_kdl(name)}{sized} command={_kdl(argv[0])} {{",
                "            args " + " ".join(_kdl(word) for word in argv[1:]),
                "        }"]

    # The panel keeps a fixed width; sessions open in the hint pane's place and then stack below it.
    (directory / "layouts" / "reins.kdl").write_text("\n".join([
        "layout {",
        '    pane split_direction="vertical" {',
        *command_pane("Reins", panel, PANEL_WIDTH),
        *command_pane(HINT_TITLE, hint),
        "    }",
        "    pane size=1 borderless=true {",
        '        plugin location="compact-bar"',
        "    }",
        "}",
        "",
    ]), encoding="utf-8")
    return before is not None and before != layout_path.read_text(encoding="utf-8")


def _zellij(executable: str, directory: Path, *arguments: str, timeout: float = 10.0,
            env: Optional[Mapping[str, str]] = None) -> "subprocess.CompletedProcess[str]":
    """Run Zellij; a call it does not answer in time comes back as a failure (exit 124), not an exception."""
    argv = [executable, "--config-dir", str(directory), *arguments]
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, check=False,
                              env=zellij_environment(os.environ if env is None else env))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, "", f"Zellij did not answer within {timeout:g} s")
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", f"could not run Zellij: {exc}")


def session_state(executable: str, directory: Path) -> Optional[str]:
    """"running", "exited", or None when there is no Reins Zellij session."""
    result = _zellij(executable, directory, "list-sessions", "--no-formatting")
    for line in (result.stdout or "").splitlines():
        words = line.split()
        if words and words[0] == SESSION_NAME:
            return "exited" if "EXITED" in line else "running"
    return None


def inside_agent(environment: Mapping[str, str]) -> Optional[str]:
    """The agent whose session this runs in, if any: `reins tui` is for people, not agents."""
    for name in AGENTS.names:
        if AGENTS.get(name).matches_environment(environment):
            return name
    return None


def resolve(identifier: str, root: Path, environment: Mapping[str, str]) -> str:
    """The session an id names: a session id or its start, a Pi session file, a task id, or a callback id."""
    identifier = identifier.strip()
    if sessions.FULL_ID.fullmatch(identifier):
        return identifier
    if identifier.endswith(".jsonl") and os.path.isabs(os.path.expanduser(identifier)):
        return os.path.normcase(str(Path(identifier).expanduser().resolve()))
    try:
        found = session_of(read_record(layout.managed_task_path(root, identifier)))
    except ValueError:
        found = None
    if found:
        return found
    for state in ("pending", "running", "failed", "canceled", "done", layout.ACTIVE_STATE):
        found = session_of(read_record(root / state / f"{identifier}.json"))
        if found:
            return found
    tasks_root = layout.managed_tasks_root(root)
    matches = {session for session in (session_of(read_record(path))
                                       for path in tasks_root.glob(f"{identifier}*/task.json")) if session}
    views = list(claude_agents.session_views(environment))
    try:
        views += codex_threads.session_views(environment, others={})
    except (codex_app_server.AppServerProtocolError, OSError, ValueError):
        pass
    views += pi_sessions.session_views(environment)
    matches |= {view.session for view in views
                if (view.agent != "pi" and view.session.startswith(identifier)) or view.handle == identifier}
    if len(matches) == 1:
        return matches.pop()
    if matches:
        raise ValueError(f"{identifier} matches more than one session; give more of the id")
    raise ValueError(f"no session, task or callback {identifier} on this machine")


WIZARD_SECONDS = 3.0


def _panes(executable: str, directory: Path) -> Optional[list]:
    result = _zellij(executable, directory, "--session", SESSION_NAME, "action", "list-panes", "--json", "--all",
                     "--command")
    try:
        panes = json.loads(result.stdout) if result.returncode == 0 else None
    except ValueError:
        return None
    return [pane for pane in panes if isinstance(pane, dict)] if isinstance(panes, list) else None


def has_panel(panes: list) -> bool:
    """Whether the Reins panel still runs in the session: a person can close its pane like any other."""
    return any(not pane.get("exited") and not pane.get("is_plugin")
               and (pane.get("title") == "Reins" or "_tui-panel" in str(pane.get("terminal_command") or ""))
               for pane in panes)


# What runs in a pane Reins opened and can open again; anything else (a shell, a command) may hold work.
_REINS_VIEWS = ("_tui-hint", "_tui-panel", "log")


def _program(command: str) -> list[str]:
    """A pane's command without the `env ...` Reins puts before it (live.pane_command)."""
    try:
        words = shlex.split(command)
    except ValueError:
        return []
    if words and Path(words[0]).name == "env":
        words = words[1:]
        while words and (words[0] == "-u" or words[0].startswith("-") or "=" in words[0]):
            words = words[2:] if words[0] == "-u" else words[1:]
    return words


def reopenable(command: str) -> bool:
    """A pane that shows something living elsewhere: a Claude background session, a Codex thread on its
    daemon, a task log, or the panel and hint. A Pi session ends with its pane, and a shell is the user's."""
    words = _program(command)
    if not words:
        return False
    name = Path(words[0]).name
    if name.startswith("claude"):
        return words[1:2] == ["attach"]
    if name == "codex":
        return "--remote" in words
    # `python .../_entry.py <command>`, or a frozen `reins <command>`.
    rest = words[2:] if words[1:2] and words[1].endswith("_entry.py") else words[1:]
    return bool(rest) and rest[0] in _REINS_VIEWS


def holds_work(panes: list) -> bool:
    """Whether closing the session would close something besides views Reins can reopen."""
    for pane in panes:
        if pane.get("is_plugin") or pane.get("exited"):
            continue
        if not reopenable(str(pane.get("terminal_command") or "")):
            return True
    return False


def ask(question: str, default: bool = True) -> bool:
    """The person's answer, ``default`` on a plain Enter; no when nobody can answer (stdin is not a terminal)."""
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(question + (" [Y/n] " if default else " [y/N] ")).strip().lower()
    except EOFError:
        return False
    return default if not answer else answer in ("y", "yes")


def zellij_servers(marker: str, proc: Path = Path("/proc")) -> set[int]:
    """This user's Zellij server processes started with ``LAUNCH_ENV=marker``."""
    wanted = f"{LAUNCH_ENV}={marker}"
    found: set[int] = set()
    if proc.is_dir():
        try:
            entries = [entry for entry in proc.iterdir() if entry.name.isdigit()]
        except OSError:
            return found
        for entry in entries:
            try:
                if entry.stat().st_uid != os.getuid():
                    continue
                argv = (entry / "cmdline").read_bytes().split(b"\0")
                if len(argv) < 2 or argv[1] != b"--server":
                    continue
                if wanted.encode() in (entry / "environ").read_bytes().split(b"\0"):
                    found.add(int(entry.name))
            except OSError:
                continue
        return found
    try:  # macOS: -E appends each of this user's processes' environment to its command.
        listing = subprocess.run(["ps", "-E", "-ww", "-e", "-o", "uid=,pid=,command="], capture_output=True,
                                 text=True, errors="replace", timeout=10, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return found
    for line in listing.splitlines():
        words = line.split()
        if (len(words) > 3 and words[0] == str(os.getuid()) and words[1].isdigit() and "--server" in words[3:5]
                and wanted in words):
            found.add(int(words[1]))
    return found


def _end(pids: set[int], grace: float = 3.0) -> set[int]:
    """Stop these processes (TERM, then KILL after ``grace``); the ones still alive."""
    def alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        try:  # A zombie is gone; a container whose pid 1 is not an init may never reap it.
            return (Path("/proc") / str(pid) / "stat").read_text().rpartition(")")[2].split()[0] != "Z"
        except (OSError, IndexError):
            return True

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and any(alive(pid) for pid in pids):
        time.sleep(0.1)
    for pid in pids:
        if alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    time.sleep(0.2)
    return {pid for pid in pids if alive(pid)}


READY, NO_PANEL, SILENT = "ready", "no panel", "silent"


def _wait_ready(executable: str, directory: Path) -> str:
    """Wait for the new session and get it into the layout Reins wrote.

    READY: the panel is up. NO_PANEL: Zellij answers but the panel never
    appeared. SILENT: Zellij never answered at all, so attaching would only
    show an empty screen.

    Zellij's first-run wizard (a floating `configuration` plugin) shows up a
    second or two after the panes, and a client that attaches before it is
    closed finds the panel fullscreen. So after the panel appears this keeps
    looking for a while, closes the wizard, and undoes a fullscreen panel.
    """
    started = time.monotonic()
    deadline = started + READY_SECONDS
    settled = None
    noticed = answered = False
    while time.monotonic() < deadline:
        if not noticed and time.monotonic() - started >= NOTICE_SECONDS:
            print("reins tui: waiting for Zellij to start; the first start can take a minute...",
                  file=sys.stderr, flush=True)
            noticed = True
        panes = _panes(executable, directory)
        answered = answered or panes is not None
        if panes is not None and any(pane.get("title") == "Reins" for pane in panes):
            settled = settled or time.monotonic() + WIZARD_SECONDS
            wizard = [pane for pane in panes if pane.get("is_plugin") and pane.get("plugin_url") == "configuration"]
            for pane in wizard:
                _zellij(executable, directory, "--session", SESSION_NAME, "action", "close-pane",
                        "--pane-id", f"plugin_{pane.get('id')}")
            if wizard or time.monotonic() >= settled:
                break
        time.sleep(0.2)
    if settled is None:
        return NO_PANEL if answered else SILENT
    time.sleep(0.3)
    for pane in _panes(executable, directory) or ():
        if not pane.get("is_plugin") and pane.get("is_fullscreen"):
            _zellij(executable, directory, "--session", SESSION_NAME, "action", "focus-pane-id",
                    f"terminal_{pane.get('id')}")
            _zellij(executable, directory, "--session", SESSION_NAME, "action", "toggle-fullscreen")
    return READY

def _by_hand(zellij: str) -> str:
    """How a person runs the Reins Zellij: with its own cache directory, or it cannot find the session."""
    return f"{CACHE_ENV}={zellij_cache_dir()} {zellij}"


def zellij_log() -> Path:
    return Path(tempfile.gettempdir()) / f"zellij-{os.getuid()}" / "zellij-log" / "zellij.log"


def main(args: argparse.Namespace) -> int:
    environment = dict(os.environ)
    agent = inside_agent(environment)
    if agent is not None:
        print(f"reins tui is for people, and this runs inside a {agent} session. Agents use `reins run`, "
              "`reins agent`, `reins ack` and `reins log --no-follow`.", file=sys.stderr)
        return 2
    if os.name == "nt":
        print("reins tui needs Zellij, which does not run on Windows; use `reins status` and `reins log`.",
              file=sys.stderr)
        return 2
    if importlib.util.find_spec("textual") is None:
        print("reins tui needs Textual: pip install 'harness-reins[tui]' (the npm package includes it).",
              file=sys.stderr)
        return 2
    zellij = zellij_executable(environment)
    if zellij is None:
        print("reins tui needs Zellij (https://zellij.dev). The npm package includes it; with pip, install it "
              f"from your package manager or set {ZELLIJ_ENV} to its path.", file=sys.stderr)
        return 2
    root = layout.queue_dir(args).expanduser().absolute()
    target = None
    if getattr(args, "id", None):
        try:
            target = resolve(args.id, root, environment)
        except ValueError as exc:
            print(f"reins tui: {exc}", file=sys.stderr)
            return 2
    try:
        ensure_zellij_cache_dir()
    except RuntimeError as exc:
        print(f"reins tui: {exc}", file=sys.stderr)
        return 1
    directory = config_dir()
    panel = worker_command("_tui-panel", *(["--queue-dir", str(args.queue_dir)] if getattr(args, "queue_dir", None) else []))
    changed = write_config(directory, panel, worker_command("_tui-hint"))
    state = session_state(zellij, directory)
    inside = os.environ.get("ZELLIJ_SESSION_NAME") == SESSION_NAME
    if state == "running" and target is None:
        panes = _panes(zellij, directory)
        if panes is not None and not has_panel(panes):
            if inside:
                print("reins tui: you are in the Reins screen already, and its panel was closed. End this screen "
                      "(Ctrl+Q in the panel, or Alt+q) and run `reins tui` again to get it back; your Claude "
                      "and Codex sessions keep running.", file=sys.stderr)
                return 1
            busy = holds_work(panes)
            if ask("reins tui: the Reins panel was closed in this screen. Start the screen again? Your Claude "
                   "and Codex sessions keep running" + ("; the terminals and Pi sessions open in it close, with "
                                                        "anything running in them." if busy else "."),
                   default=not busy):
                _zellij(zellij, directory, "kill-session", SESSION_NAME)
                _zellij(zellij, directory, "delete-session", "--force", SESSION_NAME)
                state = session_state(zellij, directory)
        elif inside:
            print("reins tui: you are in the Reins screen already; the panel is on the left. Ctrl+Q in the panel "
                  "leaves it.", file=sys.stderr)
            return 0
    if state == "running" and changed:
        print("reins tui: the Reins Zellij session was started by another version of Reins and keeps its old "
              f"panel. To get the new one, leave it and run `{_by_hand(zellij)} delete-session --force {SESSION_NAME}`, "
              "then `reins tui` again; your Claude and Codex sessions keep running.", file=sys.stderr)
    if state == "exited":
        deleted = _zellij(zellij, directory, "delete-session", SESSION_NAME)
        if deleted.returncode != 0:
            print(f"reins tui: could not remove the ended Zellij session {SESSION_NAME}: "
                  f"{(deleted.stderr or deleted.stdout).strip()}", file=sys.stderr)
            return 1
        state = None
    if state is None:
        marker = f"{os.getpid()}-{time.time_ns()}"
        created = _zellij(zellij, directory, "attach", "--create-background", SESSION_NAME,
                          env={**os.environ, LAUNCH_ENV: marker})
        if created.returncode != 0:
            print(f"reins tui: Zellij could not start the {SESSION_NAME} session: "
                  f"{(created.stderr or created.stdout).strip()}", file=sys.stderr)
            return 1
        ready = _wait_ready(zellij, directory)
        if ready == SILENT:
            # A server that never answers would leave an attached client on an empty screen. Its socket does
            # not answer `kill-session` either, so end the server this run started by its process id.
            servers = zellij_servers(marker)
            left = _end(servers)
            _zellij(zellij, directory, "delete-session", "--force", SESSION_NAME)
            if not servers:
                outcome = "and its server process was not found; end that `zellij --server` process yourself"
            elif left:
                outcome = f"and it could not be stopped; end it with `kill -9 {' '.join(map(str, sorted(left)))}`"
            else:
                outcome = "so it was stopped again"
            print(f"reins tui: Zellij started the {SESSION_NAME} session but never answered within "
                  f"{READY_SECONDS:g} s, {outcome}. Your sessions are not affected. Please report this "
                  f"with Zellij's log ({zellij_log()}) and the output of `{zellij} --version`.", file=sys.stderr)
            return 1
        if ready == NO_PANEL:
            print(f"reins tui: the Reins panel did not appear within {READY_SECONDS:g} s; attaching anyway. If it "
                  f"stays missing, run `reins _tui-panel` in a terminal to see why.", file=sys.stderr)
    if target is not None:
        from .live import LiveBackend, Zellij

        backend = LiveBackend(queue_root=root, environment=environment,
                              zellij=Zellij(zellij, session=SESSION_NAME, config_dir=str(directory),
                                            environment=environment))
        result = backend.open_session(target)
        if not result.ok:
            print(f"reins tui: {result.message}", file=sys.stderr)
    if os.environ.get("ZELLIJ_SESSION_NAME") == SESSION_NAME:
        return 0  # Already in it: the pane opened beside this one.
    os.execve(zellij, [zellij, "--config-dir", str(directory), "attach", SESSION_NAME], zellij_environment(os.environ))
    return 0  # Not reached.


HINT_TEXT = """Select a session on the left and press Enter (or double-click it) to open it here.

  Enter   open the session (or jump to its pane)    n   new session
  l       follow a task's log                        s   stop a session
  t       a terminal in the session's directory      m   message a session
  d       later panes open below / to the right      c   cancel a callback
  o       session menu (or right-click a session)    a   show archived sessions
  1-4     Sessions, Tasks, Goals, Messages

Alt+arrows  move between panes      Alt+x  close a pane      Alt+f  fullscreen
Alt+d       leave (run `reins tui` to come back)     Alt+q  leave or end (asks)
Ctrl+Q in the panel also leaves or ends it.
Mac: Alt is Option. macOS Terminal: Settings > Profiles > Keyboard > "Use Option as Meta key";
iTerm2: Settings > Profiles > Keys > Left Option key: Esc+.
Closing a pane or the screen never stops a Claude or Codex session (s in the panel stops
a Claude one). A Pi session runs in its pane and ends with it."""


def hint_main(args: argparse.Namespace) -> int:
    """Hidden `reins _tui-hint`: what the right side says before a session opens."""
    print(HINT_TEXT, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


def panel_main(args: argparse.Namespace) -> int:
    """Hidden `reins _tui-panel`: the panel inside the Reins Zellij session."""
    from .live import LiveBackend, Zellij
    from .panel import run_panel

    root = layout.queue_dir(args).expanduser().absolute()
    zellij = zellij_executable() or "zellij"
    return run_panel(LiveBackend(queue_root=root, zellij=Zellij(zellij, own_session=SESSION_NAME)))
