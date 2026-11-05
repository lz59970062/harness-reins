"""reins session, send, reply, inbox and message: sessions on this machine talking to each other."""
from __future__ import annotations

import argparse
import functools
import json
import sys
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional

from ..core import messages as rules
from ..engine import messages
from ..store import layout, link
from ..store import records as queue_store
from . import callbacks

ARRIVAL_ADVICE = {
    "claude": "Messages reach this open window through the hook `reins setup` installs, under `reins claude`, or "
              "while `reins wait` runs in the background. Without any of them a message waits for the window and "
              "never forks it; once the window closes, it resumes the session headless.",
    "codex": "Messages reach this thread through the Codex queue, and wait there while it is closed.",
    "dsh": "Messages reach this session while `reins wait` runs as a background job. "
           "Start it now and again after each one; until then they wait in `reins inbox`.",
    "pi": "Messages reach this session through the Reins Pi extension. While it is closed they wait for it to reopen, "
          "or resume it headless if it was opened with `reins pi`.",
}


def _queue(args: argparse.Namespace) -> Path:
    return layout.queue_dir(args).expanduser().absolute()


def _writes_state(command: Callable[[argparse.Namespace], int]) -> Callable[[argparse.Namespace], int]:
    """A sandbox that blocks Reins's directory is common and fixable, so it gets a useful answer."""
    @functools.wraps(command)
    def run(args: argparse.Namespace) -> int:
        try:
            return command(args)
        except OSError as exc:
            print(f"reins: could not use {layout.link_root(_queue(args)).parent} ({exc}). If this session's sandbox blocks "
                  "that directory, approve this command or add the directory to the session's writable roots.",
                  file=sys.stderr)
            return 2
    return run


def _refused(error: rules.Refused) -> int:
    print(f"reins: refused ({error.code}): {error}", file=sys.stderr)
    return 3 if error.alert else 2


def _body(args: argparse.Namespace) -> Optional[str]:
    if args.message_file is not None:
        try:
            if args.message_file == "-":
                return sys.stdin.read()
            return Path(args.message_file).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise rules.Refused("invalid_record", f"could not read the message: {exc}") from exc
    return args.message


@_writes_state
def session_register(args: argparse.Namespace) -> int:
    try:
        record = messages.register(_queue(args), args, args.name, replace=args.replace)
    except rules.Refused as error:
        return _refused(error)
    print(f"{record['name']} now names this {record['harness']} session.")
    print(ARRIVAL_ADVICE[str(record["harness"])])
    return 0


def session_list(args: argparse.Namespace) -> int:
    queue = _queue(args)
    rows = [{"name": record["name"], "harness": record["harness"], "arrival": messages.arrival(record),
             "cwd": record.get("cwd"), "session": record.get("session"), "registered_at": record.get("registered_at")}
            for record in link.sessions(queue)]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("No named sessions. Inside a session, run: reins session register NAME")
        return 0
    for row in rows:
        print(f"{row['name']:<20} {row['harness']:<7} {row['arrival']:<12} {row['cwd']}")
    print("arrival: live = reaches it now; codex-queue = Codex delivers it; window = waits for this open window's next `reins wait` or for it to close; resume = a headless resume delivers it; inbox = waits until it is read")
    return 0


@_writes_state
def session_unregister(args: argparse.Namespace) -> int:
    try:
        found = messages.unregister(_queue(args), args.name)
    except rules.Refused as error:
        return _refused(error)
    if not found:
        print(f"reins: no session is named {args.name}", file=sys.stderr)
        return 2
    print(f"{args.name} no longer names a session. Messages already sent to it stay with that session.")
    return 0


def _report(queue: Path, message: Mapping[str, object], as_json: bool) -> None:
    item = messages.summary(queue, message)
    if as_json:
        print(json.dumps(item, ensure_ascii=False, indent=2))
        return
    print(f"{item['id']} queued for {item['to']} (thread {item['thread']}, hop {item['hop']})")
    if item.get("note"):
        print(f"Note: {item['note']}.")


@_writes_state
def send(args: argparse.Namespace) -> int:
    queue = _queue(args)
    try:
        body = _body(args)
        if not body and not args.subject:
            raise rules.Refused("invalid_record", "nothing to send: pass --message, --message-file or --subject")
        message = messages.send(queue, args, args.name, subject=args.subject, body=body or "",
                                refs=[rules.parse_ref(ref) for ref in args.ref or []], wake=args.wake)
    except rules.Refused as error:
        return _refused(error)
    _report(queue, message, args.json)
    return 0


@_writes_state
def reply(args: argparse.Namespace) -> int:
    queue = _queue(args)
    try:
        body = _body(args)
        if not body and not args.subject:
            raise rules.Refused("invalid_record", "nothing to send: pass --message, --message-file or --subject")
        parent = messages.resolve(queue, args.id)
        message = messages.reply(queue, args, str(parent["id"]), subject=args.subject, body=body or "",
                                 refs=[rules.parse_ref(ref) for ref in args.ref or []], wake=args.wake)
    except rules.Refused as error:
        return _refused(error)
    _report(queue, message, args.json)
    # Replying proves the message was read, so the reply is also its ACK.
    callbacks.ack(argparse.Namespace(id=parent["local"]["callback"], callback_id=None, message="replied",
                                     queue_dir=str(queue)))
    return 0


def _print_rows(rows: Iterable[Mapping[str, object]], *, sent: bool) -> None:
    for row in rows:
        party = f"to {row['to']}" if sent else f"from {row['from']}"
        print(f"{row['id']}  {row['state']:<9} {row['created_at']}  {party}: {row['subject']}")


def inbox(args: argparse.Namespace) -> int:
    queue = _queue(args)
    try:
        name = args.name
        if args.thread and not rules.is_thread_id(args.thread):
            raise rules.Refused("invalid_record", f"{args.thread!r} is not a thread id")
        if name is None and not args.thread:
            current = messages.current_session(args)
            name = messages.name_of(queue, *current) if current else None
            if name is None:
                raise rules.Refused("unknown_name", "whose inbox? Run this in a named session, or pass --name NAME or --thread ID")
        elif name is not None:
            rules.validate_name(name)
    except rules.Refused as error:
        return _refused(error)
    rows = []
    for message in link.all_messages(queue):
        if args.thread:
            selected = message.get("thread") == args.thread
        elif args.sent:
            selected = message["from"].get("name") == name
        else:
            selected = message["to"]["name"] == name
        if selected:
            rows.append(messages.summary(queue, message))
    if not args.all and not args.thread and not args.sent:
        rows = [row for row in rows if row["state"] in messages.UNREAD_STATES]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("No messages." if args.all or args.thread or args.sent else "No unread messages.")
        return 0
    _print_rows(rows, sent=args.sent)
    if not args.sent and not args.thread:
        print("Read one: reins message show ID")
    return 0


def message_show(args: argparse.Namespace) -> int:
    queue = _queue(args)
    try:
        message = messages.resolve(queue, args.id)
    except rules.Refused as error:
        return _refused(error)
    item = messages.summary(queue, message)
    if args.json:
        print(json.dumps(dict(item, body=message.get("body"), refs=message.get("refs")), ensure_ascii=False, indent=2))
        return 0
    print(f"State: {item['state']}")
    if item.get("note"):
        print(f"Note from Reins: {item['note']}.")
    print(rules.body_text(message))
    if item["state"] in messages.UNREAD_STATES:
        print(f"\nACK after reading: reins ack {item['callback']}")
    return 0


@_writes_state
def message_alerts(args: argparse.Namespace) -> int:
    queue = _queue(args)
    guard = None
    if args.clear:
        queue_store.ensure_daemon_dirs(queue)
        guard = link.lock(queue)
    try:
        if args.clear:
            print(f"Cleared {link.clear_alerts(queue)} alert(s). Threads stay stopped; a new message starts a new thread.")
            return 0
    finally:
        if guard is not None:
            queue_store.release_owner_lock(guard, remove=False)
    found = link.alerts(queue)
    if not found:
        print("No message threads have stopped at a limit.")
        return 0
    for alert in found:
        print(f"{alert.get('at')}  {alert.get('from')} to {alert.get('to')}: {alert.get('reason')}")
    print("Clear these with: reins message alerts --clear")
    return 0
