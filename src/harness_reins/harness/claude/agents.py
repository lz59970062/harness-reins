"""Claude Code session state, read from Claude itself.

`claude agents --json --all` lists every live session (interactive windows and
background sessions) and background sessions whose process has exited. Reins
reads it each time and keeps no copy (docs/design/sessions-tui.md section 4.2).
The live session records under ``~/.claude/sessions`` add what that list does
not have: headless resumes, a window that opened after the list was taken, and
everything when the CLI is too old to list.

Every rule here leans the same way. When the sources disagree, the view that
keeps Reins away from a process wins, since a session reported closed while a
window has it would be resumed beside that window.
"""
from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Collection, Iterable, Mapping
from typing import Optional

from ...core import sessions
from ...core.sessions import SessionView
from . import session as claude_session

AGENT = "claude"
LIST_TIMEOUT_SECONDS = 10.0
BACKGROUND_KIND = "background"
INTERACTIVE_KIND = "interactive"
# kind values the session records use for the same processes.
RECORD_BACKGROUND_KINDS = frozenset({"bg", BACKGROUND_KIND})

# Background sessions report `state`, interactive windows `status`.
_BACKGROUND_ACTIVITY = {"working": sessions.WORKING, "done": sessions.IDLE, "blocked": sessions.BLOCKED}
_WINDOW_ACTIVITY = {"busy": sessions.WORKING, "idle": sessions.IDLE}

# When two sources describe one session, the higher rank wins: the more of it
# Reins must leave alone, the higher.
_RANK = {sessions.FOREIGN: 4, sessions.HEADLESS: 3, sessions.ATTACHED: 2, sessions.RUNNING: 2,
         sessions.STOPPED: 1, sessions.CLOSED: 0}

Run = Callable[..., "subprocess.CompletedProcess[str]"]
Discover = Callable[[Mapping[str, str]], Optional["claude_session.Executable"]]


def list_agents(environment: Mapping[str, str] = os.environ, *, run: Run = subprocess.run,
                discover: Discover = claude_session.discover_executable) -> Optional[list[dict[str, object]]]:
    """The raw `claude agents --json --all` entries, or None when the CLI cannot give them."""
    found = discover(environment)
    if found is None:
        return None
    try:
        result = run([found.path, "agents", "--json", "--all"], stdin=subprocess.DEVNULL, capture_output=True,
                     text=True, encoding="utf-8", errors="replace", timeout=LIST_TIMEOUT_SECONDS,
                     env=dict(environment), check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        entries = json.loads(result.stdout)
    except ValueError:
        return None
    if not isinstance(entries, list):
        return None
    return [entry for entry in entries if isinstance(entry, dict) and isinstance(entry.get("sessionId"), str)]


def _int(value: object) -> Optional[int]:
    return value if type(value) is int and value > 0 else None


def _text(value: object) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _started(value: object) -> Optional[float]:
    # Claude reports milliseconds since the epoch.
    return value / 1000.0 if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def _window_host(record: Optional[Mapping[str, object]]) -> Optional[str]:
    if not record:
        return None
    return sessions.HOST_DESKTOP if record.get("entrypoint") == claude_session.DESKTOP_ENTRYPOINT else sessions.HOST_TERMINAL


def entry_view(entry: Mapping[str, object], *, attached: Collection[str] = (),
               record: Optional[Mapping[str, object]] = None) -> Optional[SessionView]:
    """One `claude agents` entry as a session view; None for an unknown kind with no process."""
    session = str(entry["sessionId"])
    kind = entry.get("kind")
    pid = _int(entry.get("pid"))
    common = dict(agent=AGENT, session=session, name=_text(entry.get("name")), cwd=_text(entry.get("cwd")),
                  pid=pid, started_at=_started(entry.get("startedAt")))
    if kind == BACKGROUND_KIND:
        handle = _text(entry.get("id")) or session[:8]
        if pid is None:
            return SessionView(state=sessions.STOPPED, host=sessions.HOST_CLAUDE_BACKGROUND, handle=handle, **common)
        state = sessions.ATTACHED if session in attached else sessions.RUNNING
        return SessionView(state=state, host=sessions.HOST_CLAUDE_BACKGROUND, handle=handle,
                           activity=_BACKGROUND_ACTIVITY.get(str(entry.get("state"))), **common)
    if kind == INTERACTIVE_KIND:
        return SessionView(state=sessions.FOREIGN, host=_window_host(record) or sessions.HOST_TERMINAL,
                           activity=_WINDOW_ACTIVITY.get(str(entry.get("status"))), **common)
    if pid is not None:
        # A kind this version does not know, with a process: leave it alone.
        return SessionView(state=sessions.FOREIGN, host=_window_host(record), **common)
    return None


def record_view(record: Mapping[str, object], *, attached: Collection[str] = ()) -> SessionView:
    """A live session record as a session view."""
    session = str(record["sessionId"])
    common = dict(agent=AGENT, session=session, name=_text(record.get("name")), cwd=_text(record.get("cwd")),
                  pid=_int(record.get("pid")), started_at=_started(record.get("startedAt")))
    if record.get("entrypoint") == claude_session.HEADLESS_ENTRYPOINT:
        return SessionView(state=sessions.HEADLESS, host=sessions.HOST_HEADLESS,
                           activity=_WINDOW_ACTIVITY.get(str(record.get("status"))), **common)
    if record.get("kind") in RECORD_BACKGROUND_KINDS:
        state = sessions.ATTACHED if session in attached else sessions.RUNNING
        activity = (_BACKGROUND_ACTIVITY.get(str(record.get("state")))
                    or _WINDOW_ACTIVITY.get(str(record.get("status"))))
        return SessionView(state=state, host=sessions.HOST_CLAUDE_BACKGROUND, handle=session[:8],
                           activity=activity, **common)
    return SessionView(state=sessions.FOREIGN, host=_window_host(record),
                       activity=_WINDOW_ACTIVITY.get(str(record.get("status"))), **common)


def _keep(views: dict[str, SessionView], view: SessionView) -> None:
    known = views.get(view.session)
    if known is None or _RANK[view.state] > _RANK[known.state]:
        views[view.session] = view


def build_views(entries: Optional[Iterable[Mapping[str, object]]], records: Iterable[Mapping[str, object]],
                *, attached: Collection[str] = ()) -> list[SessionView]:
    """Every Claude session with a host, one view per session.

    ``entries`` is what `claude agents` listed (None when it could not), and
    ``records`` the live session records. A live record always counts, listed
    or not: it may be a headless resume, or a window that opened after the list
    was taken. When sources disagree about one session, the view that keeps
    Reins away wins (a window over a headless resume over a background process
    over a stopped one), whatever order they came in.
    """
    records = list(records)
    by_pid = {int(str(record["pid"])): record for record in records if type(record.get("pid")) is int}
    views: dict[str, SessionView] = {}
    for entry in entries or ():
        view = entry_view(entry, attached=attached, record=by_pid.get(_int(entry.get("pid")) or -1))
        if view is not None:
            _keep(views, view)
    for record in records:
        _keep(views, record_view(record, attached=attached))
    return sorted(views.values(), key=lambda view: (-(view.started_at or 0.0), view.session))


def session_views(environment: Mapping[str, str] = os.environ, *, attached: Collection[str] = (),
                  run: Run = subprocess.run) -> list[SessionView]:
    """Every Claude session that has a process, or a host that remembers it."""
    return build_views(list_agents(environment, run=run), claude_session.live_process_records(environment),
                       attached=attached)


def session_view(session: str, environment: Mapping[str, str] = os.environ, *, attached: Collection[str] = (),
                 views: Optional[Iterable[SessionView]] = None, run: Run = subprocess.run) -> Optional[SessionView]:
    """One session's view: listed, or closed when only its conversation is left, or None when unknown."""
    for view in (session_views(environment, attached=attached, run=run) if views is None else views):
        if view.session == session:
            return view
    transcript = claude_session.find_session_file(session, environment)
    if transcript is None:
        return None
    return SessionView(agent=AGENT, session=session, state=sessions.CLOSED,
                       cwd=claude_session.session_project_cwd(session, environment))
