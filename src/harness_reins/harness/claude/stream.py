"""Keep a headless Claude Code resume open, so later callbacks reach it while it runs.

A second ``claude -p --resume`` beside a running one would fork the transcript,
so a session has one process and later callbacks go into it. ``--input-format
stream-json`` is Claude Code's own way to do that: a message written to stdin
arrives between model steps, or with the result of a tool that is still running.
The channel does not work here, since ``claude -p`` drops its events silently.

Claude does not exit while stdin is open, so the worker closes it once the last
turn has ended and no background task can start another one.

A window may open the session while the resume runs. From then on the window
owns it: the resume takes no more callbacks and closes its input, so it ends
after the turn it is in. The window loaded the transcript as it was when it
opened, so the caller tells it how that turn ended (``Handover``).

There is no wall-clock limit by default, so a stuck process is found by what
the stream shows instead: waiting on the model, or failing to exit, without an
event for the stall limit; or a foreground tool that is quiet for far longer.
A background task may stay quiet as long as it runs: Claude Code limits those.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import session as claude_session

ARGUMENTS = ("--input-format", "stream-json", "--output-format", "stream-json", "--verbose")
POLL_SECONDS = 0.5
# `result` ends a turn, and a task's notification can start the next one a moment later.
QUIET_SECONDS = 5.0
# A CLI that says nothing gets its stdin closed: it still handles what it has, but takes no more.
FIRST_EVENT_SECONDS = 60.0
# A foreground tool may legitimately run quiet for hours; a stuck one still ends.
TOOL_STALL_SECONDS = 4 * 3600.0
# How often the worker looks for a window that opened the session.
WINDOW_CHECK_SECONDS = 2.0


@dataclass
class Handover:
    """A window opened the session while this resume ran."""
    window_pid: int
    # The window cannot have seen how the last turn ended: it was still running when the window was
    # found, or ended after the last look that found no window.
    turn_unseen: bool
    result: Optional[str] = None  # The resume's last turn result, once it exited.


class Stalled(subprocess.TimeoutExpired):
    """The process shows no progress, so the worker ends it and the daemon retries."""


class _Progress:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.last_event = self.started
        self.seen_event = False
        self.turn_running = True
        self.turn_ended_at: Optional[float] = None
        self.background_tasks = False
        self.tools: set[str] = set()  # tool_use ids still waiting for their result
        self.closed_at: Optional[float] = None
        self.last_result: Optional[str] = None

    def record(self, event: dict[str, object]) -> None:
        kind, subtype = event.get("type"), event.get("subtype")
        with self.lock:
            self.seen_event = True
            self.last_event = time.monotonic()
            if kind == "result":
                self.turn_running = False
                self.turn_ended_at = self.last_event
                if isinstance(event.get("result"), str):
                    self.last_result = event["result"]
                self.tools.clear()  # A turn that ended, even with an error, has no tool still running.
            elif kind in ("assistant", "user") or (kind == "system" and subtype == "init"):
                self.turn_running = True
            elif kind == "system" and subtype == "background_tasks_changed":
                self.background_tasks = bool(event.get("tasks"))
            if kind in ("assistant", "user") and event.get("parent_tool_use_id") is None:
                message = event.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                for block in content if isinstance(content, list) else ():
                    if isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                        self.tools.add(block["id"])
                    elif isinstance(block, dict) and block.get("type") == "tool_result":
                        self.tools.discard(str(block.get("tool_use_id")))

    def message_sent(self) -> None:
        with self.lock:
            self.turn_running = True
            self.last_event = time.monotonic()

    def stdin_closed(self) -> None:
        with self.lock:
            self.closed_at = time.monotonic()

    def stalled(self, limit: Optional[float]) -> Optional[str]:
        """Why the process looks stuck, or None while it may still be making progress."""
        if limit is None:
            return None
        now = time.monotonic()
        with self.lock:
            if self.closed_at is not None and not self.background_tasks:
                if now - max(self.closed_at, self.last_event) >= limit:
                    return f"it did not exit {limit:g}s after its input closed"
                return None
            quiet = now - self.last_event
            if self.background_tasks or not self.turn_running:
                return None
            if self.tools:
                tool_limit = max(limit, TOOL_STALL_SECONDS)
                return f"a tool showed no progress for {tool_limit:g}s" if quiet >= tool_limit else None
            return f"it waited on the model with no event for {limit:g}s" if quiet >= limit else None

    def finished(self) -> bool:
        now = time.monotonic()
        with self.lock:
            if not self.seen_event:
                return now - self.started >= FIRST_EVENT_SECONDS
            return (not self.turn_running and not self.background_tasks
                    and now - self.last_event >= QUIET_SECONDS)


def _read_events(stream, progress: _Progress) -> None:
    for line in stream:
        try:
            event = json.loads(line)
        except ValueError:
            event = None
        if not isinstance(event, dict):
            sys.stderr.write(line)  # Anything that is not an event belongs in the daemon's log.
            continue
        progress.record(event)
        if event.get("type") == "result" and isinstance(event.get("result"), str):
            print(event["result"], file=sys.stderr, flush=True)


def _send(process: subprocess.Popen[str], text: str, progress: _Progress) -> bool:
    message = {"type": "user", "message": {"role": "user", "content": text}}
    try:
        process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        process.stdin.flush()
    except (OSError, ValueError):
        return False  # It exited; what it did not take goes back to the daemon.
    progress.message_sent()
    return True


def _close_input(process: subprocess.Popen[str], progress: _Progress) -> None:
    progress.stdin_closed()
    try:
        process.stdin.close()
    except OSError:
        pass


def supervise(process: subprocess.Popen[str], prompt: str, root: Path, session: str,
              timeout: Optional[float], stall: Optional[float] = None, *,
              window_open: Optional[Callable[[], Optional[int]]] = None,
              handover: Optional[list[Handover]] = None) -> Optional[int]:
    """Deliver ``prompt`` and every later callback, then return the exit code.

    None means Claude refused the streaming flags without starting, so nothing
    was delivered and the caller may use a plain prompt. ``Stalled`` means it
    stopped making progress for ``stall`` seconds (see the module docstring).
    ``window_open`` returns the pid of a window that has the session open; when
    one appears, a ``Handover`` is appended to ``handover``.
    """
    if window_open is None:
        window_open = lambda: claude_session.open_window(session, os.environ)
    progress = _Progress()
    reader = threading.Thread(target=_read_events, args=(process.stdout, progress), daemon=True)
    reader.start()
    deadline = None if timeout is None else time.monotonic() + timeout
    fed: list[str] = []
    handed: Optional[Handover] = None
    # A window found later may have opened at any time after this: the last look that found none.
    no_window_since = time.monotonic()
    next_window_check = no_window_since + WINDOW_CHECK_SECONDS
    accepting = _send(process, prompt, progress)
    try:
        while process.poll() is None:
            if accepting and time.monotonic() >= next_window_check:
                looked_at = time.monotonic()
                next_window_check = looked_at + WINDOW_CHECK_SECONDS
                window = window_open()
                if window is None:
                    no_window_since = looked_at
                else:
                    with progress.lock:
                        unseen = (progress.turn_running or progress.background_tasks
                                  or (progress.turn_ended_at is not None and progress.turn_ended_at >= no_window_since))
                    handed = Handover(window_pid=window, turn_unseen=unseen)
                    print(f"reins: window {window} opened session {session}; the headless resume takes no more "
                          "callbacks and ends after its current turn", file=sys.stderr, flush=True)
                    accepting = False
                    _close_input(process, progress)
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(process.args, timeout)
            reason = progress.stalled(stall)
            if reason is not None:
                print(f"reins: ending the headless Claude resume of {session}: {reason}", file=sys.stderr, flush=True)
                raise Stalled(process.args, stall)
            if accepting:
                for callback_id, text in claude_session.claim_for_resume(root, session):
                    fed.append(callback_id)
                    accepting = _send(process, text, progress) and accepting
                if accepting and progress.finished():
                    accepting = False
                    _close_input(process, progress)
            time.sleep(POLL_SECONDS)
    finally:
        claude_session.release_resume_claims(root, fed)
        if handed is not None and handover is not None:
            reader.join(timeout=2.0)  # The last events may still be in the pipe when the process exits.
            with progress.lock:
                handed.result = progress.last_result
                if not progress.seen_event:
                    handed.turn_unseen = False  # Claude never started, so there was no turn.
            handover.append(handed)
    if process.returncode != 0 and not progress.seen_event:
        return None
    return int(process.returncode)
