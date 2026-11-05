"""Deliver queued callbacks to their target sessions: transport choice, target leases, retries and resume workers."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from ..core import capabilities, records
from .. import __version__
from ..harness.claude import session as claude_session
from ..harness.dsh import session as dsh_session
from ..harness.pi import channel as pi_channel
from ..harness import get_agent
from ..harness.base import DEFAULT_CLAUDE_PERMISSION_MODE
from ..harness.claude.adapter import CLAUDE_BIN_ENV, CLAUDE_PERMISSION_MODE_ENV
from ..runtime import worker_command
from ..system import windows_process
from ..store import layout
from ..store import records as queue_store
from ..system import shell
from . import dispatch
from . import reminders as reminder_policy
from . import resume_environment
from . import native_queue
from ..harness.codex import app_server


DEFAULT_RETRIES = 3
DEFAULT_RETRY_DELAY = 30.0
DEFAULT_RETRY_BACKOFF = 2.0
DEFAULT_RESUME_TIMEOUT = 3600.0
# Codex refuses a second writer for a thread whose original process still runs.
CODEX_ACTIVE_WRITER = "already has an active writer"


@dataclass
class BackgroundResume:
    process: subprocess.Popen[str]
    target_key: str
    deadline: float
    request_id: str


_BACKGROUND_RESUMES: dict[int, BackgroundResume] = {}


class TargetLeaseUnavailable(RuntimeError):
    """Another queue is already delivering a callback to the same target."""


def claude_command() -> str:
    return os.environ.get(CLAUDE_BIN_ENV, "claude")


def claude_permission_mode(request: dict[str, object]) -> str:
    value = request.get("permission_mode")
    if isinstance(value, str) and value:
        return value
    return os.environ.get(CLAUDE_PERMISSION_MODE_ENV, DEFAULT_CLAUDE_PERMISSION_MODE)


def codex_command() -> str:
    return os.environ.get("REINS_CODEX_BIN", "codex")


def agent_environment(agent: str) -> dict[str, str]:
    """Environment for building an agent command; local CLIs are located on demand."""
    if agent == "claude":
        return claude_session.with_resolved_executable(os.environ, which=shutil.which)
    if agent == "dsh":
        return dsh_session.with_resolved_executable(os.environ, which=shutil.which)
    return dict(os.environ)


def is_live_delivery(request: Mapping[str, object]) -> bool:
    """Whether this agent receives callbacks only through a live ``reins wait``."""
    try:
        agent = dispatch.request_agent(dict(request))
    except ValueError:
        return False  # A malformed record must reach the worker's error path, not stop selection.
    return getattr(get_agent(agent), "callback_delivery", "resume") == "live"


def resume_command(request: dict[str, object]) -> list[str]:
    agent = dispatch.request_agent(request)
    return get_agent(agent).resume_command(request, agent_environment(agent))


def resume_cwd(request: dict[str, object]) -> str:
    """Claude and DSH resolve a session from its starting directory, not the task's."""
    agent = dispatch.request_agent(request)
    session_cwd = None
    if agent == "claude":
        session_cwd = claude_session.resume_cwd(request, os.environ)
    elif agent == "dsh":
        target = request.get("target")
        if isinstance(target, dict) and target.get("kind") == "session":
            value = target.get("session")
            if isinstance(value, str) and value:
                session_cwd = dsh_session.session_cwd(value, os.environ)
    if session_cwd:
        return session_cwd
    return str(request["cwd"])


def codex_resume_command(request: dict[str, object]) -> list[str]:
    """Compatibility entrypoint for callers that explicitly request Codex."""
    return get_agent("codex").resume_command(request, os.environ)


def claude_resume_command(request: dict[str, object]) -> list[str]:
    """Compatibility entrypoint for callers that explicitly request Claude."""
    return get_agent("claude").resume_command(request, os.environ)


def next_attempt_at(path: Path) -> float:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0.0
    value = data.get("next_attempt_at") if isinstance(data, dict) else None
    return float(value) if isinstance(value, (int, float)) else 0.0


def callback_target_key(request: dict[str, object]) -> str | None:
    target = request.get("target")
    if not isinstance(target, dict):
        return None
    kind = target.get("kind")
    value = target.get("session")
    if kind == "session" and isinstance(value, str) and value:
        if dispatch.request_agent(request) == "pi":
            value = pi_channel.canonical_session(value)
        return f"session:{value}"
    return None


def target_lock_path(request: dict[str, object]) -> Path | None:
    key = callback_target_key(request)
    if key is None:
        return None
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return layout.target_lock_dir() / f"{digest}.lock"


def retained_target_lease_path(request: dict[str, object]) -> Path | None:
    path = target_lock_path(request)
    if path is None:
        return None
    return path.with_suffix(".retained.json")


def retained_target_lease_lock_path(request: dict[str, object]) -> Path | None:
    path = target_lock_path(request)
    if path is None:
        return None
    return path.with_suffix(".retained.lock")


def acquire_retained_target_lease_lock(
    request: dict[str, object],
    *,
    blocking: bool,
) -> tuple[object, Path] | None:
    path = retained_target_lease_lock_path(request)
    if path is None:
        return None
    return queue_store.acquire_path_lock(path, blocking=blocking)


def retained_target_lease_is_held(request: dict[str, object]) -> bool:
    path = retained_target_lease_path(request)
    if path is None or not path.exists():
        return False
    lock = acquire_retained_target_lease_lock(request, blocking=False)
    if lock is None:
        return True
    try:
        if not path.exists():
            return False
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True
        if not isinstance(record, dict):
            return True
        request_id = record.get("request_id")
        queue_root = record.get("queue_dir")
        if not isinstance(request_id, str) or not isinstance(queue_root, str) or not queue_root:
            return True
        root = Path(queue_root)
        if not layout.ack_path(root, request_id).exists() and not layout.request_path(root, "canceled", request_id).exists():
            return True
        try:
            path.unlink()
            queue_store.fsync_directory(path.parent)
        except OSError:
            return True
        return False
    finally:
        queue_store.release_owner_lock(lock, remove=False)


def retain_target_lease(root: Path, request: dict[str, object]) -> None:
    """Persist a cross-queue recovery lease while the target flock is held."""
    path = retained_target_lease_path(request)
    request_id = request.get("id")
    if path is None or not isinstance(request_id, str) or not request_id:
        raise ValueError("manual recovery requires a session target and request id")
    record = {
        "version": 1,
        "request_id": request_id,
        "queue_dir": str(root.resolve()),
        "recorded_at": time.time(),
    }
    lock = acquire_retained_target_lease_lock(request, blocking=True)
    if lock is None:  # pragma: no cover - blocking lock acquisition always succeeds
        raise RuntimeError(f"could not lock retained target lease: {path}")
    try:
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"retained target lease is unreadable: {path}") from exc
            if not isinstance(existing, dict) or (
                existing.get("request_id") != request_id
                or existing.get("queue_dir") != str(root.resolve())
            ):
                raise RuntimeError(f"retained target lease already exists: {path}")
            return
        queue_store.write_request(path, record)
    finally:
        queue_store.release_owner_lock(lock, remove=False)


def release_retained_target_lease(root: Path, request: dict[str, object]) -> None:
    """Release only the cross-queue recovery lease owned by this callback."""
    path = retained_target_lease_path(request)
    request_id = request.get("id")
    if path is None or not isinstance(request_id, str):
        return
    # Ordinary ACKs have no retained lease. Avoid creating a target-lock file
    # from inside the resumed agent sandbox just to discover that fact.
    if not path.exists():
        return
    lock = acquire_retained_target_lease_lock(request, blocking=True)
    if lock is None:  # pragma: no cover - blocking lock acquisition always succeeds
        raise RuntimeError(f"could not lock retained target lease: {path}")
    try:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"retained target lease is unreadable: {path}") from exc
        if not isinstance(record, dict):
            raise RuntimeError(f"retained target lease is malformed: {path}")
        if record.get("request_id") != request_id or record.get("queue_dir") != str(root.resolve()):
            return
        path.unlink()
        queue_store.fsync_directory(path.parent)
    finally:
        queue_store.release_owner_lock(lock, remove=False)


def reconcile_acknowledged_retained_leases(root: Path) -> None:
    """Let the daemon release leases when a sandboxed ACK cannot write target-locks."""
    for state in ("running", "failed", "done", "canceled"):
        for path in sorted((root / state).glob("*.json")):
            request_id = path.stem
            if not layout.ack_path(root, request_id).exists() and not layout.request_path(root, "canceled", request_id).exists():
                continue
            try:
                request = dispatch.load_request(path)
                release_retained_target_lease(root, request)
            except (OSError, RuntimeError) as exc:
                print(
                    f"reins: warning: deferred retained target lease reconciliation "
                    f"for {request_id}: {exc}",
                    file=sys.stderr,
                )


def acquire_target_lock(
    request: dict[str, object],
    *,
    blocking: bool,
) -> tuple[object, Path] | None:
    path = target_lock_path(request)
    if path is None:
        return None
    return queue_store.acquire_path_lock(path, blocking=blocking)


def target_lock_is_held(request: dict[str, object]) -> bool:
    path = target_lock_path(request)
    if path is None:
        return False
    if not path.exists():
        return False
    lock = queue_store.acquire_path_lock(path, blocking=False)
    if lock is None:
        return True
    queue_store.release_owner_lock(lock, remove=False)
    return False


def reap_background_resumes() -> None:
    now = time.monotonic()
    for pid, delivery in list(_BACKGROUND_RESUMES.items()):
        if delivery.process.poll() is not None:
            finish_delivery_worker(delivery.process)
            _BACKGROUND_RESUMES.pop(pid, None)
            continue
        if now >= delivery.deadline:
            print(
                f"reins: warning: acknowledged resume {delivery.request_id} "
                "exceeded its timeout; terminating the resume process",
                file=sys.stderr,
            )
            stop_resume_process(delivery.process)
            _BACKGROUND_RESUMES.pop(pid, None)


def pi_resume_reached_session(request: dict[str, object], request_id: str) -> bool:
    """Whether a managed Pi resume left this callback in the bound session file."""
    target = request.get("target")
    if dispatch.request_agent(request) != "pi" or not isinstance(target, dict):
        return False
    session = target.get("session")
    return isinstance(session, str) and pi_channel.session_has_callback(session, request_id)


def target_has_live_resume(root: Path, request: dict[str, object]) -> bool:
    reap_background_resumes()
    key = callback_target_key(request)
    if key is None:
        return False
    if target_lock_is_held(request):
        return True
    if retained_target_lease_is_held(request):
        return True
    if any(delivery.target_key == key for delivery in _BACKGROUND_RESUMES.values()):
        return True
    for state in ("running", "canceled", "failed"):
        for path in (root / state).glob("*.json"):
            try:
                live_request = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if (
                isinstance(live_request, dict)
                and callback_target_key(live_request) == key
                and (
                    (state == "failed" and live_request.get("retain_target_lease") is True)
                    or queue_store.delivery_lock_is_held(root, path.stem)
                )
            ):
                return True
    return False


def waits_for_open_session(root: Path, request: dict[str, object], request_id: str) -> bool:
    """A message that may not start a process waits for its closed Pi session to reopen."""
    return (request.get("wake") == "never" and request.get("agent") == "pi" and "pi_published_at" not in request
            and not layout.ack_path(root, request_id).exists() and not pi_channel.live_mailbox_open(request))


def waits_for_open_window(root: Path, request: dict[str, object], request_id: str) -> bool:
    """A resume beside an open Claude Code window forks its transcript, so the callback waits for that window."""
    target = request.get("target")
    if (dispatch.request_agent(request) != "claude" or not isinstance(target, dict)
            or target.get("kind") != "session" or not isinstance(target.get("session"), str)):
        return False
    environment = resume_environment.merged(os.environ, resume_environment.load(root, request_id))
    return claude_session.open_window(target["session"], environment) is not None


def select_pending(root: Path, now: float) -> Path | None:
    for path in sorted((root / "pending").glob("*.json")):
        if next_attempt_at(path) > now:
            continue
        try:
            request = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return path
        if isinstance(request, dict) and capabilities.unsupported(request.get("requires")) and records.is_callback(request):
            continue  # needs a newer tower; leave it untouched rather than deliver it halfway
        if isinstance(request, dict) and (
            layout.ack_path(root, path.stem).exists() or queue_store.is_canceled(root, path.stem)
        ):
            # Finalize an ACKed/canceled callback even when its delivery route
            # never lets the daemon start a resume (for example live-only DSH).
            return path
        if isinstance(request, dict) and (request.get("callback_mode") == "manual"
                                         or (request.get("delivery_state") == "queued_awaiting_ack"
                                             and not layout.ack_path(root, path.stem).exists())
                                         or is_live_delivery(request)
                                         or target_has_live_resume(root, request)
                                         or claude_session.live_watcher_is_held(request)
                                         or waits_for_open_session(root, request, path.stem)
                                         or waits_for_open_window(root, request, path.stem)):
            continue
        return path
    return None


def retry_delay(args: argparse.Namespace, attempt: int) -> float:
    delay = max(0.0, float(getattr(args, "retry_delay", DEFAULT_RETRY_DELAY)))
    backoff = max(1.0, float(getattr(args, "retry_backoff", DEFAULT_RETRY_BACKOFF)))
    return delay * (backoff ** max(0, attempt - 1))


def terminate_process_group(process: subprocess.Popen[str], grace: float = 5.0) -> None:
    if os.name == "nt":
        # Delivery workers own kill-on-close Jobs before launching an Agent.
        # Terminating that worker closes its final handle and its entire tree.
        process.terminate()
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return
    if process.poll() is not None:
        process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def desktop_submission_pending(root: Path, request: dict[str, object]) -> bool:
    """Recognize this callback's durable submission intent after worker loss.

    A parent timeout or process crash can preempt the worker before it writes
    its final JSON result. The pre-submission lease is then authoritative, not
    the worker's OS exit code. Unreadable intent remains uncertain.
    """
    path = retained_target_lease_path(request)
    if path is None:
        return False
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    if not isinstance(record, dict):
        return True
    return (record.get("request_id") == request.get("id")
            and record.get("queue_dir") == str(root.resolve()))


def start_desktop_app_server_turn(payload: dict[str, object]) -> app_server.DesktopAppServerDelivery | None:
    request = payload.get("request")
    if not isinstance(request, dict):
        return None
    if request.get("callback_mode") == "app-server":
        from . import native
        return native.start_delivery(payload)
    if request.get("callback_mode") == "manual":
        raise app_server.CallbackTransportBlocked("manual callback mode does not launch an Agent")
    if dispatch.request_agent(request) != "codex":
        return None  # The Desktop App Server delivery path is Codex-only.
    socket_path = app_server.desktop_app_server_socket(request)
    target = request.get("target")
    sandbox_policy = app_server.desktop_sandbox_policy(request)
    explicit_desktop = request.get("callback_mode") == "desktop"
    if socket_path is None or sandbox_policy is None or not isinstance(target, dict) or not isinstance(target.get("session"), str):
        if explicit_desktop:
            raise app_server.CallbackTransportBlocked("Desktop callbacks require a bound session, an App Server socket and workspace-write callback policy")
        return None
    connection = app_server.AppServerConnection(socket_path, min(15.0, max(1.0, float(payload["timeout"]))))
    delivery_context = app_server.desktop_delivery_context(payload)
    turn_start_submitted = False
    try:
        connection.connect()
        connection.request(
            "initialize",
            {
                "clientInfo": {"name": "harness-reins", "version": __version__},
                "capabilities": {"experimentalApi": True},
            },
        )
        connection.notify("initialized", {})
        resumed = connection.request("thread/resume", {"threadId": target["session"], "excludeTurns": True})
        thread = resumed.get("thread") if isinstance(resumed, dict) else None
        status = thread.get("status") if isinstance(thread, dict) else None
        if isinstance(status, dict) and status.get("type") == "active":
            raise app_server.CallbackTargetBusy("original session still has an active turn")
        turn_params: dict[str, object] = {
            "threadId": target["session"],
            "cwd": str(payload["cwd"]),
            "input": [{"type": "text", "text": str(payload["prompt"])}],
            "sandboxPolicy": sandbox_policy,
        }
        approval_policy = request.get("approval_policy")
        if isinstance(approval_policy, str) and approval_policy:
            turn_params["approvalPolicy"] = approval_policy
        approvals_reviewer = request.get("approvals_reviewer")
        if isinstance(approvals_reviewer, str) and approvals_reviewer:
            turn_params["approvalsReviewer"] = approvals_reviewer
        if delivery_context is not None:
            root, callback = delivery_context
            # The parent deadline begins before this worker's startup and RPC
            # handshake. Persist before sending any turn/start bytes so an
            # earlier parent timeout or worker crash cannot authorize replay.
            retain_target_lease(root, callback)
            if layout.ack_path(root, str(callback["id"])).exists() or queue_store.is_canceled(root, str(callback["id"])):
                try:
                    release_retained_target_lease(root, callback)
                except (OSError, RuntimeError) as cleanup_error:
                    # ACK/cancel is authoritative even when global cleanup is
                    # unavailable. Do not fall through to another transport.
                    print(f"reins: acknowledged/canceled Desktop intent cleanup deferred: {cleanup_error}", file=sys.stderr)
                return app_server.DesktopAppServerDelivery(None, target["session"], None)
        turn_start_submitted = True
        started = connection.request("turn/start", turn_params)
        turn_id = None
        if isinstance(started, dict) and isinstance(started.get("turn"), dict):
            candidate = started["turn"].get("id")
            turn_id = candidate if isinstance(candidate, str) and candidate else None
        if turn_id is None:
            print(
                "reins: desktop App Server accepted turn/start without a turn id; "
                "waiting for ACK or timeout",
                file=sys.stderr,
            )
            connection.close()
            return app_server.DesktopAppServerDelivery(None, target["session"], None)
        delivery = app_server.DesktopAppServerDelivery(connection, target["session"], turn_id)
        connection = None
        return delivery
    except app_server.AppServerRpcError as exc:
        if not turn_start_submitted and CODEX_ACTIVE_WRITER in str(exc):
            raise app_server.CallbackTransportBlocked("bound session belongs to another Core; repair the connection before retrying") from exc
        if turn_start_submitted:
            if delivery_context is not None:
                try:
                    release_retained_target_lease(*delivery_context)
                except (OSError, RuntimeError) as cleanup_error:
                    print(
                        f"reins: explicit Desktop rejection could not clear submission intent: {cleanup_error}; "
                        "waiting for ACK or manual recovery without CLI fallback",
                        file=sys.stderr,
                    )
                    return app_server.DesktopAppServerDelivery(None, target["session"], None)
            if explicit_desktop:
                raise app_server.CallbackTransportBlocked("explicit Desktop route rejected the callback; CLI fallback disabled") from exc
            print(
                f"reins: desktop App Server rejected turn/start: {exc}; falling back to CLI",
                file=sys.stderr,
            )
        else:
            if explicit_desktop:
                raise app_server.CallbackTransportBlocked("explicit Desktop route could not resume the bound session; CLI fallback disabled") from exc
            print(f"reins: desktop App Server delivery unavailable: {exc}; falling back to CLI", file=sys.stderr)
        return None
    except (OSError, ValueError, TypeError, app_server.AppServerProtocolError) as exc:
        if turn_start_submitted:
            print(
                f"reins: desktop App Server turn/start outcome is unknown: {exc}; "
                "waiting for ACK or timeout without CLI fallback",
                file=sys.stderr,
            )
            return app_server.DesktopAppServerDelivery(None, target["session"], None)
        if explicit_desktop:
            raise app_server.CallbackTransportBlocked("explicit Desktop route is unavailable; CLI fallback disabled") from exc
        print(f"reins: desktop App Server delivery unavailable: {exc}; falling back to CLI", file=sys.stderr)
        return None
    finally:
        if connection is not None:
            connection.close()


def wait_for_remote_delivery(payload: dict, delivery, timeout: float) -> dict:
    """Wait for native delivery without starting or killing its owner."""
    from . import native
    if isinstance(delivery, native.NativeAppServerDelivery):
        return native.wait_for_delivery(payload, delivery, timeout)
    delivery_context = app_server.desktop_delivery_context(payload)
    delivery_name = "pi_live_session" if isinstance(delivery, pi_channel.PiDelivery) else "desktop_app_server"
    deadline = time.monotonic() + timeout
    while True:
        acknowledged = Path(str(payload["ack_path"])).exists()
        canceled = Path(str(payload["canceled_path"])).exists()
        if acknowledged:
            if delivery_context is not None:
                release_retained_target_lease(*delivery_context)
            result = {
                "returncode": 0,
                "delivery": delivery_name,
                "skipped": "acknowledged",
            }
            break
        if canceled:
            if delivery_context is not None:
                release_retained_target_lease(*delivery_context)
            result = {"returncode": 0, "delivery": delivery_name, "skipped": "canceled"}
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            request = payload.get("request")
            queue_root = payload.get("queue_dir")
            if isinstance(request, dict) and isinstance(queue_root, str):
                retain_target_lease(Path(queue_root), request)
                if Path(str(payload["ack_path"])).exists() or Path(str(payload["canceled_path"])).exists():
                    release_retained_target_lease(Path(queue_root), request)
                    result = {
                        "returncode": 0,
                        "delivery": delivery_name,
                        "skipped": "acknowledged_or_canceled_after_timeout",
                    }
                    break
            result = {
                "returncode": 125,
                "timed_out": True,
                "delivery": delivery_name,
                "manual_recovery_required": True,
            }
            break
        if delivery.wait_for_completion(min(0.1, remaining)):
            acknowledged = Path(str(payload["ack_path"])).exists()
            if delivery_context is not None:
                # A matching turn/completed is a known outcome. Missing
                # ACK may use the normal at-least-once callback policy.
                release_retained_target_lease(*delivery_context)
            result = {
                "returncode": 0 if acknowledged else 1,
                "delivery": delivery_name,
                "turn_completed": True,
            }
            break
    return result


def delivery_worker_main() -> int:
    """Own one callback delivery and keep its locks out of agent children."""
    encoded_fds = os.environ.get("CODEX_LONG_TASK_DELIVERY_LOCK_FDS")
    if os.name == "nt":
        lock_fds = windows_process.inherited_lock_fds()
        windows_process.enter_worker_job()
    elif encoded_fds:
        lock_fds = [int(value) for value in json.loads(encoded_fds)]
    else:  # Backward compatibility with delivery workers launched by 0.4.1.
        lock_fds = [int(os.environ["CODEX_LONG_TASK_DELIVERY_LOCK_FD"])]
    payload = json.load(sys.stdin)
    delivery_context = app_server.desktop_delivery_context(payload)
    command = payload["command"]
    prompt = str(payload["prompt"])
    # Recheck here as well as in the parent: an ACK/cancel may arrive while the
    # worker starts. A callback already skipped must not consume a sequence.
    if not (Path(str(payload["ack_path"])).exists() or Path(str(payload["canceled_path"])).exists()):
        prompt = reminder_policy.select_delivery_prompt(payload)
    timeout = max(1.0, float(payload["timeout"]))
    result: dict[str, object] = {"returncode": 127}
    process: subprocess.Popen[str] | None = None
    desktop_delivery: app_server.DesktopAppServerDelivery | None = None
    pi_delivery: pi_channel.PiDelivery | None = None

    class WorkerInterrupted(Exception):
        def __init__(self, signum: int) -> None:
            self.signum = signum

    def interrupt(signum: int, _frame: object) -> None:
        raise WorkerInterrupted(signum)

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    try:
        if Path(str(payload["ack_path"])).exists():
            result = {"returncode": 0, "skipped": "already_acknowledged"}
        elif Path(str(payload["canceled_path"])).exists():
            result = {"returncode": 0, "skipped": "canceled"}
        elif isinstance(payload.get("request"), dict) and is_live_delivery(payload["request"]):
            result = {
                "returncode": 126,
                "delivery_state": "blocked",
                "error": "this agent receives callbacks only from a live `reins wait`; no resume was attempted",
            }
        elif payload.get("request", {}).get("callback_mode") == "codex-queue":
            payload["prompt"] = prompt
            result = native_queue.publish(payload)
        elif (desktop_delivery := start_desktop_app_server_turn(payload)) is not None:
            result = wait_for_remote_delivery(payload, desktop_delivery, timeout)
        elif (isinstance(payload.get("request"), dict) and payload["request"].get("wake") == "never"
              and waits_for_open_session(Path(str(payload["queue_dir"])), payload["request"], str(payload["request"]["id"]))):
            result = {"returncode": 123, "delivery_state": "waiting_for_open"}  # it closed after selection
        else:
            request = payload.get("request")
            if isinstance(request, dict) and dispatch.request_agent(request) == "pi":
                prompt = pi_channel.with_unacknowledged_notice(Path(str(payload["queue_dir"])), request, prompt)
                payload["prompt"] = prompt
                pi_delivery = pi_channel.prepare_delivery(payload)
                if not pi_delivery.online:
                    command = pi_delivery.command
            if pi_delivery is not None and (Path(str(payload["ack_path"])).exists() or Path(str(payload["canceled_path"])).exists()):
                release_retained_target_lease(*delivery_context)
                result = {"returncode": 0, "delivery": pi_delivery.delivery_name, "skipped": "acknowledged_or_canceled"}
            elif pi_delivery is not None and pi_delivery.online:
                # The live Pi process owns it now; the daemon supervises the ACK
                # without blocking other callbacks and task launches.
                result = {"returncode": pi_channel.PUBLISHED_RETURN_CODE, "delivery": pi_delivery.delivery_name}
            else:
                session_environment = payload.get("environment")
                if pi_delivery is not None:
                    resume_env = pi_delivery.environment
                elif isinstance(session_environment, dict):
                    resume_env = resume_environment.merged(os.environ, session_environment)
                else:
                    resume_env = None
                target_key = callback_target_key(request) if isinstance(request, dict) else None
                if target_key is not None and dispatch.request_agent(request) == "claude":
                    # This worker holds the session's target lock for the whole resume;
                    # a `reins wait` inside the resume must still reach its own session.
                    resume_env = dict(os.environ if resume_env is None else resume_env)
                    resume_env[claude_session.RESUMED_TARGET_ENV] = target_key
                # A Codex thread whose original process is still running refuses a
                # second writer before any turn starts; that means busy, not failed.
                codex_resume = pi_delivery is None and isinstance(request, dict) and dispatch.request_agent(request) == "codex"
                process = subprocess.Popen(
                    shell.process_command([str(part) for part in command]),
                    env=resume_env,
                    stdin=subprocess.PIPE,
                    stdout=sys.stderr,
                    stderr=subprocess.PIPE if codex_resume else sys.stderr,
                    text=True,
                    encoding="utf-8",
                    cwd=str(payload["cwd"]),
                    close_fds=True,
                    **(windows_process.background_popen_kwargs() if os.name == "nt" else {"start_new_session": True}),
                )
                if pi_delivery is not None:
                    pi_delivery.monitor_owner(process)
                try:
                    _, errors = process.communicate(input=prompt, timeout=timeout)
                    if errors:
                        sys.stderr.write(errors)
                    result = {"returncode": int(process.returncode)}
                    if codex_resume and process.returncode != 0 and CODEX_ACTIVE_WRITER in (errors or ""):
                        result = {"returncode": 123, "delivery_state": "waiting_for_idle"}
                except subprocess.TimeoutExpired:
                    terminate_process_group(process)
                    result = {"returncode": 124, "timed_out": True}
                if pi_delivery is not None:
                    if Path(str(payload["ack_path"])).exists() or Path(str(payload["canceled_path"])).exists():
                        release_retained_target_lease(*delivery_context)
                        result = {"returncode": 0, "delivery": "pi_managed_resume"}
                    else:
                        result = {"returncode": 125, "delivery": "pi_managed_resume", "manual_recovery_required": True}
    except app_server.CallbackTargetBusy:
        result = {"returncode": 123, "delivery_state": "waiting_for_idle"}
    except app_server.CallbackTransportBlocked as exc:
        print(f"reins: callback blocked: {exc}", file=sys.stderr)
        result = {"returncode": 126, "delivery_state": "blocked", "error": str(exc)}
    except WorkerInterrupted as exc:
        if process is not None:
            terminate_process_group(process)
        result = {"returncode": 128 + exc.signum, "interrupted": exc.signum}
    except BaseException as exc:
        if process is not None:
            terminate_process_group(process)
        result = {"returncode": 127, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if desktop_delivery is not None:
            desktop_delivery.close()
        if pi_delivery is not None:
            pi_delivery.close()
        # A Windows worker's Job may still be collecting Agent descendants.
        # Keep its raw lease handles until OS process teardown, not before it.
        for lock_fd in ([] if os.name == "nt" else lock_fds):
            try:
                os.close(lock_fd)
            except OSError:
                pass

    try:
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        pass
    return 0


def delivery_worker_command() -> list[str]:
    return worker_command("_delivery-worker")


def finish_delivery_worker(process: subprocess.Popen[str]) -> int:
    process.wait()
    output = ""
    if process.stdout is not None:
        output = process.stdout.read()
        process.stdout.close()
    for line in reversed(output.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        returncode = payload.get("returncode") if isinstance(payload, dict) else None
        if isinstance(returncode, int):
            return returncode
    return process.returncode if process.returncode != 0 else 127


def stop_resume_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=7.0)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait()
    finish_delivery_worker(process)


def run_resume_until_exit_or_ack(
    root: Path,
    request_id: str,
    request: dict[str, object],
    args: argparse.Namespace,
) -> tuple[subprocess.CompletedProcess[str] | None, bool, bool]:
    command = resume_command(request)
    delivery_lock = queue_store.acquire_owner_lock(root, layout.delivery_lock_id(request_id), blocking=False)
    if delivery_lock is None:
        return None, layout.ack_path(root, request_id).exists(), True
    if layout.ack_path(root, request_id).exists():
        queue_store.release_owner_lock(delivery_lock, remove=False)
        return subprocess.CompletedProcess(command, 0), True, False
    if queue_store.is_canceled(root, request_id):
        queue_store.release_owner_lock(delivery_lock, remove=False)
        return None, False, False
    target_lock = acquire_target_lock(request, blocking=False)
    if target_lock is None:
        queue_store.release_owner_lock(delivery_lock, remove=False)
        raise TargetLeaseUnavailable(callback_target_key(request) or "unknown target")
    if retained_target_lease_is_held(request):
        queue_store.release_owner_lock(delivery_lock, remove=False)
        queue_store.release_owner_lock(target_lock, remove=False)
        raise TargetLeaseUnavailable(callback_target_key(request) or "unknown target")
    if layout.ack_path(root, request_id).exists():
        queue_store.release_owner_lock(delivery_lock, remove=False)
        queue_store.release_owner_lock(target_lock, remove=False)
        return subprocess.CompletedProcess(command, 0), True, False
    if queue_store.is_canceled(root, request_id):
        queue_store.release_owner_lock(delivery_lock, remove=False)
        queue_store.release_owner_lock(target_lock, remove=False)
        return None, False, False
    delivery_handle, _ = delivery_lock
    target_handle, _ = target_lock
    timeout = max(1.0, float(getattr(args, "resume_timeout", DEFAULT_RESUME_TIMEOUT)))
    deadline = time.monotonic() + timeout
    payload = {
        "command": command,
        "prompt": str(request["prompt"]),
        "callback_hook_path": str(layout.callback_hook_path()),
        "cwd": resume_cwd(request),
        "request": request,
        "queue_dir": str(root),
        "timeout": timeout,
        "ack_path": str(layout.ack_path(root, request_id)),
        "canceled_path": str(layout.request_path(root, "canceled", request_id)),
        # The submitting session's environment; the worker resumes with it on top of its own.
        "environment": resume_environment.load(root, request_id),
    }
    env = os.environ.copy()
    if os.name == "nt":
        env["PYTHONIOENCODING"] = "utf-8"
    lock_fds = (delivery_handle.fileno(), target_handle.fileno())
    env["CODEX_LONG_TASK_DELIVERY_LOCK_FDS"] = json.dumps(lock_fds)
    env["CODEX_LONG_TASK_DELIVERY_LOCK_FD"] = str(delivery_handle.fileno())
    try:
        if os.name == "nt":
            process = windows_process.spawn_delivery_worker(
                delivery_worker_command(), lock_fds, env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8",
            )
        else:
            process = subprocess.Popen(
                delivery_worker_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                text=True, encoding="utf-8", close_fds=True, pass_fds=lock_fds, env=env,
            )
    except BaseException:
        queue_store.release_owner_lock(delivery_lock, remove=False)
        queue_store.release_owner_lock(target_lock, remove=False)
        raise
    queue_store.close_parent_lock_copy(delivery_lock)
    queue_store.close_parent_lock_copy(target_lock)
    try:
        if process.stdin is not None:
            try:
                process.stdin.write(json.dumps(payload, ensure_ascii=False))
                process.stdin.flush()
            except BrokenPipeError:
                pass
            finally:
                process.stdin.close()

        while True:
            acked = layout.ack_path(root, request_id).exists()
            canceled = queue_store.is_canceled(root, request_id)
            returncode = process.poll()
            if acked or canceled:
                if returncode is None:
                    key = callback_target_key(request)
                    if key is not None:
                        _BACKGROUND_RESUMES[process.pid] = BackgroundResume(
                            process=process,
                            target_key=key,
                            deadline=deadline,
                            request_id=request_id,
                        )
                else:
                    child_returncode = finish_delivery_worker(process)
                    return subprocess.CompletedProcess(command, child_returncode), acked, False
                result = subprocess.CompletedProcess(command, returncode if returncode is not None else 0) if acked else None
                return result, acked, returncode is None
            if returncode is not None:
                child_returncode = finish_delivery_worker(process)
                acked = layout.ack_path(root, request_id).exists()
                if (not acked and child_returncode not in (pi_channel.PUBLISHED_RETURN_CODE, native_queue.PUBLISHED_RETURN_CODE)
                        and not queue_store.is_canceled(root, request_id) and desktop_submission_pending(root, request)):
                    child_returncode = 125
                return subprocess.CompletedProcess(command, child_returncode), acked, False
            if time.monotonic() >= deadline:
                stop_resume_process(process)
                acked = layout.ack_path(root, request_id).exists()
                code = (125 if not acked and not queue_store.is_canceled(root, request_id)
                        and desktop_submission_pending(root, request) else 124)
                return subprocess.CompletedProcess(command, code), acked, False
            time.sleep(0.1)
    except BaseException:
        stop_resume_process(process)
        raise


def process_one(root: Path, args: argparse.Namespace) -> bool:
    queue_store.ensure_daemon_dirs(root)
    path = select_pending(root, time.time())
    if path is None:
        return False

    request_id = path.stem
    if queue_store.is_canceled(root, request_id):
        queue_store.remove_live_request_copies(root, request_id)
        return True
    try:
        running = queue_store.move_request(path, root / "running")
    except FileNotFoundError:
        return True
    except Exception as exc:
        print(f"reins: warning: could not claim {path.name}: {exc}", file=sys.stderr)
        return True
    if queue_store.is_canceled(root, request_id):
        queue_store.remove_live_request_copies(root, request_id)
        return True

    try:
        request = dispatch.load_request(running)
        request_id = str(request.get("id", running.stem))
        if layout.ack_path(root, request_id).exists():
            queue_store.move_request(running, root / "done")
            return True
        deferred_until = request.get("next_attempt_at")
        if isinstance(deferred_until, (int, float)) and deferred_until > time.time():
            # select_pending saw it due, but it was deferred before this claim:
            # `reins wait` can deliver it to the live session and exit in between.
            # Delivering it now would duplicate that delivery.
            queue_store.move_request(running, root / "pending")
            return True
        attempts = int(request.get("attempts", 0)) + 1
        request["attempts"] = attempts
        request.pop("next_attempt_at", None)
        queue_store.write_request(running, request)
        try:
            result, acked, resume_still_running = run_resume_until_exit_or_ack(root, request_id, request, args)
        except TargetLeaseUnavailable as exc:
            # A daemon for another queue won the cross-queue race after this
            # request was selected. Put it back without consuming a retry.
            request["attempts"] = max(0, attempts - 1)
            request["last_deferred_reason"] = f"target lease held: {exc}"
            if not running.exists() or queue_store.is_canceled(root, request_id):
                return True
            queue_store.write_request(running, request)
            queue_store.move_request(running, root / "pending")
            return True
        if resume_still_running:
            return True
        if result is None:
            return True
        if result.returncode == native_queue.PUBLISHED_RETURN_CODE and not acked:
            request["attempts"] = max(0, attempts - 1)
            request["delivery_state"] = "queued_awaiting_ack"
            request.setdefault("live_delivered_at", time.time())
            request["last_deferred_reason"] = "Published via native codex queue; awaiting receiver ACK (no re-send)."
            if running.exists() and not queue_store.is_canceled(root, request_id):
                queue_store.write_request(running, request)
                queue_store.move_request(running, root / "pending")
            return True
        if result.returncode == pi_channel.PUBLISHED_RETURN_CODE and not acked:
            # Not a failed attempt: the retained target lease keeps later
            # callbacks for this session in order until ACK or recovery.
            now = time.time()
            request["attempts"] = max(0, attempts - 1)
            request.setdefault("pi_published_at", now)
            request.setdefault("live_delivered_at", now)
            request["last_deferred_reason"] = pi_channel.PUBLISHED_REASON
            if running.exists() and not queue_store.is_canceled(root, request_id):
                queue_store.write_request(running, request)
                queue_store.move_request(running, root / "pending")
            return True
        if result.returncode == 123 and not acked:
            request["attempts"] = max(0, attempts - 1)
            request["next_attempt_at"] = time.time() + 10
            request["last_deferred_reason"] = ("session is closed; the message stays in its inbox" if request.get("wake") == "never"
                                               else "original session has an active turn")
            if running.exists() and not queue_store.is_canceled(root, request_id):
                queue_store.write_request(running, request)
                queue_store.move_request(running, root / "pending")
            return True
        if result.returncode == 124 and not acked:
            request["last_error"] = "agent resume timed out"
            print(
                f"reins: warning: daemon callback {running.name} timed out",
                file=sys.stderr,
            )
        if result.returncode == 126 and not acked:
            request["delivery_state"] = "blocked"
            if request.get("callback_mode") == "desktop":
                request["last_error"] = "callback transport blocked; move it to the native queue with " + app_server.desktop_route_repair(request)
            else:
                request["last_error"] = "callback transport blocked; repair the original session connection, then use reins retry --id " + request_id
        if result.returncode == 125 and not acked and pi_resume_reached_session(request, request_id):
            # The resumed Pi turn saw the callback but ended without an ACK. It is
            # in the session, so order is kept and nothing needs replaying: later
            # callbacks continue and name this one until it is ACKed.
            request.pop("retain_target_lease", None)
            request["pi_session_observed"] = True
            request["last_error"] = ("Pi callback reached the session but was not ACKed by the resumed turn. Later "
                                     f"callbacks for this session continue and name it. Inspect the session, then "
                                     f"reins ack {request_id}")
            destination_dir = root / "failed"
            print(f"reins: warning: daemon callback {running.name} reached the session without an ACK; "
                  "later callbacks continue", file=sys.stderr)
            if running.exists():
                queue_store.write_request(running, request)
                release_retained_target_lease(root, request)
            else:
                return True
        elif result.returncode == 125 and not acked:
            delivery_name = "Codex queue" if request.get("callback_mode") == "codex-queue" else "Pi" if dispatch.request_agent(request) == "pi" else "Desktop"
            request["last_error"] = delivery_name + " callback outcome is unknown; automatic retry suppressed to prevent duplicate delivery"
            request["retain_target_lease"] = True
            destination_dir = root / "failed"
            print(
                f"reins: warning: daemon callback {running.name} has an unknown {delivery_name} outcome; "
                "manual recovery is required to avoid duplicate delivery",
                file=sys.stderr,
            )
            if running.exists():
                queue_store.write_request(running, request)
            else:
                return True
        else:
            if result.returncode == 125 and acked:
                release_retained_target_lease(root, request)
            destination_dir = root / ("done" if acked else "failed")
        if result.returncode != 0:
            print(
                f"reins: warning: daemon callback {running.name} exited with {result.returncode}",
                file=sys.stderr,
            )
        if not acked and result.returncode not in (125, 126):
            request.setdefault("last_error", f"missing acknowledgement marker after exit {result.returncode}")
            max_retries = max(0, int(getattr(args, "retries", DEFAULT_RETRIES)))
            if attempts <= max_retries:
                delay = retry_delay(args, attempts)
                request["next_attempt_at"] = time.time() + delay
                if not running.exists():
                    print(
                        f"reins: callback {request_id} disappeared from running; assuming it was canceled",
                        file=sys.stderr,
                    )
                    return True
                queue_store.write_request(running, request)
                queue_store.move_request(running, root / "pending")
                print(
                    f"reins: warning: daemon callback {running.name} was not acknowledged; "
                    f"retry {attempts}/{max_retries} in {delay:.1f}s",
                    file=sys.stderr,
                )
                return True
        if result.returncode == 126 and running.exists():
            queue_store.write_request(running, request)
    except Exception as exc:
        destination_dir = root / "failed"
        print(f"reins: warning: daemon failed to process {running.name}: {exc}", file=sys.stderr)

    if not running.exists():
        print(f"reins: callback {running.stem} disappeared from running; assuming it was canceled", file=sys.stderr)
        return True
    try:
        finalized = queue_store.move_request(running, destination_dir)
        # Close the final-attempt race with ack(): the marker may become
        # durable after ack() looked for failed/<id>, but just before this
        # process moved running/<id> there.  Once failed/ is visible, either
        # this reconciliation or ack() itself will converge it to done/.
        if destination_dir == root / "failed" and layout.ack_path(root, request_id).exists():
            try:
                queue_store.move_request(finalized, root / "done")
                if request.get("retain_target_lease") is True:
                    release_retained_target_lease(root, request)
            except FileNotFoundError:
                pass
    except FileNotFoundError:
        return True
    except Exception as exc:
        print(f"reins: warning: could not finalize {running.name}: {exc}", file=sys.stderr)
    return True


def recover_running(root: Path) -> None:
    queue_store.ensure_daemon_dirs(root)
    for path in sorted((root / "running").glob("*.json")):
        if queue_store.delivery_lock_is_held(root, path.stem):
            continue
        try:
            request = dispatch.load_request(path)
            request_id = str(request.get("id", path.stem))
            destination = root / ("done" if layout.ack_path(root, request_id).exists() else "pending")
            queue_store.move_request(path, destination)
        except FileNotFoundError:
            if queue_store.is_canceled(root, path.stem):
                continue
            print(
                f"reins: running callback {path.stem} changed state during recovery",
                file=sys.stderr,
            )
        except Exception as exc:
            print(f"reins: warning: failed to recover {path.name}: {exc}", file=sys.stderr)
            if not path.exists():
                continue
            try:
                queue_store.move_request(path, root / "failed")
            except FileNotFoundError:
                continue
            except Exception as move_exc:
                print(
                    f"reins: warning: could not quarantine {path.name}: {move_exc}",
                    file=sys.stderr,
                )
