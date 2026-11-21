"""reins owner: which session started a Reins task, by its name in the Reins address book.

A task's record keeps the session it calls back (``origin``). The name is read
from the address book when asked, so a session renamed later shows its current name.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Optional

from ..harness import get_agent
from ..store import layout, link
from .logs import RUNNING_STATES, find_task, read_task

SCHEMA = "reins.owner/1"
COMMAND_CELLS = 160
Names = dict[tuple[str, str], str]


def address_book(queue: Path) -> Names:
    return {(str(record.get("harness")), str(record.get("session"))): str(record["name"])
            for record in link.sessions(queue) if record.get("name")}


def _modified(path: object) -> Optional[float]:
    try:
        return os.stat(str(path)).st_mtime if path else None
    except OSError:
        return None


def owner_session(task: dict[str, object], names: Names) -> Optional[dict[str, object]]:
    """The session a task calls back, with its Reins name if it has one."""
    origin = task.get("origin") if isinstance(task.get("origin"), dict) else {}
    target = task.get("target") if isinstance(task.get("target"), dict) else {}
    harness = origin.get("harness") or task.get("agent")
    session = origin.get("session") or target.get("session")
    if not harness or not session:
        return None
    return {"harness": str(harness), "id": str(session), "name": names.get((str(harness), str(session))),
            "cwd": origin.get("cwd") or task.get("cwd")}


def describe(task: dict[str, object], names: Names) -> dict[str, object]:
    return {
        "id": task.get("id"),
        "name": task.get("task"),
        "state": task.get("state"),
        "command": task.get("command"),
        "cwd": task.get("cwd"),
        "started_at": task.get("started_at"),
        "completed_at": task.get("completed_at"),
        "log_path": task.get("log_path"),
        "log_written_at": _modified(task.get("log_path")),
        "session": owner_session(task, names),
    }


def active(tasks_root: Path, names: Names) -> list[dict[str, object]]:
    """Tasks that have not ended, the one whose log has been quiet longest first."""
    found = [describe(task, names) for task in (read_task(path.parent) for path in sorted(tasks_root.glob("*/task.json")))
             if task.get("state") in RUNNING_STATES]
    return sorted(found, key=lambda item: item["log_written_at"] or item["started_at"] or 0)


def age(seconds: object, now: float) -> str:
    """How long ago, in words: '47 min', '3 h 12 min', '2 days'."""
    if not isinstance(seconds, (int, float)):
        return "unknown"
    elapsed = max(0, int(now - seconds))
    if elapsed < 60:
        return f"{elapsed} s"
    minutes = elapsed // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    return f"{hours // 24} days"


def _harness(name: object) -> str:
    try:
        return get_agent(str(name)).display_name
    except (KeyError, ValueError):
        return str(name)


def _short(session: str) -> str:
    return session if "/" in session or len(session) <= 12 else session[:8]


def _clip(text: object, cells: int = COMMAND_CELLS) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= cells else text[: cells - 1] + "…"


def _who(session: Optional[dict[str, object]]) -> str:
    if not session:
        return "unknown (the task records no session)"
    name = f"@{session['name']}" if session.get("name") else "(no name)"
    return f"{_harness(session['harness'])} session {_short(str(session['id']))}  {name}"


def summary(task: dict[str, object], now: float) -> str:
    """What the task is and which session started it."""
    state = str(task.get("state") or "unknown")
    if state == "running":
        heading = f"running for {age(task.get('started_at'), now)}"
    elif task.get("completed_at"):
        heading = f"{state}, ended {age(task.get('completed_at'), now)} ago"
    else:
        heading = state
    lines = [f"Task {task['id']}  {heading}   {_clip(task.get('name'), 80)}",
             f"  Command  {_clip(task.get('command'))}",
             f"  Folder   {task.get('cwd')}"]
    if task.get("log_path"):
        written = task.get("log_written_at")
        lines.append(f"  Log      {f'last written {age(written, now)} ago' if written else 'not written yet'}")
    lines.append(f"Started by {_who(task.get('session'))}")
    return "\n".join(lines)


def render(task: dict[str, object], now: float, queue_dir: Optional[str] = None) -> str:
    """The summary, then the commands that reach the task and its owner, on the same queue."""
    queue = f" --queue-dir {shlex.quote(queue_dir)}" if queue_dir else ""
    session = task.get("session")
    lines = [summary(task, now), "", f"  Log:            reins log {task['id']}{queue}"]
    if isinstance(session, dict) and session.get("name"):
        lines.append(f"  Message owner:  reins send {session['name']}{queue} -m \"...\"")
    lines.append(f"  Open it:        reins tui {task['id']}{queue}")
    return "\n".join(lines)


def render_active(tasks: list[dict[str, object]], now: float) -> str:
    if not tasks:
        return "No Reins tasks that have not ended."
    rows = [("TASK", "STATE", "RUNNING", "LOG QUIET", "STARTED BY", "NAME")]
    for task in tasks:
        session = task.get("session")
        owner = "unknown"
        if isinstance(session, dict):
            owner = (f"@{session['name']}" if session.get("name") else _short(str(session["id"]))) \
                + f" ({session['harness']})"
        running = age(task.get("started_at"), now) if task.get("state") == "running" else "-"
        quiet = age(task.get("log_written_at"), now) if task.get("log_written_at") else "-"
        rows.append((str(task["id"]), str(task.get("state")), running, quiet, owner, _clip(task.get("name"), 60)))
    widths = [max(len(row[column]) for row in rows) for column in range(5)]
    return "\n".join("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row[:5])) + "  " + row[5]
                     for row in rows)


def owner(args: argparse.Namespace) -> int:
    queue = layout.queue_dir(args)
    tasks_root = layout.managed_tasks_root(queue)
    names = address_book(queue)
    now = time.time()
    if not args.task_id:
        tasks = active(tasks_root, names)
        print(json.dumps({"schema": SCHEMA, "tasks": tasks}, indent=2) if args.json else render_active(tasks, now))
        return 0
    directory, problem = find_task(tasks_root, args.task_id)
    record = read_task(directory) if directory is not None else {}
    if not record:
        problem = problem or f"task {args.task_id} has no readable record"
        if args.json:
            print(json.dumps({"schema": SCHEMA, "tasks": [], "error": problem}, indent=2))
        else:
            print(f"reins: {problem}", file=sys.stderr)
        return 1
    task = describe(record, names)
    print(json.dumps({"schema": SCHEMA, "tasks": [task]}, indent=2) if args.json else render(task, now, args.queue_dir))
    return 0
