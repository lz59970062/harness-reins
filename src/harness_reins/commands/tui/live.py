"""The panel's live backend: each harness's own session list, the Reins queue, and the Zellij session around it.

Nothing here keeps state between calls (design invariant 2): each read asks
Claude (`claude agents`), the Codex daemon, Pi's session files, the queue files
and Zellij again. The only thing the panel stores is which sessions the user
pinned or archived, in one small file of its own.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from ...core import sessions
from ...core.messages import Refused, validate_name
from ...core.sessions import SessionView
from ...engine import claude_background
from ...harness.agy import session as agy_session
from ...harness.agy import sessions as agy_sessions
from ...harness.claude import agents as claude_agents
from ...harness.claude import session as claude_session
from ...harness.codex import app_server as codex_app_server
from ...harness.codex import threads as codex_threads
from ...harness.pi import channel as pi_channel
from ...harness.pi import sessions as pi_sessions
from ...install import skills as skill_install
from ...runtime import worker_command
from ...store import layout, link, records
from .backend import (BELOW, ActionResult, GoalRow, MessageRow, SessionRow, TaskRow, collaborator_notice,
                      collaborator_prompt, free_name, short_id, suggested_name)

Run = Callable[..., "subprocess.CompletedProcess[str]"]

# How far back the panel looks for tasks, and for closed sessions they came from.
RECENT_SECONDS = 14 * 86400
MAX_ROWS = 300
RUNNING_TASK_STATES = frozenset({"submitting", "submitted", "launching", "running"})
FINISHED_GOAL_PREFIXES = ("complete", "done", "cancel", "abandon", "stop")
CALLBACK_STATES = ("pending", "running", "failed", "canceled", "done")
ZELLIJ_TIMEOUT_SECONDS = 10.0


# Zellij 0.46 keeps a SQLite index of its sessions under $XDG_CACHE_HOME. On a network filesystem (a container
# whose ~/.cache is JuiceFS or NFS) its locks can wedge, and then every `zellij action` waits forever. So the
# Reins Zellij keeps its cache in a local directory of its own, and what runs inside its panes gets the user's
# own XDG_CACHE_HOME back.
CACHE_ENV = "XDG_CACHE_HOME"
USER_CACHE_ENV = "REINS_USER_XDG_CACHE_HOME"  # The user's value; empty when it was not set.
# Set on the Zellij that starts the session; its server inherits it, which tells that server from any other.
LAUNCH_ENV = "REINS_TUI_LAUNCH"


def zellij_cache_dir() -> Path:
    owner = os.getuid() if hasattr(os, "getuid") else os.environ.get("USERNAME", "user")
    return Path(tempfile.gettempdir()) / f"reins-zellij-{owner}"


def ensure_zellij_cache_dir() -> Path:
    """Create the private cache directory; refuse one that is not this user's own real directory."""
    directory = zellij_cache_dir()
    try:
        directory.mkdir(mode=0o700, exist_ok=True)
        status = directory.lstat()
    except OSError as exc:
        raise RuntimeError(f"cannot create {directory}: {exc}") from None
    if not stat.S_ISDIR(status.st_mode) or status.st_uid != os.getuid():
        raise RuntimeError(f"{directory} is not a directory owned by you; remove it and try again")
    if status.st_mode & 0o077:
        directory.chmod(0o700)
    return directory


def zellij_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """``environment`` for running Zellij: the Reins cache directory, remembering the user's own."""
    result = dict(environment)
    result.setdefault(USER_CACHE_ENV, environment.get(CACHE_ENV, ""))
    result[CACHE_ENV] = str(zellij_cache_dir())
    if not result.get("SHELL"):  # Zellij would fall back to /bin/sh, which shows a bash prompt as raw escapes.
        result["SHELL"] = default_shell(result)
    return result


def default_shell(environment: Mapping[str, str]) -> str:
    return environment.get("SHELL") or shutil.which("bash", path=environment.get("PATH")) or "/bin/sh"


def user_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """``environment`` as the user had it, for Claude and anything else started from the panel."""
    result = dict(environment)
    result.pop(LAUNCH_ENV, None)
    if USER_CACHE_ENV in result:
        original = result.pop(USER_CACHE_ENV)
        if original:
            result[CACHE_ENV] = original
        else:
            result.pop(CACHE_ENV, None)
    return result


def pane_command(argv: Sequence[str], environment: Mapping[str, str]) -> list[str]:
    """``argv`` for a pane: panes start with the Zellij server's environment, so restore the user's."""
    original = environment.get(USER_CACHE_ENV, environment.get(CACHE_ENV, ""))
    restore = [f"{CACHE_ENV}={original}"] if original else ["-u", CACHE_ENV]
    return ["env", "-u", USER_CACHE_ENV, "-u", LAUNCH_ENV, *restore, *argv]


class Zellij:
    """`zellij action` on the session the panel runs in (or on ``session`` from outside it)."""

    def __init__(self, executable: str, *, session: Optional[str] = None, config_dir: Optional[str] = None,
                 run: Run = subprocess.run, environment: Optional[Mapping[str, str]] = None,
                 own_session: Optional[str] = None) -> None:
        self.executable = executable
        self.session = session
        self.own_session = own_session  # The only session end() may end, when set.
        self.config_dir = config_dir
        self.run = run
        self.environment = zellij_environment(os.environ if environment is None else environment)

    def _action(self, *arguments: str) -> str:
        argv = [self.executable]
        if self.config_dir:
            argv += ["--config-dir", self.config_dir]
        if self.session:
            argv += ["--session", self.session]
        argv += ["action", *arguments]
        try:
            result = self.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=ZELLIJ_TIMEOUT_SECONDS, check=False, env=self.environment)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"zellij {arguments[0]} did not answer within {ZELLIJ_TIMEOUT_SECONDS:g} s; "
                               "try again in a moment") from None
        if result.returncode != 0:
            raise RuntimeError(f"zellij {arguments[0]} failed: {(result.stderr or result.stdout or '').strip()}")
        return result.stdout or ""

    def panes(self) -> list[dict[str, object]]:
        try:
            panes = json.loads(self._action("list-panes", "--json", "--command", "--tab"))
        except (RuntimeError, ValueError, OSError):
            return []
        return [pane for pane in panes if isinstance(pane, dict)] if isinstance(panes, list) else []

    def new_pane(self, argv: Sequence[str], *, name: str, direction: Optional[str] = "right",
                 close_on_exit: bool = True, cwd: Optional[str] = None, replace: Optional[str] = None) -> str:
        """Open a pane; ``replace`` puts it in place of that pane, which is closed."""
        arguments = ["new-pane", "--name", name]
        if replace:
            arguments += ["--in-place", "--pane-id", replace, "--close-replaced-pane"]
        elif direction:
            arguments += ["--direction", direction]
        if close_on_exit:
            arguments.append("--close-on-exit")
        if cwd:
            arguments += ["--cwd", cwd]
        return self._action(*arguments, "--", *argv).strip()

    def focus(self, pane: str) -> None:
        try:
            self._action("focus-pane-id", pane)
        except RuntimeError as exc:
            # Zellij 0.46 fails focus-pane-id on the pane that already has the focus, which is what was asked.
            if "already focused" not in str(exc):
                raise

    def hide_floating(self) -> None:
        # Zellij 0.46 fails hide-floating-panes, with no message, when they are already hidden, and fails
        # are-floating-panes-visible when its answer is "false". A Zellij older than 0.44.1 has no such query: then
        # hide them anyway, and an error from that is not proof they are showing.
        try:
            answer = self._action("are-floating-panes-visible").strip()
        except RuntimeError as exc:
            answer = "false" if str(exc).endswith(": false") else "unknown"
        if answer == "false":
            return
        try:
            self._action("hide-floating-panes")
        except RuntimeError:
            if answer == "true":
                raise

    def embed(self, pane: str) -> None:
        """Turn a floating pane into a tiled one; it keeps running."""
        self.focus(pane)
        self._action("toggle-pane-embed-or-floating")

    def apply_swap_layout(self, name: str) -> None:
        """Rearrange the tab's tiled panes by a swap layout of its layout file; no pane starts or stops."""
        self._action("apply-tiled-swap-layout", name)

    def move_pane(self, direction: str) -> None:
        """Swap the focused pane with its neighbour in ``direction``."""
        self._action("move-pane", direction)

    def rename(self, pane: str, name: str) -> None:
        """A name the pane keeps: unlike `new-pane --name`, a program setting its terminal title does not replace it
        (Claude Code sets "Claude Code"; measured on Zellij 0.46.0)."""
        self._action("rename-pane", "--pane-id", pane, name)

    def detach(self) -> None:
        self._action("detach")

    def end(self) -> None:
        """End the whole session, as Quit in Alt+q's dialog does; from inside it, this process ends with it."""
        name = self.session or os.environ.get("ZELLIJ_SESSION_NAME")
        if not name:
            raise RuntimeError("cannot tell which Zellij session to end")
        if self.own_session and name != self.own_session:
            raise RuntimeError(f"this panel runs in the Zellij session {name}, not {self.own_session}; "
                               "it does not end other sessions")
        argv = [self.executable, *(["--config-dir", self.config_dir] if self.config_dir else []), "kill-session", name]
        try:
            result = self.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=ZELLIJ_TIMEOUT_SECONDS, check=False, env=self.environment)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"zellij kill-session did not answer within {ZELLIJ_TIMEOUT_SECONDS:g} s") from None
        if result.returncode != 0:
            raise RuntimeError(f"zellij kill-session failed: {(result.stderr or result.stdout or '').strip()}")


# Harnesses whose sessions open in a Reins pane. DeepSeek Harness has no terminal interface.
PANE_AGENTS = ("claude", "codex", "pi", "agy")
NEW_AGENTS = PANE_AGENTS
# Harnesses whose new sessions can take a Reins name at once (a Codex thread exists after its first message).
NAMED_AT_START = ("claude", "pi")
# How long a collaborator's agy pane is given to create its conversation (it starts on its brief at once).
AGY_LIST_SECONDS = 15.0
CODEX_LIST_SECONDS = 30.0  # How long a collaborator waits for Codex to list its new thread.
_PANE_NAME = re.compile(r"(claude|codex|pi|agy) ([0-9a-f]{8})\b")
CODEX_NEW_PREFIX = "codex new "
DSH_NO_WINDOW = ("DeepSeek Harness has no terminal window; open this session in its web or desktop app. "
                 "Messages and logs work here.")


def pane_name(handle: str, title: str, address: Optional[str] = None, agent: str = "claude") -> str:
    """How Reins names a session pane: harness, handle, address and title, so a person tells panes apart.

    attached_handles reads the harness and handle back from the start.
    """
    return (f"{agent} {handle}" + (f" @{address}" if address else "")
            + (f" {title}" if title and title != handle else ""))


# The pane beside the panel until the first session opens; launch.write_config names it.
HINT_TITLE = "Reins: select a session and press Enter"
LOG_PREFIX = "log "
SHELL_PREFIX = "shell "


PANEL_TITLE = "Reins"


_PANEL_COMMAND = re.compile(r"(^|\s)_tui-panel(\s|$)")  # not `_tui-panel-reopen`, which Alt+r runs


def _is_panel(pane: Mapping[str, object]) -> bool:
    return pane.get("title") == PANEL_TITLE or bool(_PANEL_COMMAND.search(str(pane.get("terminal_command") or "")))


def place(zellij: "Zellij", argv: Sequence[str], *, name: str, close_on_exit: bool, direction: str = BELOW,
          cwd: Optional[str] = None, keep_name: bool = False) -> str:
    """Open a pane and return its id. ``keep_name``: rename it so its program's own terminal title does not
    replace ``name``."""
    pane = _place(zellij, argv, name=name, close_on_exit=close_on_exit, direction=direction, cwd=cwd)
    if keep_name and pane.startswith("terminal_"):
        try:
            zellij.rename(pane, name)
        except (RuntimeError, OSError):
            pass  # Only the frame's label; the pane itself works.
    return pane


def _place(zellij: "Zellij", argv: Sequence[str], *, name: str, close_on_exit: bool, direction: str = BELOW,
           cwd: Optional[str] = None) -> str:
    """Open a pane on the right without narrowing the panel.

    The first one takes the hint pane's place; later ones split the newest
    session, log or shell pane, below it or to its right as asked, so the
    panel keeps its width. Only with nothing on the right does it split the panel.
    """
    panes = [pane for pane in zellij.panes() if not pane.get("is_plugin") and not pane.get("exited")]
    hint = next((pane for pane in panes if pane.get("title") == HINT_TITLE), None)
    if hint is not None:
        return zellij.new_pane(argv, name=name, close_on_exit=close_on_exit, cwd=cwd,
                               replace=f"terminal_{hint.get('id')}")
    # Every pane but the panel is on the right. Names cannot tell: a shell renames its pane. Only tiled panes in
    # the panel's own tab count, so a split never pulls the user to another tab or into a floating pane.
    panel_tab = next((pane.get("tab_id") for pane in panes if _is_panel(pane)), None)
    right = [pane for pane in panes if isinstance(pane.get("id"), int) and not _is_panel(pane)
             and not pane.get("is_floating") and pane.get("tab_id") == panel_tab]
    if right:
        zellij.focus(f"terminal_{max(int(pane['id']) for pane in right)}")
        return zellij.new_pane(argv, name=name, close_on_exit=close_on_exit, cwd=cwd, direction=direction)
    return zellij.new_pane(argv, name=name, close_on_exit=close_on_exit, cwd=cwd, direction="right")


def attached_handles(panes: Sequence[Mapping[str, object]]) -> dict[tuple[str, str], str]:
    """Session panes that are still open: (harness, handle) -> pane id.

    Zellij reports a pane's command only while it knows the foreground
    process, so the pane name Reins gave it is read first.
    """
    found = {}
    for pane in panes:
        if pane.get("is_plugin") or pane.get("exited"):
            continue
        name = pane.get("title")
        match = _PANE_NAME.match(name) if isinstance(name, str) else None
        key = (match.group(1), match.group(2)) if match else None
        command = pane.get("terminal_command")
        if key is None and isinstance(command, str):
            try:
                words = shlex.split(command)
            except ValueError:
                words = []
            if len(words) >= 3 and words[-2] == "attach" and Path(words[-3]).name.startswith("claude"):
                key = ("claude", words[-1])
        if key:
            found[key] = f"terminal_{pane.get('id')}"
    return found


def mark_attached(view: SessionView, handles: Mapping[tuple[str, str], str]) -> SessionView:
    """A session whose one process shows in a Reins pane. Pi and agy have no daemon: the process is the pane's."""
    if (view.agent, view.handle or "") not in handles:
        return view
    if view.state == sessions.RUNNING or (view.agent in ("pi", "agy") and view.state == sessions.FOREIGN):
        return replace(view, state=sessions.ATTACHED)
    return view


def read_record(path: Path) -> Optional[dict]:
    """A JSON object from a queue or task file, or None."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def tui_root(root: Path) -> Path:
    """The panel's own files; beside the default queue, like the address book."""
    root = root.expanduser().absolute()
    return root.parent / "tui" if root.name == "queue" else root / "tui"


CHOICES = ("pinned", "archived")
PROCESS_STATES = (sessions.ATTACHED, sessions.RUNNING, sessions.FOREIGN, sessions.HEADLESS)


def session_of(record: Optional[Mapping[str, object]]) -> Optional[str]:
    """The session a task or callback record belongs to."""
    for key in ("target", "origin"):
        value = (record or {}).get(key)
        if isinstance(value, dict) and isinstance(value.get("session"), str):
            return value["session"]
    return None


def _when(value: object) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


# `reins doctor` issues in the words the panel shows. Ones that only concern an agent session (which
# session a submission binds to, child agents) are left out: the panel is not one.
HEALTH_IGNORED = frozenset({"session_unbound", "submission_unverified", "child_agent_unspecified",
                            "child_agent_unavailable", "child_agent_desktop_only"})
HEALTH_TEXT = {
    "daemon_not_running": "the Reins daemon is not running: reins setup --enable --now",
    "daemon_unverified": "cannot tell whether the Reins daemon runs: reins doctor",
    "daemon_version_mismatch": "the daemon runs another Reins version: reins setup --now",
    "daemon_build_mismatch": "the daemon runs another Reins build: reins setup --now",
    "coordinator_restart_required": "the daemon needs a restart: reins setup --now",
    "callback_delivery_failed": "{count} callbacks failed to deliver (Tasks tab; c cancels one)",
    "callback_transport_blocked": "{count} callbacks are blocked (Tasks tab)",
    "callback_outcome_unknown": "{count} callbacks have an unknown outcome (Tasks tab)",
    "callback_record_unverified": "failed callback records cannot be read: reins doctor",
    "callback_queue_unverified": "failed callback records cannot be read: reins doctor",
    "skill_missing": "the Reins skill is not installed: reins setup",
    "queue_unwritable": "the Reins queue cannot be written",
    "tasks_unwritable": "Reins task records cannot be written",
    "task_records_unavailable": "Reins task records cannot be read",
    "backend_unavailable": "no task backend (systemd or screen) is available: reins doctor",
    "callback_agent_unavailable": "Claude Code was not found on PATH",
    "message_limit_reached": "messages between sessions stopped at a limit: reins message alerts",
    "platform_unsupported": "this system is not supported",
    "diagnostics_failed": "the check itself failed: reins doctor",
}


def health_lines(report: Mapping[str, object]) -> list[str]:
    lines = []
    for issue in report.get("issues") or ():
        if not isinstance(issue, dict) or issue.get("code") in HEALTH_IGNORED:
            continue
        code = str(issue.get("code"))
        text = HEALTH_TEXT.get(code, code.replace("_", " ") + ": reins doctor")
        lines.append(text.format(count=issue.get("count", "some")))
    return lines


class LiveBackend:
    def __init__(self, *, queue_root: Optional[Path] = None, environment: Optional[Mapping[str, str]] = None,
                 zellij: Optional[Zellij] = None, run: Run = subprocess.run,
                 now: Callable[[], float] = time.time,
                 codex_connect: Optional[codex_threads.Connect] = None,
                 codex_writers: Optional[Callable[[], Mapping[str, tuple[int, bool]]]] = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.sleep = sleep
        self.root = (queue_root or layout.queue_dir(None)).expanduser().absolute()
        given = dict(os.environ if environment is None else environment)
        self.environment = user_environment(given)
        self._pane_environment = given
        self.zellij = zellij
        self.run = run
        self.now = now
        self._codex_connect = codex_connect
        self._codex_writers = codex_writers
        self._agents: dict[str, str] = {}  # session -> harness, as last listed; tells which harness to ask.
        self._remembered: dict[str, SessionView] = {}  # Sessions only Reins knows of (DeepSeek Harness).
        # New Codex windows whose thread is not listed yet (Codex lists it after its first turn): pane, cwd, when.
        self._codex_new: list[tuple[str, str, float]] = []
        # New agy panes whose conversation agy creates on their first prompt: (pane, cwd, ids before it, since).
        self._agy_new: list[tuple[str, str, frozenset[str], float]] = []

    # Reads.

    def _callback_states(self) -> dict[str, str]:
        states = {}
        for state in CALLBACK_STATES:
            for path in (self.root / state).glob("*.json"):
                states[path.stem] = "delivered" if state == "running" else state
        for path in (self.root / "acks").glob("*.json"):
            states[path.stem] = "acked"
        return states

    def _task_records(self) -> list[dict]:
        cutoff = self.now() - RECENT_SECONDS
        records = []
        for path in layout.managed_tasks_root(self.root).glob("*/task.json"):
            record = read_record(path)
            if record is not None and (_when(record.get("submitted_at")) or 0) >= cutoff:
                records.append(record)
        records.sort(key=lambda record: -(_when(record.get("submitted_at")) or 0))
        return records[:MAX_ROWS]

    # Each harness's own list. One that fails (no daemon, unreadable files) leaves only its rows out.

    def _codex_others(self) -> Mapping[str, tuple[int, bool]]:
        return self._codex_writers() if self._codex_writers else codex_threads.writers()

    def _codex_views(self) -> list[SessionView]:
        try:
            return codex_threads.session_views(self.environment, connect=self._codex_connect,
                                               others=self._codex_others())
        except (codex_app_server.AppServerProtocolError, OSError, ValueError):
            return []

    def _pi_views(self) -> list[SessionView]:
        try:
            return pi_sessions.session_views(self.environment)
        except (OSError, ValueError):
            return []

    def _agy_views(self) -> list[SessionView]:
        try:
            return [self._headless_agy(view) for view in agy_sessions.session_views(self.environment)]
        except (OSError, ValueError):
            return []

    def _headless_agy(self, view: SessionView) -> SessionView:
        """An agy conversation the daemon is resuming headless: its presence lock is held, and so is the
        Reins target lock its delivery worker keeps for the whole turn. It must not be opened beside that."""
        if view.state != sessions.FOREIGN:
            return view
        from ...engine import delivery as delivery_engine
        request = {"agent": "agy", "target": {"kind": "session", "session": view.session}}
        if delivery_engine.target_lock_is_held(request):
            return replace(view, state=sessions.HEADLESS, host=sessions.HOST_HEADLESS)
        return view

    def _views(self, panes: Optional[list] = None) -> list[SessionView]:
        views = [*claude_agents.session_views(self.environment, run=self.run), *self._codex_views(), *self._pi_views(),
                 *self._agy_views()]
        if panes is None:
            panes = self.zellij.panes() if self.zellij else []
        handles = attached_handles(panes)
        return [mark_attached(view, handles) for view in views]

    def _name_panes(self, panes: list, views: Sequence[SessionView], addresses: Mapping[str, str]) -> None:
        """Give open session panes their current name: older panes, renamed sessions, new addresses."""
        if self.zellij is None:
            return
        titles = {f"terminal_{pane.get('id')}": pane.get("title") for pane in panes}
        handles = attached_handles(panes)
        self._adopt_codex_windows(panes, views, handles)
        for view in views:
            pane = handles.get((view.agent, view.handle or ""))
            if pane is None:
                continue
            name = pane_name(view.handle or "", view.name or short_id(view.session), addresses.get(view.session),
                             view.agent)
            if titles.get(pane) != name:
                try:
                    self.zellij.rename(pane, name)
                except (RuntimeError, OSError):
                    return

    def _adopt_codex_windows(self, panes: list, views: Sequence[SessionView],
                             handles: dict[tuple[str, str], str]) -> None:
        """Name a new Codex window after its thread once Codex lists that thread, so Enter finds the pane."""
        open_panes = {f"terminal_{pane.get('id')}" for pane in panes if not pane.get("exited")}
        waiting = [(pane, cwd, since) for pane, cwd, since in self._codex_new
                   if pane in open_panes and self.now() - since < 3600]
        taken = {handle for agent, handle in handles if agent == "codex"}
        for pane, cwd, since in list(waiting):
            found = sorted((view for view in views if view.agent == "codex" and view.state == sessions.RUNNING
                            and view.cwd == cwd and (view.started_at or 0) >= since - 5
                            and view.handle not in taken), key=lambda view: view.started_at or 0)
            if found:
                taken.add(found[0].handle or "")
                handles[("codex", found[0].handle or "")] = pane
                waiting.remove((pane, cwd, since))
        self._codex_new = waiting

    def _adopt_agy_windows(self, panes: list) -> None:
        """Name a new agy window after its conversation once agy has created it, on the first prompt.

        Until then the pane keeps its `agy new` name; a conversation is taken only as the one new conversation
        in that directory held open by a process, never guessed from the directory and time alone.
        """
        if not self._agy_new:
            return
        open_panes = {f"terminal_{pane.get('id')}": pane for pane in panes if not pane.get("exited")}
        handles = attached_handles(panes)
        waiting = []
        for pane, cwd, before, since in self._agy_new:
            if pane not in open_panes or self.now() - since >= 3600:
                continue
            conversation = agy_sessions.started_in(cwd, set(before), self.environment)
            if conversation is None or ("agy", agy_sessions.handle(conversation)) in handles:
                waiting.append((pane, cwd, before, since))
                continue
            name = pane_name(agy_sessions.handle(conversation), short_id(conversation), agent="agy")
            if self.zellij is not None:
                try:
                    self.zellij.rename(pane, name)
                except (RuntimeError, OSError):
                    waiting.append((pane, cwd, before, since))
                    continue
            open_panes[pane]["title"] = name  # So this same pass already shows it as here; _name_panes completes it.
        self._agy_new = waiting

    def sessions(self) -> Sequence[SessionRow]:
        panes = self.zellij.panes() if self.zellij else []
        self._adopt_agy_windows(panes)
        live_views = self._views(panes)
        views = {view.session: view for view in live_views}
        tasks = self._task_records()
        running: dict[str, int] = {}
        for record in tasks:
            session = session_of(record)
            if session and record.get("state") in RUNNING_TASK_STATES:
                running[session] = running.get(session, 0) + 1
        pending: dict[str, int] = {}
        for path in (self.root / "pending").glob("*.json"):
            record = read_record(path)
            session = session_of(record) if record else None
            if session and not layout.ack_path(self.root, path.stem).exists():
                pending[session] = pending.get(session, 0) + 1
        goals = {row.session: row.task for row in self.goals() if not row.state.startswith(FINISHED_GOAL_PREFIXES)}
        book = link.sessions(self.root)
        addresses = {str(record.get("session")): str(record["name"]) for record in book
                     if isinstance(record.get("name"), str)}
        self._name_panes(panes, live_views, addresses)
        choices = self._choices()
        latest: dict[str, float] = {}
        for record in tasks:
            session = session_of(record)
            if session:
                latest[session] = max(latest.get(session, 0.0), _when(record.get("submitted_at")) or 0.0)
        # Closed sessions are listed when Reins has worked with them lately: that is how a user finds them again.
        # DeepSeek Harness has no session list Reins can read, so its named sessions are listed too.
        for record in tasks:
            session = session_of(record)
            agent = record.get("agent") if isinstance(record.get("agent"), str) else "claude"
            if session and session not in views:
                views[session] = self._closed_view(agent, session, record.get("cwd"), _when(record.get("submitted_at")))
        for record in book:
            session = record.get("session")
            if record.get("harness") == "dsh" and isinstance(session, str) and session not in views:
                views[session] = self._closed_view("dsh", session, record.get("cwd"), _when(record.get("registered_at")))
        self._agents = {view.session: view.agent for view in views.values()}
        self._remembered = {view.session: view for view in views.values() if view.agent == "dsh"}
        def archived(view: SessionView) -> bool:
            # Archived until it does something again: runs, gets a task, or has a result waiting.
            since = choices["archived"].get(view.session)
            return (since is not None and view.state not in PROCESS_STATES and not pending.get(view.session)
                    and latest.get(view.session, 0.0) <= since)

        return [SessionRow(session=view.session, agent=view.agent, state=view.state,
                           title=view.name or short_id(view.session), cwd=view.cwd, activity=view.activity, host=view.host,
                           tasks_running=running.get(view.session, 0), callbacks_pending=pending.get(view.session, 0),
                           goal=goals.get(view.session), started_at=view.started_at,
                           address=addresses.get(view.session), pinned=view.session in choices["pinned"],
                           archived=archived(view))
                for view in views.values()]

    def _choices_path(self) -> Path:
        return tui_root(self.root) / "sessions.json"

    def _choices(self) -> dict[str, dict[str, float]]:
        """Which sessions the user pinned or archived, and when."""
        record = read_record(self._choices_path()) or {}
        result: dict[str, dict[str, float]] = {}
        for key in CHOICES:
            value = record.get(key)
            result[key] = ({str(session): float(when) for session, when in value.items()
                            if isinstance(when, (int, float)) and not isinstance(when, bool)}
                           if isinstance(value, dict) else {})
        return result

    def _choose(self, key: str, session: str, on: bool) -> None:
        choices = self._choices()
        if on:
            choices[key][session] = self.now()
        else:
            choices[key].pop(session, None)
        path = self._choices_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        records.write_private_text(path, json.dumps(choices, indent=2, sort_keys=True) + "\n")

    @staticmethod
    def _closed_view(agent: str, session: str, cwd: object, when: Optional[float]) -> SessionView:
        handle = (codex_threads.handle(session) if agent == "codex" else pi_sessions.handle(session) if agent == "pi"
                  else agy_sessions.handle(session) if agent == "agy" else None)
        return SessionView(agent=agent, session=session, state=sessions.CLOSED, handle=handle,
                           cwd=cwd if isinstance(cwd, str) else None, started_at=when)

    def _agent_of(self, session: str) -> Optional[str]:
        """The harness as last listed; a path is a Pi session file; None when this panel has not seen it."""
        return self._agents.get(session) or ("pi" if os.path.isabs(session) else None)

    def _codex_view(self, session: str) -> Optional[SessionView]:
        try:
            return codex_threads.session_view(session, self.environment, connect=self._codex_connect,
                                              others=self._codex_others())
        except (codex_app_server.AppServerProtocolError, OSError, ValueError):
            return None

    def _view(self, session: str) -> Optional[SessionView]:
        """The session's state now, asked of its own harness. A session this panel has not listed yet (from
        `reins tui <id>`) is looked up in Claude Code, then in Codex."""
        agent = self._agent_of(session)
        if agent == "dsh":
            return self._remembered.get(session)
        panes = self.zellij.panes() if self.zellij else []
        handles = attached_handles(panes)
        if agent == "codex":
            view = self._codex_view(session) or self._closed_view("codex", session, None, None)
            return mark_attached(view, handles)
        if agent == "pi":
            view = pi_sessions.session_view(session, self.environment)
            return mark_attached(view, handles) if view is not None else None
        if agent == "agy":
            view = agy_sessions.session_view(session, self.environment)
            return mark_attached(self._headless_agy(view), handles) if view is not None else None
        views = claude_agents.session_views(self.environment, run=self.run)
        view = next((view for view in views if view.session == session), None)
        if view is None:
            view = claude_agents.session_view(session, self.environment, views=(), run=self.run)
        if view is None and agent is None:
            view = self._codex_view(session)
        return mark_attached(view, handles) if view is not None else None

    def tasks(self) -> Sequence[TaskRow]:
        callbacks = self._callback_states()
        rows = []
        for record in self._task_records():
            task = str(record.get("id", ""))
            callback = next((key for key in callbacks if key.startswith(f"{task}-c")), None)
            rows.append(TaskRow(task=task, title=str(record.get("task") or record.get("command") or ""),
                                state=str(record.get("state") or "unknown"), session=session_of(record),
                                agent=record.get("agent") if isinstance(record.get("agent"), str) else None,
                                started_at=_when(record.get("started_at") or record.get("submitted_at")),
                                ended_at=_when(record.get("completed_at")), callback=callback,
                                callback_state=callbacks.get(callback) if callback else None))
        return rows

    def goals(self) -> Sequence[GoalRow]:
        rows = []
        for path in sorted((self.root / "goals").glob("*.json")):
            record = read_record(path)
            if record is None or not session_of(record):
                continue
            done, total = record.get("plan_completed_items"), record.get("plan_total_items")
            rows.append(GoalRow(goal=str(record.get("id", path.stem)), session=str(session_of(record)),
                                task=str(record.get("task") or ""), state=str(record.get("state") or "unknown"),
                                progress=f"{done}/{total} steps" if isinstance(done, int) and isinstance(total, int) else None))
        return rows

    def messages(self) -> Sequence[MessageRow]:
        rows = []
        for path in (layout.link_root(self.root) / "messages").glob("*.json"):
            record = read_record(path)
            if record is None:
                continue
            sender, recipient = record.get("from"), record.get("to")
            local = record.get("local") if isinstance(record.get("local"), dict) else {}
            rows.append(MessageRow(
                message=str(record.get("id", path.stem)),
                sender=str(sender.get("name") or sender.get("session") or "?") if isinstance(sender, dict) else "?",
                recipient=str(recipient.get("name") or recipient.get("session") or "?") if isinstance(recipient, dict) else "?",
                subject=str(record.get("subject") or ""), sent_at=_when(record.get("created_at")) or 0.0,
                read=bool(local.get("read_at") or local.get("acked_at"))))
        rows.sort(key=lambda row: -row.sent_at)
        return rows[:MAX_ROWS]

    # Actions.

    def _pane(self, argv: Sequence[str]) -> list[str]:
        return pane_command(argv, self._pane_environment)

    def _settings(self) -> str:
        return claude_background.settings_argument(skill_install.agent_command_names())

    def _attach(self, view_title: str, handle: str, direction: str = BELOW,
                address: Optional[str] = None) -> ActionResult:
        return self._open_pane("claude", handle, view_title,
                               lambda: claude_background.attach_command(handle, self.environment), direction, address)

    def _open_pane(self, agent: str, handle: str, title: str, argv: Callable[[], Sequence[str]],
                   direction: str = BELOW, address: Optional[str] = None, cwd: Optional[str] = None) -> ActionResult:
        """Move to the session's pane, or open one running ``argv()``."""
        if self.zellij is None:
            return ActionResult(False, "not inside the Reins Zellij session")
        handles = attached_handles(self.zellij.panes())
        if (agent, handle) in handles:
            self.zellij.focus(handles[(agent, handle)])
            return ActionResult(True, f"{title} is already open; moved to its pane")
        place(self.zellij, self._pane(argv()), name=pane_name(handle, title, address, agent),
              close_on_exit=True, direction=direction, cwd=cwd if cwd and os.path.isdir(cwd) else None,
              keep_name=True)
        return ActionResult(True, f"opened {title}")

    @staticmethod
    def _elsewhere(view: SessionView, title: str) -> Optional[ActionResult]:
        """Refuse a session whose one process is somewhere Reins does not host (invariant 1)."""
        if view.state == sessions.FOREIGN:
            where = {sessions.HOST_DESKTOP: "the Claude desktop app"}.get(view.host or "", "another terminal")
            return ActionResult(False, f"{title} is open in {where}" + (f" (pid {view.pid})" if view.pid else "")
                                + "; close it there to open it here. Messages and logs still work.")
        if view.state == sessions.HEADLESS:
            return ActionResult(False, f"{title} is finishing a headless Reins turn; open it again when that ends")
        return None

    def _ensure_codex_daemon(self) -> Optional[ActionResult]:
        """Codex windows opened here go through its shared daemon; start the daemon when it is not running."""
        if codex_threads.executable(self.environment) is None:
            return ActionResult(False, "Codex was not found on PATH (or set REINS_CODEX_BIN)")
        if self._codex_connect is not None or codex_threads.available(self.environment):
            return None
        try:
            result = self.run(codex_threads.start_daemon_command(self.environment), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
                              check=False, env=self.environment)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ActionResult(False, f"could not start the Codex app-server daemon: {exc}")
        if result.returncode != 0 or not codex_threads.available(self.environment):
            detail = ((result.stderr or result.stdout or "").strip().splitlines() or ["no socket appeared"])[-1]
            return ActionResult(False, f"could not start the Codex app-server daemon: {detail}")
        return None

    def open_session(self, session: str, direction: str = BELOW) -> ActionResult:
        view = self._view(session)
        if view is None:
            return ActionResult(False, f"no session {short_id(session)} on this machine")
        title = view.name or short_id(session)
        if view.agent == "dsh":
            return ActionResult(False, DSH_NO_WINDOW)
        refused = self._elsewhere(view, title)
        if refused is not None:
            return refused
        address = self._session_name(session)
        if view.agent == "codex":
            refused = self._ensure_codex_daemon()
            if refused is not None:
                return refused
            return self._open_pane("codex", view.handle or codex_threads.handle(session), title,
                                   lambda: codex_threads.resume_command(session, self.environment), direction,
                                   address, view.cwd)
        if view.agent == "pi":
            if not view.cwd or not os.path.isdir(view.cwd):
                return ActionResult(False, f"{title} belongs to {view.cwd or 'an unknown directory'}, which is not "
                                           "here any more; Pi can only open it from there")
            return self._open_pane("pi", view.handle or pi_sessions.handle(session), title,
                                   lambda: pi_sessions.open_command(session, view.cwd or ""), direction, address,
                                   view.cwd)
        if view.agent == "agy":
            if agy_session.discover_executable(self.environment) is None:
                return ActionResult(False, "Antigravity (agy) was not found on PATH (or set REINS_AGY_BIN)")
            if not agy_session.conversation_exists(session, self.environment):
                # agy would start a new conversation in its place.
                return ActionResult(False, f"Antigravity has no conversation {short_id(session)} any more")
            return self._open_pane("agy", view.handle or agy_sessions.handle(session), title,
                                   lambda: agy_sessions.open_command(session, self.environment), direction, address,
                                   view.cwd)
        try:
            if view.state == sessions.CLOSED:
                if not view.cwd:
                    return ActionResult(False, f"cannot tell which directory {title} belongs to")
                handle = claude_background.resume(session, view.cwd, self.environment, stopped=False,
                                                  settings=self._settings(), run=self.run)
            else:
                handle = view.handle or session[:8]
            return self._attach(title, handle, direction, address=address)
        except claude_background.StartedCopy:
            return ActionResult(False, f"{title} was opened somewhere else just now; try again when it closes")
        except (claude_background.BackgroundError, RuntimeError, OSError) as exc:
            return ActionResult(False, str(exc))

    def new_agents(self) -> Sequence[str]:
        """The harnesses a new session can start in: the ones installed here."""
        found = []
        if claude_session.discover_executable(self.environment) is not None:
            found.append("claude")
        if codex_threads.executable(self.environment):
            found.append("codex")
        if self.environment.get("REINS_PI_BIN") or shutil.which("pi", path=self.environment.get("PATH")):
            found.append("pi")
        if agy_session.discover_executable(self.environment) is not None:
            found.append("agy")
        return found

    def new_session(self, agent: str, cwd: str, name: str = "") -> ActionResult:
        directory = os.path.abspath(os.path.expanduser(cwd))
        if not os.path.isdir(directory):
            return ActionResult(False, f"no directory {cwd}")
        if agent not in NEW_AGENTS:
            return ActionResult(False, DSH_NO_WINDOW if agent == "dsh" else f"Reins cannot start {agent} sessions")
        # A Codex thread exists only after its first message, so a new Codex session is named from its menu later.
        name = name.strip() if agent in NAMED_AT_START else ""
        if name:
            try:
                validate_name(name)
                holder = link.load_session(self.root, name)
            except Refused as exc:
                return ActionResult(False, str(exc))
            if holder is not None:
                return ActionResult(False, f"@{name} already names another session; choose another name")
        try:
            if agent == "codex":
                return self._new_codex(directory)
            if agent == "agy":
                return self._new_agy(directory, f"new in {os.path.basename(directory) or directory}")
            if agent == "pi":
                session = pi_channel.create_session(directory)
                if name:
                    named = self._register(name, "pi", session, directory)
                    if not named.ok:
                        return ActionResult(False, f"could not name the new Pi session {name}: {named.message}")
                return self._open_pane("pi", pi_sessions.handle(session),
                                       f"new in {os.path.basename(directory) or directory}",
                                       lambda: pi_sessions.open_command(session, directory), cwd=directory,
                                       address=name or None)
            if not name:
                handle = claude_background.start(directory, self.environment, settings=self._settings(),
                                                 run=self.run)
                return self._attach(handle, handle)
            before = self._claude_ids()
            if before is None:
                return ActionResult(False, "could not list Claude sessions, so the new one could not be told apart "
                                           "to name it; try again, or start it without a name")
            handle = claude_background.start(directory, self.environment, settings=self._settings(), run=self.run)
            full = self._claude_full_id(handle, before)
            named = (self._register(name, "claude", full, directory) if full is not None else
                     ActionResult(False, "`claude agents` does not list it yet"))
            opened = self._attach(handle, handle, address=name if named.ok else None)
            if named.ok or not opened.ok:
                return opened
            return ActionResult(False, f"{opened.message}, but it has no name: {named.message}; "
                                       f"name it @{name} from its menu")
        except (claude_background.BackgroundError, RuntimeError, OSError, ValueError) as exc:
            return ActionResult(False, str(exc))

    def _claude_ids(self) -> Optional[set[str]]:
        """The ids `claude agents` lists now; None when it could not list them."""
        entries = claude_agents.list_agents(self.environment, run=self.run)
        return None if entries is None else {str(entry["sessionId"]) for entry in entries}

    def _claude_full_id(self, handle: str, before: set[str]) -> Optional[str]:
        """The full id of the background session ``handle`` names, once `claude agents` lists it: the one id
        starting with ``handle`` that was not listed before it started, so an older session with the same first
        eight characters is never taken for it."""
        def found() -> Optional[str]:
            new = [session for session in self._claude_ids() or () if session.startswith(handle)
                   and session not in before]
            return new[0] if len(new) == 1 else None
        return self._wait_for(found)

    def _new_codex(self, directory: str) -> ActionResult:
        refused = self._ensure_codex_daemon()
        if refused is not None:
            return refused
        if self.zellij is None:
            return ActionResult(False, "not inside the Reins Zellij session")
        since = self.now()
        pane = place(self.zellij, self._pane(codex_threads.new_command(directory, self.environment)),
                     name=f"{CODEX_NEW_PREFIX}{os.path.basename(directory) or directory}", close_on_exit=True,
                     cwd=directory, keep_name=True)
        if pane.startswith("terminal_"):
            self._codex_new.append((pane, directory, since))
        return ActionResult(True, f"opened a new Codex session in {directory}; it is listed after its first message")

    def fork_session(self, session: str, direction: str = BELOW) -> ActionResult:
        view = self._view(session)
        if view is None:
            return ActionResult(False, f"no session {short_id(session)} on this machine")
        title = view.name or short_id(session)
        if view.agent == "codex":
            return self._fork_codex(session, title, direction)
        if view.agent != "claude":
            return ActionResult(False, f"Reins cannot fork {view.agent} sessions")
        if not view.cwd:
            return ActionResult(False, f"cannot tell which directory {title} belongs to")
        name = f"{title} (fork)"
        try:
            handle = claude_background.fork(session, view.cwd, self.environment, name=name if view.name else None,
                                            settings=self._settings(), run=self.run)
            return self._attach(name, handle, direction)
        except (claude_background.BackgroundError, RuntimeError, OSError) as exc:
            return ActionResult(False, str(exc))

    def _fork_codex(self, session: str, title: str, direction: str) -> ActionResult:
        refused = self._ensure_codex_daemon()
        if refused is not None:
            return refused
        try:
            with codex_threads.Daemon(self.environment, connect=self._codex_connect) as daemon:
                fork = daemon.fork(session)
        except (codex_app_server.AppServerProtocolError, OSError, ValueError) as exc:
            return ActionResult(False, f"Codex could not fork {title}: {exc}")
        name = f"{title} (fork)"
        return self._open_pane("codex", codex_threads.handle(fork), name,
                               lambda: codex_threads.resume_command(fork, self.environment), direction)

    def start_collaborator(self, session: str, agent: str, cwd: str, task: str = "",
                           direction: str = BELOW) -> ActionResult:
        directory = os.path.abspath(os.path.expanduser(cwd))
        if not os.path.isdir(directory):
            return ActionResult(False, f"no directory {cwd}")
        if agent not in NEW_AGENTS:
            return ActionResult(False, DSH_NO_WINDOW if agent == "dsh" else f"Reins cannot start {agent} sessions")
        view = self._view(session)
        if view is None:
            return ActionResult(False, f"no session {short_id(session)} on this machine")
        title = view.name or short_id(session)
        taken = {str(record.get("name")) for record in link.sessions(self.root)}
        partner = self._session_name(session)
        if partner is None:
            partner = free_name(suggested_name(title, session), taken)
            named = self.name_session(session, partner)
            if not named.ok:
                return ActionResult(False, f"could not name {title}: {named.message}")
            taken.add(partner)
        mate = free_name(partner, taken, keep=f"-{agent}")
        # The agents run `reins send` with their own environment: name the queue unless it is the default one.
        queue = None if self.root == (Path.home() / ".reins" / "queue").absolute() else str(self.root)
        prompt = collaborator_prompt(mate, partner, view.agent, view.cwd, task, queue)
        try:
            started = self._start_mate(agent, directory, mate, f"with @{partner}", prompt, direction)
        except (claude_background.BackgroundError, RuntimeError, OSError, ValueError) as exc:
            return ActionResult(False, str(exc))
        if not started.ok:
            return started
        told = self._reins("send", partner, "--message", collaborator_notice(mate, agent, directory, task, queue),
                           "--wake", "never")
        if not told.ok:
            return ActionResult(False, f"started @{mate}, but could not tell @{partner}: {told.message}")
        if view.agent in ("dsh", "agy"):
            # The notice starts nothing: these sessions read messages while their `reins wait` runs.
            return ActionResult(True, f"started @{mate}; @{partner} reads how to reach it while its `reins wait` runs")
        return ActionResult(True, f"started @{mate} to work with @{partner}, and told @{partner} how to reach it")

    def _register(self, name: str, agent: str, session: str, cwd: str) -> ActionResult:
        return self._reins("session", "register", name, "--agent", agent, "--session", session, "--cwd", cwd)

    def _start_mate(self, agent: str, directory: str, mate: str, title: str, prompt: str,
                    direction: str) -> ActionResult:
        """Start the collaborator on ``prompt``, name it ``mate`` and open its pane, titled ``title``."""
        if self.zellij is None:
            return ActionResult(False, "not inside the Reins Zellij session")
        if agent == "pi":
            session = pi_channel.create_session(directory)
            named = self._register(mate, "pi", session, directory)
            if not named.ok:
                return ActionResult(False, f"could not name the new Pi session {mate}: {named.message}")
            return self._open_pane("pi", pi_sessions.handle(session), title,
                                   lambda: [*pi_sessions.open_command(session, directory), "--", prompt], direction,
                                   mate, directory)
        if agent == "codex":
            return self._start_codex_mate(directory, mate, prompt, direction)
        if agent == "agy":
            return self._start_agy_mate(directory, mate, title, prompt, direction)
        before = self._claude_ids()
        if before is None:
            return ActionResult(False, "could not list Claude sessions, so the new one could not be told apart "
                                       "to name it; try again")
        handle = claude_background.start(directory, self.environment, settings=self._settings(), prompt=prompt,
                                         run=self.run)
        full = self._claude_full_id(handle, before)
        if full is None:
            return ActionResult(False, f"Claude started {handle}, but `claude agents` does not list it; "
                                       f"name it @{mate} from its menu so its partner can reach it")
        named = self._register(mate, "claude", full, directory)
        if not named.ok:
            return ActionResult(False, f"could not name the new Claude session {mate}: {named.message}")
        return self._attach(title, handle, direction, address=mate)

    def _start_codex_mate(self, directory: str, mate: str, prompt: str, direction: str) -> ActionResult:
        refused = self._ensure_codex_daemon()
        if refused is not None:
            return refused
        assert self.zellij is not None
        since = self.now()
        # The threads there are now, so none of them is ever taken for the new one; without that list, no window.
        try:
            before = codex_threads.thread_ids(self.environment, connect=self._codex_connect)
        except (codex_app_server.AppServerProtocolError, OSError, ValueError) as exc:
            return ActionResult(False, f"could not list Codex threads, so the new one could not be told apart: {exc}")
        pane = place(self.zellij, self._pane([*codex_threads.new_command(directory, self.environment), prompt]),
                     name=f"{CODEX_NEW_PREFIX}{mate}", close_on_exit=True, direction=direction, cwd=directory,
                     keep_name=True)
        if pane.startswith("terminal_"):
            self._codex_new.append((pane, directory, since))
        # Its first message names it (`You are @mate,`, a name no other session holds), so the thread is found
        # by that and never guessed from the directory and time alone.
        opening = prompt.split(",", 1)[0] + ","

        def listed() -> Optional[str]:
            try:
                return codex_threads.started_on(opening, directory, since - 5, before, self.environment,
                                                connect=self._codex_connect)
            except (codex_app_server.AppServerProtocolError, OSError, ValueError):
                return None
        thread = self._wait_for(listed, CODEX_LIST_SECONDS)
        if thread is None:
            return ActionResult(False, f"Codex has not listed the new thread yet; when it shows up, name it @{mate} "
                                       "from its menu so its partner can reach it")
        named = self._register(mate, "codex", thread, directory)
        if not named.ok:
            return ActionResult(False, f"could not name the new Codex thread {mate}: {named.message}")
        return ActionResult(True, f"opened @{mate}")

    def _new_agy(self, directory: str, title: str) -> ActionResult:
        """A new agy conversation in a pane. agy creates the conversation on its first prompt, so the pane is
        named after it later (_adopt_agy_windows), and the session is named from its menu, as for Codex."""
        if self.zellij is None:
            return ActionResult(False, "not inside the Reins Zellij session")
        if agy_session.discover_executable(self.environment) is None:
            return ActionResult(False, "Antigravity (agy) was not found on PATH (or set REINS_AGY_BIN)")
        before = frozenset(agy_sessions.conversation_ids(self.environment))
        since = self.now()
        pane = place(self.zellij, self._pane(agy_sessions.new_command(self.environment)),
                     name=f"agy new {os.path.basename(directory) or directory}", close_on_exit=True, cwd=directory,
                     keep_name=True)
        if pane.startswith("terminal_"):
            self._agy_new.append((pane, directory, before, since))
        return ActionResult(True, f"opened {title}")

    def _start_agy_mate(self, directory: str, mate: str, title: str, prompt: str, direction: str) -> ActionResult:
        """A collaborator in agy, started on its brief, so agy creates its conversation at once; it is found
        as the one new conversation in this directory held open by a process, then named ``mate``."""
        if self.zellij is None:
            return ActionResult(False, "not inside the Reins Zellij session")
        if agy_session.discover_executable(self.environment) is None:
            return ActionResult(False, "Antigravity (agy) was not found on PATH (or set REINS_AGY_BIN)")
        before = agy_sessions.conversation_ids(self.environment)
        pane = place(self.zellij, self._pane(agy_sessions.new_command(self.environment, prompt)),
                     name=f"agy new {mate}", close_on_exit=True, direction=direction, cwd=directory, keep_name=True)
        conversation = self._wait_for(lambda: agy_sessions.started_in(directory, before, self.environment),
                                      AGY_LIST_SECONDS)
        if conversation is None:
            return ActionResult(False, f"Antigravity has not shown the new conversation yet; when it does, name it "
                                       f"@{mate} from its menu so its partner can reach it")
        named = self._register(mate, "agy", conversation, directory)
        if not named.ok:
            return ActionResult(False, f"could not name the new Antigravity conversation {mate}: {named.message}")
        if pane.startswith("terminal_"):
            try:  # The panel finds a session's pane by this name; without it Enter would call the pane foreign.
                self.zellij.rename(pane, pane_name(agy_sessions.handle(conversation), title, mate, "agy"))
            except (RuntimeError, OSError):
                pass
        return ActionResult(True, f"opened @{mate}")

    def _wait_for(self, found: Callable[[], Optional[str]], seconds: float = 10.0) -> Optional[str]:
        deadline = self.now() + seconds
        while True:
            value = found()
            if value is not None or self.now() >= deadline:
                return value
            self.sleep(1.0)

    def pin_session(self, session: str, pinned: bool) -> ActionResult:
        try:
            self._choose("pinned", session, pinned)
        except OSError as exc:
            return ActionResult(False, f"cannot save the pin: {exc}")
        return ActionResult(True, ("pinned " if pinned else "unpinned ") + short_id(session))

    def archive_session(self, session: str, archived: bool) -> ActionResult:
        view = self._view(session) if archived else None
        title = (view.name if view else None) or short_id(session)
        if view is not None and view.state in (sessions.FOREIGN, sessions.HEADLESS):
            return ActionResult(False, f"{title} is open elsewhere; close it there first")
        stopped = ""
        if (view is not None and view.agent == "claude" and view.state in (sessions.ATTACHED, sessions.RUNNING)
                and view.handle):
            try:
                claude_background.stop(view.handle, self.environment, run=self.run)
            except claude_background.BackgroundError as exc:
                return ActionResult(False, str(exc))
            stopped = "stopped and "
        try:
            self._choose("archived", session, archived)
        except OSError as exc:
            return ActionResult(False, f"cannot save the archive: {exc}")
        if not archived:
            return ActionResult(True, f"{title} is back in the list")
        return ActionResult(True, f"{stopped}archived {title}; it comes back when it gets work, or press a to see it")

    def stop_session(self, session: str) -> ActionResult:
        agent = self._agent_of(session)
        if agent == "codex":
            return ActionResult(False, "Codex unloads an idle thread by itself; close its pane with Alt+x")
        if agent == "pi":
            return ActionResult(False, "a Pi session runs in its pane; close the pane (Alt+x) to stop it")
        view = self._view(session)
        if view is None or view.state not in (sessions.ATTACHED, sessions.RUNNING) or not view.handle:
            return ActionResult(False, "only a background session that is running can be stopped here")
        try:
            claude_background.stop(view.handle, self.environment, run=self.run)
        except claude_background.BackgroundError as exc:
            return ActionResult(False, str(exc))
        return ActionResult(True, f"stopped {view.name or short_id(session)}; its conversation stays and opens again with Enter")

    def show_log(self, task: str, direction: str = BELOW) -> ActionResult:
        if self.zellij is None:
            return ActionResult(False, "not inside the Reins Zellij session")
        try:
            place(self.zellij, self._pane(worker_command("log", task)), name=f"{LOG_PREFIX}{task[:8]}", close_on_exit=False,
                  direction=direction)
        except (RuntimeError, OSError) as exc:
            return ActionResult(False, str(exc))
        return ActionResult(True, f"following task {task[:8]}")

    def open_terminal(self, cwd: str, direction: str = BELOW) -> ActionResult:
        if self.zellij is None:
            return ActionResult(False, "not inside the Reins Zellij session")
        directory = os.path.expanduser(cwd) if cwd and os.path.isdir(os.path.expanduser(cwd)) else os.path.expanduser("~")
        shell = default_shell(self.environment)
        try:
            place(self.zellij, self._pane([shell]), name=f"{SHELL_PREFIX}{os.path.basename(directory.rstrip(os.sep)) or directory}",
                  close_on_exit=True, direction=direction, cwd=directory)
        except (RuntimeError, OSError) as exc:
            return ActionResult(False, str(exc))
        return ActionResult(True, f"opened a terminal in {directory}")

    def _reins(self, *arguments: str) -> ActionResult:
        if self.root != layout.queue_dir(None).expanduser().absolute():
            arguments = (*arguments, "--queue-dir", str(self.root))
        try:
            result = self.run(worker_command(*arguments), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60, check=False,
                              env=self.environment)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ActionResult(False, f"reins {arguments[0]} failed: {exc}")
        output = ((result.stdout or "") + (result.stderr or "")).strip().splitlines()
        return ActionResult(result.returncode == 0, output[-1] if output else f"reins {arguments[0]} done")

    def _session_name(self, session: str) -> Optional[str]:
        for record in link.sessions(self.root):
            if record.get("session") == session and isinstance(record.get("name"), str):
                return record["name"]
        return None

    def send_message(self, recipient: str, text: str) -> ActionResult:
        name = (self._session_name(recipient)
                if sessions.FULL_ID.fullmatch(recipient) or recipient in self._agents or os.path.isabs(recipient)
                else recipient)
        if name is None:
            return ActionResult(False, "this session has no Reins name yet: "
                                       "right-click it (or press o) and name it first")
        return self._reins("send", name, "--message", text)

    def cancel_callback(self, callback: str) -> ActionResult:
        return self._reins("cancel", "--id", callback)

    def name_session(self, session: str, name: str, rename: bool = False) -> ActionResult:
        view = self._view(session)
        if view is None:
            return ActionResult(False, f"no session {short_id(session)} on this machine")
        arguments = ["session", "register", name, "--agent", view.agent, "--session", session]
        if view.cwd:
            arguments += ["--cwd", view.cwd]
        if rename:
            # --replace drops this session's old name, but would also take the name from another session.
            try:
                holder = link.load_session(self.root, name)
            except Refused as exc:
                return ActionResult(False, str(exc))
            if holder is not None and holder.get("session") != session:
                return ActionResult(False, f"{name} already names another session; choose another name")
            arguments.append("--replace")
        return self._reins(*arguments)

    def leave(self, end: bool) -> ActionResult:
        if self.zellij is None:
            return ActionResult(False, "not inside the Reins Zellij session")
        try:
            self.zellij.end() if end else self.zellij.detach()
        except (RuntimeError, OSError) as exc:
            return ActionResult(False, str(exc))
        return ActionResult(True, "ended the Reins screen" if end else "left the Reins screen")

    def health(self) -> Sequence[str]:
        """`reins doctor` for this queue, read in process, plus what the TUI itself needs."""
        import argparse

        from ...install import diagnostics

        args = argparse.Namespace(mode="doctor", queue_dir=str(self.root), backend="auto", operation="run",
                                  agent="claude", agent_worker=None, callback_mode=None, app_server_socket=None,
                                  session=None)
        try:
            lines = health_lines(diagnostics.inspect(args))
        except (Exception, SystemExit) as exc:
            lines = [f"the check itself failed ({type(exc).__name__}): reins doctor"]
        if not claude_session.hook_installed(claude_session.config_dir(self.environment) / "settings.json"):
            lines.append("the Claude Code hook is not installed, so sessions get no callbacks: reins setup")
        return lines
