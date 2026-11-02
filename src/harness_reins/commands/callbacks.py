"""reins ack, retry, cancel and status: handle callbacks that reached, or are waiting for, a session."""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from ..core import capabilities
from ..engine import routes
from ..harness.pi import channel as pi_channel
from ..store import layout
from ..store import records as queue_store
from ..engine import delivery as delivery_engine
from ..engine import dispatch
from ..engine import messages
from ..engine import recovery


def ack(args: argparse.Namespace) -> int:
    positional = getattr(args, "callback_id", None)
    if positional and args.id and positional != args.id:
        print("reins: error: two different callback ids given", file=sys.stderr)
        return 2
    args.id = args.id or positional
    if not args.id:
        print("reins: error: name the callback to acknowledge: reins ack <callback-id>", file=sys.stderr)
        return 2
    root = layout.queue_dir(args)
    queue_store.ensure_daemon_dirs(root)
    marker = layout.ack_path(root, args.id)
    payload = {
        "id": args.id,
        "marked_at": time.time(),
    }
    if args.message:
        payload["message"] = args.message
    queue_store.write_request(marker, payload)
    for state in ("failed", "running", "pending"):
        source = layout.request_path(root, state, args.id)
        if not source.exists():
            continue
        try:
            request = dispatch.load_request(source)
            if state == "failed" or (state == "pending" and (request.get("callback_mode") == "manual"
                                                              or request.get("live_delivered_at"))):
                queue_store.move_request(source, root / "done")
            try:
                delivery_engine.release_retained_target_lease(root, request)
            except (OSError, RuntimeError) as exc:
                print(
                    f"reins: warning: callback {args.id} is durably acknowledged; "
                    f"daemon will reconcile its retained target lease: {exc}",
                    file=sys.stderr,
                )
        except FileNotFoundError:
            continue
        break
    print(f"reins: acknowledged callback {args.id} in {root}", file=sys.stderr)
    return 0


def retry_callback(args: argparse.Namespace) -> int:
    """Requeue a known callback; never resubmit its completed workload."""
    root = layout.queue_dir(args).expanduser().absolute()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.id):
        print("reins: invalid callback id", file=sys.stderr)
        return 2
    delivery_lock = target_lock = None
    try:
        queue_store.ensure_daemon_dirs(root)
        delivery_lock = queue_store.acquire_owner_lock(root, layout.delivery_lock_id(args.id), blocking=False)
        if delivery_lock is None:
            raise ValueError("callback delivery is still active")
        source = layout.request_path(root, "failed", args.id)
        if not source.exists():
            source = layout.request_path(root, "pending", args.id)
        request = dispatch.load_request(source)
        if source.parent.name == "pending" and request.get("callback_mode") != "manual":
            raise ValueError("callback is already pending")
        if layout.ack_path(root, args.id).exists() or queue_store.is_canceled(root, args.id):
            raise ValueError("acknowledged or canceled callbacks cannot be retried")
        if request.get("retain_target_lease") is True or delivery_engine.desktop_submission_pending(root, request):
            raise ValueError("previous submission outcome is unknown; inspect the original session before recovery")
        target_lock = delivery_engine.acquire_target_lock(request, blocking=False)
        if target_lock is None or delivery_engine.retained_target_lease_is_held(request):
            raise ValueError("the bound session has an active or unresolved delivery")
        if getattr(args, "app_server_socket", None) and args.callback_mode != "app-server":
            raise ValueError("--app-server-socket requires --callback-mode app-server")
        if args.callback_mode:
            options = argparse.Namespace(callback_mode=args.callback_mode, agent=dispatch.request_agent(request),
                                         app_server_socket=(getattr(args, "app_server_socket", None) or request.get("callback_socket"))
                                         if args.callback_mode == "app-server" else None)
            pi_route = {key: request[key] for key in pi_channel.ROUTE_FIELDS if key in request}
            for key in routes.ROUTE_FIELDS:
                request.pop(key, None)
            request.update(routes.selection(options))
            request.update(pi_route)
            request["requires"] = capabilities.required_for(request)
        capability = routes.inspect_route(request)
        if capability["status"] == "blocked":
            action = capability.get("action")
            raise ValueError("callback route remains blocked: " + str(capability.get("reason"))
                             + (f". {action}" if request.get("callback_mode") == "desktop" and action else ""))
        history = request.setdefault("retry_history", [])
        if not isinstance(history, list):
            raise ValueError("callback retry history is invalid")
        history.append({"at": time.time(), "attempts": request.get("attempts", 0), "last_error": request.get("last_error")})
        request["retry_history"] = history[-10:]
        request["attempts"] = 0
        for key in ("last_error", "delivery_state", "next_attempt_at", "last_deferred_reason"):
            request.pop(key, None)
        if not source.exists() or layout.ack_path(root, args.id).exists() or queue_store.is_canceled(root, args.id):
            raise ValueError("callback changed during recovery; inspect its current state")
        queue_store.write_request(source, request)
        if source.parent.name != "pending":
            queue_store.move_request(source, root / "pending")
        print(f"reins: requeued callback {args.id}; business task was not relaunched", file=sys.stderr)
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"reins: callback retry refused: {error}", file=sys.stderr)
        return 2
    finally:
        if target_lock is not None:
            queue_store.release_owner_lock(target_lock, remove=False)
        if delivery_lock is not None:
            queue_store.release_owner_lock(delivery_lock, remove=False)


def cancel(args: argparse.Namespace) -> int:
    root = layout.queue_dir(args)
    if args.all:
        recovery.cancel_all(root, args.message)
        return 0
    return 0 if recovery.cancel_one(root, args.id, args.message) else 1


def queued_items(root: Path, states: list[str]) -> list[tuple[str, Path, dict[str, object]]]:
    items: list[tuple[str, Path, dict[str, object]]] = []
    for state in states:
        for path in sorted((root / state).glob("*.json")):
            try:
                request = dispatch.load_request(path)
            except Exception as exc:
                request = {"id": path.stem, "prompt": "Task: unreadable callback", "last_error": str(exc)}
            items.append((state, path, request))
    items.sort(key=lambda item: float(item[2].get("created_at", 0.0)))
    return items


def status(args: argparse.Namespace) -> int:
    root = layout.queue_dir(args)
    states = args.state or ["pending", "running"]
    items = queued_items(root, states)
    alerts = messages.alerts(root)
    if alerts:
        print(f"[reins] {len(alerts)} message thread(s) stopped at a limit; no agent was told again. "
              "Inspect: reins message alerts")
    if not items:
        if not args.quiet_empty:
            print(f"No callbacks need attention in {root}")
        return 0

    counts = {state: sum(1 for item in items if item[0] == state) for state in states}
    summary = ", ".join(f"{state}={count}" for state, count in counts.items() if count)
    print(f"[reins] {len(items)} item(s) need attention ({summary})")
    limit = max(1, args.limit)
    for state, path, request in items[:limit]:
        request_id = str(request.get("id", path.stem))
        line = f"  - [{state}] {dispatch.request_task(request)} | {dispatch.request_target(request)} | id {request_id}"
        if request.get("callback_mode") == "manual":
            line += (" | waiting in the inbox of its session" if request.get("message_id")
                     else " | manual receipt; automatic callback disabled")
        if request.get("delivery_state") == "blocked":
            line += " | callback blocked; saved workload result retained"
        if request.get("delivery_state") == "queued_awaiting_ack":
            line += " | in the Codex queue; waiting for the thread to ACK"
        if state == "running" and layout.ack_path(root, request_id).exists():
            line += " | acknowledged; resumed Codex process still active"
        error = request.get("last_error")
        if isinstance(error, str) and error:
            line += f" | {error}"
        print(line)
    if len(items) > limit:
        print(f"  ... {len(items) - limit} more item(s)")
    print("  Inspect: reins status --state pending --state running")
    return 0
