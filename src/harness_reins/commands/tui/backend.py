"""What the Reins panel shows and asks for, without Textual.

The panel (panel.py) only draws rows and calls a ``Backend``. The live backend
(live.py) reads Claude, the Reins queue and Zellij; tests and development use
``FakeBackend``. See docs/design/sessions-tui.md section 7.

Every Backend call may block for up to a few seconds (it runs `claude agents`
or `zellij action`), so the panel calls it off the UI thread.
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field, replace
from typing import Container, Optional, Protocol, Sequence

from ...core import sessions


@dataclass(frozen=True)
class SessionRow:
    session: str             # Full session id; a Pi session is its absolute session file.
    agent: str               # "claude", "codex", "pi" or "dsh".
    state: str               # One of core.sessions.STATES.
    title: str               # The harness's own name for it, else a short id.
    cwd: Optional[str] = None
    activity: Optional[str] = None  # core.sessions.ACTIVITIES, when known.
    host: Optional[str] = None      # core.sessions.HOST_*, when known.
    tasks_running: int = 0
    callbacks_pending: int = 0
    goal: Optional[str] = None      # The active goal's task text, if any.
    started_at: Optional[float] = None
    address: Optional[str] = None   # Its name in the Reins address book, what `reins send` takes.
    pinned: bool = False            # Kept at the top of the list (the panel's own choice, not Claude's).
    archived: bool = False          # Left out of the list until it is shown again or wakes up.


_FILE_ID = re.compile(r"_([0-9A-Za-z-]+)\.jsonl\Z")


def short_id(session: str) -> str:
    """Eight characters a person can tell sessions apart by: an id's start, or for a Pi session file the
    end of the id in its name."""
    if os.path.isabs(session):
        match = _FILE_ID.search(os.path.basename(session))
        return (match.group(1) if match else os.path.basename(session)).replace("-", "")[-8:]
    return session[:8]


# A Reins address: lowercase letters, digits, '-' or '_', up to 32 characters (`reins session register`).
NAME_LIMIT = 32
AGENT_TITLES = {"claude": "Claude Code", "codex": "Codex", "pi": "Pi", "dsh": "DeepSeek Harness"}


def suggested_name(title: str, session: str) -> str:
    """An address-book name from a session's title: lowercase letters, digits and '-'."""
    name = "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in title.lower())
    name = "-".join(part for part in name.split("-") if part)[:NAME_LIMIT].strip("-")
    return name if name else "s-" + short_id(session)


def free_name(base: str, taken: Container[str], keep: str = "") -> str:
    """``base`` + ``keep``, or with ``-2``, ``-3``... after it when another session holds that; ``base`` is cut
    short to stay within the length limit, ``keep`` never is."""
    number = 1
    while True:
        suffix = keep + (f"-{number}" if number > 1 else "")
        name = (base[:NAME_LIMIT - len(suffix)].strip("-") or "s") + suffix
        if name not in taken:
            return name
        number += 1


def send_command(name: str, queue: Optional[str] = None) -> str:
    """The `reins send` line an agent runs to reach ``name``; ``queue`` when it is not the default one."""
    return f"reins send {name}" + (f" --queue-dir {shlex.quote(queue)}" if queue else "") + ' -m "..."'


def collaborator_prompt(mate: str, partner: str, partner_agent: str, partner_cwd: Optional[str], task: str,
                        queue: Optional[str] = None) -> str:
    """The first prompt of a session started to work with ``partner``."""
    where = f" in {partner_cwd}" if partner_cwd else ""
    lines = [
        f"You are @{mate}, a session started from the Reins screen to work together with @{partner}, a "
        f"{AGENT_TITLES.get(partner_agent, partner_agent)} session{where}.",
        f"Reach it with `{send_command(partner, queue)}` (the harness-reins skill has the details); "
        "what it sends you arrives here.",
        f"Your first task: {task}" if task else f"You have no task yet: wait for @{partner} or the user to give you one.",
    ]
    return "\n".join(lines)


def collaborator_notice(mate: str, mate_agent: str, cwd: str, task: str, queue: Optional[str] = None) -> str:
    """What the original session is told about the session started to work with it."""
    lines = [
        f"@{mate} is a new {AGENT_TITLES.get(mate_agent, mate_agent)} session in {cwd}, started from the Reins "
        "screen to work with you.",
        f"Reach it with `{send_command(mate, queue)}`; what it sends you arrives here.",
        f"Its first task: {task}" if task else "It has no task yet: tell it what to do.",
    ]
    return "\n".join(lines)


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


# Where a new pane goes, beside the newest pane on the right: below it, or to its right.
BELOW = "down"
BESIDE = "right"


class Backend(Protocol):
    def sessions(self) -> Sequence[SessionRow]: ...
    def tasks(self) -> Sequence[TaskRow]: ...
    def goals(self) -> Sequence[GoalRow]: ...
    def messages(self) -> Sequence[MessageRow]: ...

    def open_session(self, session: str, direction: str = BELOW) -> ActionResult:
        """Show it in a pane: jump to its pane if one has it, else resume and attach (section 5)."""

    def new_agents(self) -> Sequence[str]:
        """The harnesses a new session can start in, the first being the default."""

    def new_session(self, agent: str, cwd: str) -> ActionResult:
        """Start a new session in ``cwd`` and open it in a pane."""

    def stop_session(self, session: str) -> ActionResult:
        """Stop its process; the conversation stays and it can be opened again."""

    def show_log(self, task: str, direction: str = BELOW) -> ActionResult:
        """Follow the task's output in a pane."""

    def open_terminal(self, cwd: str, direction: str = BELOW) -> ActionResult:
        """A shell in a pane, in ``cwd``."""

    def send_message(self, recipient: str, text: str) -> ActionResult:
        """Same as `reins send <recipient> <text>`."""

    def cancel_callback(self, callback: str) -> ActionResult:
        """Same as `reins cancel <callback>`."""

    def health(self) -> Sequence[str]:
        """What `reins doctor` finds wrong, one plain line each; empty when Reins is ready."""

    def name_session(self, session: str, name: str, rename: bool = False) -> ActionResult:
        """Put the session in the address book under ``name`` (`reins session register`).

        With ``rename`` the session's old name goes; a name another session holds is still refused.
        """

    def fork_session(self, session: str, direction: str = BELOW) -> ActionResult:
        """Start a new session from a copy of this one's conversation, and open it in a pane."""

    def start_collaborator(self, session: str, agent: str, cwd: str, task: str = "",
                           direction: str = BELOW) -> ActionResult:
        """Start a new ``agent`` session in ``cwd`` to work with ``session``: name both in the address book,
        start the new one knowing its partner (and ``task``, if any), open it in a pane, and tell ``session``
        how to reach it."""

    def leave(self, end: bool) -> ActionResult:
        """Detach from the Reins screen (Alt+d), or end it (Quit in Alt+q's dialog); Claude sessions keep running either way."""

    def pin_session(self, session: str, pinned: bool) -> ActionResult:
        """Keep the session at the top of the list, or stop doing so."""

    def archive_session(self, session: str, archived: bool) -> ActionResult:
        """Leave the session out of the list (stopping it first if it runs), or bring it back."""


@dataclass
class FakeBackend:
    """An in-memory backend that records what it was asked to do."""
    session_rows: list[SessionRow] = field(default_factory=list)
    task_rows: list[TaskRow] = field(default_factory=list)
    goal_rows: list[GoalRow] = field(default_factory=list)
    message_rows: list[MessageRow] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    agents: list[str] = field(default_factory=lambda: ["claude"])

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

    def open_session(self, session: str, direction: str = BELOW) -> ActionResult:
        self.calls.append(("open", session) if direction == BELOW else ("open", session, direction))
        row = self._find(session)
        if row is None:
            return ActionResult(False, f"no session {session[:8]}")
        if row.agent == "dsh":
            return ActionResult(False, "DeepSeek Harness has no terminal window")
        if row.state == sessions.FOREIGN:
            return ActionResult(False, f"{row.title} is open in another window; close it there to open it here")
        if row.state == sessions.HEADLESS:
            return ActionResult(False, f"{row.title} is finishing a headless turn; open it again in a moment")
        self.session_rows = [replace(r, state=sessions.ATTACHED) if r is row else r for r in self.session_rows]
        return ActionResult(True, f"opened {row.title}")

    def new_agents(self) -> Sequence[str]:
        return list(self.agents)

    def new_session(self, agent: str, cwd: str) -> ActionResult:
        self.calls.append(("new", agent, cwd))
        return ActionResult(True, f"started a {agent} session in {cwd}")

    def stop_session(self, session: str) -> ActionResult:
        self.calls.append(("stop", session))
        row = self._find(session)
        if row is None or row.agent != "claude" or row.state not in (sessions.ATTACHED, sessions.RUNNING):
            return ActionResult(False, "only a session Reins hosts can be stopped here")
        self.session_rows = [replace(r, state=sessions.STOPPED, activity=None) if r is row else r
                             for r in self.session_rows]
        return ActionResult(True, f"stopped {row.title}")

    def show_log(self, task: str, direction: str = BELOW) -> ActionResult:
        self.calls.append(("log", task) if direction == BELOW else ("log", task, direction))
        return ActionResult(True, f"following task {task[:8]}")

    def open_terminal(self, cwd: str, direction: str = BELOW) -> ActionResult:
        self.calls.append(("terminal", cwd, direction))
        return ActionResult(True, f"opened a terminal in {cwd}")

    def send_message(self, recipient: str, text: str) -> ActionResult:
        self.calls.append(("send", recipient, text))
        return ActionResult(True, f"sent to {recipient}")

    def cancel_callback(self, callback: str) -> ActionResult:
        self.calls.append(("cancel", callback))
        return ActionResult(True, f"canceled {callback}")

    def health(self) -> Sequence[str]:
        return list(self.problems)

    def name_session(self, session: str, name: str, rename: bool = False) -> ActionResult:
        self.calls.append(("rename", session, name) if rename else ("name", session, name))
        row = self._find(session)
        if row is None:
            return ActionResult(False, f"no session {session[:8]}")
        if any(r.address == name and r.session != session for r in self.session_rows):
            return ActionResult(False, f"{name} already names another session")
        self.session_rows = [replace(r, address=name) if r is row else r for r in self.session_rows]
        return ActionResult(True, f"named {row.title} {name}")

    def leave(self, end: bool) -> ActionResult:
        self.calls.append(("end",) if end else ("detach",))
        return ActionResult(True, "ended" if end else "left")

    def fork_session(self, session: str, direction: str = BELOW) -> ActionResult:
        self.calls.append(("fork", session, direction))
        row = self._find(session)
        if row is None:
            return ActionResult(False, f"no session {session[:8]}")
        return ActionResult(True, f"forked {row.title}")

    def start_collaborator(self, session: str, agent: str, cwd: str, task: str = "",
                           direction: str = BELOW) -> ActionResult:
        self.calls.append(("collaborate", session, agent, cwd, task, direction))
        row = self._find(session)
        if row is None:
            return ActionResult(False, f"no session {session[:8]}")
        return ActionResult(True, f"started a {agent} session in {cwd} to work with {row.title}")

    def _change(self, session: str, **changes: object) -> Optional[SessionRow]:
        row = self._find(session)
        if row is not None:
            self.session_rows = [replace(r, **changes) if r is row else r for r in self.session_rows]
        return row

    def pin_session(self, session: str, pinned: bool) -> ActionResult:
        self.calls.append(("pin" if pinned else "unpin", session))
        row = self._change(session, pinned=pinned)
        if row is None:
            return ActionResult(False, f"no session {session[:8]}")
        return ActionResult(True, f"{'pinned' if pinned else 'unpinned'} {row.title}")

    def archive_session(self, session: str, archived: bool) -> ActionResult:
        self.calls.append(("archive" if archived else "unarchive", session))
        row = self._find(session)
        if row is None:
            return ActionResult(False, f"no session {session[:8]}")
        if archived and row.state in (sessions.FOREIGN, sessions.HEADLESS):
            return ActionResult(False, f"{row.title} is open elsewhere; close it there first")
        changes: dict = {"archived": archived}
        if archived and row.state in (sessions.ATTACHED, sessions.RUNNING):
            changes.update(state=sessions.STOPPED, activity=None)
        self._change(session, **changes)
        return ActionResult(True, f"{'archived' if archived else 'unarchived'} {row.title}")
