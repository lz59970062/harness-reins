"""Antigravity CLI conversations on this machine, for the Reins panel.

agy keeps a summary row per conversation in ``<app data>/conversation_summaries.db``
(SQLite): id, title, workspace URIs, last change and nesting depth. The panel
reads only that table, read-only, and never a conversation's own database,
which holds the model's reasoning. Whether a process has a conversation open
comes from its presence lock (session.window_open). A new ``agy`` creates its
conversation at once, before the first prompt, so a pane started here is found
as the one new conversation in its directory that a process holds open.
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

from ...core import sessions
from ...core.sessions import SessionView
from . import session as agy_session

LIST_LIMIT = 100
SUMMARIES_FILE = "conversation_summaries.db"


def handle(conversation: str) -> str:
    """What a Reins pane is named by: the id's first eight hex digits."""
    return conversation.replace("-", "")[:8].lower()


def _path_of(uris: object) -> Optional[str]:
    """The first ``file://`` workspace of a summary row, as a local path."""
    import json

    try:
        values = json.loads(uris) if isinstance(uris, str) else None
    except ValueError:
        return None
    for value in values if isinstance(values, list) else ():
        parsed = urlparse(value) if isinstance(value, str) else None
        if parsed is not None and parsed.scheme == "file" and parsed.path:
            return unquote(parsed.path)
    return None


def _timestamp(value: object) -> Optional[float]:
    """agy writes times like ``2026-11-27 16:20:39.449807923+00:00``; a new conversation has year 1."""
    if not isinstance(value, str) or value.startswith("0001-"):
        return None
    text = value.replace(" ", "T", 1)
    head, _, rest = text.partition(".")
    if rest:  # Python takes at most six fraction digits.
        digits = "".join(ch for ch in rest if ch.isdigit())
        zone = rest[len(digits):]
        text = f"{head}.{digits[:6]}{zone}"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def summaries(environment: Mapping[str, str], limit: int = LIST_LIMIT) -> list[dict[str, object]]:
    """The newest top-level conversations' summary rows; [] when agy's table cannot be read."""
    path = agy_session.app_data_dir(environment) / SUMMARIES_FILE
    if not path.is_file():
        return []
    try:
        connection = sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return []
    try:
        rows = connection.execute(
            "SELECT conversation_id, title, workspace_uris, last_modified_time FROM conversation_summaries "
            "WHERE nesting_depth = 0 AND parent_conversation_id = '' "
            "ORDER BY last_modified_time DESC LIMIT ?", (limit,)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    found = []
    for conversation, title, uris, modified in rows:
        if isinstance(conversation, str) and agy_session.is_valid_conversation(conversation):
            found.append({"id": conversation, "title": title.strip() if isinstance(title, str) else "",
                          "cwd": _path_of(uris), "modified": _timestamp(modified)})
    return found


def _view(row: Mapping[str, object], environment: Mapping[str, str]) -> SessionView:
    conversation = str(row["id"])
    open_now = agy_session.window_open(conversation, environment)
    cwd = row.get("cwd") if isinstance(row.get("cwd"), str) else None
    return SessionView(agent="agy", session=conversation,
                       state=sessions.FOREIGN if open_now else sessions.CLOSED,
                       host=sessions.HOST_TERMINAL if open_now else None,
                       name=str(row.get("title") or "") or None, cwd=cwd, handle=handle(conversation),
                       started_at=row.get("modified") if isinstance(row.get("modified"), float) else None)


def session_views(environment: Mapping[str, str]) -> list[SessionView]:
    """The newest conversations. One an agy process holds open shows as open elsewhere; the panel marks the
    ones in its own panes as attached."""
    return [_view(row, environment) for row in summaries(environment)]


def session_view(conversation: str, environment: Mapping[str, str]) -> Optional[SessionView]:
    if not agy_session.conversation_exists(conversation, environment):
        return None
    row = next((row for row in summaries(environment, limit=1000) if row["id"] == conversation), None)
    return _view(row or {"id": conversation}, environment)


def conversation_ids(environment: Mapping[str, str]) -> set[str]:
    """Every conversation agy has on disk now, so a new one is never taken for an old one."""
    root = agy_session.app_data_dir(environment) / "conversations"
    try:
        return {path.stem for path in root.glob("*.db") if agy_session.is_valid_conversation(path.stem)}
    except OSError:
        return set()


def started_in(directory: str, before: set[str], environment: Mapping[str, str]) -> Optional[str]:
    """The one conversation that is new since ``before``, held open by a process and in ``directory``.

    None when there is none yet, or more than one: two agy started here at once cannot be told apart.
    """
    new = conversation_ids(environment) - before
    if not new:
        return None
    rows = {str(row["id"]): row for row in summaries(environment, limit=1000)}
    here = os.path.realpath(directory)
    found = [conversation for conversation in new
             if agy_session.window_open(conversation, environment)
             and isinstance(rows.get(conversation, {}).get("cwd"), str)
             and os.path.realpath(str(rows[conversation]["cwd"])) == here]
    return found[0] if len(found) == 1 else None


def open_command(conversation: str, environment: Mapping[str, str]) -> list[str]:
    """agy on this conversation in a pane; its process is the conversation's one writer."""
    found = agy_session.discover_executable(environment)
    return [found.path if found is not None else "agy", "--resume", conversation]


def new_command(environment: Mapping[str, str], prompt: Optional[str] = None) -> list[str]:
    """A new agy conversation in a pane, starting on ``prompt`` when one is given."""
    found = agy_session.discover_executable(environment)
    command = [found.path if found is not None else "agy"]
    return [*command, "--prompt-interactive", prompt] if prompt else command
