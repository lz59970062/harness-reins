"""reins stop: stop a Reins task on purpose, so the session that started it is told not to start it again."""
from __future__ import annotations

import argparse
import sys
import time
from typing import Callable

from ..engine import tasks as managed_tasks
from ..owners import OwnerState
from ..owners.linux import SystemdUserBackend, stop_marked_processes
from ..store import layout
from . import owner as owner_command
from .logs import RUNNING_STATES, find_task, read_task

BACKENDS = ("systemd-user", "screen")
Marked = Callable[[list[str]], bool]


def task_marker(task: dict[str, object]) -> Marked:
    """Whether a process belongs to this task, from the environment every process it starts inherits.

    REINS_TASK_ID decides when it is set: a task started from inside another one has its own.
    Tasks started before it existed fall back to screen's STY or the systemd unit's INVOCATION_ID.
    """
    own = f"{managed_tasks.TASK_ID_ENV}={task['id']}"
    prefix = f"{managed_tasks.TASK_ID_ENV}="
    invocation = f"INVOCATION_ID={task['owner_invocation_id']}" if task.get("owner_invocation_id") else None
    screen = f".{task['screen_session']}" if task.get("screen_session") else None

    def marked(lines: list[str]) -> bool:
        if own in lines:
            return True
        if any(line.startswith(prefix) for line in lines):
            return False
        return any(line == invocation or (screen and line.startswith("STY=") and line.endswith(screen))
                   for line in lines)
    return marked


def _confirm(args: argparse.Namespace) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print("reins: not a terminal; add --yes to stop it", file=sys.stderr)
        return False
    try:
        return input("Stop it? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def stop(args: argparse.Namespace) -> int:
    queue = layout.queue_dir(args)
    tasks_root = layout.managed_tasks_root(queue)
    directory, problem = find_task(tasks_root, args.task_id)
    task = read_task(directory) if directory is not None else {}
    if not task:
        print(f"reins: {problem or f'task {args.task_id} has no readable record'}", file=sys.stderr)
        return 1
    task_id = str(task["id"])
    backend = str(task.get("execution_backend") or "screen")
    if not sys.platform.startswith("linux") or backend not in BACKENDS:
        print(f"reins: reins stop works for Linux tasks run by systemd or screen; task {task_id} uses {backend}",
              file=sys.stderr)
        return 1
    print(owner_command.summary(owner_command.describe(task, owner_command.address_book(queue)), time.time()))
    print()
    if not _confirm(args):
        return 1

    active = task.get("state") in RUNNING_STATES
    if active:
        managed_tasks.write_stop_request(queue, task_id, args.message)
    unit_stopped = True
    owner = task.get("execution_owner")
    if active and backend == "systemd-user" and isinstance(owner, str) and owner:
        unit = SystemdUserBackend()
        unit_stopped = unit.stop(owner) or unit.probe(owner) == OwnerState.ABSENT  # already gone counts
    signalled, left = stop_marked_processes(task_marker(task), grace=args.grace)

    if active:
        state = managed_tasks.settle_stopped_task(queue, task_id)
        ended = f" ({signalled} process(es) ended)" if signalled else ""
        print(f"Stopped task {task_id}{ended}." if not left and unit_stopped
              else f"Stopping task {task_id} is incomplete.")
        if state == "interrupted":
            print("Its session gets a callback saying you stopped it on purpose and not to start it again.")
        else:
            print("The Reins daemon records it as stopped shortly; its session is then told not to start it again.")
    elif signalled or left:
        print(f"Task {task_id} had already ended; stopped {signalled} process(es) it left running.")
    else:
        print(f"Task {task_id} has already ended and left nothing running.")
    if not unit_stopped:
        print(f"reins: systemd did not stop {owner}; run reins stop {task_id} again", file=sys.stderr)
    if left:
        print(f"reins: still running: {' '.join(map(str, left))}; run reins stop {task_id} again", file=sys.stderr)
    return 0 if unit_stopped and not left else 2
