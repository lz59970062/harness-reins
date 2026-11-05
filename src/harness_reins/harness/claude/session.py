"""Claude Code installation and session discovery on the local host.

Claude Code is often installed without a ``claude`` launcher on PATH: the
desktop app bundles its own versioned binary (on macOS inside the app, on a
Linux host reached over SSH under ``~/.claude/remote``), nvm installs live in a
per-Node-version directory, and launchd/systemd services see a minimal PATH. Session transcripts are stored per original project directory,
so a headless resume must start where the session started. These helpers only
read the filesystem; they never launch Claude or change its configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NamedTuple, Optional

from .adapter import CLAUDE_BIN_ENV, CLAUDE_THREAD_ID_ENV as SESSION_ENV
from ..dsh.adapter import DSH_SESSION_ID_ENV

EXECPATH_ENV = "CLAUDE_CODE_EXECPATH"
ENTRYPOINT_ENV = "CLAUDE_CODE_ENTRYPOINT"
DESKTOP_ENTRYPOINT = "claude-desktop"

# Standalone installers (native, npm global, Homebrew, bun) in preference order.
STANDALONE_LOCATIONS = (
    "~/.local/bin/claude",
    "~/.claude/local/claude",
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
    "~/.npm-global/bin/claude",
    "~/.bun/bin/claude",
    "~/.nvm/versions/node/*/bin/claude",  # newest Node version first
)
# The macOS desktop app keeps one bundle per version/build.
DESKTOP_BUNDLE_GLOB = "Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude"
# The desktop app's SSH sessions install one plain CLI per version on the remote
# host. It shares the host's ~/.claude sign-in, so unlike the macOS bundle it can
# resume headless whenever a standalone CLI could; it is only versioned.
DESKTOP_REMOTE_GLOB = ".claude/remote/ccd-cli/*"
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
# A session's first records carry its starting cwd; never scan a whole transcript.
SESSION_CWD_SCAN_LINES = 200


class Executable(NamedTuple):
    path: str
    source: str  # configured | path | standalone | session | desktop-remote | desktop-bundle


Which = Callable[..., Optional[str]]


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _home(environment: Mapping[str, str]) -> Path:
    return Path(environment.get("HOME") or Path.home())


def is_desktop_bundle(path: str | os.PathLike[str]) -> bool:
    value = Path(path).as_posix()  # Windows paths use backslashes.
    return "/claude-code/" in value and "claude.app/Contents/MacOS/" in value


def is_desktop_remote(path: str | os.PathLike[str]) -> bool:
    return "/.claude/remote/ccd-cli/" in Path(path).as_posix()


def _source_of(path: str) -> str | None:
    if is_desktop_bundle(path):
        return "desktop-bundle"
    return "desktop-remote" if is_desktop_remote(path) else None


def _newest(home: Path, pattern: str) -> list[Path]:
    candidates = [path for path in home.glob(pattern) if _is_executable(path)]
    return sorted(candidates, key=lambda path: path.stat().st_mtime, reverse=True)


def desktop_bundles(environment: Mapping[str, str]) -> list[Path]:
    """Return bundled desktop binaries, newest first."""
    return _newest(_home(environment), DESKTOP_BUNDLE_GLOB)


def desktop_remote_binaries(environment: Mapping[str, str]) -> list[Path]:
    """Return the desktop app's SSH-session CLIs on this host, newest first."""
    return _newest(_home(environment), DESKTOP_REMOTE_GLOB)


def discover_executable(environment: Mapping[str, str], *, which: Which = shutil.which) -> Executable | None:
    """Find a Claude Code binary usable outside the launching session.

    An explicit configuration wins. A standalone CLI is preferred to the
    desktop bundle because the bundle authenticates through its host app and
    usually cannot run headless on its own. Bare names are resolved by
    ``which`` against the current process PATH.
    """
    home = _home(environment)
    configured = environment.get(CLAUDE_BIN_ENV, "").strip()
    if configured:
        expanded = Path(configured).expanduser()
        if expanded.is_absolute() or os.sep in configured:
            # Honor an explicit path even when it is missing, so a broken
            # configuration fails visibly. Only a versioned desktop binary
            # replaced by an app update is rediscovered.
            if _is_executable(expanded) or _source_of(configured) is None:
                return Executable(str(expanded), "configured")
        else:
            found = which(configured)
            if found:
                return Executable(found, "configured")
            if configured != "claude":
                # Setup records the bare default when nothing was found; any
                # other name is a deliberate choice that must not be replaced.
                return Executable(configured, "configured")
    found = which("claude")
    if found:
        return Executable(found, _source_of(found) or "path")
    for location in STANDALONE_LOCATIONS:
        matches = _newest(home, location[2:]) if "*" in location else [Path(location.replace("~", str(home), 1))]
        if matches and _is_executable(matches[0]):
            return Executable(str(matches[0]), "standalone")
    session_binary = environment.get(EXECPATH_ENV, "").strip()
    if session_binary and _is_executable(Path(session_binary)):
        return Executable(session_binary, _source_of(session_binary) or "session")
    for source, binaries in (("desktop-remote", desktop_remote_binaries(environment)),
                             ("desktop-bundle", desktop_bundles(environment))):
        if binaries:
            return Executable(str(binaries[0]), source)
    return None


def with_resolved_executable(environment: Mapping[str, str], *, which: Which = shutil.which) -> dict[str, str]:
    """Return ``environment`` whose configured Claude binary is runnable when possible."""
    resolved = dict(environment)
    found = discover_executable(environment, which=which)
    if found is not None:
        resolved[CLAUDE_BIN_ENV] = found.path
    return resolved


def is_desktop_session(environment: Mapping[str, str]) -> bool:
    return environment.get(ENTRYPOINT_ENV, "").strip() == DESKTOP_ENTRYPOINT


def background_tasks_disabled(environment: Mapping[str, str]) -> bool:
    """Whether Claude Code runs without background Bash, so a session cannot hold a background ``reins wait``."""
    return environment.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS", "").strip().lower() in ("1", "true", "yes", "on")


def config_dir(environment: Mapping[str, str]) -> Path:
    configured = environment.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(configured).expanduser() if configured else _home(environment) / ".claude"


def find_session_file(session_id: str, environment: Mapping[str, str]) -> Path | None:
    if not SESSION_ID_PATTERN.fullmatch(session_id):
        return None
    matches = [path for path in (config_dir(environment) / "projects").glob(f"*/{session_id}.jsonl") if path.is_file()]
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def session_project_cwd(session_id: str, environment: Mapping[str, str]) -> str | None:
    """Return the directory a session started in, if it still exists.

    Claude Code stores a session under a slug of its starting directory and
    ``--resume <id>`` looks the session up from the current directory, so a
    later ``cd`` inside the session must not change where it is resumed.
    """
    path = find_session_file(session_id, environment)
    if path is None:
        return None
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= SESSION_CWD_SCAN_LINES:
                    break
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                cwd = record.get("cwd") if isinstance(record, dict) else None
                if isinstance(cwd, str) and cwd:
                    return cwd if Path(cwd).is_dir() else None
    except OSError:
        return None
    return None


def resume_cwd(request: Mapping[str, object], environment: Mapping[str, str]) -> str | None:
    target = request.get("target")
    if not isinstance(target, dict) or target.get("kind") != "session":
        return None
    value = target.get("session")
    return session_project_cwd(value, environment) if isinstance(value, str) else None


# Live delivery: `reins wait` ---------------------------------------------------
#
# Claude Code and DeepSeek Harness wake a live session when a background shell
# or harness job exits. The session runs `reins wait` in the background; it
# prints the callback when one is queued and exits, which wakes that session.
# While a waiter is alive the daemon leaves the session's callbacks alone. Only
# live-wait agents are touched here; other agents keep the normal daemon route.

LIVE_ACK_GRACE_SECONDS = 30 * 60.0
LIVE_WATCHER_DIR_NAME = "live-watchers"
# Agents that wake a live session when a background ``reins wait`` exits. DSH
# runs one through its own job system; a DSH callback has no headless fallback.
LIVE_WAIT_AGENTS = ("claude", "dsh")
# A channel server (claude_channel.py) holds the same lock under this prefix.
CHANNEL_WATCHER_PREFIX = "channel-"
# So does the waiter Claude Code runs from the hook `reins setup` installs.
HOOK_WATCHER_PREFIX = "hook-"
# A headless run keeps a session record too, under this entrypoint, and ends with its turn.
HEADLESS_ENTRYPOINT = "sdk-cli"
# SessionStart can run before the window has written its session record.
WINDOW_RECORD_GRACE_SECONDS = 30.0
# `reins run` returns before the session starts its background `reins wait`. A
# task that ends inside that gap (a command that fails at once) must not make
# the daemon resume the session headless next to the live one, so a submission
# from a live session reserves its callbacks for a waiter for this long.
LIVE_WAIT_GRACE_ENV = "REINS_CLAUDE_WAIT_GRACE_SECONDS"
DEFAULT_LIVE_WAIT_GRACE_SECONDS = 120.0
EXPECTED_WAITER_SUFFIX = ".expected"
# Set by the daemon in a headless resume: the delivery target its worker holds.
# A `reins wait` run inside that resume may deliver into its own session.
RESUMED_TARGET_ENV = "REINS_RESUMED_TARGET"
# How long a waiter sees its callback held by a headless resume before saying so.
HEADLESS_NOTICE_SECONDS = 3.0
HEADLESS_EXIT_CODE = 4


def live_wait_agent(agent: object) -> str | None:
    """The agent name when it supports live ``reins wait`` delivery."""
    return agent if isinstance(agent, str) and agent in LIVE_WAIT_AGENTS else None


def session_environment_name(agent: str) -> str:
    return DSH_SESSION_ID_ENV if agent == "dsh" else SESSION_ENV


def _target_of(request: Mapping[str, object]) -> tuple[str, str] | None:
    agent = live_wait_agent(request.get("agent"))
    target = request.get("target")
    if agent is None or not isinstance(target, dict) or target.get("kind") != "session":
        return None
    value = target.get("session")
    return (agent, value) if isinstance(value, str) and value else None


def _session_of(request: Mapping[str, object]) -> str | None:
    target = _target_of(request)
    return target[1] if target is not None else None


def live_watcher_dir(session: str, *, agent: str = "claude") -> Path:
    from ...engine import dispatch
    from ...engine import delivery as delivery_engine
    from ...engine import reminders as reminder_policy
    from ...store import records as queue_store
    from ...store import layout

    # Claude keeps its original key: a waiter or channel started before an upgrade
    # must still hold back the upgraded daemon.
    key = f"session:{session}" if agent == "claude" else f"{agent}:{session}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return layout.target_lock_dir() / LIVE_WATCHER_DIR_NAME / digest


def register_live_watcher(session: str, *, prefix: str = "", agent: str = "claude") -> tuple[object, Path]:
    """Lock a uniquely named file; the OS releases it if the waiter dies.

    The daemon must never observe an unlocked live file. On POSIX the lock is
    taken under a staging name and renamed into place. On Windows the file is
    created already locked (share mode 0), and a file open that way can be
    neither renamed nor deleted, so it is locked under its final name.
    """
    from ...engine import dispatch
    from ...engine import delivery as delivery_engine
    from ...engine import reminders as reminder_policy
    from ...store import records as queue_store
    from ...store import layout

    directory = live_watcher_dir(session, agent=agent)
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{prefix}{os.getpid()}-{secrets.token_hex(6)}"
    final = directory / f"{name}.lock"
    lock = queue_store.acquire_path_lock(final if os.name == "nt" else directory / f".{name}.staging", blocking=False)
    if lock is None:
        raise RuntimeError("could not lock a fresh live waiter file")
    handle, staging = lock
    if staging != final:
        os.replace(staging, final)
    return handle, final


def release_live_watcher(lock: tuple[object, Path]) -> None:
    from ...engine import dispatch
    from ...engine import delivery as delivery_engine
    from ...engine import reminders as reminder_policy
    from ...store import records as queue_store
    from ...store import layout

    if os.name != "nt":
        lock[1].unlink(missing_ok=True)
        queue_store.release_owner_lock(lock, remove=False)
        return
    # Windows deletes nothing that is still open: close first. A concurrent
    # `_held` probe may hold the file for a moment; it deletes it itself.
    queue_store.release_owner_lock(lock, remove=False)
    try:
        lock[1].unlink(missing_ok=True)
    except PermissionError:
        pass


def live_wait_grace(environment: Mapping[str, str]) -> float:
    value = environment.get(LIVE_WAIT_GRACE_ENV, "").strip()
    try:
        return max(0.0, float(value)) if value else DEFAULT_LIVE_WAIT_GRACE_SECONDS
    except ValueError:
        return DEFAULT_LIVE_WAIT_GRACE_SECONDS


def expect_live_waiter(session: str, grace: float, *, agent: str = "claude") -> None:
    """Reserve this session's callbacks for a `reins wait` that starts within ``grace`` seconds."""
    if grace <= 0:
        return
    try:
        directory = live_watcher_dir(session, agent=agent)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{os.getpid()}-{secrets.token_hex(6)}{EXPECTED_WAITER_SUFFIX}"
        path.write_text(repr(time.time() + grace), encoding="utf-8")
    except OSError:
        pass  # Only the grace is lost: the daemon delivers as before.


def _expected(agent: str, session: str) -> bool:
    directory = live_watcher_dir(session, agent=agent)
    if not directory.is_dir():
        return False
    now = time.time()
    expected = False
    for path in directory.glob(f"*{EXPECTED_WAITER_SUFFIX}"):
        try:
            until = float(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            until = 0.0
        if until > now:
            expected = True
        else:
            path.unlink(missing_ok=True)
    return expected


def live_watcher_is_held(request: Mapping[str, object]) -> bool:
    """Daemon hook: whether a live waiter owns, or is about to own, delivery for this callback."""
    target = _target_of(request)
    if target is None:
        return False
    agent, session = target
    return _held(agent, session, "") or _expected(agent, session)


def inside_resume_of(request: Mapping[str, object]) -> bool:
    """Whether this process runs inside the daemon's headless resume of the callback's session."""
    from ...engine import delivery as delivery_engine

    held = os.environ.get(RESUMED_TARGET_ENV, "").strip()
    return bool(held) and held == delivery_engine.callback_target_key(dict(request))


def channel_is_held(session: str) -> bool:
    """Whether a `reins claude` channel is delivering this session's callbacks."""
    return _held("claude", session, CHANNEL_WATCHER_PREFIX)


def hook_is_held(session: str) -> bool:
    """Whether the Reins Claude Code hook is delivering this session's callbacks."""
    return _held("claude", session, HOOK_WATCHER_PREFIX)


def open_window(session: str, environment: Mapping[str, str]) -> Optional[int]:
    """The process of a Claude Code window that has this session open, from its session record."""
    from ...tower import main as tower

    try:
        paths = list((config_dir(environment) / "sessions").glob("*.json"))
    except OSError:
        return None
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (not isinstance(record, dict) or record.get("sessionId") != session
                or record.get("entrypoint") == HEADLESS_ENTRYPOINT or type(record.get("pid")) is not int):
            continue
        identity = tower.daemon_process_identity(record["pid"])
        ticks = identity.get("start_ticks") if identity else None
        if identity and (ticks is None or str(ticks) == str(record.get("procStart", ticks))):
            return record["pid"]  # The start time rules out a reused pid after a crash.
    return None


def _held(agent: str, session: str, prefix: str) -> bool:
    from ...engine import dispatch
    from ...engine import delivery as delivery_engine
    from ...engine import reminders as reminder_policy
    from ...store import records as queue_store
    from ...store import layout

    directory = live_watcher_dir(session, agent=agent)
    if not directory.is_dir():
        return False
    held = False
    for path in directory.glob(f"{prefix}*.lock"):
        lock = queue_store.acquire_path_lock(path, blocking=False)
        if lock is None:
            held = True
        else:
            release_live_watcher(lock)  # Its waiter exited; names are never reused.
    return held


def _claim(root: Path, path: Path) -> str | None:
    """Move one callback through running/ and return the prompt to print."""
    from ...engine import dispatch
    from ...engine import delivery as delivery_engine
    from ...engine import reminders as reminder_policy
    from ...store import records as queue_store
    from ...store import layout

    request_id = path.stem
    delivery_lock = queue_store.acquire_owner_lock(root, layout.delivery_lock_id(request_id), blocking=False)
    if delivery_lock is None:
        return None
    target_lock = None
    running: Path | None = None
    try:
        try:
            request = dispatch.load_request(path)
        except FileNotFoundError:
            return None
        target_lock = delivery_engine.acquire_target_lock(request, blocking=False)
        if target_lock is None and not inside_resume_of(request):
            # The daemon is delivering to this session right now. Inside that
            # delivery's own resume the session is this process, so it may take it.
            return None
        try:
            running = queue_store.move_request(path, root / "running")
        except FileNotFoundError:
            return None
        request = dispatch.load_request(running)
        if layout.ack_path(root, request_id).exists() or queue_store.is_canceled(root, request_id):
            return None  # The finally block returns it; the daemon finalizes it.
        payload = {"prompt": str(request["prompt"]), "callback_hook_path": str(layout.callback_hook_path()),
                   "request": request, "queue_dir": str(root)}
        prompt = reminder_policy.select_delivery_prompt(payload)
        now = time.time()
        request["live_delivered_at"] = now
        request["next_attempt_at"] = now + LIVE_ACK_GRACE_SECONDS
        request["last_deferred_reason"] = "delivered to the live session by reins wait; awaiting ACK"
        queue_store.write_request(running, request)
        return prompt
    finally:
        if running is not None and running.exists():
            try:
                queue_store.move_request(running, root / "pending")
            except OSError as exc:
                print(f"reins: warning: could not return callback {request_id} to pending: {exc}", file=sys.stderr)
        queue_store.release_owner_lock(target_lock, remove=False)
        queue_store.release_owner_lock(delivery_lock, remove=False)


def _matches(request: Mapping[str, object], agent: str, session: str, task: str | None) -> bool:
    target = _target_of(request)
    return (target == (agent, session)
            and (task is None or request.get("managed_task_id") == task)
            and request.get("retain_target_lease") is not True)


def _task_already_acknowledged(root: Path, agent: str, session: str, task: str) -> bool:
    from ...store import layout

    # An ACKed callback stays under running/ while the resume that ACKed it is still alive.
    for state in ("done", "running"):
        for path in (root / state).glob("*.json"):
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (isinstance(request, dict) and _matches(request, agent, session, task)
                    and (state == "done" or layout.ack_path(root, path.stem).exists())):
                return True
    return False


def _held_by_headless_resume(root: Path, agent: str, session: str, task: str | None) -> list[dict[str, object]]:
    """Unacknowledged callbacks of this session that the daemon is delivering to a headless resume."""
    from ...store import records as queue_store
    from ...store import layout

    if agent == "dsh":
        return []  # DSH has no headless fallback, so nothing is ever held that way.
    held = []
    for path in sorted((root / "running").glob("*.json")):
        try:
            request = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(request, dict) and _matches(request, agent, session, task)
                and not layout.ack_path(root, path.stem).exists()
                and not inside_resume_of(request)
                and queue_store.delivery_lock_is_held(root, path.stem)):
            held.append(request)
    return held


def _headless_notice(requests: list[dict[str, object]], session: str) -> str:
    from ...engine import dispatch

    lines = []
    for request in requests:
        lines += [
            f"[reins] {request.get('id')} went to a headless resume of this session, not to this waiter",
            f"Task: {dispatch.request_task(request)}",
        ]
    lines += [
        f"The daemon found no `reins wait` for session {session} when the callback was due and started "
        f"`claude -p --resume {session}`. That copy of the session is handling it and will ACK it; do not "
        "repeat its work. Check `reins status` and the task's files, and wait for that resume to end before "
        "starting more work on the same task.",
    ]
    return "\n".join(lines)


def _claim_pending(root: Path, agent: str, session: str, task: str | None,
                   skip_delivered: bool = False) -> list[str]:
    from ...store import layout

    prompts = []
    for state in ("pending", "failed"):
        for path in sorted((root / state).glob("*.json")):
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (not isinstance(request, dict) or not _matches(request, agent, session, task)
                    or layout.ack_path(root, path.stem).exists()):
                continue
            if skip_delivered and "live_delivered_at" in request:
                continue
            prompt = _claim(root, path)
            if prompt is not None:
                prompts.append(prompt)
    return prompts


def hook(args: argparse.Namespace) -> int:
    """Hidden `reins _claude-hook`: Claude Code runs it in the background on SessionStart and Stop.

    Exit code 2 wakes the model in the open window with what is on stderr, even
    when the window is idle, and the next Stop starts this again. A callback
    waiting for its ACK is not repeated in this window: the turn that did not
    ACK it would end, start this hook, and wake the model again without end.
    If it is still unacknowledged when the window closes, the daemon resumes it.
    """
    from ...store import records as queue_store
    from ...store import layout

    try:
        session = json.load(sys.stdin).get("session_id")
    except (ValueError, AttributeError):
        return 0
    # A headless run does not exit while this hook is alive, so leave at once.
    if (not isinstance(session, str) or not SESSION_ID_PATTERN.fullmatch(session)
            or os.environ.get(ENTRYPOINT_ENV, "").strip() == HEADLESS_ENTRYPOINT
            or os.environ.get(RESUMED_TARGET_ENV, "").strip()
            or channel_is_held(session) or hook_is_held(session)):
        return 0
    root = layout.queue_dir(args).expanduser().absolute()
    queue_store.ensure_daemon_dirs(root)
    watcher = register_live_watcher(session, prefix=HOOK_WATCHER_PREFIX)
    started = time.monotonic()
    try:
        while True:
            prompts = _claim_pending(root, "claude", session, None, skip_delivered=True)
            if prompts:
                if hasattr(sys.stderr, "reconfigure"):
                    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
                print("Harness Reins delivered this through the hook your user installed with `reins setup`. It is "
                      "not an error or feedback on your work. An ACK command in it only records that you saw it.\n\n"
                      + "\n\n".join(prompts),
                      file=sys.stderr, flush=True)
                return 2
            if (open_window(session, os.environ) is None
                    and time.monotonic() - started >= WINDOW_RECORD_GRACE_SECONDS):
                return 0  # The window closed or moved to another session; the daemon takes over.
            time.sleep(max(0.05, args.poll_interval))
    finally:
        release_live_watcher(watcher)


def wait(args: argparse.Namespace) -> int:
    """`reins wait`: print this live session's unacknowledged callback(s), waiting if none yet.

    A callback keeps being returned until it is ACKed. If the session closes,
    the OS releases the waiter's lock and the daemon delivers as usual.
    """
    from ...engine import dispatch
    from ...engine import delivery as delivery_engine
    from ...engine import reminders as reminder_policy
    from ...store import records as queue_store
    from ...store import layout

    agent = getattr(args, "agent", None)
    if agent is not None and agent not in LIVE_WAIT_AGENTS:
        print(f"reins wait: {agent} does not deliver callbacks through a live waiter", file=sys.stderr)
        return 2
    if agent not in LIVE_WAIT_AGENTS:
        # Use the registry's precedence rather than a two-marker guess: a DSH
        # shell nested under Claude can retain both markers, and DSH outranks it.
        from ...harness import AGENTS
        detected = AGENTS.detect(os.environ)
        if detected in LIVE_WAIT_AGENTS:
            agent = detected
        elif args.session:
            agent = "claude"  # Legacy explicit session without a live marker.
    if agent not in LIVE_WAIT_AGENTS:
        print("reins wait is only for Claude Code and DeepSeek Harness sessions: "
              "CLAUDE_CODE_SESSION_ID or DSH_SESSION_ID is unset. "
              "Other agents receive callbacks from the daemon and should not run it.", file=sys.stderr)
        return 2
    environment_name = session_environment_name(agent)
    session = args.session or os.environ.get(environment_name, "").strip()
    if not session:
        print(f"reins wait: {environment_name} is unset for the detected {agent} session.", file=sys.stderr)
        return 2
    root = layout.queue_dir(args).expanduser().absolute()
    queue_store.ensure_daemon_dirs(root)
    try:
        if args.task and not layout.managed_task_path(root, args.task).exists():
            raise ValueError(f"unknown task {args.task} in {root}")
    except ValueError as exc:
        print(f"reins wait: {exc}", file=sys.stderr)
        return 2
    if agent == "claude" and channel_is_held(session):
        print("reins wait: this session was started with `reins claude`; its callbacks arrive through the "
              "reins channel, so no waiter is needed", file=sys.stderr)
        return 0
    if agent == "claude" and hook_is_held(session):
        print("reins wait: the Reins hook delivers this session's callbacks, so no waiter is needed", file=sys.stderr)
        return 0
    deadline = time.monotonic() + args.timeout if args.timeout else None
    watcher = register_live_watcher(session, agent=agent)
    print(f"reins wait: watching {'DeepSeek Harness' if agent == 'dsh' else 'Claude Code'} session {session}",
          file=sys.stderr)
    held_since: float | None = None
    try:
        while True:
            prompts = _claim_pending(root, agent, session, args.task)
            resumed = inside_resume_of({"agent": agent, "target": {"kind": "session", "session": session}})
            if prompts:
                # A message asks this session to wait again; hold the next one for that wait.
                if not resumed and any("[reins-msg]" in prompt for prompt in prompts):
                    expect_live_waiter(session, live_wait_grace(os.environ), agent=agent)
                print("\n\n".join(prompts), flush=True)
                return 0
            if resumed and not args.task:
                print("reins wait: this headless resume ends with its turn; the next message resumes the session "
                      "again", file=sys.stderr)
                return 0
            if args.task and _task_already_acknowledged(root, agent, session, args.task):
                print(f"reins wait: task {args.task}'s callback is already acknowledged", file=sys.stderr)
                return 0
            # A callback that went to a headless resume never comes back here
            # while that resume runs; say so instead of waiting in silence.
            held = _held_by_headless_resume(root, agent, session, args.task)
            if not held:
                held_since = None
            elif held_since is None:
                held_since = time.monotonic()
            elif time.monotonic() - held_since >= HEADLESS_NOTICE_SECONDS:
                print(_headless_notice(held, session), flush=True)
                return HEADLESS_EXIT_CODE
            if deadline is not None and time.monotonic() >= deadline:
                print("reins wait: timed out; the callback will still be delivered when it arrives", file=sys.stderr)
                return 3
            time.sleep(max(0.05, args.poll_interval))
    except KeyboardInterrupt:
        return 130
    finally:
        release_live_watcher(watcher)
