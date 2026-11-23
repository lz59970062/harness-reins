"""Pi sessions on this machine, for the Reins panel.

Pi has no daemon and no list command. Its sessions are JSONL files in a
documented place and format (Pi docs: session-format.md): one file per
session under ``<profile>/sessions/--<cwd>--/<timestamp>_<id>.jsonl``, a
``session`` header line with the id and directory, and ``session_info`` lines
carrying the display name; without a name, the panel shows the first thing the
user wrote, as Pi's own session picker does. Whether a Pi process has a session open comes from
the owner record the Reins Pi extension keeps for it (channel.py); a Pi
started without that extension cannot be seen. Nothing here writes a session.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ...core import sessions
from ...core.sessions import SessionView
from . import channel

LIST_LIMIT = 100
_FILE_ID = re.compile(r"_([0-9A-Za-z-]+)\.jsonl\Z")
_NAME_MARK = b'"session_info"'
_USER_MARK = b'"user"'


def profile(environment: Mapping[str, str]) -> Path:
    return Path(environment.get(channel.PROFILE_ENV) or "~/.pi/agent").expanduser()


def handle(session: str) -> str:
    """What a Reins pane is named by: the tail of the id in the file name (Pi ids are UUIDv7, whose
    first digits are a shared timestamp)."""
    match = _FILE_ID.search(os.path.basename(session))
    ident = match.group(1) if match else os.path.basename(session)
    return ident.replace("-", "")[-8:]


@dataclass
class _Read:
    size: int
    mtime: int
    header: Optional[dict]
    name: Optional[str]
    first: Optional[str]  # The first thing the user wrote.


# Session files only grow; names are read from what was added since the last look.
_cache: dict[str, _Read] = {}


def _first_text(message: object) -> Optional[str]:
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    content = message.get("content")
    text = content if isinstance(content, str) else " ".join(
        str(part.get("text") or "") for part in content or () if isinstance(part, dict) and part.get("type") == "text")
    text = " ".join(text.split())
    return text[:80] or None


def _scan(stream, name: Optional[str], first: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    for line in stream:
        if _NAME_MARK in line or (first is None and _USER_MARK in line):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("type") == "session_info":
                value = entry.get("name")
                name = value.strip() if isinstance(value, str) and value.strip() else None
            elif first is None and entry.get("type") == "message":
                first = _first_text(entry.get("message"))
    return name, first


def describe(path: Path) -> Optional[tuple[dict, Optional[str]]]:
    """(header, title) of one session file, or None when it is not a Pi session. The title is its name, else
    the first thing the user wrote."""
    try:
        status = path.stat()
    except OSError:
        return None
    key = str(path)
    known = _cache.get(key)
    if known is not None and known.size == status.st_size and known.mtime == status.st_mtime_ns:
        return (known.header, known.name or known.first) if known.header is not None else None
    try:
        with path.open("rb") as stream:
            if known is not None and known.header is not None and status.st_size > known.size:
                stream.seek(known.size)
                header = known.header
                name, first = _scan(stream, known.name, known.first)
            else:
                try:
                    header = json.loads(stream.readline(65536))
                except ValueError:
                    header = None
                if not (isinstance(header, dict) and header.get("type") == "session" and isinstance(header.get("id"), str)):
                    header = None
                name, first = _scan(stream, None, None) if header is not None else (None, None)
            size = stream.tell() if header is not None else status.st_size
    except OSError:
        return None
    _cache[key] = _Read(size=size, mtime=status.st_mtime_ns, header=header, name=name, first=first)
    return (header, name or first) if header is not None else None


def session_files(environment: Mapping[str, str], limit: int = LIST_LIMIT) -> list[Path]:
    root = profile(environment) / "sessions"
    try:
        found = [(path.stat().st_mtime, path) for path in root.glob("*/*.jsonl") if path.is_file()]
    except OSError:
        return []
    found.sort(key=lambda item: -item[0])
    return [path for _, path in found[:limit]]


def _alive(pid: int) -> bool:
    # The panel runs where Zellij does, never on Windows, where os.kill(pid, 0) sends Ctrl+C to the console.
    if os.name == "nt":
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def live_owner(session: str, environment: Mapping[str, str]) -> Optional[int]:
    """The pid of the Pi process that has this session open, as its Reins extension recorded."""
    root = Path(environment.get(channel.CHANNEL_ROOT_ENV) or profile(environment) / "harness-reins" / "channels")
    try:
        owner = channel.read_record(channel.channel_dir(session, root.expanduser()) / "owner.json")
    except (OSError, ValueError):
        return None
    pid = owner.get("owner_pid")
    if owner.get("state") not in ("active", "reserved") or type(pid) is not int or pid <= 0:
        return None
    return pid if _alive(pid) else None


def _view(path: Path, header: Mapping[str, object], name: Optional[str], environment: Mapping[str, str]) -> SessionView:
    session = channel.canonical_session(str(path))
    pid = live_owner(session, environment)
    stamp = None
    try:
        stamp = path.stat().st_mtime
    except OSError:
        pass
    cwd = header.get("cwd") if isinstance(header.get("cwd"), str) else None
    return SessionView(agent="pi", session=session,
                       state=sessions.FOREIGN if pid else sessions.CLOSED,
                       host=sessions.HOST_TERMINAL if pid else None, name=name, cwd=cwd, pid=pid,
                       handle=handle(session), started_at=stamp)


def session_views(environment: Mapping[str, str]) -> list[SessionView]:
    """The newest Pi sessions. One with a live Pi process shows as open elsewhere; the panel marks the ones
    in its own panes as attached."""
    views = []
    for path in session_files(environment):
        described = describe(path)
        if described is not None:
            views.append(_view(path, described[0], described[1], environment))
    return views


def session_view(session: str, environment: Mapping[str, str]) -> Optional[SessionView]:
    try:
        path = Path(channel.canonical_session(session))
    except ValueError:
        return None
    described = describe(path)
    return _view(path, described[0], described[1], environment) if described is not None else None


def open_command(session: str, cwd: str) -> list[str]:
    """Pi on this session under Reins' writer lease, with the callback extension (`reins pi`)."""
    from ...runtime import worker_command

    return worker_command("pi", "--cwd", cwd, "--session", session)
