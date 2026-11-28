"""Files for sessions and messages under link/: names, message records, thread counters and alerts.

Everything is written under one lock (see ``lock``), by whichever process admits
a message or registers a name. A message record never changes once written;
what happened to it is read from its callback, so there is one source of truth.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Iterator, Optional

import yaml

from ..core import messages
from . import layout
from . import records as queue_store

LOCK_ID = "link"
CONFIG_FILE_NAME = "config.yaml"


def lock(queue: Path) -> tuple[object, Path]:
    return queue_store.acquire_owner_lock(queue, LOCK_ID, blocking=True)


def _directory(queue: Path, name: str) -> Path:
    return layout.link_root(queue) / name


def _save(path: Path, record: dict[str, object]) -> None:
    queue_store.private_directory(path.parent.parent)
    queue_store.private_directory(path.parent)
    queue_store.write_request(path, record)


def load_record(path: Path) -> Optional[dict[str, object]]:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _records(directory: Path) -> Iterator[dict[str, object]]:
    for path in sorted(directory.glob("*.json")):
        record = load_record(path)
        if record is not None:
            yield record


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    queue_store.fsync_directory(path.parent)


def node_id(queue: Path) -> str:
    """This node's stable identity; call under the lock so two processes cannot create two."""
    path = layout.link_root(queue) / "node.json"
    record = load_record(path)
    if record is not None and isinstance(record.get("node_id"), str):
        return str(record["node_id"])
    node = messages.new_node_id()
    queue_store.private_directory(path.parent)
    queue_store.write_request(path, {"schema": messages.NODE_SCHEMA, "node_id": node})
    return node


def limits(queue: Path) -> dict[str, float]:
    """The defaults, overlaid by positive numbers from link/config.yaml; anything else is ignored."""
    values: dict[str, float] = dict(messages.DEFAULT_LIMITS)
    try:
        config = yaml.safe_load((layout.link_root(queue) / CONFIG_FILE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return values
    configured = config.get("limits") if isinstance(config, dict) else None
    if isinstance(configured, dict):
        for name in values:
            value = configured.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                values[name] = value
    return values


# Session names ------------------------------------------------------------------

def session_path(queue: Path, name: str) -> Path:
    return _directory(queue, "sessions") / f"{messages.validate_name(name)}.json"


def load_session(queue: Path, name: str) -> Optional[dict[str, object]]:
    return load_record(session_path(queue, name))


def save_session(queue: Path, record: dict[str, object]) -> None:
    _save(session_path(queue, str(record["name"])), record)


def remove_session(queue: Path, name: str) -> None:
    path = session_path(queue, name)
    _remove(path)
    _remove(path.with_suffix(".env"))


def sessions(queue: Path) -> list[dict[str, object]]:
    return list(_records(_directory(queue, "sessions")))


def save_session_environment(queue: Path, name: str, values: dict[str, str]) -> None:
    path = session_path(queue, name).with_suffix(".env")
    queue_store.private_directory(path.parent)
    queue_store.write_private_text(path, json.dumps(values, ensure_ascii=False))


def load_session_environment(queue: Path, name: str) -> Optional[dict[str, str]]:
    return load_record(session_path(queue, name).with_suffix(".env"))


# Messages -----------------------------------------------------------------------

def message_path(queue: Path, message_id: str) -> Path:
    if not messages.is_message_id(message_id):
        raise messages.Refused("invalid_record", f"{message_id!r} is not a message id")
    return _directory(queue, "messages") / f"{message_id}.json"


def _is_message(record: Optional[dict[str, object]]) -> bool:
    return (record is not None and record.get("schema") == messages.MESSAGE_SCHEMA
            and isinstance(record.get("local"), dict) and isinstance(record.get("from"), dict)
            and isinstance(record.get("to"), dict))


def load_message(queue: Path, message_id: str) -> Optional[dict[str, object]]:
    record = load_record(message_path(queue, message_id))
    return record if _is_message(record) else None


def save_message(queue: Path, message: dict[str, object]) -> None:
    _save(message_path(queue, str(message["id"])), message)


def remove_message(queue: Path, message_id: str) -> None:
    _remove(message_path(queue, message_id))


def all_messages(queue: Path) -> list[dict[str, object]]:
    # Oldest first: an ID starts with its time. A damaged record is skipped, never fatal to the tower.
    return [record for record in _records(_directory(queue, "messages")) if _is_message(record)]


# Thread counters and alerts -------------------------------------------------------

def thread_path(queue: Path, thread: str) -> Path:
    if not messages.is_thread_id(thread):
        raise messages.Refused("invalid_record", f"{thread!r} is not a thread id")
    return _directory(queue, "threads") / f"{thread}.json"


def load_thread(queue: Path, thread: str) -> dict[str, object]:
    return load_record(thread_path(queue, thread)) or {"id": thread, "count": 0, "recent": []}


def save_thread(queue: Path, record: dict[str, object]) -> None:
    _save(thread_path(queue, str(record["id"])), record)


def all_threads(queue: Path) -> list[dict[str, object]]:
    return [record for record in _records(_directory(queue, "threads")) if messages.is_thread_id(record.get("id"))]


def remove_thread(queue: Path, thread: str) -> None:
    _remove(thread_path(queue, thread))


def load_rates(queue: Path) -> dict[str, list[float]]:
    record = load_record(layout.link_root(queue) / "rates.json") or {}
    return {key: [t for t in value if isinstance(t, (int, float))] for key, value in record.items()
            if isinstance(value, list)}


def save_rates(queue: Path, rates: dict[str, list[float]]) -> None:
    path = layout.link_root(queue) / "rates.json"
    queue_store.private_directory(path.parent)
    queue_store.write_request(path, rates)


def save_alert(queue: Path, key: str, record: dict[str, object]) -> None:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    _save(_directory(queue, "alerts") / f"{digest}.json", record)


def alerts(queue: Path) -> list[dict[str, object]]:
    return list(_records(_directory(queue, "alerts")))


def clear_alerts(queue: Path) -> int:
    paths = list(_directory(queue, "alerts").glob("*.json"))
    for path in paths:
        _remove(path)
    return len(paths)


# Paired machines ------------------------------------------------------------------
# Every file below is written by the process the protocol names (docs/design/link-protocol.md, section 8).

def peer_path(queue: Path, alias: str) -> Path:
    return _directory(queue, "peers") / f"{messages.validate_name(alias)}.json"


def load_peer(queue: Path, alias: str) -> Optional[dict[str, object]]:
    record = load_record(peer_path(queue, alias))
    return record if record is not None and record.get("schema") == "reins.peer/1" else None


def save_peer(queue: Path, record: dict[str, object]) -> None:
    _save(peer_path(queue, str(record["alias"])), record)


def remove_peer(queue: Path, alias: str) -> None:
    _remove(peer_path(queue, alias))
    _remove(names_path(queue, alias))
    _remove(status_path(queue, alias))


def peers(queue: Path) -> list[dict[str, object]]:
    return [record for record in _records(_directory(queue, "peers")) if record.get("schema") == "reins.peer/1"]


def peer_by_node(queue: Path, node: str) -> Optional[dict[str, object]]:
    return next((record for record in peers(queue) if record.get("node") == node), None)


def names_path(queue: Path, alias: str) -> Path:
    return _directory(queue, "names") / f"{messages.validate_name(alias)}.json"


def load_names(queue: Path, alias: str) -> list[dict[str, object]]:
    record = load_record(names_path(queue, alias)) or {}
    names = record.get("names")
    return [item for item in names if isinstance(item, dict)] if isinstance(names, list) else []


def save_names(queue: Path, alias: str, record: dict[str, object]) -> None:
    _save(names_path(queue, alias), record)


def status_path(queue: Path, alias: str) -> Path:
    return _directory(queue, "status") / f"{messages.validate_name(alias)}.json"


def load_status(queue: Path, alias: str) -> dict[str, object]:
    return load_record(status_path(queue, alias)) or {}


def save_status(queue: Path, alias: str, record: dict[str, object]) -> None:
    _save(status_path(queue, alias), record)


def sent_path(queue: Path, message_id: str) -> Path:
    if not messages.is_message_id(message_id):
        raise messages.Refused("invalid_record", f"{message_id!r} is not a message id")
    return _directory(queue, "sent") / f"{message_id}.json"


def load_sent(queue: Path, message_id: str) -> Optional[dict[str, object]]:
    record = load_record(sent_path(queue, message_id))
    return record if record is not None and isinstance(record.get("sent"), dict) else None


def save_sent(queue: Path, record: dict[str, object]) -> None:
    _save(sent_path(queue, str(record["id"])), record)


def remove_sent(queue: Path, message_id: str) -> None:
    _remove(sent_path(queue, message_id))


def all_sent(queue: Path) -> list[dict[str, object]]:
    return [record for record in _records(_directory(queue, "sent")) if isinstance(record.get("sent"), dict)]


def _spool(queue: Path, kind: str, alias: str) -> Path:
    return _directory(queue, kind) / messages.validate_name(alias)


def outbox(queue: Path, alias: str) -> Path:
    return _spool(queue, "outbox", alias)


def inbound(queue: Path, alias: str) -> Path:
    return _spool(queue, "inbound", alias)


def quarantine(queue: Path, alias: str) -> Path:
    return _spool(queue, "quarantine", alias)


def spool_write(directory: Path, name: str, data: bytes) -> None:
    """One file into an outbox, inbound or quarantine directory, written atomically."""
    queue_store.private_directory(directory.parent)
    queue_store.private_directory(directory)
    # Inbound and outbox files are checked JSON; a quarantined one may be anything, and must not fail to save.
    queue_store.write_private_text(directory / name, data.decode("utf-8", "replace"))


def spool_files(directory: Path, limit: Optional[int] = None) -> list[Path]:
    try:
        found = sorted(path for path in directory.iterdir() if path.suffix == ".json" and not path.name.startswith("."))
    except OSError:
        return []
    return found[:limit] if limit is not None else found


def spool_remove(path: Path) -> None:
    _remove(path)


def mailbox_root(queue: Path, pair: str) -> Path:
    """Where this node keeps the mailbox of a pair it hosts over SSH."""
    if not re.fullmatch(r"p-[0-9a-f]{16}", pair):
        raise messages.Refused("invalid_record", f"{pair!r} is not a pair id")
    return _directory(queue, "mailboxes") / pair


def reported_path(queue: Path, local_id: str) -> Path:
    return _directory(queue, "reported") / f"{local_id}.json"


def load_reported(queue: Path, local_id: str) -> Optional[str]:
    record = load_record(reported_path(queue, local_id)) or {}
    return record.get("state") if isinstance(record.get("state"), str) else None


def save_reported(queue: Path, local_id: str, state: str) -> None:
    _save(reported_path(queue, local_id), {"state": state})


def remove_reported(queue: Path, local_id: str) -> None:
    _remove(reported_path(queue, local_id))
