"""Follow a Claude Code window whose session id changed while it stayed open.

`/clear` gives the open window a new id, and a context continuation can too. Work submitted before
stays bound to the old id, which now has no window, so the daemon would resume it headless: a copy that
handles the result, ACKs it and edits shared files while the window never hears of it. The window's
session record follows the id, so the daemon watches each window process and notes the move.

`/resume` also changes the id of an open window, but to another conversation. Only a transcript that
starts with the old id's records, or was born after a recent sight of the old id, carries on from it. An old
sight proves nothing: the resumed conversation may just be younger than that. Missing a `/clear` while the
daemon was down costs a headless resume; a false move sends results to the wrong conversation.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from ..core import targets
from ..harness.claude import session as claude_session
from ..store import layout
from ..store import records as queue_store
from ..system import files
from . import dispatch, goals, resume_environment

STATE_FILE = "claude-windows.json"
STATE_LOCK_ID = "claude-windows"
CONTINUATION_TEXT = "This session is being continued from a previous conversation"
TRACK_INTERVAL_SECONDS = 5.0
SEEN_REFRESH_SECONDS = 30.0
SEEN_FRESH_SECONDS = 120.0
# A new transcript with nothing dated yet is looked at again for this long, then taken as no move.
UNDECIDED_SECONDS = 60.0
FORGET_WINDOW_SECONDS = 3600.0
KEEP_MOVE_SECONDS = 30 * 86400.0
OPENING_LINES = 200

_next_track = 0.0


def _load(root: Path) -> dict[str, dict[str, dict[str, object]]]:
    try:
        state = json.loads(files.read_text(root / STATE_FILE))
    except (OSError, ValueError):
        state = None
    state = state if isinstance(state, dict) else {}
    return {name: value if isinstance(value := state.get(name), dict) else {} for name in ("windows", "moves")}


def _opening(session: str, environment: Mapping[str, str]) -> dict[str, object] | None:
    """How a transcript starts, or None when it has no file yet."""
    path = claude_session.find_session_file(session, environment)
    if path is None:
        return None
    opening: dict[str, object] = {"born_at": None, "first_session": None, "continued": False}
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for number, line in enumerate(handle):
                if number >= OPENING_LINES:
                    break
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                if opening["first_session"] is None and isinstance(record.get("sessionId"), str):
                    opening["first_session"] = record["sessionId"]
                if opening["born_at"] is None and isinstance(record.get("timestamp"), str):
                    try:
                        stamp = record["timestamp"].replace("Z", "+00:00")
                        opening["born_at"] = datetime.datetime.fromisoformat(stamp).timestamp()
                    except ValueError:
                        pass
                if record.get("type") == "user":
                    opening["continued"] = CONTINUATION_TEXT in json.dumps(record.get("message"), ensure_ascii=False)
                    break
    except OSError:
        return None
    return opening


def _carries_on(old: str, new: str, seen_at: float, environment: Mapping[str, str]) -> bool | None:
    """Whether ``new`` carries on from ``old`` in the same window; None until its transcript can tell."""
    opening = _opening(new, environment)
    if opening is None:
        return True  # A conversation that was resumed has a transcript; this one has not been written yet.
    if opening["first_session"] == old:
        return True
    if opening["born_at"] is None:
        return None
    return time.time() - seen_at <= SEEN_FRESH_SECONDS and float(str(opening["born_at"])) >= seen_at


def sync(root: Path, environment: Mapping[str, str] = os.environ, *, wait: bool = False) -> None:
    """Note every open window's current id, and any move since the last look.

    Without ``wait`` it leaves the look to a process already looking; the check before a resume must see the result.
    """
    windows = claude_session.open_windows(environment)
    lock = queue_store.acquire_owner_lock(root, STATE_LOCK_ID, blocking=wait)
    if lock is None:
        return
    try:
        state = _load(root)
        now = time.time()
        changed = False
        for record in windows:
            key = f"{record['pid']}:{record.get('procStart', '')}"
            session = str(record["sessionId"])
            known = state["windows"].get(key)
            if isinstance(known, dict) and known.get("session") != session:
                carries_on = _carries_on(str(known.get("session")), session, float(str(known.get("seen_at", now))), environment)
                if carries_on is None:
                    undecided_at = float(str(known.get("undecided_at", now)))
                    if now - undecided_at <= UNDECIDED_SECONDS:
                        if "undecided_at" not in known:
                            known["undecided_at"] = now
                            changed = True
                        continue
                if carries_on:
                    state["moves"][str(known.get("session"))] = {"to": session, "at": now}
            elif isinstance(known, dict) and now - float(str(known.get("seen_at", 0))) < SEEN_REFRESH_SECONDS:
                continue
            state["windows"][key] = {"session": session, "seen_at": now}
            changed = True
        open_sessions = {record["sessionId"] for record in windows}
        for move in state["moves"].values():
            # A window can outlive the limit below, and the id it left must still lead to it.
            if isinstance(move, dict) and move.get("to") in open_sessions and now - float(str(move.get("at", 0))) > 86400.0:
                move["at"] = now
                changed = True
        for name, keep in (("windows", FORGET_WINDOW_SECONDS), ("moves", KEEP_MOVE_SECONDS)):
            for key, value in list(state[name].items()):
                stamp = value.get("seen_at", value.get("at", 0)) if isinstance(value, dict) else 0
                if now - float(str(stamp)) > keep:
                    del state[name][key]
                    changed = True
        if changed:
            queue_store.write_request(root / STATE_FILE, state)
    finally:
        queue_store.release_owner_lock(lock, remove=False)


def current_session(root: Path, session: str, environment: Mapping[str, str] = os.environ) -> str:
    """Where this session's callbacks belong now: its own window, else the open window that carried it on.

    With no window open anywhere, a continuation takes over from the transcript it summarized. A `/clear`
    does not: the new conversation knows nothing of the work, and the old one has no window beside it.
    """
    moves = _load(root)["moves"]
    if session not in moves:
        return session
    latest = current = session
    unbroken = True
    seen = {session}
    while claude_session.open_window(current, environment) is None:
        move = moves.get(current)
        following = move.get("to") if isinstance(move, dict) else None
        if not isinstance(following, str) or following in seen:
            return latest
        seen.add(following)
        current = following
        opening = _opening(current, environment)
        unbroken = unbroken and opening is not None and bool(opening["continued"])
        if unbroken:
            latest = current
    return current


def _settling(root: Path, session: str) -> bool:
    """Whether a window just left this id for one whose transcript cannot tell yet if it carries on."""
    now = time.time()
    return any(isinstance(window, dict) and window.get("session") == session and "undecided_at" in window
               and now - float(str(window["undecided_at"])) <= UNDECIDED_SECONDS  # Its window may have closed since.
               for window in _load(root)["windows"].values())


def holds(root: Path, request: Mapping[str, object], environment: Mapping[str, str] = os.environ) -> bool:
    """Whether this callback must not be resumed under its target, because its session moved or may have."""
    session = targets.session_of(request.get("target"))
    if session is None:
        return False
    origin = str(request.get("moved_from") or session)
    if _settling(root, origin) or _settling(root, session):
        return True
    following = current_session(root, origin, environment)
    if following == session:
        return False
    # `follow` leaves these where they are, so holding one for it would never end.
    return request.get("retain_target_lease") is not True or claude_session.open_window(following, environment) is not None


def _retarget(root: Path, path: Path, environment: Mapping[str, str]) -> None:
    lock = queue_store.acquire_owner_lock(root, layout.delivery_lock_id(path.stem), blocking=False)
    if lock is None:
        return
    try:
        request = dispatch.load_request(path)
        target = request.get("target")
        if not isinstance(target, dict) or not isinstance(target.get("session"), str):
            return
        session = target["session"]
        # From where it started: a cleared session goes back to its context once the window has closed.
        origin = str(request.get("moved_from") or session)
        following = current_session(root, origin, environment)
        if following == session:
            return
        if following == origin:
            request.pop("moved_from", None)
        else:
            request["moved_from"] = origin
        request["target"] = {**target, "session": following}
        for shown_to_the_old_id in ("live_delivered_at", "live_window_pid", "live_reminders", "next_attempt_at"):
            request.pop(shown_to_the_old_id, None)
        request["last_deferred_reason"] = f"its session moved from {session} to {following} in the same window"
        queue_store.write_request(path, request)
    except (OSError, ValueError):
        pass  # It moved on while this looked; a pending one is seen again next time.
    finally:
        queue_store.release_owner_lock(lock, remove=False)


def _retarget_goal(root: Path, path: Path) -> None:
    lock = queue_store.acquire_owner_lock(root, layout.goal_lock_id(path.stem), blocking=False)
    if lock is None:
        return
    try:
        goal = goals.load_goal(root, path.stem)
        session = targets.session_of(goal.get("target"))
        if goal.get("agent") != "claude" or goal.get("state") != "active" or not isinstance(session, str):
            return
        origin = str(goal.get("moved_from") or session)
        following = current_session(root, origin)
        if following != session:
            if following == origin:
                goal.pop("moved_from", None)
            else:
                goal["moved_from"] = origin
            goal["target"] = {**goal["target"], "session": following}
            goals.write_goal(root, goal)
    except (OSError, ValueError):
        pass
    finally:
        queue_store.release_owner_lock(lock, remove=False)


def follow(root: Path, *, force: bool = False) -> None:
    """Daemon: note moved windows and send what is queued for an old id to where that session went."""
    global _next_track
    if not force and time.monotonic() < _next_track:
        return
    _next_track = time.monotonic() + TRACK_INTERVAL_SECONDS
    try:
        sync(root)
        synced = {claude_session.config_dir(os.environ)}
        queued = []
        for state in ("pending", "failed"):
            for path in sorted((root / state).glob("*.json")):
                try:
                    request = json.loads(files.read_text(path))
                except (OSError, ValueError):
                    continue
                target = request.get("target") if isinstance(request, dict) else None
                if not (isinstance(target, dict) and target.get("kind") == "session"
                        and request.get("retain_target_lease") is not True):
                    continue
                try:
                    if dispatch.request_agent(request) != "claude":
                        continue
                except ValueError:
                    continue  # A malformed record must not stop this for the rest of the queue.
                environment = resume_environment.merged(os.environ, resume_environment.load(root, path.stem))
                directory = claude_session.config_dir(environment)
                if directory not in synced:  # A session started with its own CLAUDE_CONFIG_DIR.
                    synced.add(directory)
                    sync(root, environment)
                queued.append((path, str(request.get("moved_from") or target.get("session")), environment))
        moves = _load(root)["moves"]
        if not moves:
            return
        for path, origin, environment in queued:
            if origin in moves:
                _retarget(root, path, environment)
        for path in sorted((root / "goals").glob("*.json")):
            _retarget_goal(root, path)
    except (OSError, ValueError) as exc:
        print(f"reins: warning: could not follow moved Claude Code sessions: {exc}", file=sys.stderr)
