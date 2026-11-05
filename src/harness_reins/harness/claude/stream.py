"""Keep a headless Claude Code resume open, so later callbacks reach it while it runs.

A second ``claude -p --resume`` beside a running one would fork the transcript,
so a session has one process and later callbacks go into it. ``--input-format
stream-json`` is Claude Code's own way to do that: a message written to stdin
arrives between model steps, or with the result of a tool that is still running.
The channel does not work here, since ``claude -p`` drops its events silently.

Claude does not exit while stdin is open, so the worker closes it once the last
turn has ended and no background task can start another one.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from . import session as claude_session

ARGUMENTS = ("--input-format", "stream-json", "--output-format", "stream-json", "--verbose")
POLL_SECONDS = 0.5
# `result` ends a turn, and a task's notification can start the next one a moment later.
QUIET_SECONDS = 5.0
# A CLI that says nothing gets its stdin closed: it still handles what it has, but takes no more.
FIRST_EVENT_SECONDS = 60.0


class _Progress:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.started = time.monotonic()
        self.last_event = self.started
        self.seen_event = False
        self.turn_running = True
        self.background_tasks = False

    def record(self, event: dict[str, object]) -> None:
        kind, subtype = event.get("type"), event.get("subtype")
        with self.lock:
            self.seen_event = True
            self.last_event = time.monotonic()
            if kind == "result":
                self.turn_running = False
            elif kind in ("assistant", "user") or (kind == "system" and subtype == "init"):
                self.turn_running = True
            elif kind == "system" and subtype == "background_tasks_changed":
                self.background_tasks = bool(event.get("tasks"))

    def message_sent(self) -> None:
        with self.lock:
            self.turn_running = True
            self.last_event = time.monotonic()

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


def supervise(process: subprocess.Popen[str], prompt: str, root: Path, session: str,
              timeout: Optional[float]) -> Optional[int]:
    """Deliver ``prompt`` and every later callback, then return the exit code.

    None means Claude refused the streaming flags without starting, so nothing
    was delivered and the caller may use a plain prompt.
    """
    progress = _Progress()
    threading.Thread(target=_read_events, args=(process.stdout, progress), daemon=True).start()
    deadline = None if timeout is None else time.monotonic() + timeout
    fed: list[str] = []
    accepting = _send(process, prompt, progress)
    try:
        while process.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(process.args, timeout)
            if accepting:
                for callback_id, text in claude_session.claim_for_resume(root, session):
                    fed.append(callback_id)
                    accepting = _send(process, text, progress) and accepting
                if accepting and progress.finished():
                    accepting = False
                    try:
                        process.stdin.close()
                    except OSError:
                        pass
            time.sleep(POLL_SECONDS)
    finally:
        claude_session.release_resume_claims(root, fed)
    if process.returncode != 0 and not progress.seen_event:
        return None
    return int(process.returncode)
