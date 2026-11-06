"""The tower's main loop, its runtime identity and safe in-place reloads."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path
from .. import __version__
from .. import runtime as reins_runtime
from ..harness.pi import channel as pi_channel
from ..runtime import worker_command
from ..owners import linux as linux_platform
from ..store import layout
from ..owners import host
from ..store import records as queue_store
from ..engine import delivery as delivery_engine
from ..engine import resume_environment
from ..engine import goals
from ..engine import messages
from ..engine import recovery
from ..engine import tasks as managed_tasks
from . import environment as tower_environment


RELOAD_PROTOCOL_VERSION = 1


def daemon_command(args: argparse.Namespace) -> list[str]:
    explicit = getattr(args, "exec_start", None)
    prefix = [explicit] if explicit else worker_command()
    return [*prefix, *daemon_arguments(args)]


def daemon_arguments(args: argparse.Namespace) -> list[str]:
    command = [
        "daemon",
        "--interval",
        str(args.interval),
        "--retries",
        str(args.retries),
        "--retry-delay",
        str(args.retry_delay),
        "--retry-backoff",
        str(args.retry_backoff),
        "--resume-timeout",
        str(getattr(args, "resume_timeout", delivery_engine.DEFAULT_RESUME_TIMEOUT)),
        "--resume-stall",
        str(getattr(args, "resume_stall", delivery_engine.DEFAULT_RESUME_STALL)),
        "--max-resumes",
        str(getattr(args, "max_resumes", delivery_engine.DEFAULT_MAX_RESUMES)),
    ]
    if args.queue_dir:
        command.extend(["--queue-dir", str(Path(args.queue_dir).expanduser())])
    return command


def daemon_reexec_command(args: argparse.Namespace) -> list[str]:
    return worker_command(*daemon_arguments(args))


def daemon_has_live_delivery_workers(root: Path) -> bool:
    if delivery_engine._BACKGROUND_RESUMES or delivery_engine.deliveries_active():
        return True
    for path in (root / "running").glob("*.json"):
        if queue_store.delivery_lock_is_held(root, path.stem):
            return True
    # ACK can move a callback to done before its Agent exits. A replacement
    # daemon has no in-memory child table; the inherited OS lease is still the
    # source of truth, regardless of the callback's current queue state.
    for path in (root / layout.LOCKS_STATE).glob("delivery-*.lock"):
        lock = queue_store.acquire_path_lock(path, blocking=False)
        if lock is None:
            return True
        queue_store.release_owner_lock(lock, remove=False)
    return False


def daemon(args: argparse.Namespace) -> int:
    root = layout.queue_dir(args)
    queue_store.ensure_daemon_dirs(root)
    daemon_lock = queue_store.acquire_owner_lock(root, "daemon-singleton", blocking=False)
    if daemon_lock is None:
        print(f"reins: another daemon already owns {root}", file=sys.stderr)
        return 1

    reload_requested = False
    stop_requested = False
    reload_deferred_reported = False

    def request_reload(_signum: int, _frame: object) -> None:
        nonlocal reload_requested
        reload_requested = True

    previous_hup_handler: object | None = None
    if hasattr(signal, "SIGHUP"):
        previous_hup_handler = signal.signal(signal.SIGHUP, request_reload)
    try:
        tower_environment.load_service_proxy_environment()
        write_daemon_runtime(root)
        delivery_engine.recover_running(root)
        print(f"reins: daemon watching {root}", file=sys.stderr)

        processed = 0
        while True:
            if os.name == "nt" and consume_daemon_reload_request(root, filename="daemon-stop.json"):
                stop_requested = True
                reload_requested = True
            if consume_daemon_reload_request(root):
                reload_requested = True
            if reload_requested:
                delivery_engine.reap_background_resumes()
                if daemon_has_live_delivery_workers(root):
                    if not reload_deferred_reported:
                        print(
                            "reins: reload deferred until live delivery workers exit; "
                            "their leases and resumed Codex processes will not be interrupted.",
                            file=sys.stderr,
                        )
                        reload_deferred_reported = True
                    if args.once:
                        return 0
                    # A drain must not admit another delivery or new workload
                    # on each iteration, otherwise a busy queue never reloads.
                    time.sleep(args.interval)
                    continue
                else:
                    if stop_requested:
                        return 0
                    tower_environment.load_service_proxy_environment()
                    print(
                        "reins: reloading daemon in place after delivery workers drained.",
                        file=sys.stderr,
                    )
                    if os.name == "nt":
                        # The scheduled coordinator supervisor restarts only
                        # this control process, after leases have drained.
                        return 75
                    os.execv(sys.executable, daemon_reexec_command(args))
                    raise RuntimeError("daemon reload exec unexpectedly returned")
            delivery_engine.reap_background_resumes()
            delivery_engine.recover_running(root)
            delivery_engine.reconcile_acknowledged_retained_leases(root)
            resume_environment.prune(root)
            pi_channel.reconcile_publications(root, delivery_engine.resume_limit(getattr(args, "resume_timeout", delivery_engine.DEFAULT_RESUME_TIMEOUT)) or delivery_engine.PUBLISHED_ACK_TIMEOUT)
            managed_work = managed_tasks.recover_managed_tasks(root)
            recovery.recover_active(root)
            messages.maintain(root)
            if managed_work:
                processed += managed_work
                if args.max_items is not None and processed >= args.max_items:
                    return 0
                continue
            if goals.process_goal_reminders(root):
                continue
            if goals.process_blocked_goal_email(root):
                continue
            # A bounded run stays in order on this thread; it has to count what it finished.
            bounded = args.once or args.max_items is not None
            did_work = (delivery_engine.process_one if bounded else delivery_engine.start_one)(root, args)
            if did_work:
                processed += 1
                if args.max_items is not None and processed >= args.max_items:
                    return 0
                continue
            if args.once:
                return 0
            time.sleep(args.interval)
    finally:
        if previous_hup_handler is not None and hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, previous_hup_handler)
        clear_daemon_runtime()
        # Keep the singleton inode stable. Unlinking a lock file after unlock
        # can let two newly starting daemons lock different inodes.
        queue_store.release_owner_lock(daemon_lock, remove=False)


def daemon_process_identity(pid: int) -> dict[str, object] | None:
    return host.host_platform().process_identity(pid)


def write_daemon_runtime(root: Path | None = None) -> None:
    path = layout.daemon_runtime_path()
    payload = {
        "pid": os.getpid(),
        "boot_id": host.current_boot_id(),
        "process_identity": daemon_process_identity(os.getpid()),
        "queue_dir": str((root if root is not None else layout.queue_dir()).resolve()),
        "reload_protocol": RELOAD_PROTOCOL_VERSION,
        "version": __version__,
        "build": reins_runtime.build_id(),
        "started_at": time.time(),
    }
    queue_store.write_request(path, payload)


def read_daemon_runtime() -> dict[str, object] | None:
    path = layout.daemon_runtime_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def daemon_supports_hot_reload(
    expected_pid: int | None = None,
    *,
    expected_queue: Path | None = None,
) -> bool:
    payload = read_daemon_runtime()
    if payload is None or payload.get("reload_protocol") != RELOAD_PROTOCOL_VERSION:
        return False
    pid = payload.get("pid")
    if type(pid) is not int or pid <= 0 or (expected_pid is not None and pid != expected_pid):
        return False
    if expected_queue is not None and payload.get("queue_dir") != str(expected_queue.resolve()):
        return False
    identity = payload.get("process_identity")
    return isinstance(identity, dict) and bool(identity) and daemon_process_identity(pid) == identity


def send_standalone_reload(pid: int, *, expected_queue: Path | None = None) -> bool:
    """Reload only a verified daemon; Linux pins the target before rechecking."""
    if not daemon_supports_hot_reload(pid, expected_queue=expected_queue):
        return False
    payload = read_daemon_runtime()
    if payload is None or payload.get("pid") != pid:
        return False
    if expected_queue is not None and payload.get("queue_dir") != str(expected_queue.resolve()):
        return False
    identity = payload.get("process_identity")
    if not isinstance(identity, dict):
        return False
    if sys.platform in ("darwin", "win32"):
        # macOS has no pidfd signal. The lock-owning daemon verifies this
        # request against itself, avoiding a check-then-kill PID reuse race.
        root = Path(str(payload.get("queue_dir", "")))
        if not root.is_absolute():
            return False
        queue_store.write_request(root / "daemon-reload.json", {"pid": pid, "process_identity": identity})
        return True
    return linux_platform.signal_if_identity_matches(pid, identity, signal.SIGHUP)


def consume_daemon_reload_request(root: Path, *, filename: str = "daemon-reload.json") -> bool:
    path = root / filename
    try:
        request = json.loads(path.read_text(encoding="utf-8"))
        identity = daemon_process_identity(os.getpid())
        accepted = (isinstance(request, dict) and request.get("pid") == os.getpid()
                    and identity is not None and request.get("process_identity") == identity)
        path.unlink()
        queue_store.fsync_directory(root)
        return accepted
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return False


def clear_daemon_runtime() -> None:
    path = layout.daemon_runtime_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return
    identity = daemon_process_identity(os.getpid())
    if (isinstance(payload, dict) and payload.get("pid") == os.getpid()
            and identity is not None and payload.get("process_identity") == identity):
        try:
            path.unlink()
            queue_store.fsync_directory(path.parent)
        except FileNotFoundError:
            pass
