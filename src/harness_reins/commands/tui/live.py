"""The panel's live backend: Claude's own session list, the Reins queue, and the Zellij session around it.

Nothing here keeps state between calls (design invariant 2): each read asks
Claude, the queue files and Zellij again. The only thing the panel stores is
which sessions the user pinned or archived, in one small file of its own.
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
from ...core.messages import Refused
from ...core.sessions import SessionView
from ...engine import claude_background
from ...harness.claude import agents as claude_agents
from ...harness.claude import session as claude_session
from ...install import skills as skill_install
from ...runtime import worker_command
from ...store import layout, link, records
from .backend import BELOW, ActionResult, GoalRow, MessageRow, SessionRow, TaskRow

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
                 run: Run = subprocess.run, environment: Optional[Mapping[str, str]] = None) -> None:
        self.executable = executable
        self.session = session
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
        self._action("focus-pane-id", pane)

    def detach(self) -> None:
        self._action("detach")

    def end(self) -> None:
        """End the whole session, as Alt+q does; from inside it, this process ends with it."""
        name = self.session or os.environ.get("ZELLIJ_SESSION_NAME")
        if not name:
            raise RuntimeError("cannot tell which Zellij session to end")
        argv = [self.executable, *(["--config-dir", self.config_dir] if self.config_dir else []), "kill-session", name]
        try:
            result = self.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=ZELLIJ_TIMEOUT_SECONDS, check=False, env=self.environment)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"zellij kill-session did not answer within {ZELLIJ_TIMEOUT_SECONDS:g} s") from None
        if result.returncode != 0:
            raise RuntimeError(f"zellij kill-session failed: {(result.stderr or result.stdout or '').strip()}")


_PANE_NAME = re.compile(r"claude ([0-9a-f]{8})\b")


def pane_name(handle: str, title: str) -> str:
    """How Reins names a session pane; attached_handles reads the handle back from it."""
    return f"claude {handle}" + (f" {title}" if title and title != handle else "")


# The pane beside the panel until the first session opens; launch.write_config names it.
HINT_TITLE = "Reins: select a session and press Enter"
LOG_PREFIX = "log "
SHELL_PREFIX = "shell "


PANEL_TITLE = "Reins"


def _is_panel(pane: Mapping[str, object]) -> bool:
    return pane.get("title") == PANEL_TITLE or "_tui-panel" in str(pane.get("terminal_command") or "")


def place(zellij: "Zellij", argv: Sequence[str], *, name: str, close_on_exit: bool, direction: str = BELOW,
          cwd: Optional[str] = None) -> None:
    """Open a pane on the right without narrowing the panel.

    The first one takes the hint pane's place; later ones split the newest
    session, log or shell pane, below it or to its right as asked, so the
    panel keeps its width. Only with nothing on the right does it split the panel.
    """
    panes = [pane for pane in zellij.panes() if not pane.get("is_plugin") and not pane.get("exited")]
    hint = next((pane for pane in panes if pane.get("title") == HINT_TITLE), None)
    if hint is not None:
        zellij.new_pane(argv, name=name, close_on_exit=close_on_exit, cwd=cwd, replace=f"terminal_{hint.get('id')}")
        return
    # Every pane but the panel is on the right. Names cannot tell: a shell renames its pane. Only tiled panes in
    # the panel's own tab count, so a split never pulls the user to another tab or into a floating pane.
    panel_tab = next((pane.get("tab_id") for pane in panes if _is_panel(pane)), None)
    right = [pane for pane in panes if isinstance(pane.get("id"), int) and not _is_panel(pane)
             and not pane.get("is_floating") and pane.get("tab_id") == panel_tab]
    if right:
        zellij.focus(f"terminal_{max(int(pane['id']) for pane in right)}")
        zellij.new_pane(argv, name=name, close_on_exit=close_on_exit, cwd=cwd, direction=direction)
        return
    zellij.new_pane(argv, name=name, close_on_exit=close_on_exit, cwd=cwd, direction="right")


def attached_handles(panes: Sequence[Mapping[str, object]]) -> dict[str, str]:
    """Session panes that are still open: handle -> pane id.

    Zellij reports a pane's command only while it knows the foreground
    process, so the pane name Reins gave it is read first.
    """
    found = {}
    for pane in panes:
        if pane.get("is_plugin") or pane.get("exited"):
            continue
        name = pane.get("title")
        match = _PANE_NAME.match(name) if isinstance(name, str) else None
        handle = match.group(1) if match else None
        command = pane.get("terminal_command")
        if handle is None and isinstance(command, str):
            try:
                words = shlex.split(command)
            except ValueError:
                words = []
            if len(words) >= 3 and words[-2] == "attach" and Path(words[-3]).name.startswith("claude"):
                handle = words[-1]
        if handle:
            found[handle] = f"terminal_{pane.get('id')}"
    return found


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
                 now: Callable[[], float] = time.time) -> None:
        self.root = (queue_root or layout.queue_dir(None)).expanduser().absolute()
        given = dict(os.environ if environment is None else environment)
        self.environment = user_environment(given)
        self._pane_environment = given
        self.zellij = zellij
        self.run = run
        self.now = now

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

    def _views(self) -> list[SessionView]:
        views = claude_agents.session_views(self.environment, run=self.run)
        handles = attached_handles(self.zellij.panes()) if self.zellij else {}
        return [replace(view, state=sessions.ATTACHED) if view.state == sessions.RUNNING and view.handle in handles
                else view for view in views]

    def sessions(self) -> Sequence[SessionRow]:
        views = {view.session: view for view in self._views()}
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
        addresses = {str(record.get("session")): str(record["name"]) for record in link.sessions(self.root)
                     if isinstance(record.get("name"), str)}
        choices = self._choices()
        latest: dict[str, float] = {}
        for record in tasks:
            session = session_of(record)
            if session:
                latest[session] = max(latest.get(session, 0.0), _when(record.get("submitted_at")) or 0.0)
        # Closed sessions are listed when Reins has worked with them lately: that is how a user finds them again.
        for record in tasks:
            session = session_of(record)
            if session and session not in views and record.get("agent") in (None, "claude"):
                views[session] = SessionView(agent="claude", session=session, state=sessions.CLOSED,
                                             cwd=record.get("cwd") if isinstance(record.get("cwd"), str) else None,
                                             started_at=_when(record.get("submitted_at")))
        def archived(view: SessionView) -> bool:
            # Archived until it does something again: runs, gets a task, or has a result waiting.
            since = choices["archived"].get(view.session)
            return (since is not None and view.state not in PROCESS_STATES and not pending.get(view.session)
                    and latest.get(view.session, 0.0) <= since)

        return [SessionRow(session=view.session, agent=view.agent, state=view.state,
                           title=view.name or view.session[:8], cwd=view.cwd, activity=view.activity, host=view.host,
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

    def _view(self, session: str) -> Optional[SessionView]:
        view = next((view for view in self._views() if view.session == session), None)
        if view is None:
            view = claude_agents.session_view(session, self.environment, views=(), run=self.run)
        return view

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

    def _attach(self, view_title: str, handle: str, direction: str = BELOW) -> ActionResult:
        if self.zellij is None:
            return ActionResult(False, "not inside the Reins Zellij session")
        handles = attached_handles(self.zellij.panes())
        if handle in handles:
            self.zellij.focus(handles[handle])
            return ActionResult(True, f"{view_title} is already open; moved to its pane")
        place(self.zellij, self._pane(claude_background.attach_command(handle, self.environment)),
              name=pane_name(handle, view_title),
              close_on_exit=True, direction=direction)
        return ActionResult(True, f"opened {view_title}")

    def open_session(self, session: str, direction: str = BELOW) -> ActionResult:
        view = self._view(session)
        if view is None:
            return ActionResult(False, f"no Claude session {session[:8]} on this machine")
        title = view.name or session[:8]
        if view.state == sessions.FOREIGN:
            where = {sessions.HOST_DESKTOP: "the Claude desktop app"}.get(view.host or "", "another terminal")
            return ActionResult(False, f"{title} is open in {where}" + (f" (pid {view.pid})" if view.pid else "")
                                + "; close it there to open it here. Messages and logs still work.")
        if view.state == sessions.HEADLESS:
            return ActionResult(False, f"{title} is finishing a headless Reins turn; open it again when that ends")
        try:
            if view.state == sessions.CLOSED:
                if not view.cwd:
                    return ActionResult(False, f"cannot tell which directory {title} belongs to")
                handle = claude_background.resume(session, view.cwd, self.environment, stopped=False,
                                                  settings=self._settings(), run=self.run)
            else:
                handle = view.handle or session[:8]
            return self._attach(title, handle, direction)
        except claude_background.StartedCopy:
            return ActionResult(False, f"{title} was opened somewhere else just now; try again when it closes")
        except (claude_background.BackgroundError, RuntimeError, OSError) as exc:
            return ActionResult(False, str(exc))

    def new_session(self, agent: str, cwd: str) -> ActionResult:
        if agent != "claude":
            return ActionResult(False, f"{agent} sessions come in a later version")
        directory = os.path.expanduser(cwd)
        if not os.path.isdir(directory):
            return ActionResult(False, f"no directory {cwd}")
        try:
            handle = claude_background.start(directory, self.environment, settings=self._settings(), run=self.run)
            return self._attach(handle, handle)
        except (claude_background.BackgroundError, RuntimeError, OSError) as exc:
            return ActionResult(False, str(exc))

    def fork_session(self, session: str, direction: str = BELOW) -> ActionResult:
        view = self._view(session)
        if view is None:
            return ActionResult(False, f"no Claude session {session[:8]} on this machine")
        title = view.name or session[:8]
        if not view.cwd:
            return ActionResult(False, f"cannot tell which directory {title} belongs to")
        name = f"{title} (fork)"
        try:
            handle = claude_background.fork(session, view.cwd, self.environment, name=name if view.name else None,
                                            settings=self._settings(), run=self.run)
            return self._attach(name, handle, direction)
        except (claude_background.BackgroundError, RuntimeError, OSError) as exc:
            return ActionResult(False, str(exc))

    def pin_session(self, session: str, pinned: bool) -> ActionResult:
        try:
            self._choose("pinned", session, pinned)
        except OSError as exc:
            return ActionResult(False, f"cannot save the pin: {exc}")
        return ActionResult(True, ("pinned " if pinned else "unpinned ") + session[:8])

    def archive_session(self, session: str, archived: bool) -> ActionResult:
        view = self._view(session) if archived else None
        title = (view.name if view else None) or session[:8]
        if view is not None and view.state in (sessions.FOREIGN, sessions.HEADLESS):
            return ActionResult(False, f"{title} is open elsewhere; close it there first")
        stopped = ""
        if view is not None and view.state in (sessions.ATTACHED, sessions.RUNNING) and view.handle:
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
        view = next((view for view in self._views() if view.session == session), None)
        if view is None or view.state not in (sessions.ATTACHED, sessions.RUNNING) or not view.handle:
            return ActionResult(False, "only a background session that is running can be stopped here")
        try:
            claude_background.stop(view.handle, self.environment, run=self.run)
        except claude_background.BackgroundError as exc:
            return ActionResult(False, str(exc))
        return ActionResult(True, f"stopped {view.name or session[:8]}; its conversation stays and opens again with Enter")

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
        name = self._session_name(recipient) if sessions.FULL_ID.fullmatch(recipient) else recipient
        if name is None:
            return ActionResult(False, "this session has no Reins name yet: "
                                       "right-click it (or press o) and name it first")
        return self._reins("send", name, "--message", text)

    def cancel_callback(self, callback: str) -> ActionResult:
        return self._reins("cancel", "--id", callback)

    def name_session(self, session: str, name: str, rename: bool = False) -> ActionResult:
        view = self._view(session)
        if view is None:
            return ActionResult(False, f"no Claude session {session[:8]} on this machine")
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
