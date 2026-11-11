"""reins log: print a task's output, like ``tail -f``, until the task ends."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import IO

from ..core import ids
from ..store import layout

# A task in one of these states may still write output; any other state is final.
RUNNING_STATES = frozenset({"submitting", "submitted", "launching", "running"})
POLL_SECONDS = 0.5
_BLOCK = 65536


def _read_task(directory: Path) -> dict[str, object]:
    try:
        task = json.loads((directory / "task.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return task if isinstance(task, dict) else {}


def find_task(tasks_root: Path, wanted: str) -> tuple[Path | None, str]:
    """The task directory for an id, a callback or attempt id, or a unique prefix."""
    wanted = ids.root_of(wanted.strip())
    if not wanted or not ids.SAFE.fullmatch(wanted):
        return None, f"not a task id: {wanted!r}"
    exact = tasks_root / wanted
    if (exact / "task.json").is_file():
        return exact, ""
    matches = sorted(path for path in tasks_root.glob(f"{wanted}*") if (path / "task.json").is_file())
    if len(matches) == 1:
        return matches[0], ""
    if not matches:
        return None, f"no task {wanted} under {tasks_root}"
    names = ", ".join(path.name for path in matches[:5])
    more = "" if len(matches) <= 5 else f" and {len(matches) - 5} more"
    return None, f"{wanted} matches several tasks: {names}{more}"


def tail_offset(path: Path, lines: int) -> int:
    """Where the last ``lines`` lines of ``path`` start; 0 when it is shorter."""
    if lines <= 0:
        return path.stat().st_size
    with path.open("rb") as handle:
        end = handle.seek(0, 2)
        # A final newline ends the last line rather than starting an empty one.
        if end:
            handle.seek(end - 1)
            if handle.read(1) == b"\n":
                end -= 1
        position, found = end, 0
        while position > 0:
            start = max(0, position - _BLOCK)
            handle.seek(start)
            block = handle.read(position - start)
            index = len(block)
            while True:
                index = block.rfind(b"\n", 0, index)
                if index < 0:
                    break
                found += 1
                if found == lines:
                    return start + index + 1
            position = start
    return 0


def _copy(path: Path, position: int, out: IO[bytes]) -> int:
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            if size < position:  # Truncated or replaced: start over.
                position = 0
            handle.seek(position)
            while True:
                chunk = handle.read(_BLOCK)
                if not chunk:
                    break
                out.write(chunk)
                position += len(chunk)
    except FileNotFoundError:
        return position
    out.flush()
    return position


def _binary_stdout() -> IO[bytes]:
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        sys.stdout.flush()
        return buffer

    class _Text:
        def write(self, chunk: bytes) -> None:
            sys.stdout.write(chunk.decode("utf-8", "replace"))

        def flush(self) -> None:
            sys.stdout.flush()

    return _Text()  # type: ignore[return-value]


def _ending(task: dict[str, object]) -> str:
    state = str(task.get("state") or "unknown")
    code = task.get("exit_code")
    return state if code is None else f"{state}, exit code {code}"


def log(args: argparse.Namespace) -> int:
    tasks_root = layout.managed_tasks_root(layout.queue_dir(args))
    directory, problem = find_task(tasks_root, args.task_id)
    if directory is None:
        print(f"reins: error: {problem}", file=sys.stderr)
        return 2
    task = _read_task(directory)
    path = Path(str(task.get("log_path") or directory / "attempt-1.log")).expanduser()
    out = _binary_stdout()
    follow = not args.no_follow
    position = None
    try:
        while True:
            running = task.get("state") in RUNNING_STATES
            if position is None and path.exists():
                position = 0 if getattr(args, "all", False) else tail_offset(path, args.lines)
            if position is not None:
                position = _copy(path, position, out)
            if not follow or not running:
                break
            time.sleep(POLL_SECONDS)
            task = _read_task(directory) or task
    except KeyboardInterrupt:
        return 130
    if position is None:
        print(f"reins: task {directory.name} has no output yet ({_ending(task)}); log: {path}", file=sys.stderr)
        return 1
    if follow:
        print(f"reins: task {directory.name} ended: {_ending(task)}", file=sys.stderr)
    return 0
