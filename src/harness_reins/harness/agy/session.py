"""Google Antigravity CLI installation and conversation state on the local host.

Every agy process that has a conversation open, an interactive window or a
headless ``agy -p``, holds an exclusive flock on
``<app data>/presence/<conversation id>.lock``; the OS drops it when that
process exits. Everything here only reads or tries that lock and locates the
launcher; no conversation is opened or changed.
"""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NamedTuple, Optional

from .adapter import AGY_BIN_ENV

APP_DATA_ENV = "ANTIGRAVITY_APP_DATA_DIR"
CONVERSATION_ID_PATTERN = re.compile(r"[0-9a-fA-F-]{8,64}")
# Where agy's own installer puts it, and the usual package-manager places.
STANDALONE_LOCATIONS = ("~/.local/bin/agy", "/opt/homebrew/bin/agy", "/usr/local/bin/agy")

Which = Callable[..., Optional[str]]


class Executable(NamedTuple):
    path: str
    source: str  # configured | path | standalone


def _home(environment: Mapping[str, str]) -> Path:
    return Path(environment.get("HOME") or Path.home())


def app_data_dir(environment: Mapping[str, str]) -> Path:
    configured = environment.get(APP_DATA_ENV, "").strip()
    return Path(configured).expanduser() if configured else _home(environment) / ".gemini" / "antigravity-cli"


def config_dir(environment: Mapping[str, str]) -> Path:
    """agy's global customization root; its ``skills`` child holds user skills."""
    return _home(environment) / ".gemini" / "config"


def is_valid_conversation(conversation: str) -> bool:
    return bool(CONVERSATION_ID_PATTERN.fullmatch(conversation))


def presence_path(conversation: str, environment: Mapping[str, str]) -> Path | None:
    if not is_valid_conversation(conversation):
        return None
    return app_data_dir(environment) / "presence" / f"{conversation}.lock"


def conversation_exists(conversation: str, environment: Mapping[str, str]) -> bool:
    """Whether agy has this conversation on disk.

    ``agy --resume`` with an id it does not know only warns, then starts a new
    conversation and runs the prompt there; Reins must never resume one that is
    not here.
    """
    if not is_valid_conversation(conversation):
        return False
    return (app_data_dir(environment) / "conversations" / f"{conversation}.db").is_file()


# The Reins commands a headless resume runs on its own. Headless agy cannot ask anyone, so it denies a
# command no rule allows, which once kept a resumed conversation from replying to a message. Commands that
# start work (`run`, `agent`, `done`) are left out: follow-up jobs wait for the user's window. Rules match a
# command's prefix, so nothing is listed whose arguments reach further: `inbox --name` reads another session's
# inbox and `message alerts --clear` erases limit alerts; the envelope's own message id serves `message show`.
AGENT_COMMANDS = ("ack", "reply", "send", "wait", "message show", "status", "session list", "goal check",
                  "goal ack")


def settings_path(environment: Mapping[str, str]) -> Path:
    return app_data_dir(environment) / "settings.json"


def resume_settings(reins_commands: list[str], environment: Mapping[str, str]) -> str:
    """The ``--settings`` of a headless resume: the user's own permissions, plus rules for the Reins commands.

    Session settings replace keys, so the user's ``permissions`` (deny rules too) are copied in first.
    """
    permissions: dict[str, object] = {}
    try:
        current = json.loads(settings_path(environment).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = None
    if isinstance(current, dict) and isinstance(current.get("permissions"), dict):
        permissions = dict(current["permissions"])
    allow = [rule for rule in permissions.get("allow") or () if isinstance(rule, str)]
    for command in reins_commands:
        for mode in AGENT_COMMANDS:
            rule = f"command({command} {mode})"
            if rule not in allow:
                allow.append(rule)
    permissions["allow"] = allow
    return json.dumps({"permissions": permissions}, separators=(",", ":"))


def window_open(conversation: str, environment: Mapping[str, str]) -> bool:
    """Whether an agy process has this conversation open now.

    A shared lock that cannot be taken means agy holds its exclusive one. A
    shared lock that can be is released at once, so agy opening the
    conversation at that moment would at most retry its own lock.
    """
    path = presence_path(conversation, environment)
    if path is None or os.name == "nt":
        return False  # Windows has no flock; agy callbacks there are delivered only by `reins wait`.
    import fcntl

    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return False  # No presence file: nothing has opened this conversation since agy last cleaned up.
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                return True
            raise
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


def discover_executable(environment: Mapping[str, str], *, which: Which = shutil.which) -> Executable | None:
    """Find the ``agy`` launcher; an explicit configuration wins so a broken path fails visibly."""
    configured = environment.get(AGY_BIN_ENV, "").strip()
    if configured:
        expanded = Path(configured).expanduser()
        if expanded.is_absolute() or os.sep in configured:
            return Executable(str(expanded), "configured")
        found = which(configured)
        return Executable(found or configured, "configured")
    found = which("agy")
    if found:
        return Executable(found, "path")
    home = _home(environment)
    for location in STANDALONE_LOCATIONS:
        path = Path(location.replace("~", str(home), 1))
        if path.is_file() and os.access(path, os.X_OK):
            return Executable(str(path), "standalone")
    return None


def with_resolved_executable(environment: Mapping[str, str], *, which: Which = shutil.which) -> dict[str, str]:
    resolved = dict(environment)
    found = discover_executable(environment, which=which)
    if found is not None:
        resolved[AGY_BIN_ENV] = found.path
    return resolved
