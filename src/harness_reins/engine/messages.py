"""Sessions talking to sessions on one machine: names, admission, limits and what became of a message.

A message becomes one ordinary callback, so delivery, target locks, ACK and
``reins retry`` are the ones every result already uses. Only the route differs,
and ``wake`` decides it: ``never`` may not start a process, ``resume`` may. Limits
are checked at admission and any refusal stops there, with an alert for a person
rather than a reply that could become the next turn of the same conversation.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import time
from typing import Mapping, Optional

from ..core import capabilities, envelope, ids, messages as rules, records, results, targets
from ..harness import get_agent
from ..harness.base import (DEFAULT_APPROVALS_REVIEWER, DEFAULT_APPROVAL_POLICY, DEFAULT_CLAUDE_PERMISSION_MODE,
                            DEFAULT_SANDBOX_MODE)
from ..harness.claude import session as claude_session
from ..harness.claude.adapter import CLAUDE_PERMISSION_MODE_ENV
from ..harness.pi import channel as pi_channel
from ..store import layout, link
from ..store import records as queue_store
from . import dispatch, native_queue, recovery, resume_environment, routes

EXPIRED_NOTE = "message expired before it was read"
MAINTAIN_INTERVAL = 30.0
UNREAD_STATES = ("queued", "published", "failed", "unknown")
_last_maintained = 0.0


def current_session(args: argparse.Namespace) -> Optional[tuple[str, str]]:
    """(harness, session) this command runs in, or None from a plain shell. Never inferred from anything else."""
    agent = dispatch.resolve_agent(args)
    session = getattr(args, "session", None) or os.environ.get(get_agent(agent).session_id_env, "").strip()
    if not session:
        return None
    try:
        return agent, pi_channel.canonical_session(session) if agent == "pi" else session
    except ValueError as exc:
        raise rules.Refused("invalid_record", str(exc)) from exc


def name_of(queue: Path, harness: str, session: str) -> Optional[str]:
    for record in link.sessions(queue):
        if record.get("harness") == harness and record.get("session") == session:
            return str(record["name"])
    return None


def _route(harness: str) -> dict[str, object]:
    """Everything delivery needs that only the receiving session's own environment knows."""
    route: dict[str, object] = {}
    if harness == "codex":
        route.update(native_queue.route_fields())
    elif harness == "pi":
        route.update(pi_channel.route_fields())
    route.update(approvals_reviewer=os.environ.get("REINS_APPROVALS_REVIEWER", DEFAULT_APPROVALS_REVIEWER),
                 approval_policy=os.environ.get("REINS_APPROVAL_POLICY", DEFAULT_APPROVAL_POLICY),
                 sandbox_mode=os.environ.get("REINS_SANDBOX_MODE", DEFAULT_SANDBOX_MODE),
                 permission_mode=os.environ.get(CLAUDE_PERMISSION_MODE_ENV, DEFAULT_CLAUDE_PERMISSION_MODE))
    return route


def register(queue: Path, args: argparse.Namespace, name: str, *, replace: bool) -> dict[str, object]:
    rules.validate_name(name)
    current = current_session(args)
    if current is None:
        agent = dispatch.resolve_agent(args)
        raise rules.Refused("invalid_record", f"cannot tell which session to name: {get_agent(agent).session_id_env} is "
                                              "unset. Run this inside the session, or pass --agent and --session.")
    harness, session = current
    route = _route(harness)
    if harness == "codex":
        capability = native_queue.inspect_route({"agent": "codex", "target": targets.session(session),
                                                 "callback_mode": "codex-queue", **route}, timeout=10)
        if capability["status"] == "blocked":
            raise rules.Refused("unsupported_capability", f"this Codex cannot receive messages: {capability.get('action')}")
    queue_store.ensure_daemon_dirs(queue)
    guard = link.lock(queue)
    try:
        holder = link.load_session(queue, name)
        taken = holder is not None and (holder.get("harness"), holder.get("session")) != (harness, session)
        if taken and not replace:
            raise rules.Refused("name_taken", f"{name} already names another {holder.get('harness')} session. "
                                              "Choose another name, or pass --replace to take it over.")
        previous = name_of(queue, harness, session)
        if previous not in (None, name):
            if not replace:
                raise rules.Refused("name_taken", f"this session is already named {previous}. Pass --replace to rename it.")
            link.remove_session(queue, previous)
        record = {"schema": rules.SESSION_SCHEMA, "name": name, "harness": harness, "session": session,
                  "cwd": os.path.abspath(getattr(args, "cwd", None) or os.getcwd()),
                  "registered_at": results.timestamp(time.time()), "route": route}
        link.save_session(queue, record)
        link.save_session_environment(queue, name, resume_environment.session_environment(os.environ))
        return record
    finally:
        queue_store.release_owner_lock(guard, remove=False)


def unregister(queue: Path, name: str) -> bool:
    guard = link.lock(queue)
    try:
        found = link.load_session(queue, name) is not None
        link.remove_session(queue, name)
        return found
    finally:
        queue_store.release_owner_lock(guard, remove=False)


def arrival(record: Mapping[str, object]) -> str:
    """How a message reaches this session right now. A hint at reading time, never a basis for delivery."""
    harness, session = record.get("harness"), str(record.get("session"))
    probe = {"agent": harness, "target": targets.session(session), **dict(record.get("route") or {})}
    if harness == "codex":
        return "codex-queue"
    if harness == "claude":
        return "live" if claude_session.live_watcher_is_held(probe) else "inbox"
    return "live" if pi_channel.live_mailbox_open(probe) else "inbox"


def _stop(queue: Path, key: str, message: Mapping[str, object], text: str) -> rules.Refused:
    link.save_alert(queue, key, {"at": results.timestamp(time.time()), "reason": text, "thread": message["thread"],
                                 "from": rules.sender_text(message), "to": message["to"]["name"]})
    return rules.Refused("limit_exceeded", f"{text}. Stop messaging about this and tell your user; "
                                           "a person has been alerted.", alert=True)


def _admit_limits(queue: Path, message: Mapping[str, object], thread: dict[str, object],
                  rates: dict[str, list[float]], limits: Mapping[str, float], now: float) -> None:
    thread_key = str(message["thread"])
    sender = str(message["from"]["name"] or rules.SHELL_SENDER)
    pair = f"{sender}>{message['to']['name']}"
    if message["hop"] > limits["hop"]:
        raise _stop(queue, thread_key, message, f"thread {thread_key} reached the limit of {limits['hop']:g} replies in a row")
    if thread["count"] >= limits["thread_messages"]:
        raise _stop(queue, thread_key, message, f"thread {thread_key} reached the limit of {limits['thread_messages']:g} messages")
    if sum(1 for at in thread["recent"] if now - at < 60) >= limits["thread_per_minute"]:
        raise _stop(queue, thread_key, message, f"thread {thread_key} reached the limit of {limits['thread_per_minute']:g} messages a minute")
    if sum(1 for at in rates.get(sender, []) if now - at < 60) >= limits["sender_per_minute"]:
        raise _stop(queue, sender, message, f"{sender} reached the limit of {limits['sender_per_minute']:g} messages a minute")
    if sum(1 for at in rates.get(pair, []) if now - at < 3600) >= limits["pair_per_hour"]:
        raise _stop(queue, pair, message, f"{sender} reached the limit of {limits['pair_per_hour']:g} messages an hour "
                                          f"to {message['to']['name']}")
    if sum(1 for _ in (queue / "pending").glob("m-*.json")) >= limits["pending_messages"]:
        raise _stop(queue, "pending", message, f"{limits['pending_messages']:g} messages are already waiting on this machine")


def _sender(queue: Path, args: argparse.Namespace) -> tuple[Optional[str], Optional[str]]:
    """(name, harness); a session must have a name so that a reply can reach it."""
    current = current_session(args)
    if current is None:
        return None, None
    name = name_of(queue, *current)
    if name is None:
        raise rules.Refused("unknown_name", "this session has no name, so nobody could reply to it. "
                                            "Run: reins session register NAME")
    return name, current[0]


def send(queue: Path, args: argparse.Namespace, to: str, *, subject: Optional[str], body: str, refs: list[dict[str, str]],
         wake: str = "never", reply_to: Optional[str] = None) -> dict[str, object]:
    """Admit one message and queue its callback, or raise Refused with nothing queued."""
    rules.validate_address(to)
    rules.validate_body(body)
    queue_store.ensure_daemon_dirs(queue)
    guard = link.lock(queue)
    try:
        sender_name, sender_harness = _sender(queue, args)
        target = link.load_session(queue, to)
        if target is None:
            known = ", ".join(sorted(str(item["name"]) for item in link.sessions(queue))) or "none registered"
            raise rules.Refused("unknown_name", f"no session is named {to}; nothing was sent. Names: {known}.")
        parent = None
        if reply_to is not None:
            parent = link.load_message(queue, reply_to)
            if parent is None:
                raise rules.Refused("unknown_name", f"no message {reply_to} on this machine")
        limits = link.limits(queue)
        now = time.time()
        message = rules.new_message(
            node=link.node_id(queue), sender_name=sender_name, sender_harness=sender_harness, to=to,
            subject=subject if subject is not None else rules.subject_from(body, sender_name or rules.SHELL_SENDER),
            body=body, refs=refs, wake=wake, thread=parent["thread"] if parent else None,
            in_reply_to=parent["id"] if parent else None, hop=int(parent["hop"]) + 1 if parent else 0,
            ttl_days=limits["ttl_days"], now=now)
        while link.message_path(queue, str(message["id"])).exists():  # the same millisecond and random part
            message["id"] = rules.new_message_id(now)
        thread = link.load_thread(queue, str(message["thread"]))
        rates = link.load_rates(queue)
        _admit_limits(queue, message, thread, rates, limits, now)
        effective_wake = wake
        note = None
        if wake == "resume" and thread["resumes"] >= limits["thread_resumes"]:
            effective_wake = "never"
            note = f"this thread used its {limits['thread_resumes']:g} resumes, so a closed session is not started again"
        message["local"] = {"name": to, "harness": target["harness"], "session": target["session"],
                            "cwd": target["cwd"], "route": target.get("route") or {}, "wake": effective_wake,
                            "note": note, "callback": ids.callback_id(str(message["id"])), "admitted_at": now}
        # The record first: if this process dies now, maintain() queues the callback it names.
        link.save_message(queue, message)
        _queue_callback(queue, message, link.load_session_environment(queue, to))
        thread["count"] = int(thread["count"]) + 1
        thread["recent"] = [at for at in thread["recent"] if now - at < 60] + [now]
        thread["last_at"] = now
        thread["resumes"] = int(thread["resumes"]) + (1 if effective_wake == "resume" else 0)
        link.save_thread(queue, thread)
        sender_key = str(sender_name or rules.SHELL_SENDER)
        rates[sender_key] = [at for at in rates.get(sender_key, []) if now - at < 60] + [now]
        pair = f"{sender_key}>{to}"
        rates[pair] = [at for at in rates.get(pair, []) if now - at < 3600] + [now]
        link.save_rates(queue, rates)
        return message
    finally:
        queue_store.release_owner_lock(guard, remove=False)


def reply(queue: Path, args: argparse.Namespace, parent_id: str, *, subject: Optional[str], body: str,
          refs: list[dict[str, str]], wake: str = "never") -> dict[str, object]:
    parent = link.load_message(queue, parent_id)
    if parent is None:
        raise rules.Refused("unknown_name", f"no message {parent_id} on this machine")
    sender = parent["from"].get("name")
    if not isinstance(sender, str):
        raise rules.Refused("unknown_name", f"{parent_id} was sent from a shell, so there is nobody to reply to")
    current = current_session(args)
    if current != (parent["local"]["harness"], parent["local"]["session"]):
        raise rules.Refused("permission_denied", f"{parent_id} was sent to {parent['to']['name']}; only that session can reply")
    if subject is None and isinstance(parent.get("subject"), str):
        subject = parent["subject"] if parent["subject"].startswith("Re: ") else f"Re: {parent['subject']}"
        if len(subject) > rules.SUBJECT_LIMIT:
            subject = subject[:rules.SUBJECT_LIMIT - 3] + "..."
    return send(queue, args, sender, subject=subject, body=body, refs=refs, wake=wake, reply_to=parent_id)


def _callback_mode(harness: str, wake: str) -> str:
    """Never starts a process: Codex keeps it in its queue, Claude in the inbox, Pi waits for its mailbox."""
    if wake == "never" and harness == "codex":
        return "codex-queue"
    if wake == "never" and harness == "claude":
        return "manual"
    return "cli"


def _queue_callback(queue: Path, message: Mapping[str, object], environment: Optional[Mapping[str, str]]) -> None:
    local = message["local"]
    harness, wake = str(local["harness"]), str(local["wake"])
    callback = str(local["callback"])
    route = dict(local["route"])
    mode = _callback_mode(harness, wake)
    message_id = str(message["id"])
    details = queue.absolute() / "details" / f"{callback}.md"
    text = rules.body_text(message)
    if local.get("note"):
        text = f"Note from Reins: {local['note']}.\n\n{text}"
    queue_store.write_private_text(details, text)
    replyable = isinstance(message["from"].get("name"), str)
    prompt = envelope.render_message(
        message, callback, details, dispatch.ack_command(queue, callback),
        (dispatch.session_command(queue, "reply", message_id) + ' --message "..."') if replyable else None,
        keep_listening=harness == "claude" and mode == "manual")
    sender = message["from"]
    request: dict[str, object] = {
        "schema": records.CALLBACK_SCHEMA, "id": callback, "created_at": time.time(), "cwd": str(local["cwd"]),
        "agent": harness, "target": targets.session(str(local["session"])),
        "origin": targets.origin(str(sender.get("harness") or "shell"), str(sender.get("name") or rules.SHELL_SENDER),
                                 str(local["cwd"]), "reins send"),
        "prompt": prompt, "prompt_compact": prompt, "prompt_format": envelope.FORMAT,
        "prompt_details_path": str(details), "task": f"message from {sender.get('name') or rules.SHELL_SENDER}: {message['subject']}",
        "exit_code": None, "message": None, "message_id": message_id, "thread": message["thread"], "wake": wake,
        "details_required": True, "queue_dir": str(queue), "link_dir": str(layout.link_root(queue)),
        "lifecycle_state": "pending", "callback_mode": mode,
        "callback_origin": "other",
        **{key: value for key, value in route.items() if key in routes.ROUTE_FIELDS
           and (harness == "pi" or mode == "codex-queue")},
        **{key: route[key] for key in ("approvals_reviewer", "approval_policy", "sandbox_mode", "permission_mode") if key in route},
    }
    request["requires"] = capabilities.required_for(request)
    if environment and mode != "manual":
        resume_environment.capture(queue, callback, environment)
    queue_store.write_request(layout.request_path(queue, "pending", callback), request)


def state_of(queue: Path, message: Mapping[str, object]) -> str:
    """What is known about a message. "published" means handed to the session's channel, not read."""
    callback = str(message["local"]["callback"])
    if layout.ack_path(queue, callback).exists():
        return "acked"
    tombstone = link.load_record(layout.request_path(queue, "canceled", callback))
    if tombstone is not None:
        return "expired" if tombstone.get("cancel_message") == EXPIRED_NOTE else "canceled"
    if layout.request_path(queue, "done", callback).exists():
        return "acked"
    failed = link.load_record(layout.request_path(queue, "failed", callback))
    if failed is not None:
        return "unknown" if failed.get("retain_target_lease") is True else "failed"
    for name in ("pending", "running"):
        request = link.load_record(layout.request_path(queue, name, callback))
        if request is not None and any(request.get(key) for key in ("live_delivered_at", "pi_published_at")):
            return "published"
    return "queued"


def summary(queue: Path, message: Mapping[str, object]) -> dict[str, object]:
    local = message["local"]
    return {"id": message["id"], "callback": local["callback"], "state": state_of(queue, message),
            "from": rules.sender_text(message), "to": message["to"]["name"], "subject": message["subject"],
            "thread": message["thread"], "hop": message["hop"], "in_reply_to": message.get("in_reply_to"),
            "created_at": message["created_at"], "expires_at": message["expires_at"], "wake": local["wake"],
            "note": local.get("note")}


def resolve(queue: Path, reference: str) -> dict[str, object]:
    """The message named by its ID or by its callback ID, as copied from an envelope."""
    message = link.load_message(queue, ids.root_of(reference))
    if message is None:
        raise rules.Refused("unknown_name", f"no message {reference} on this machine")
    return message


def alerts(queue: Path) -> list[dict[str, object]]:
    return link.alerts(queue)


def maintain(queue: Path, *, now: Optional[float] = None) -> None:
    """Tower hook: finish an interrupted admission, expire what was never read and drop what is long settled."""
    global _last_maintained
    moment = time.time() if now is None else now
    if now is None and moment - _last_maintained < MAINTAIN_INTERVAL:
        return
    _last_maintained = moment
    limits = link.limits(queue)
    for message in link.all_messages(queue):
        try:
            callback = str(message["local"]["callback"])
            expires = results.seconds(message.get("expires_at"))
            if not _has_callback(queue, callback):
                guard = link.lock(queue)  # the admitting process may still be between its two writes
                try:
                    if not _has_callback(queue, callback):
                        _queue_callback(queue, message, link.load_session_environment(queue, str(message["local"]["name"])))
                finally:
                    queue_store.release_owner_lock(guard, remove=False)
            state = state_of(queue, message)
            if state in UNREAD_STATES and expires is not None and moment >= expires:
                recovery.cancel_one(queue, callback, EXPIRED_NOTE)
            elif state not in UNREAD_STATES and expires is not None and moment - expires >= limits["retention_days"] * 86400:
                link.remove_message(queue, str(message["id"]))
                try:
                    (queue / "details" / f"{callback}.md").unlink()
                except OSError:
                    pass
        except (KeyError, TypeError, OSError, rules.Refused) as exc:
            print(f"reins: warning: could not maintain message {message.get('id')}: {exc}", file=sys.stderr)
    for thread in link.all_threads(queue):
        if isinstance(thread.get("last_at"), (int, float)) and moment - thread["last_at"] >= limits["retention_days"] * 86400:
            link.remove_thread(queue, str(thread["id"]))


def _has_callback(queue: Path, callback: str) -> bool:
    return layout.ack_path(queue, callback).exists() or any(
        layout.request_path(queue, name, callback).exists()
        for name in (layout.ACTIVE_STATE, "pending", "running", "done", "failed", "canceled"))
