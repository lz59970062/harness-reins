"""Deliver once through Codex's public queue CLI; the existing owner wakes itself.

No private Desktop IPC, alternate writer, idle/status loop or --resume is used.
Accepted messages stay pending until the receiving conversation executes ACK.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from ..core import capabilities
from ..store import layout, records
from ..system import shell
from . import resume_environment

PUBLISHED_RETURN_CODE = 121


def route_fields() -> dict[str, str]:
    configured = os.environ.get("REINS_CODEX_BIN")
    candidate = configured
    if not candidate and sys.platform == "darwin":
        for app in ("ChatGPT", "Codex"):
            resources = Path("/Applications") / (app + ".app") / "Contents/Resources"
            for path in (resources / "codex-cli/CodexCLI.app/Contents/MacOS/codex", resources / "codex"):
                if path.is_file() and os.access(path, os.X_OK):
                    candidate = str(path)
                    break
            if candidate:
                break
    candidate = candidate or "codex"
    found = shutil.which(candidate)
    # Preserve an absolute missing path for a useful preflight diagnostic.
    executable = str(Path(found or candidate).expanduser().absolute())
    return {"callback_codex_bin": executable, "callback_codex_home": str(layout.codex_home().resolve())}


def _hosted_environment(payload: dict[str, object]) -> dict[str, str]:
    session = payload.get("environment")
    return resume_environment.merged(os.environ, session) if isinstance(session, dict) else dict(os.environ)


def hosted_socket(payload: dict[str, object]) -> Path | None:
    """The Codex app-server daemon's socket, when a `cli` Codex callback's thread is loaded there.

    A thread opened in the Reins panel (or Codex Desktop over SSH, or `codex resume --remote`) lives on the
    daemon. A headless `codex exec resume` would load the same thread a second time and write it beside the
    daemon, so such a callback goes to the daemon instead: a turn started over its protocol (with the queue
    writable, delivery.start_desktop_app_server_turn), else `codex queue` (design V21, V22). A thread that
    is not loaded keeps the headless resume. Any failure here keeps it too, rather than costing an attempt.
    """
    from ..harness.codex import threads
    from . import dispatch

    request = payload.get("request")
    if (not isinstance(request, dict) or request.get("callback_mode") != "cli"
            or dispatch.request_agent(request) != "codex"):
        return None
    target = request.get("target")
    thread = target.get("session") if isinstance(target, dict) and target.get("kind") == "session" else None
    if not isinstance(thread, str) or not thread:
        return None
    try:
        env = _hosted_environment(payload)
        return threads.socket_path(env) if threads.loaded_on_daemon(thread, env) else None
    except Exception:
        return None


def hosted_request(payload: dict[str, object]) -> dict[str, object]:
    """The request with the `codex queue` route, for a hosted thread the daemon would not start a turn on."""
    from ..harness.codex import threads

    request = payload["request"]
    assert isinstance(request, dict)
    route = route_fields()
    route["callback_codex_home"] = str(threads.codex_home(_hosted_environment(payload)).resolve())
    requires = [*request.get("requires", []), capabilities.CODEX_QUEUE]
    return dict(request, **route, callback_mode="codex-queue", requires=sorted(set(requires)))


def validate_route(request: dict[str, object]) -> None:
    target = request.get("target")
    if (request.get("agent", "codex") != "codex" or not isinstance(target, dict)
            or target.get("kind") != "session" or not isinstance(target.get("session"), str)
            or not target["session"]):
        raise ValueError("codex-queue requires a bound Codex session")
    for key in ("callback_codex_bin", "callback_codex_home"):
        value = request.get(key)
        if not isinstance(value, str) or not value or "\0" in value or not Path(value).is_absolute():
            raise ValueError(f"codex-queue requires an absolute frozen {key}")
    if capabilities.CODEX_QUEUE not in request.get("requires", []):
        raise ValueError("codex-queue requires its native delivery capability")


def environment(request: dict[str, object], session: object = None) -> dict[str, str]:
    # A service coordinator's PATH may not reach the `node` an npm Codex needs;
    # the submitting session's environment, as a headless resume uses, does.
    base = resume_environment.merged(os.environ, session) if isinstance(session, dict) else os.environ
    return dict(base, CODEX_HOME=str(request["callback_codex_home"]))


def inspect_route(request: dict[str, object], *, timeout: float) -> dict[str, object]:
    try:
        validate_route(dict(request, requires=capabilities.required_for(request)))
        env = environment(request)
        result = subprocess.run(shell.process_command([str(request["callback_codex_bin"]), "queue", "--help"], env),
                                env=env, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, encoding="utf-8", timeout=max(1, timeout))
        if result.returncode != 0 or not all(flag in result.stdout for flag in ("--thread", "--message")):
            raise ValueError("selected Codex executable does not support native queue delivery")
        return {"status": "available", "transport": "codex_queue", "reason": "native_queue_command_available",
                "end_to_end_verified": False,
                "action": "Capability only: an open receiving owner must consume the message and ACK."}
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as exc:
        return {"status": "blocked", "transport": "codex_queue", "reason": "native_queue_unavailable",
                "end_to_end_verified": False, "action": str(exc)}


def queue_timeout(limit: object) -> float:
    """At most 30 s for `codex queue`, a short CLI call. The daemon's resume limit is None by default (no
    limit, for long resumed turns); that must not reach float() after the receipt is written."""
    return 30.0 if limit is None else min(30.0, max(1.0, float(limit)))


def publish(payload: dict[str, object]) -> dict[str, object]:
    from . import delivery

    request = payload["request"]
    validate_route(request)
    root = Path(str(payload["queue_dir"]))
    ident = str(request["id"])
    receipt = root / "native-queue" / (ident + ".json")
    if receipt.exists():
        # A killed sender may already have queued its message. Never infer
        # permission to replay from a missing/partial acceptance response.
        return {"returncode": 125, "delivery": "codex_queue", "manual_recovery_required": True}
    delivery.retain_target_lease(root, request)
    if layout.ack_path(root, ident).exists() or records.is_canceled(root, ident):
        delivery.release_retained_target_lease(root, request)
        return {"returncode": 0, "delivery": "codex_queue", "skipped": "acknowledged_or_canceled"}
    record = {"callback_id": ident, "thread_id": request["target"]["session"],
              "state": "submitting", "started_at": time.time()}
    records.write_request(receipt, record)
    env = environment(request, payload.get("environment"))
    try:
        # Windows resolves the executable here, so a missing Codex raises before anything is sent.
        command = shell.process_command([str(request["callback_codex_bin"]), "queue", "--thread",
                                         str(request["target"]["session"]), "--message", str(payload["prompt"])], env)
        result = subprocess.run(command, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding="utf-8",
                                timeout=queue_timeout(payload.get("timeout")))
    except OSError as exc:
        record.update(state="not_started", error=str(exc))
        records.write_request(receipt, record)
        receipt.unlink()
        delivery.release_retained_target_lease(root, request)
        return {"returncode": 126, "delivery": "codex_queue", "error": str(exc)}
    except subprocess.TimeoutExpired:
        record.update(state="unknown", error="native queue command timed out")
        records.write_request(receipt, record)
        return {"returncode": 125, "delivery": "codex_queue", "manual_recovery_required": True}
    accepted = re.search(r"Queued message ([0-9a-f-]+) for thread ([0-9a-f-]+)\.", result.stdout)
    if result.returncode == 0 and accepted and accepted[2] == request["target"]["session"]:
        record.update(state="queued", message_id=accepted[1], queued_at=time.time())
        records.write_request(receipt, record)
        # Codex owns the message now and keeps the order. An unanswered ACK must
        # not hold back later callbacks for this thread; the receipt stops a replay.
        delivery.release_retained_target_lease(root, request)
        return {"returncode": PUBLISHED_RETURN_CODE, "delivery": "codex_queue", "message_id": accepted[1]}
    if result.returncode > 0:
        # The CLI said it failed (no such thread, broken install): repairable with
        # `reins retry`. Only a kill or an unreadable success leaves the outcome unknown.
        print(f"reins: codex queue failed for {ident}: {result.stderr.strip()[-500:]}", file=sys.stderr)
        receipt.unlink()
        delivery.release_retained_target_lease(root, request)
        return {"returncode": 126, "delivery": "codex_queue", "error": result.stderr[-2000:]}
    record.update(state="unknown", exit_code=result.returncode, stderr=result.stderr[-2000:])
    records.write_request(receipt, record)
    return {"returncode": 125, "delivery": "codex_queue", "manual_recovery_required": True}
