"""What the Reins panel shows and asks for, without Textual.

The panel (panel.py) only draws rows and calls a ``Backend``. The live backend
(live.py) reads Claude, the Reins queue and Zellij; tests and development use
``FakeBackend``. See docs/design/sessions-tui.md section 7.

Every Backend call may block for up to a few seconds (it runs `claude agents`
or `zellij action`), so the panel calls it off the UI thread.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional, Protocol, Sequence

from ...core import sessions


@dataclass(frozen=True)
class SessionRow:
    session: str             # Full session id.
    agent: str               # "claude" for now; "codex", "pi", "dsh" in 0.15.
    state: str               # One of core.sessions.STATES.
    title: str               # The harness's own name for it, else a short id.
    cwd: Optional[str] = None
    activity: Optional[str] = None  # core.sessions.ACTIVITIES, when known.
    host: Optional[str] = None      # core.sessions.HOST_*, when known.
    tasks_running: int = 0
    callbacks_pending: int = 0
    goal: Optional[str] = None      # The active goal's task text, if any.
    started_at: Optional[float] = None


@dataclass(frozen=True)
class TaskRow:
    task: str                # Task id.
    title: str               # The task text given at submission.
    state: str               # Reins task state: submitted, running, succeeded, failed, canceled, ...
    session: Optional[str]   # The session that submitted it, if known.
    agent: Optional[str] = None
    started_at: Optional[float] = None
    ended_at: Optional[float] = None
    callback: Optional[str] = None        # Its callback id, once one exists.
    callback_state: Optional[str] = None  # pending, delivered, acked, canceled, failed.


@dataclass(frozen=True)
class GoalRow:
    goal: str
    session: str
    task: str
    state: str               # active, done, ...
    progress: Optional[str] = None  # One line, e.g. "3/5 steps".


@dataclass(frozen=True)
class MessageRow:
    message: str
    sender: str
    recipient: str
    subject: str
    sent_at: float
    read: bool = False


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str             # One line for the status bar, in plain words.


class Backend(Protocol):
    def sessions(self) -> Sequence[SessionRow]: ...
    def tasks(self) -> Sequence[TaskRow]: ...
    def goals(self) -> Sequence[GoalRow]: ...
    def messages(self) -> Sequence[MessageRow]: ...

    def open_session(self, session: str) -> ActionResult:
        """Show it in a pane: jump to its pane if one has it, else resume and attach (section 5)."""

    def new_session(self, agent: str, cwd: str) -> ActionResult:
        """Start a new session in ``cwd`` and open it in a pane."""

    def stop_session(self, session: str) -> ActionResult:
        """Stop its process; the conversation stays and it can be opened again."""

    def show_log(self, task: str) -> ActionResult:
        """Follow the task's output in a pane."""

    def send_message(self, recipient: str, text: str) -> ActionResult:
        """Same as `reins send <recipient> <text>`."""

    def cancel_callback(self, callback: str) -> ActionResult:
        """Same as `reins cancel <callback>`."""


@dataclass
class FakeBackend:
    """An in-memory backend that records what it was asked to do."""
    session_rows: list[SessionRow] = field(default_factory=list)
    task_rows: list[TaskRow] = field(default_factory=list)
    goal_rows: list[GoalRow] = field(default_factory=list)
    message_rows: list[MessageRow] = field(default_factory=list)
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def sessions(self) -> Sequence[SessionRow]:
        return list(self.session_rows)

    def tasks(self) -> Sequence[TaskRow]:
        return list(self.task_rows)

    def goals(self) -> Sequence[GoalRow]:
        return list(self.goal_rows)

    def messages(self) -> Sequence[MessageRow]:
        return list(self.message_rows)

    def _find(self, session: str) -> Optional[SessionRow]:
        return next((row for row in self.session_rows if row.session == session), None)

    def open_session(self, session: str) -> ActionResult:
        self.calls.append(("open", session))
        row = self._find(session)
        if row is None:
            return ActionResult(False, f"no session {session[:8]}")
        if row.state == sessions.FOREIGN:
            return ActionResult(False, f"{row.title} is open in another window; close it there to open it here")
        if row.state == sessions.HEADLESS:
            return ActionResult(False, f"{row.title} is finishing a headless turn; open it again in a moment")
        self.session_rows = [replace(r, state=sessions.ATTACHED) if r is row else r for r in self.session_rows]
        return ActionResult(True, f"opened {row.title}")

    def new_session(self, agent: str, cwd: str) -> ActionResult:
        self.calls.append(("new", agent, cwd))
        return ActionResult(True, f"started a {agent} session in {cwd}")

    def stop_session(self, session: str) -> ActionResult:
        self.calls.append(("stop", session))
        row = self._find(session)
        if row is None or row.state not in (sessions.ATTACHED, sessions.RUNNING):
            return ActionResult(False, "only a session Reins hosts can be stopped here")
        self.session_rows = [replace(r, state=sessions.STOPPED, activity=None) if r is row else r
                             for r in self.session_rows]
        return ActionResult(True, f"stopped {row.title}")

    def show_log(self, task: str) -> ActionResult:
        self.calls.append(("log", task))
        return ActionResult(True, f"following task {task[:8]}")

    def send_message(self, recipient: str, text: str) -> ActionResult:
        self.calls.append(("send", recipient, text))
        return ActionResult(True, f"sent to {recipient}")

    def cancel_callback(self, callback: str) -> ActionResult:
        self.calls.append(("cancel", callback))
        return ActionResult(True, f"canceled {callback}")
