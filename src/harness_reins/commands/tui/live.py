"""The panel's live backend: Claude's own session list, the Reins queue, and the Zellij session around it.

Nothing here keeps state between calls (design invariant 2): each read asks
Claude, the queue files and Zellij again.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from ...core import sessions
from ...core.sessions import SessionView
from ...engine import claude_background
from ...harness.claude import agents as claude_agents
from ...install import skills as skill_install
from ...runtime import worker_command
from ...store import layout, link
from .backend import ActionResult, GoalRow, MessageRow, SessionRow, TaskRow

Run = Callable[..., "subprocess.CompletedProcess[str]"]

# How far back the panel looks for tasks, and for closed sessions they came from.
RECENT_SECONDS = 14 * 86400
MAX_ROWS = 300
RUNNING_TASK_STATES = frozenset({"submitting", "submitted", "launching", "running"})
FINISHED_GOAL_PREFIXES = ("complete", "done", "cancel", "abandon", "stop")
CALLBACK_STATES = ("pending", "running", "failed", "canceled", "done")
ZELLIJ_TIMEOUT_SECONDS = 10.0


class Zellij:
    """`zellij action` on the session the panel runs in (or on ``session`` from outside it)."""

    def __init__(self, executable: str, *, session: Optional[str] = None, config_dir: Optional[str] = None,
                 run: Run = subprocess.run) -> None:
        self.executable = executable
        self.session = session
        self.config_dir = config_dir
        self.run = run

    def _action(self, *arguments: str) -> str:
        argv = [self.executable]
        if self.config_dir:
            argv += ["--config-dir", self.config_dir]
        if self.session:
            argv += ["--session", self.session]
        argv += ["action", *arguments]
        result = self.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=ZELLIJ_TIMEOUT_SECONDS, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"zellij {arguments[0]} failed: {(result.stderr or result.stdout or '').strip()}")
        return result.stdout or ""

    def panes(self) -> list[dict[str, object]]:
        try:
            panes = json.loads(self._action("list-panes", "--json", "--command", "--tab"))
        except (RuntimeError, ValueError, OSError, subprocess.TimeoutExpired):
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


_PANE_NAME = re.compile(r"claude ([0-9a-f]{8})\b")


def pane_name(handle: str, title: str) -> str:
    """How Reins names a session pane; attached_handles reads the handle back from it."""
    return f"claude {handle}" + (f" {title}" if title and title != handle else "")


# The pane beside the panel until the first session opens; launch.write_config names it.
HINT_TITLE = "Reins: select a session and press Enter"
LOG_PREFIX = "log "


def place(zellij: "Zellij", argv: Sequence[str], *, name: str, close_on_exit: bool) -> None:
    """Open a pane on the right without narrowing the panel.

    The first one takes the hint pane's place; later ones split the newest
    session or log pane downwards, so they stack instead of each taking a slice
    of the width. Only with nothing on the right does it split the panel.
    """
    panes = [pane for pane in zellij.panes() if not pane.get("is_plugin") and not pane.get("exited")]
    hint = next((pane for pane in panes if pane.get("title") == HINT_TITLE), None)
    if hint is not None:
        zellij.new_pane(argv, name=name, close_on_exit=close_on_exit, replace=f"terminal_{hint.get('id')}")
        return
    right = [pane for pane in panes if isinstance(pane.get("title"), str)
             and (_PANE_NAME.match(str(pane["title"])) or str(pane["title"]).startswith(LOG_PREFIX))
             and isinstance(pane.get("id"), int)]
    if right:
        zellij.focus(f"terminal_{max(int(pane['id']) for pane in right)}")
        zellij.new_pane(argv, name=name, close_on_exit=close_on_exit, direction="down")
        return
    zellij.new_pane(argv, name=name, close_on_exit=close_on_exit, direction="right")


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
            if len(words) >= 3 and words[-2] == "attach" and Path(words[0]).name.startswith("claude"):
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


class LiveBackend:
    def __init__(self, *, queue_root: Optional[Path] = None, environment: Optional[Mapping[str, str]] = None,
                 zellij: Optional[Zellij] = None, run: Run = subprocess.run,
                 now: Callable[[], float] = time.time) -> None:
        self.root = (queue_root or layout.queue_dir(None)).expanduser().absolute()
        self.environment = dict(os.environ if environment is None else environment)
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
        # Closed sessions are listed when Reins has worked with them lately: that is how a user finds them again.
        for record in tasks:
            session = session_of(record)
            if session and session not in views and record.get("agent") in (None, "claude"):
                views[session] = SessionView(agent="claude", session=session, state=sessions.CLOSED,
                                             cwd=record.get("cwd") if isinstance(record.get("cwd"), str) else None,
                                             started_at=_when(record.get("submitted_at")))
        return [SessionRow(session=view.session, agent=view.agent, state=view.state,
                           title=view.name or view.session[:8], cwd=view.cwd, activity=view.activity, host=view.host,
                           tasks_running=running.get(view.session, 0), callbacks_pending=pending.get(view.session, 0),
                           goal=goals.get(view.session), started_at=view.started_at)
                for view in views.values()]

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

    def _settings(self) -> str:
        return claude_background.settings_argument(skill_install.agent_command_names())

    def _attach(self, view_title: str, handle: str) -> ActionResult:
        if self.zellij is None:
            return ActionResult(False, "not inside the Reins Zellij session")
        handles = attached_handles(self.zellij.panes())
        if handle in handles:
            self.zellij.focus(handles[handle])
            return ActionResult(True, f"{view_title} is already open; moved to its pane")
        place(self.zellij, claude_background.attach_command(handle, self.environment), name=pane_name(handle, view_title),
              close_on_exit=True)
        return ActionResult(True, f"opened {view_title}")

    def open_session(self, session: str) -> ActionResult:
        view = next((view for view in self._views() if view.session == session), None)
        if view is None:
            view = claude_agents.session_view(session, self.environment, views=(), run=self.run)
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
            return self._attach(title, handle)
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

    def stop_session(self, session: str) -> ActionResult:
        view = next((view for view in self._views() if view.session == session), None)
        if view is None or view.state not in (sessions.ATTACHED, sessions.RUNNING) or not view.handle:
            return ActionResult(False, "only a background session that is running can be stopped here")
        try:
            claude_background.stop(view.handle, self.environment, run=self.run)
        except claude_background.BackgroundError as exc:
            return ActionResult(False, str(exc))
        return ActionResult(True, f"stopped {view.name or session[:8]}; its conversation stays and opens again with Enter")

    def show_log(self, task: str) -> ActionResult:
        if self.zellij is None:
            return ActionResult(False, "not inside the Reins Zellij session")
        try:
            place(self.zellij, worker_command("log", task), name=f"{LOG_PREFIX}{task[:8]}", close_on_exit=False)
        except (RuntimeError, OSError) as exc:
            return ActionResult(False, str(exc))
        return ActionResult(True, f"following task {task[:8]}")

    def _reins(self, *arguments: str) -> ActionResult:
        try:
            result = self.run(worker_command(*arguments), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60, check=False)
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
            return ActionResult(False, "this session has no Reins name yet; ask it to run `reins session register`")
        return self._reins("send", name, "--message", text)

    def cancel_callback(self, callback: str) -> ActionResult:
        return self._reins("cancel", "--id", callback)
