"""Sessions talking to sessions on one machine: names, admission, limits and what became of a message.

A message becomes one ordinary callback, so delivery, target locks, ACK and
``reins retry`` are the ones every result already uses. Only the route differs,
and ``wake`` decides it: ``never`` may not start a process, ``resume`` may, and
a session a job result would resume (Claude Code, managed Pi) defaults to ``resume``. Limits
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

from ..core import capabilities, envelope, ids, linkwire, messages as rules, records, results, targets
from ..harness import get_agent
from ..harness.base import (DEFAULT_APPROVALS_REVIEWER, DEFAULT_APPROVAL_POLICY, DEFAULT_CLAUDE_PERMISSION_MODE,
                            DEFAULT_SANDBOX_MODE)
from ..harness.agy import session as agy_session
from ..harness.claude import session as claude_session
from ..harness.claude.adapter import CLAUDE_PERMISSION_MODE_ENV
from ..harness.pi import channel as pi_channel
from ..store import layout, link
from ..store import records as queue_store
from . import dispatch, goals, native_queue, peering, recovery, resume_environment, routes
from . import tasks as managed_tasks

EXPIRED_NOTE = "message expired before it was read"
MAINTAIN_INTERVAL = 30.0
UNREAD_STATES = ("queued", "published", "failed", "unknown")
_last_maintained = 0.0
REPORT_INTERVAL = 3.0
_last_reported = 0.0


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


ACTIVE_TASK_STATES = ("submitted", "launching", "running")


def expects_callbacks(queue: Path, harness: str, session: str) -> bool:
    """Whether something may still call back into this session: a task it started, an active goal and its
    inactivity reminders, or a name messages reach."""
    if name_of(queue, harness, session) is not None:
        return True
    return (goals.has_active_goal(queue, harness, session)
            or any(task.get("state") in ACTIVE_TASK_STATES for task in managed_tasks.session_tasks(queue, harness, session)))


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
        _expect_waiter(args)
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
    if claude_session.live_wait_agent(harness) is not None:
        if claude_session.live_watcher_is_held(probe):
            return "live"
        if _live_only(str(harness)):
            return "inbox"
        if harness == "agy":
            return "window" if agy_session.window_open(session, os.environ) else "resume"
        return "window" if claude_session.open_window(session, os.environ) is not None else "resume"
    return "live" if pi_channel.live_mailbox_open(probe) else "resume" if _managed_pi(record) else "inbox"


def _stop(queue: Path, key: str, message: Mapping[str, object], text: str) -> rules.Refused:
    link.save_alert(queue, key, {"at": results.timestamp(time.time()), "reason": text, "thread": message["thread"],
                                 "from": rules.sender_text(message), "to": message["to"]["name"]})
    return rules.Refused("limit_exceeded", f"{text}. Stop messaging about this and tell your user; "
                                           "a person has been alerted.", alert=True)


def _admit_limits(queue: Path, message: Mapping[str, object], thread: dict[str, object],
                  rates: dict[str, list[float]], limits: Mapping[str, float], now: float, *,
                  waits_here: bool = True) -> None:
    """Refuse a message past a limit. ``waits_here``: it becomes a pending callback on this machine, which a
    message for another machine does not (that one only goes to the outbox)."""
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
    if waits_here and sum(1 for _ in (queue / "pending").glob("m-*.json")) >= limits["pending_messages"]:
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
         wake: Optional[str] = None, reply_to: Optional[str] = None, held: bool = False) -> dict[str, object]:
    """Admit one message and queue its callback, or raise Refused with nothing queued.

    ``held``: the caller already holds the link lock (send_many does, for all its recipients).
    """
    name, alias = linkwire.parse_address(to)
    if alias is not None:
        return _send_remote(queue, args, name, alias, subject=subject, body=body, refs=refs, wake=wake,
                            reply_to=reply_to, held=held)
    rules.validate_address(to)
    rules.validate_body(body)
    queue_store.ensure_daemon_dirs(queue)
    guard = None if held else link.lock(queue)
    try:
        sender_name, sender_harness = _sender(queue, args)
        target = link.load_session(queue, to)
        if target is None:
            known = ", ".join(sorted(str(item["name"]) for item in link.sessions(queue))) or "none registered"
            raise rules.Refused("unknown_name", f"no session is named {to}; nothing was sent. Names: {known}.")
        parent = None
        if reply_to is not None:
            parent = link.load_message(queue, reply_to)
            if parent is None or parent["local"].get("peer"):
                raise rules.Refused("unknown_name", f"no message {reply_to} on this machine")
        wake = wake or _default_wake(target)
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
        _admit(queue, message, target, wake=wake, sender=str(sender_name or rules.SHELL_SENDER), limits=limits, now=now)
        _expect_waiter(args)
        return message
    finally:
        queue_store.release_owner_lock(guard, remove=False)  # None when the caller holds it


def _effective_wake(target: Mapping[str, object], wake: str) -> tuple[str, Optional[str]]:
    """What the receiving session's harness allows of ``wake``, and a note for the reader when that is less."""
    harness = str(target["harness"])
    if wake == "resume" and _live_only(harness):
        return "never", f"{get_agent(harness).display_name} cannot be resumed headless, so a closed session is not started"
    if wake == "resume" and harness == "pi" and not _managed_pi(target):
        return "never", "only a session opened with `reins pi` can be resumed headless, so a closed one gets this when it reopens"
    return wake, None


def _admit(queue: Path, message: dict[str, object], target: Mapping[str, object], *, wake: str, sender: str,
           limits: Mapping[str, float], now: float, note: Optional[str] = None,
           local: Optional[Mapping[str, object]] = None) -> None:
    """The one admission every message goes through, local or from a paired machine; under the link lock.

    ``sender`` is the key the limits count: a session name here, ``@machine`` for another machine.
    """
    name = str(target["name"])
    thread = link.load_thread(queue, str(message["thread"]))
    rates = link.load_rates(queue)
    _admit_limits(queue, dict(message, **{"from": dict(message["from"], name=sender)}), thread, rates, limits, now)
    effective, harness_note = _effective_wake(target, wake)
    message["local"] = {"name": name, "harness": target["harness"], "session": target["session"],
                        "cwd": target["cwd"], "route": target.get("route") or {}, "wake": effective,
                        "note": note or harness_note, "callback": ids.callback_id(str(message["id"])),
                        "admitted_at": now, **(local or {})}
    # Counted first: a crash below then errs towards the limit, never past it.
    _count(queue, thread, rates, sender, name, now)
    # The record before the callback: if this process dies now, maintain() queues the callback it names.
    link.save_message(queue, message)
    _queue_callback(queue, message, link.load_session_environment(queue, name))


def send_many(queue: Path, args: argparse.Namespace, names: list[str], *, subject: Optional[str], body: str,
              refs: list[dict[str, str]], wake: Optional[str] = None) -> list[dict[str, object]]:
    """One message to each of ``names``, or to none of them.

    Each recipient gets an ordinary message of its own (own thread, own receipts), so every rule of a single
    send applies to each, counted once per recipient. Everything this machine can check is checked for all
    of them first; a name on another machine is checked there, as for a single send.
    """
    recipients = list(dict.fromkeys(names))  # the same name twice gets one message
    if len(recipients) == 1:
        return [send(queue, args, recipients[0], subject=subject, body=body, refs=refs, wake=wake)]
    rules.validate_body(body)
    queue_store.ensure_daemon_dirs(queue)
    guard = link.lock(queue)
    try:
        sender_name, _ = _sender(queue, args)
        sender = str(sender_name or rules.SHELL_SENDER)
        limits = link.limits(queue)
        now = time.time()
        rates = {key: list(times) for key, times in link.load_rates(queue).items()}  # a copy to count into
        pending = sum(1 for _ in (queue / "pending").glob("m-*.json"))
        for to in recipients:
            name, alias = linkwire.parse_address(to)
            if alias is not None:
                _active_peer(queue, alias)
            elif link.load_session(queue, name) is None:
                raise rules.Refused("unknown_name", f"no session is named {name}; nothing was sent to anyone.")
            elif pending >= limits["pending_messages"]:
                raise _stop(queue, "pending", {"thread": None, "from": {"name": sender}, "to": {"name": to}},
                            f"{limits['pending_messages']:g} messages are already waiting on this machine. "
                            "Nothing was sent to anyone")
            else:
                pending += 1  # a local message waits as a callback here
            probe = {"thread": rules.new_thread_id(), "hop": 0, "from": {"name": sender, "harness": None},
                     "to": {"name": to}}
            try:
                # The pending count is checked above, counting the local recipients before this one.
                _admit_limits(queue, probe, {"count": 0, "recent": []}, rates, limits, now, waits_here=False)
            except rules.Refused as exc:
                raise rules.Refused(exc.code, f"{exc} Nothing was sent to anyone.", alert=exc.alert) from None
            rates[sender] = rates.get(sender, []) + [now]
            rates[f"{sender}>{to}"] = rates.get(f"{sender}>{to}", []) + [now]
        return [send(queue, args, to, subject=subject, body=body, refs=refs, wake=wake, held=True)
                for to in recipients]
    finally:
        queue_store.release_owner_lock(guard, remove=False)


def _count(queue: Path, thread: dict[str, object], rates: dict[str, list[float]], sender: str, to: str,
           now: float) -> None:
    """Record one admitted message against its sender, the pair and its thread; under the link lock.

    The rates go first: they are what stops a peer, and a thread's own count only ever errs high on a retry.
    """
    rates[sender] = [at for at in rates.get(sender, []) if now - at < 60] + [now]
    pair = f"{sender}>{to}"
    rates[pair] = [at for at in rates.get(pair, []) if now - at < 3600] + [now]
    link.save_rates(queue, rates)
    thread["count"] = int(thread["count"]) + 1
    thread["recent"] = [at for at in thread["recent"] if now - at < 60] + [now]
    thread["last_at"] = now
    link.save_thread(queue, thread)


# Other machines ----------------------------------------------------------------------
# A message to name@machine goes to sent/ and the outbox; _link-sync carries it. What arrives from a
# paired machine lands in inbound/ and is admitted here, by the same rules and limits as `reins send`.

def _active_peer(queue: Path, alias: str) -> dict[str, object]:
    peer = link.load_peer(queue, alias)
    if peer is None or peer.get("state") != "active":
        known = ", ".join(sorted(str(item["alias"]) for item in link.peers(queue) if item.get("state") == "active"))
        raise rules.Refused("unknown_peer", f"no machine is paired as {alias}; nothing was sent. Paired: "
                                            f"{known or 'none'}. Pair one with: reins link connect HOST")
    return peer


def _send_remote(queue: Path, args: argparse.Namespace, name: str, alias: str, *, subject: Optional[str], body: str,
                 refs: list[dict[str, str]], wake: Optional[str], reply_to: Optional[str],
                 held: bool = False) -> dict[str, object]:
    rules.validate_body(body)
    queue_store.ensure_daemon_dirs(queue)
    guard = None if held else link.lock(queue)
    try:
        peer = _active_peer(queue, alias)
        sender_name, sender_harness = _sender(queue, args)
        parent = None
        if reply_to is not None:
            parent = link.load_message(queue, reply_to)
            if parent is None or parent["local"].get("peer") != alias:
                raise rules.Refused("unknown_name", f"no message {reply_to} from {alias} on this machine")
        limits = link.limits(queue)
        now = time.time()
        node = link.node_id(queue)
        message = rules.new_message(
            node=node, sender_name=sender_name, sender_harness=sender_harness, to=name,
            subject=subject if subject is not None else rules.subject_from(body, sender_name or rules.SHELL_SENDER),
            body=body, refs=refs, wake=wake or "never", thread=parent["thread"] if parent else None,
            in_reply_to=str(parent["origin_id"]) if parent else None, hop=int(parent["hop"]) + 1 if parent else 0,
            ttl_days=limits["ttl_days"], now=now)
        message["to"]["node"] = peer["node"]
        message["wake_asked"] = wake is not None  # otherwise the receiver uses its own default
        while link.sent_path(queue, str(message["id"])).exists() or link.message_path(queue, str(message["id"])).exists():
            message["id"] = rules.new_message_id(now)
        label = f"{name}@{alias}"
        thread = link.load_thread(queue, str(message["thread"]))
        rates = link.load_rates(queue)
        sender = str(sender_name or rules.SHELL_SENDER)
        _admit_limits(queue, dict(message, to=dict(message["to"], name=label)), thread, rates, limits, now,
                      waits_here=False)
        data = linkwire.encode(linkwire.wrap(peer, node, "message", message))
        _count(queue, thread, rates, sender, label, now)
        # The record holds its file until the outbox has it, so a crash between the two writes is repaired by
        # maintain(); writing the outbox file twice is harmless, the receiver takes a message once.
        record = dict(message, sent={"peer": alias, "state": "queued", "at": now, "code": None, "reason": None,
                                     "wire": data.decode("utf-8")})
        link.save_sent(queue, record)
        _write_outbox(queue, record)
        _expect_waiter(args)
        return record
    finally:
        queue_store.release_owner_lock(guard, remove=False)


def _write_outbox(queue: Path, record: dict[str, object]) -> None:
    """Put a sent message's file in its outbox, then drop the copy the record kept for that."""
    sent = record["sent"]
    link.spool_write(link.outbox(queue, str(sent["peer"])), f"{record['id']}.json", str(sent["wire"]).encode("utf-8"))
    sent.pop("wire", None)
    link.save_sent(queue, record)


def _receipt(queue: Path, peer: Mapping[str, object], node: str, message_id: str, state: str, *,
             code: Optional[str] = None, reason: Optional[str] = None) -> None:
    at = time.time()
    record = linkwire.wrap(peer, node, "receipt", {"id": message_id, "state": state, "at": at, "code": code,
                                                   "reason": reason})
    link.spool_write(link.outbox(queue, str(peer["alias"])), linkwire.receipt_name(message_id, state, int(at * 1000)),
                     linkwire.encode(record))


def _short(value: object, limit: int) -> Optional[str]:
    return str(value)[:limit] if isinstance(value, str) and value.isprintable() else None


def _checked_remote(peer: Mapping[str, object], node: str, message: Mapping[str, object]) -> dict[str, object]:
    """The fields a message from another machine must have; nothing it claims about this node is trusted."""
    if message.get("schema") != rules.MESSAGE_SCHEMA or not rules.is_message_id(message.get("id")):
        raise rules.Refused("invalid_record", "not a reins.message/1 record")
    sender, target = message.get("from"), message.get("to")
    if not isinstance(sender, dict) or sender.get("node") != peer["node"]:
        raise rules.Refused("identity_mismatch", "the message claims another sending machine")
    if not isinstance(target, dict) or target.get("kind") != "session_name" or target.get("node") != node:
        raise rules.Refused("invalid_record", "the message is addressed to another machine")
    rules.validate_name(target.get("name"))
    if sender.get("name") is not None:
        rules.validate_name(sender.get("name"))
    if sender.get("harness") is not None and _short(sender.get("harness"), 32) is None:
        raise rules.Refused("invalid_record", "the sender's harness is not a short printable name")
    if not rules.is_thread_id(message.get("thread")) or not isinstance(message.get("hop"), int) or message["hop"] < 0:
        raise rules.Refused("invalid_record", "the message has no usable thread or hop")
    if message.get("in_reply_to") is not None and not rules.is_message_id(message.get("in_reply_to")):
        raise rules.Refused("invalid_record", "in_reply_to is not a message id")
    if message.get("wake") not in rules.WAKE_MODES:
        raise rules.Refused("invalid_record", "unknown wake")
    rules.validate_subject(message.get("subject"))
    rules.validate_body(message.get("body"))
    rules.validate_refs(message.get("refs"))
    for field in ("created_at", "expires_at"):
        if _short(message.get(field), 40) is None:
            raise rules.Refused("invalid_record", f"the message has no usable {field}")
    if message.get("requires") != []:
        raise rules.Refused("unsupported_capability", "the message requires capabilities this machine does not offer")
    return dict(message)


def _admit_remote(queue: Path, peer: Mapping[str, object], node: str, incoming: Mapping[str, object]) -> None:
    """Admit one message from a paired machine, exactly as `reins send` would, or raise Refused."""
    message = _checked_remote(peer, node, incoming)
    alias = str(peer["alias"])
    origin_id = str(message["id"])
    local_id = linkwire.local_root(str(peer["node"]), origin_id)
    digest = linkwire.digest(incoming)
    guard = link.lock(queue)
    try:
        existing = link.load_message(queue, local_id)
        if existing is not None:
            if existing["local"].get("digest") == digest:
                return  # a repeat: the forwarded receipt goes out again, nothing else happens
            raise rules.Refused("id_conflict", f"{origin_id} already arrived from {alias} with other content")
        name = str(message["to"]["name"])
        target = link.load_session(queue, name)
        if target is None:
            raise rules.Refused("unknown_name", f"no session is named {name} on the receiving machine")
        if message.get("in_reply_to") is not None:
            parent = link.load_sent(queue, str(message["in_reply_to"]))
            if (parent is None or parent["sent"].get("peer") != alias or parent.get("thread") != message["thread"]
                    or message["hop"] != int(parent.get("hop") or 0) + 1):
                raise rules.Refused("invalid_record", f"a reply must answer a message this machine sent to {alias}, "
                                                      "one hop further on the same thread")
        elif message["hop"] != 0:
            raise rules.Refused("invalid_record", "a message that answers nothing starts a thread at hop 0")
        limits = link.limits(queue)
        now = time.time()
        expires = results.seconds(message.get("expires_at"))
        if expires is not None and now >= expires:
            raise rules.Refused("expired", "the message expired before it reached this machine")
        message.update(id=local_id, origin_id=origin_id, expires_at=results.timestamp(now + limits["ttl_days"] * 86400))
        asked = message.get("wake_asked") is True
        wake = str(message["wake"]) if asked else _default_wake(target)
        note = None
        if wake == "resume" and peer.get("wake") == "never":  # this machine's choice for that peer
            wake = "never"
            if asked:
                note = f"messages from {alias} do not start a closed session here, so this one waits for it to open"
        # The limits count the machine, not the name it claims, so a peer cannot get around them by renaming.
        _admit(queue, message, target, wake=wake, sender=f"@{alias}", limits=limits, now=now, note=note,
               local={"peer": alias, "digest": digest})
        link.save_reported(queue, local_id, "forwarded")
    finally:
        queue_store.release_owner_lock(guard, remove=False)


def _import_receipt(queue: Path, peer: Mapping[str, object], body: Mapping[str, object]) -> None:
    message_id, state, at = body.get("id"), body.get("state"), body.get("at")
    if not rules.is_message_id(message_id) or state not in linkwire.RECEIPT_STATES \
            or not isinstance(at, (int, float)) or isinstance(at, bool):
        raise rules.Refused("invalid_record", "a receipt needs a message id, a known state and a time")
    guard = link.lock(queue)
    try:
        record = link.load_sent(queue, str(message_id))
        if record is None or record["sent"].get("peer") != peer["alias"]:
            return  # long gone here, or never sent to this machine
        if linkwire.moves_forward(record["sent"], str(state), float(at)):
            record["sent"].update(state=state, at=float(at), code=_short(body.get("code"), 64),
                                  reason=_short(body.get("reason"), 500))
            link.save_sent(queue, record)
    finally:
        queue_store.release_owner_lock(guard, remove=False)


def _import_names(queue: Path, peer: Mapping[str, object], body: Mapping[str, object]) -> None:
    at = body.get("at")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        raise rules.Refused("invalid_record", "a names file needs its time")
    names = []
    for item in body.get("names") or []:
        if isinstance(item, dict) and _short(item.get("harness"), 32) is not None:
            try:
                names.append({"name": rules.validate_name(item.get("name")), "harness": str(item["harness"])})
            except rules.Refused:
                continue
    guard = link.lock(queue)
    try:
        current = link.load_record(link.names_path(queue, str(peer["alias"]))) or {}
        if isinstance(current.get("at"), (int, float)) and current["at"] >= at:
            return  # an older list, replayed or late: the newer one stays
        link.save_names(queue, str(peer["alias"]), {"names": names[:1000], "at": float(at)})
    finally:
        queue_store.release_owner_lock(guard, remove=False)


def _own_node(queue: Path) -> str:
    guard = link.lock(queue)
    try:
        return link.node_id(queue)
    finally:
        queue_store.release_owner_lock(guard, remove=False)


def import_inbound(queue: Path) -> None:
    """Tower hook: admit what _link-sync fetched. Every file is checked again; a bad one is set aside.

    A pairing that ends (a bye, or a host that no longer knows it) is forgotten only after everything
    already fetched from it has been imported.
    """
    peers = [peer for peer in link.peers(queue) if peer.get("state") in ("active", "gone")]
    if not any(peer.get("state") == "gone" or link.spool_files(link.inbound(queue, str(peer["alias"])), 1)
               for peer in peers):
        return
    node = _own_node(queue)
    for peer in peers:
        alias = str(peer["alias"])
        bye = peer.get("state") == "gone"
        for path in link.spool_files(link.inbound(queue, alias), 200):
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                continue
            try:
                record = linkwire.unwrap(peer, node, data)
                kind, body = record["kind"], record["body"]
                if kind == "message":
                    try:
                        _admit_remote(queue, peer, node, body)
                        _receipt(queue, peer, node, str(body["id"]), "forwarded")
                    except rules.Refused as exc:
                        if not rules.is_message_id(body.get("id")):
                            raise
                        _receipt(queue, peer, node, str(body["id"]), "expired" if exc.code == "expired" else "rejected",
                                 code=exc.code, reason=str(exc))
                elif kind == "receipt":
                    _import_receipt(queue, peer, body)
                elif kind == "names":
                    _import_names(queue, peer, body)
                elif kind == "bye":
                    bye = True
            except (rules.Refused, KeyError, TypeError, ValueError) as exc:
                link.spool_write(link.quarantine(queue, alias), path.name, data)
                guard = link.lock(queue)
                try:
                    link.save_alert(queue, f"link:{alias}:{path.name}", {
                        "at": results.timestamp(time.time()), "reason": f"a file from {alias} was set aside: {exc}",
                        "kind": "link", "thread": None, "from": alias, "to": "this machine"})
                finally:
                    queue_store.release_owner_lock(guard, remove=False)
            except OSError as exc:
                print(f"reins: warning: could not import {path.name} from {alias}: {exc}", file=sys.stderr)
                continue  # try again next round
            link.spool_remove(path)
        if bye and not link.spool_files(link.inbound(queue, alias), 1):
            peering.forget(queue, alias)
            print(f"reins: {alias} unpaired from this machine", file=sys.stderr)


def _names(queue: Path) -> list[dict[str, str]]:
    return sorted(({"name": str(item["name"]), "harness": str(item["harness"])} for item in link.sessions(queue)),
                  key=lambda item: item["name"])


def report(queue: Path, *, now: Optional[float] = None) -> None:
    """Tower hook: tell each paired machine what became of its messages, and which names live here."""
    global _last_reported
    moment = time.time() if now is None else now
    if now is None and moment - _last_reported < REPORT_INTERVAL:
        return
    _last_reported = moment
    paired = {str(peer["alias"]): peer for peer in link.peers(queue) if peer.get("state") == "active"}
    if not paired:
        return
    node = _own_node(queue)
    names = _names(queue)
    digest = linkwire.digest({"names": names})
    for alias, peer in paired.items():
        key = f"names-{alias}"
        if "names" in (peer.get("features") or []) and link.load_reported(queue, key) != f"{peer['pair']}:{digest}":
            link.spool_write(link.outbox(queue, alias), f"n-{int(moment * 1000)}.json",
                             linkwire.encode(linkwire.wrap(peer, node, "names", {"names": names, "at": moment})))
            link.save_reported(queue, key, f"{peer['pair']}:{digest}")
    for message in link.all_messages(queue):
        alias = message["local"].get("peer")
        if not alias or alias not in paired:
            continue
        try:
            reported = link.load_reported(queue, str(message["id"]))
            if reported in linkwire.TERMINAL:
                continue
            state = state_of(queue, message)
            if state != "queued" and state != reported:
                _receipt(queue, paired[alias], node, str(message["origin_id"]), state)
                link.save_reported(queue, str(message["id"]), state)
        except (KeyError, TypeError, OSError, rules.Refused) as exc:
            print(f"reins: warning: could not report message {message.get('id')}: {exc}", file=sys.stderr)


def reply(queue: Path, args: argparse.Namespace, parent_id: str, *, subject: Optional[str], body: str,
          refs: list[dict[str, str]], wake: Optional[str] = None) -> dict[str, object]:
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
    peer = parent["local"].get("peer")
    to = f"{sender}@{peer}" if peer else sender
    return send(queue, args, to, subject=subject, body=body, refs=refs, wake=wake, reply_to=parent_id)


def _live_only(harness: str) -> bool:
    return getattr(get_agent(harness), "callback_delivery", "resume") == "live"


def _managed_pi(target: Mapping[str, object]) -> bool:
    """Whether this Pi session was opened by `reins pi`, the only kind that may be resumed offline."""
    probe = {"agent": "pi", "target": targets.session(str(target.get("session"))), **dict(target.get("route") or {})}
    try:
        return pi_channel.owner_record(probe)[1].get("managed") is True
    except (OSError, ValueError, KeyError):
        return False


def _default_wake(target: Mapping[str, object]) -> str:
    """What a message may do when the sender does not say: what a job result would.

    Codex stays on its queue: only that reaches an open Codex in its own turn, and
    nothing tells an open thread from a closed one, so a resume could be a second writer.
    """
    harness = str(target["harness"])
    if harness == "claude" or (harness == "agy" and not _live_only(harness)) or (harness == "pi" and _managed_pi(target)):
        return "resume"
    return "never"


def _expect_waiter(args: argparse.Namespace) -> None:
    """A live session that just registered or sent is about to wait; don't resume next to it first."""
    current = current_session(args)
    if (current is None or getattr(args, "session", None) or claude_session.live_wait_agent(current[0]) is None
            or os.environ.get(claude_session.RESUMED_TARGET_ENV, "").strip()):
        return
    claude_session.expect_live_waiter(current[1], claude_session.live_wait_grace(os.environ), agent=current[0])


def _callback_mode(harness: str, wake: str) -> str:
    """Never starts a process: Codex keeps it in its queue, Claude in the inbox, Pi waits for its mailbox.

    DSH stays "cli" for any wake: the daemon never resumes a live-only agent.
    """
    if wake == "never" and harness == "codex":
        return "codex-queue"
    if wake == "never" and harness in ("claude", "agy"):
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
        keep_listening=claude_session.live_wait_agent(harness) is not None and not claude_session.hook_delivers(harness))
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
    if isinstance(message.get("sent"), dict):  # to another machine: its state is what the receipts said
        sent = message["sent"]
        return {"id": message["id"], "callback": None, "state": sent.get("state"), "from": rules.sender_text(message),
                "to": f"{message['to']['name']}@{sent.get('peer')}", "subject": message["subject"],
                "thread": message["thread"], "hop": message["hop"], "in_reply_to": message.get("in_reply_to"),
                "created_at": message["created_at"], "expires_at": message["expires_at"], "wake": message.get("wake"),
                "note": sent.get("reason")}
    local = message["local"]
    return {"id": message["id"], "callback": local["callback"], "state": state_of(queue, message),
            "from": rules.sender_text(message), "to": message["to"]["name"], "subject": message["subject"],
            "thread": message["thread"], "hop": message["hop"], "in_reply_to": message.get("in_reply_to"),
            "created_at": message["created_at"], "expires_at": message["expires_at"], "wake": local["wake"],
            "note": local.get("note")}


def resolve(queue: Path, reference: str) -> dict[str, object]:
    """The message named by its ID or by its callback ID, as copied from an envelope."""
    root = ids.root_of(reference)
    message = link.load_message(queue, root) or (link.load_sent(queue, root) if rules.is_message_id(root) else None)
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
                link.remove_reported(queue, str(message["id"]))
                try:
                    (queue / "details" / f"{callback}.md").unlink()
                except OSError:
                    pass
        except (KeyError, TypeError, OSError, rules.Refused) as exc:
            print(f"reins: warning: could not maintain message {message.get('id')}: {exc}", file=sys.stderr)
    for record in link.all_sent(queue):
        try:
            sent = record["sent"]
            expires = results.seconds(record.get("expires_at"))
            if expires is None:
                continue
            if sent.get("wire") is not None:  # the sender died between its two writes
                guard = link.lock(queue)
                try:
                    current = link.load_sent(queue, str(record["id"]))
                    if current is not None and current["sent"].get("wire") is not None:
                        _write_outbox(queue, current)
                finally:
                    queue_store.release_owner_lock(guard, remove=False)
                continue
            waiting = link.outbox(queue, str(sent.get("peer"))) / f"{record['id']}.json"
            if sent.get("state") == "queued" and moment >= expires and waiting.exists():
                # Still here, so it never reached the other machine. Once it has left, only receipts decide.
                guard = link.lock(queue)
                try:
                    current = link.load_sent(queue, str(record["id"]))
                    if current is not None and current["sent"].get("state") == "queued" and waiting.exists():
                        link.spool_remove(waiting)
                        current["sent"].update(state="expired", at=moment, reason="never reached the other machine")
                        link.save_sent(queue, current)
                finally:
                    queue_store.release_owner_lock(guard, remove=False)
            elif moment - expires >= limits["retention_days"] * 86400:
                link.remove_sent(queue, str(record["id"]))
        except (KeyError, TypeError, OSError, rules.Refused) as exc:
            print(f"reins: warning: could not maintain sent message {record.get('id')}: {exc}", file=sys.stderr)
    for thread in link.all_threads(queue):
        if isinstance(thread.get("last_at"), (int, float)) and moment - thread["last_at"] >= limits["retention_days"] * 86400:
            link.remove_thread(queue, str(thread["id"]))


def _has_callback(queue: Path, callback: str) -> bool:
    return layout.ack_path(queue, callback).exists() or any(
        layout.request_path(queue, name, callback).exists()
        for name in (layout.ACTIVE_STATE, "pending", "running", "done", "failed", "canceled"))
