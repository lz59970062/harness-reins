"""Opt-in native tool-output delivery to an already loaded Codex thread.

The RPC is platform independent. This transport uses verified same-user Unix
sockets on macOS/Linux; it neither starts Desktop nor uses its private MCP pipe.
"""
from __future__ import annotations

import sys
from pathlib import Path

from .. import __version__
from ..core import capabilities
from ..harness.codex import app_server
from ..store import layout


def selected_socket(raw: object) -> str:
    if not isinstance(raw, str) or not raw or "\0" in raw:
        raise ValueError("app-server mode requires --app-server-socket")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("App Server socket must be an absolute path")
    # Freeze the actual endpoint, not the daemon's movable convenience symlink.
    # Missing endpoints are reported by the read-only capability probe.
    return str(path.resolve())


def validate_route(request: dict[str, object]) -> Path:
    if request.get("agent", "codex") != "codex":
        raise ValueError("native App Server callbacks require Codex")
    target = request.get("target")
    if (not isinstance(target, dict) or target.get("kind") != "session"
            or not isinstance(target.get("session"), str) or not target["session"]):
        raise ValueError("native App Server callbacks require an explicit bound session")
    raw = request.get("callback_socket")
    if not isinstance(raw, str) or not raw or "\0" in raw or not Path(raw).is_absolute():
        raise ValueError("native App Server callbacks require a frozen absolute socket path")
    if capabilities.CODEX_TOOL_OUTPUT not in request.get("requires", []):
        raise ValueError("native App Server callback is missing its delivery capability")
    return Path(raw)


def initialize(connection: app_server.AppServerConnection) -> None:
    connection.connect()
    connection.request("initialize", {
        "clientInfo": {"name": "harness-reins", "version": __version__},
        "capabilities": {"experimentalApi": True},
    })
    connection.notify("initialized", {})


def require_loaded(connection: app_server.AppServerConnection, thread_id: str) -> None:
    cursor = None
    # Bounded snapshot pagination, not a readiness or completion polling loop.
    for _ in range(8):
        result = connection.request("thread/loaded/list", {"cursor": cursor, "limit": 100})
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise app_server.CallbackTransportBlocked("invalid loaded-thread response")
        if thread_id in result["data"]:
            return
        cursor = result.get("nextCursor")
        if not cursor:
            break
    raise app_server.CallbackTransportBlocked("bound session is not loaded on this server; no resume or CLI fallback")


def inspect_route(request: dict[str, object], *, timeout: float) -> dict[str, object]:
    connection = None
    try:
        path = validate_route(dict(request, requires=capabilities.required_for(request)))
        if sys.platform not in ("darwin", "linux"):
            raise ValueError("native Unix-socket peer verification is implemented for macOS/Linux only")
        connection = app_server.AppServerConnection(path, timeout)
        initialize(connection)
        require_loaded(connection, request["target"]["session"])
        return {"status": "owner_reachable", "transport": "app_server_tool_output",
                "reason": "bound_session_loaded", "end_to_end_verified": False,
                "action": "Ownership snapshot only; toolOutput support and receipt still require a live callback test."}
    except (OSError, ValueError, TypeError, app_server.AppServerProtocolError) as exc:
        return {"status": "blocked", "transport": "app_server_tool_output",
                "reason": "native_app_server_unavailable", "end_to_end_verified": False,
                "action": str(exc)}
    finally:
        if connection is not None:
            connection.close()


class NativeAppServerDelivery(app_server.DesktopAppServerDelivery):
    """Completion is a pushed event, never a repeated thread/status request."""

    def wait_for_completion(self, timeout: float) -> bool:
        if self.connection is None or self.turn_id is None:
            raise app_server.AppServerProtocolError("native submission outcome is unknown")
        return self.connection.wait_for_turn_completion(self.thread_id, self.turn_id, timeout)


def start_delivery(payload: dict[str, object]) -> NativeAppServerDelivery:
    from . import delivery as engine

    request = payload["request"]
    try:
        path = validate_route(request)
        if sys.platform not in ("darwin", "linux"):
            raise ValueError("native Unix-socket delivery is implemented for macOS/Linux only")
    except (ValueError, TypeError) as exc:
        raise app_server.CallbackTransportBlocked(str(exc)) from exc
    target = request["target"]["session"]
    connection = app_server.AppServerConnection(path, min(15.0, max(1.0, float(payload["timeout"]))))
    context = app_server.desktop_delivery_context(payload)
    submitted = False
    try:
        initialize(connection)
        require_loaded(connection, target)
        # Subscribe to the existing owner's events. Never resume an absent
        # thread on some other daemon merely because its history is on disk.
        resumed = connection.request("thread/resume", {"threadId": target, "excludeTurns": True})
        thread = resumed.get("thread") if isinstance(resumed, dict) else None
        if not isinstance(thread, dict) or thread.get("id") != target:
            raise app_server.CallbackTransportBlocked("App Server resumed an unexpected thread")
        if context is not None:
            engine.retain_target_lease(*context)
            root, callback = context
            if layout.ack_path(root, callback["id"]).exists() or engine.queue_store.is_canceled(root, callback["id"]):
                engine.release_retained_target_lease(*context)
                return NativeAppServerDelivery(None, target, None)
        # Keep the original thread's policy and environment. Native tool output
        # queues into an active turn, so there is no wait-until-idle loop.
        submitted = True
        started = connection.request("turn/start", {
            "threadId": target, "input": [],
            "toolOutput": {"name": "reins_task_completed", "namespace": None,
                           "output": str(payload["prompt"])},
        })
        turn = started.get("turn") if isinstance(started, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not isinstance(turn_id, str) or not turn_id:
            raise app_server.AppServerProtocolError("native turn/start response has no turn id")
        result = NativeAppServerDelivery(connection, target, turn_id)
        connection = None
        return result
    except app_server.AppServerRpcError as exc:
        # A definite rejection may be retried after repair, but never through
        # another transport. An interrupted write/unknown response may not.
        if submitted and context is not None:
            engine.release_retained_target_lease(*context)
        raise app_server.CallbackTransportBlocked(f"native callback rejected; CLI fallback disabled: {exc}") from exc
    except (OSError, ValueError, TypeError, app_server.AppServerProtocolError) as exc:
        if submitted:
            return NativeAppServerDelivery(None, target, None)
        raise app_server.CallbackTransportBlocked(f"native callback unavailable; CLI fallback disabled: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def wait_for_delivery(payload: dict, delivery: NativeAppServerDelivery, timeout: float) -> dict:
    from . import delivery as engine

    context = app_server.desktop_delivery_context(payload)
    ack = Path(str(payload["ack_path"]))
    canceled = Path(str(payload["canceled_path"]))

    def receipt() -> bool:
        return ack.exists() or canceled.exists()

    completed = False
    error = None
    if not receipt():
        try:
            # One blocking read of pushed events, bounded by the delivery
            # deadline. No status RPCs, ACK-file polling or sleep loop.
            completed = delivery.wait_for_completion(timeout)
        except app_server.AppServerProtocolError as exc:
            error = str(exc)
    if receipt():
        if context is not None:
            engine.release_retained_target_lease(*context)
        return {"returncode": 0, "delivery": "app_server_tool_output", "skipped": "acknowledged_or_canceled"}
    if completed:
        if context is not None:
            engine.release_retained_target_lease(*context)
        return {"returncode": 1, "delivery": "app_server_tool_output", "turn_completed": True}
    if context is not None:
        engine.retain_target_lease(*context)
    return {"returncode": 125, "delivery": "app_server_tool_output",
            "manual_recovery_required": True, "timed_out": error is None,
            "error": error or "native callback completion deadline expired"}
