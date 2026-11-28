"""reins link connect, list and remove: pair this machine with another one so their sessions can talk.

Also the two hidden commands the pairing runs on its own: ``_link-box``, which an
SSH connection runs on the machine that hosts the mailbox, and ``_link-sync``,
which the daemon keeps running for each paired machine.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..core import linkwire
from ..core.messages import Refused
from ..store import layout

# The parser is built on every reins call; the engine is imported only when a link command runs.

TRANSPORT = {"ssh": "SSH", "dir": "shared directory"}


def _queue(args: argparse.Namespace) -> Path:
    return layout.queue_dir(args).expanduser().absolute()


def _daemon_note(queue: Path) -> None:
    from ..tower.main import read_daemon_runtime
    runtime = read_daemon_runtime()
    if runtime is None or runtime.get("queue_dir") != str(queue.resolve()):
        print("Note: no Reins daemon is running for this queue, and it is what carries messages. Start it with: "
              "reins setup --enable --now", file=sys.stderr)


def connect(args: argparse.Namespace) -> int:
    from ..engine import mailbox, peering
    from ..store import link
    from ..store import records as queue_store
    from .messages import _refused
    queue = _queue(args)
    queue_store.ensure_daemon_dirs(queue)
    try:
        if args.dir and args.host:
            raise Refused("invalid_record", "give either an SSH host or --dir, not both")
        wake = args.wake or "resume"  # as on one machine: a closed session is started when its harness allows it
        if args.dir and args.code:
            peer = peering.join_dir(queue, args.dir, args.code, alias=args.name, wake=wake)
            print(f"Paired with {peer['alias']} through {args.dir}. Send to its sessions as NAME@{peer['alias']}.")
        elif args.dir:
            peer, code = peering.invite_dir(queue, args.dir, alias=args.name, wake=wake)
            print("On the other machine, run (with its own path to the same shared directory):")
            print(f"  reins link connect --dir {args.dir} --code {code}")
            print("This pairing code holds the pair's key: pass it on by hand, and only once. The pairing finishes "
                  "by itself when the other machine accepts; `reins link list` shows it.")
        elif args.host:
            known = link.load_peer(queue, args.host) if _is_alias(args.host) else None
            if known is not None and not args.name:
                if args.wake is None:
                    print(f"{args.host} is already paired. To let its messages start a closed session here, or stop "
                          f"them: reins link connect {args.host} --wake resume|never. To pair again: "
                          f"reins link remove {args.host}, then connect.")
                    return 0
                # A setting of this side only: the pairing itself stays as it is.
                guard = link.lock(queue)
                try:
                    link.save_peer(queue, dict(known, wake=args.wake))
                finally:
                    queue_store.release_owner_lock(guard, remove=False)
                print(f"Messages from {args.host} may "
                      f"{'now start a closed session headless' if args.wake == 'resume' else 'no longer start a closed session'}.")
                return 0
            peer = peering.connect_ssh(queue, args.host, alias=args.name, wake=wake)
            print(f"Paired with {peer['alias']} over SSH; this machine carries messages both ways. "
                  f"Send to its sessions as NAME@{peer['alias']}.")
        else:
            raise Refused("invalid_record", "connect to what? Give an SSH host (reins link connect HOST) or a shared "
                                            "directory (reins link connect --dir PATH)")
    except Refused as error:
        return _refused(error)
    except mailbox.MailboxError as error:
        print(f"reins: could not reach the other machine: {error}", file=sys.stderr)
        print("Check that `ssh HOST` works without a password prompt and that Reins is installed there "
              "(`reins setup` puts it at ~/.reins/bin/reins).", file=sys.stderr)
        return 2
    if wake == "never":
        print("Messages from it never start a closed session here; they wait until the session opens.")
    _daemon_note(queue)
    return 0


def _is_alias(text: str) -> bool:
    try:
        linkwire.validate_alias(text)
        return True
    except Refused:
        return False


def list_peers(args: argparse.Namespace) -> int:
    from ..engine import link_sync
    from ..store import link
    queue = _queue(args)
    rows = []
    for peer in link.peers(queue):
        alias = str(peer["alias"])
        status = link.load_status(queue, alias)
        rows.append({
            "alias": alias, "node": peer.get("node"), "state": peer.get("state"),
            "transport": peer.get("transport"), "role": peer.get("role"),
            "where": peer.get("host") or peer.get("path") or "connects to this machine",
            "wake": peer.get("wake"), "protocol": peer.get("protocol"), "paired_at": peer.get("paired_at"),
            "running": link_sync.running(queue, alias),
            "waiting_to_send": len(link.spool_files(link.outbox(queue, alias))),
            "names": [f"{item['name']}@{alias}" for item in link.load_names(queue, alias)],
            "problem": link_sync.problem(queue, alias),
            "last_ok": max((str(value) for key, value in status.items() if key.endswith("_ok_at")), default=None),
        })
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("No paired machines. Pair one with: reins link connect HOST  (or --dir PATH for a shared directory)")
        return 0
    for row in rows:
        how = TRANSPORT.get(str(row["transport"]), row["transport"])
        if row["role"] == "carrier":
            how += ", this machine carries"
        elif row["role"] == "host":
            how += ", it carries"
        print(f"{row['alias']:<16} {row['state']:<8} {how}  {row['where']}")
        if row["state"] == "pending":
            print("    waiting for the other machine to accept the pairing code")
            continue
        print(f"    carrier {'running' if row['running'] else 'not running'}; last contact {row['last_ok'] or 'never'}; "
              f"{row['waiting_to_send']} file(s) waiting to go; closed sessions "
              f"{'may' if row['wake'] == 'resume' else 'are not'} started by its messages")
        if row["names"]:
            print(f"    sessions: {', '.join(row['names'])}")
        if row["problem"]:
            print(f"    NOT GETTING THROUGH: {row['problem']}")
    return 0


def remove(args: argparse.Namespace) -> int:
    from ..engine import peering
    from .messages import _refused
    try:
        result = peering.remove(_queue(args), args.alias)
    except Refused as error:
        return _refused(error)
    told = {True: "it was told", False: "it could not be told now and will stop hearing from this one",
            "next": "it finds out the next time it connects"}[result["told"]]
    print(f"Unpaired {args.alias}; {told}. Messages already received stay.")
    return 0


def box(args: argparse.Namespace) -> int:
    """Hidden ``reins _link-box``: what an SSH connection runs here. JSON in on stdin, JSON out on stdout."""
    from ..engine import mailbox, peering
    from ..store import link
    from ..store import records as queue_store
    queue = _queue(args)
    try:
        request = json.loads(sys.stdin.buffer.read(64 * 1024 * 1024).decode("utf-8"))
    except (UnicodeError, ValueError):
        print(json.dumps({"error": "the request is not JSON"}))
        return 0
    if args.hello:
        if args.pair:
            answer: object = {"error": "this key may only use its mailbox, not pair again", "code": "permission_denied"}
        else:
            queue_store.ensure_daemon_dirs(queue)
            answer = peering.serve_hello(queue, request)
    else:
        pair = request.get("pair") if isinstance(request, dict) else None
        root = link.mailbox_root(queue, str(pair)) if linkwire.is_pair_id(pair) else None
        paired = root is not None and any(peer.get("pair") == pair and peer.get("role") == "host"
                                          for peer in link.peers(queue))
        if args.pair and pair != args.pair:
            answer = {"error": "this key may only use its own mailbox", "code": "permission_denied"}
        elif paired:
            answer = mailbox.serve(root, request)
        else:  # the carrier drops its side of the pairing when it hears this
            answer = {"error": "no such pairing on this machine; pair again with reins link connect",
                      "code": "unknown_peer"}
    sys.stdout.write(json.dumps(answer))
    sys.stdout.flush()
    return 0


def sync(args: argparse.Namespace) -> int:
    """Hidden ``reins _link-sync --peer ALIAS``, started by the daemon."""
    from ..engine import link_sync
    return link_sync.run(_queue(args), args.peer)


def add_parsers(sub: argparse._SubParsersAction) -> None:
    link_parser = sub.add_parser("link", help="Pair this machine with another so their sessions can message each other")
    actions = link_parser.add_subparsers(dest="link_action", required=True)
    connect_parser = actions.add_parser(
        "connect", help="Pair over SSH (reins link connect HOST) or a shared directory (--dir PATH)")
    connect_parser.add_argument("host", nargs="?", help="An SSH destination this machine can reach, as for `ssh`")
    connect_parser.add_argument("--dir", help="A directory both machines see; the first machine gets a pairing code")
    connect_parser.add_argument("--code", help="With --dir: the code the other machine printed")
    connect_parser.add_argument("--name", help="What to call the other machine here (default: its host name)")
    connect_parser.add_argument("--wake", choices=linkwire.WAKE_MODES,
                                help="resume (default): its messages start a closed session as messages on this "
                                     "machine do, which costs model usage. never: they wait until the session opens. "
                                     "With the name of a machine already paired, only changes this")
    connect_parser.add_argument("--queue-dir", help="Wakeup queue directory")
    list_parser = actions.add_parser("list", help="Paired machines, their sessions and whether messages get through")
    list_parser.add_argument("--json", action="store_true", help="Print JSON")
    list_parser.add_argument("--queue-dir", help="Wakeup queue directory")
    remove_parser = actions.add_parser("remove", help="Unpair a machine")
    remove_parser.add_argument("alias", help="Its name in reins link list")
    remove_parser.add_argument("--queue-dir", help="Wakeup queue directory")
    box_parser = sub.add_parser("_link-box", help=argparse.SUPPRESS)
    box_parser.add_argument("--hello", action="store_true", help=argparse.SUPPRESS)
    box_parser.add_argument("--pair", help=argparse.SUPPRESS)
    box_parser.add_argument("--queue-dir", help=argparse.SUPPRESS)
    sync_parser = sub.add_parser("_link-sync", help=argparse.SUPPRESS)
    sync_parser.add_argument("--peer", required=True, help=argparse.SUPPRESS)
    sync_parser.add_argument("--queue-dir", help=argparse.SUPPRESS)


def dispatch(args: argparse.Namespace) -> int:
    if args.mode == "_link-box":
        return box(args)
    if args.mode == "_link-sync":
        return sync(args)
    from .messages import _writes_state
    command = {"connect": _writes_state(connect), "list": list_peers, "remove": _writes_state(remove)}[args.link_action]
    return command(args)
