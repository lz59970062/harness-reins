"""Codex sessions on the shared local app-server daemon, for the Reins panel and callback routing.

Codex 0.166 keeps one app-server daemon per user (`codex app-server daemon`,
socket under ``$CODEX_HOME/app-server-control``). A thread opened with
``codex resume --remote unix://<socket> <id>`` loads on that daemon and stays
loaded after its window closes, much like a Claude background session; the
daemon unloads idle threads by itself (measured, docs/design/sessions-tui.md
V16-V21). Everything here asks the daemon over its public JSON-RPC protocol,
plus, on Linux, which other Codex processes hold a thread's rollout file open:
a Codex window that is not on the daemon writes its thread itself, and opening
that thread on the daemon too would give it two writers.
"""
from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Optional

from ...core import sessions
from ...core.sessions import SessionView
from . import app_server

HOST_DAEMON = "codex-daemon"
LIST_LIMIT = 100
RPC_TIMEOUT_SECONDS = 5.0
CODEX_BIN_ENV = "REINS_CODEX_BIN"
_ROLLOUT = re.compile(r"rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl\Z")

Connect = Callable[[Path, float], "app_server.AppServerConnection"]


def codex_home(environment: Mapping[str, str]) -> Path:
    return Path(environment.get("CODEX_HOME") or "~/.codex").expanduser()


def socket_path(environment: Mapping[str, str]) -> Path:
    return codex_home(environment) / "app-server-control" / "app-server-control.sock"


def executable(environment: Mapping[str, str]) -> Optional[str]:
    configured = environment.get(CODEX_BIN_ENV, "").strip()
    if configured:
        return configured
    return shutil.which("codex", path=environment.get("PATH"))


def handle(thread: str) -> str:
    """What a Reins pane is named by. Codex ids are UUIDv7, whose first digits are a timestamp that threads
    started within a minute share, so the random tail tells them apart."""
    return thread.replace("-", "")[-8:]


def resume_command(thread: str, environment: Mapping[str, str]) -> list[str]:
    """Codex's own window on the daemon's copy of the thread; it never loads a second one."""
    return [executable(environment) or "codex", "resume", "--remote", f"unix://{socket_path(environment)}", thread]


def new_command(cwd: str, environment: Mapping[str, str]) -> list[str]:
    """A new thread, started by Codex's own window on the daemon. (A thread started over the protocol with
    no turn has an empty rollout that `codex resume` refuses: V19.)"""
    return [executable(environment) or "codex", "--remote", f"unix://{socket_path(environment)}", "-C", cwd]


def start_daemon_command(environment: Mapping[str, str]) -> list[str]:
    return [executable(environment) or "codex", "app-server", "daemon", "start"]


class Daemon:
    """One connection to the daemon; use it as a context manager."""

    def __init__(self, environment: Mapping[str, str], *, connect: Optional[Connect] = None,
                 timeout: float = RPC_TIMEOUT_SECONDS) -> None:
        self.path = socket_path(environment)
        self.connection = (connect or app_server.AppServerConnection)(self.path, timeout)

    def __enter__(self) -> "Daemon":
        from ... import __version__

        self.connection.connect()
        try:
            self.connection.request("initialize", {
                "clientInfo": {"name": "harness-reins", "version": __version__},
                "capabilities": {"experimentalApi": True},
            })
            self.connection.notify("initialized", {})
        except BaseException:
            self.connection.close()
            raise
        return self

    def __exit__(self, *_exc: object) -> None:
        self.connection.close()

    def threads(self, limit: int = LIST_LIMIT, *, strict: bool = False) -> list[dict]:
        """The newest interactive threads (Codex leaves out `exec` runs and sub-agents by default). An answer
        of another shape is no threads, or with ``strict`` a ValueError."""
        # The state database answers in milliseconds; a rollout scan takes seconds, too slow for a panel.
        result = self.connection.request("thread/list", {"limit": limit, "sortKey": "updated_at",
                                                         "useStateDbOnly": True})
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list):
            if strict:
                raise ValueError("Codex answered thread/list without a list of threads")
            return []
        return [thread for thread in data if isinstance(thread, dict) and isinstance(thread.get("id"), str)]

    def loaded(self) -> set[str]:
        found: set[str] = set()
        cursor = None
        for _ in range(8):  # Bounded pagination.
            result = self.connection.request("thread/loaded/list", {"cursor": cursor, "limit": 100})
            data = result.get("data") if isinstance(result, dict) else None
            if isinstance(data, list):
                found.update(item for item in data if isinstance(item, str))
            cursor = result.get("nextCursor") if isinstance(result, dict) else None
            if not cursor:
                break
        return found

    def read(self, thread: str) -> Optional[dict]:
        try:
            result = self.connection.request("thread/read", {"threadId": thread})
        except app_server.AppServerRpcError:
            return None
        value = result.get("thread") if isinstance(result, dict) else None
        return value if isinstance(value, dict) else None

    def fork(self, thread: str) -> str:
        """A new thread with this one's history, loaded on the daemon (V20); the original is untouched."""
        result = self.connection.request("thread/fork", {"threadId": thread})
        value = result.get("thread") if isinstance(result, dict) else None
        fork = value.get("id") if isinstance(value, dict) else None
        if not isinstance(fork, str) or not fork or fork == thread:
            raise app_server.AppServerProtocolError("Codex did not report the forked thread")
        return fork


def available(environment: Mapping[str, str]) -> bool:
    return socket_path(environment).is_socket()


# Other Codex processes.

def writers(proc: Path = Path("/proc")) -> dict[str, tuple[int, bool]]:
    """Threads whose rollout another Codex process holds open: id -> (pid, headless).

    Linux only (elsewhere this returns nothing). The daemon itself holds the
    rollouts of its loaded threads and is left out; `codex exec` (a headless
    Reins callback, or a child agent) counts as headless.
    """
    if not proc.is_dir() or not hasattr(os, "getuid"):
        return {}
    uid = os.getuid()
    found: dict[str, tuple[int, bool]] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid or Path(os.readlink(entry / "exe")).name != "codex":
                continue
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        words = [word.decode("utf-8", "replace") for word in argv if word]
        if "app-server" in words and any(word.startswith("--listen") for word in words):
            continue  # The daemon.
        headless = "exec" in words or "e" in words[1:2]
        try:
            descriptors = list((entry / "fd").iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                match = _ROLLOUT.search(os.readlink(descriptor))
            except OSError:
                continue
            if match:
                found[match.group(1)] = (int(entry.name), headless)
    return found


# The panel's view.

def _title(thread: Mapping[str, object]) -> Optional[str]:
    for key in ("name", "preview"):
        value = thread.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:80]
    return None


def _view(thread: Mapping[str, object], loaded: set[str], others: Mapping[str, tuple[int, bool]]) -> SessionView:
    ident = str(thread["id"])
    status = thread.get("status") if isinstance(thread.get("status"), dict) else {}
    kind = status.get("type")
    state, activity, host, pid = sessions.CLOSED, None, None, None
    if ident in others:
        pid, headless = others[ident]
        state = sessions.HEADLESS if headless else sessions.FOREIGN
        host = sessions.HOST_HEADLESS if headless else sessions.HOST_TERMINAL
    elif ident in loaded or kind in ("idle", "active", "systemError"):
        state, host = sessions.RUNNING, HOST_DAEMON
        if kind == "active":
            activity = sessions.BLOCKED if status.get("activeFlags") else sessions.WORKING
        elif kind == "idle":
            activity = sessions.IDLE
        elif kind == "systemError":
            activity = sessions.BLOCKED
    cwd = thread.get("cwd") if isinstance(thread.get("cwd"), str) else None
    created = thread.get("createdAt")
    return SessionView(agent="codex", session=ident, state=state, activity=activity, host=host, name=_title(thread),
                       cwd=cwd, pid=pid, handle=handle(ident),
                       started_at=float(created) if isinstance(created, (int, float)) else None)


def session_views(environment: Mapping[str, str], *, connect: Optional[Connect] = None,
                  others: Optional[Mapping[str, tuple[int, bool]]] = None) -> list[SessionView]:
    """Codex's interactive threads as the panel shows them; empty when the daemon is not running."""
    if connect is None and not available(environment):
        return []
    with Daemon(environment, connect=connect) as daemon:
        threads = daemon.threads()
        loaded = daemon.loaded()
    found = writers() if others is None else others
    return [_view(thread, loaded, found) for thread in threads]


def thread_ids(environment: Mapping[str, str], *, connect: Optional[Connect] = None) -> set[str]:
    """Every thread the daemon lists now. Errors are the daemon's."""
    with Daemon(environment, connect=connect) as daemon:
        return {str(thread["id"]) for thread in daemon.threads(strict=True)}


def started_on(opening: str, cwd: str, since: float, before: set[str], environment: Mapping[str, str], *,
               connect: Optional[Connect] = None) -> Optional[str]:
    """The thread in ``cwd``, not among ``before`` and created at ``since`` or later, whose first message begins
    with ``opening``: how a window started on a message of its own is told from every other thread. Errors are
    the daemon's."""
    wanted = " ".join(opening.split())
    with Daemon(environment, connect=connect) as daemon:
        threads = daemon.threads()
    for thread in threads:
        preview, created = thread.get("preview"), thread.get("createdAt")
        if (str(thread["id"]) not in before and thread.get("cwd") == cwd and isinstance(preview, str)
                and " ".join(preview.split()).startswith(wanted)
                and isinstance(created, (int, float)) and created >= since):
            return str(thread["id"])
    return None


def session_view(thread: str, environment: Mapping[str, str], *, connect: Optional[Connect] = None,
                 others: Optional[Mapping[str, tuple[int, bool]]] = None) -> Optional[SessionView]:
    """One thread, also one older than the panel's list; None when Codex does not know it."""
    if connect is None and not available(environment):
        return None
    with Daemon(environment, connect=connect) as daemon:
        record = daemon.read(thread)
        loaded = daemon.loaded() if record is not None else set()
    if record is None:
        return None
    return _view(record, loaded, writers() if others is None else others)


def loaded_on_daemon(thread: str, environment: Mapping[str, str], *, connect: Optional[Connect] = None) -> bool:
    """Whether the daemon has this thread loaded now: then it, and only it, may write the thread."""
    if connect is None and not available(environment):
        return False
    with Daemon(environment, connect=connect, timeout=3.0) as daemon:
        return thread in daemon.loaded()
