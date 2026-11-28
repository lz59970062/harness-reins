"""``_link-sync``: moves files between this node's outbox and inbound and a paired mailbox (protocol 9.4).

The tower keeps one running per paired machine; a lock makes sure there is only one.
It never touches ``queue/**`` and never admits anything: what it fetches and checks
lands in ``link/inbound/<alias>/`` for the tower, which runs the same admission as
``reins send``. Two threads keep sending and receiving independent:

- send: whatever is in the outbox goes into the other side's compartment, then
  leaves the outbox. A crash between the two only repeats a put, which the name
  makes harmless.
- receive: wait on this side's compartment, fetch, check the signature, write to
  inbound (or quarantine), and only then remove it from the mailbox, on the next
  round. A crash repeats a file, never loses one.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Mapping, Optional

from ..core import linkwire, results
from ..core.messages import Refused
from ..runtime import worker_command
from ..store import link
from ..store import records as queue_store
from . import mailbox, peering

BATCH = 50              # files per round trip
BATCH_BYTES = 8 * 1024 * 1024  # bytes of files one put batch carries
RECEIVE_WAIT = 25.0     # seconds one wait on the mailbox may block
SEND_POLL = 0.5         # seconds between looks at the local outbox
BACKOFF_FIRST = 2.0
BACKOFF_LIMIT = 300.0
RESTART_GAP = 5.0       # seconds between two starts of the same peer's process
STATUS_EVERY = 30.0     # seconds between two "still fine" writes of the status file
STOP_GRACE = 2.0        # seconds to let the threads finish what they are doing when the carrier stops


def lock_id(alias: str) -> str:
    return f"link-sync-{alias}"


# Common causes, in the words a person (or an agent) can act on without digging. First match wins: the
# connection-stage patterns come before the login ones, because both can say "Permission denied".
_CAUSES = (
    (r"Could not resolve hostname|Name or service not known|nodename nor servname",
     "its host name does not resolve; check the name you gave `reins link connect` and the DNS"),
    (r"connect to host .*: Connection refused", "nothing answers SSH there; is the machine up and its sshd running?"),
    (r"connect to host .*: (Permission denied|Operation not permitted)",
     "this machine is not allowed to open that connection (a firewall or network policy blocks it)"),
    (r"timed out|TimeoutExpired|No route to host|Network is unreachable|Connection reset|Broken pipe|"
     r"Connection closed|kex_exchange_identification",
     "it cannot be reached over the network; is it up, and is this machine's network (VPN, proxy) working?"),
    (r"Host key verification failed|REMOTE HOST IDENTIFICATION HAS CHANGED",
     "its SSH host key is unknown or changed; run `ssh {where}` once by hand and confirm the key"),
    (r"Permission denied", "SSH refused the login; `ssh {where} true` must work with a key and no password prompt"),
    (r"reins: not found|command not found|exited 127",
     "Reins is not installed there; run `reins setup` on it (it puts Reins at ~/.reins/bin/reins)"),
    (r"unsupported_version", "the two machines speak different protocol versions; upgrade Reins on both"),
    (r"shared directory", "the shared directory is not there; is it mounted on this machine?"),
    (r"No space left|Disk quota", "a disk is full"),
)


def diagnose(text: str, where: str) -> str:
    """The likely cause of a link error and what to do about it, in one line."""
    for pattern, cause in _CAUSES:
        if re.search(pattern, text):
            return cause.format(where=where)
    return "the last attempt failed (the error is below); the carrier keeps retrying"


class _Status:
    """What `reins link list`, `reins status` and `reins send` report: when it last worked, and, while it does not,
    since when, why and what to do, per direction."""

    def __init__(self, queue: Path, alias: str, where: str = "") -> None:
        self.queue, self.alias, self.where = queue, alias, where or alias
        self.guard = threading.Lock()
        self.record = dict(link.load_status(queue, alias))
        self.written = 0.0

    def ok(self, direction: str) -> None:
        with self.guard:
            now = time.time()
            changed = self.record.pop(f"{direction}_error", None) is not None
            for key in ("hint", "since", "error_at"):
                self.record.pop(f"{direction}_{key}", None)
            self.record[f"{direction}_ok_at"] = results.timestamp(now)
            if changed:
                print(f"reins: link {self.alias}: {direction}: working again", file=sys.stderr, flush=True)
            if changed or now - self.written >= STATUS_EVERY:
                self._write(now)

    def error(self, direction: str, text: str) -> None:
        with self.guard:
            now = time.time()
            changed = self.record.get(f"{direction}_error") != text
            self.record[f"{direction}_error"] = text
            self.record[f"{direction}_hint"] = diagnose(text, self.where)
            self.record.setdefault(f"{direction}_since", results.timestamp(now))
            self.record[f"{direction}_error_at"] = results.timestamp(now)
            if changed:
                print(f"reins: link {self.alias}: {direction}: {self.record[f'{direction}_hint']} ({text})",
                      file=sys.stderr, flush=True)
            self._write(now)

    def _write(self, now: float) -> None:
        self.written = now
        try:
            link.save_status(self.queue, self.alias, dict(self.record, pid=os.getpid()))
        except OSError:
            pass


def _alert(queue: Path, alias: str, name: str, reason: str) -> None:
    guard = link.lock(queue)
    try:
        link.save_alert(queue, f"link:{alias}:{name}", {"at": results.timestamp(time.time()), "reason": reason,
                                                         "kind": "link", "thread": None, "from": alias,
                                                         "to": "this machine"})
    finally:
        queue_store.release_owner_lock(guard, remove=False)


def _same_pair(queue: Path, alias: str, pair: object) -> Optional[dict[str, object]]:
    peer = link.load_peer(queue, alias)
    return peer if peer is not None and peer.get("pair") == pair else None


def send_once(queue: Path, peer: Mapping[str, object], box: mailbox.Mailbox, theirs: str) -> int:
    """Put what waits in the outbox into the other side's compartment; how many files went."""
    alias = str(peer["alias"])
    ops, sending, size = [], [], 0
    for path in link.spool_files(link.outbox(queue, alias), BATCH):
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            continue
        if ops and size + len(data) > BATCH_BYTES:
            break  # the rest goes in the next round trip
        ops.append({"op": "put", "box": theirs, "name": path.name, "data": mailbox._text(data)})
        sending.append((path, data))
        size += len(data)
    if not ops:
        return 0
    for (path, data), answer in zip(sending, box.batch(ops)):
        if answer.get("ok"):
            link.spool_remove(path)
        elif answer.get("code"):  # refused for good: keep it out of the way and say so
            link.spool_write(link.quarantine(queue, alias), path.name, data)
            link.spool_remove(path)
            _alert(queue, alias, path.name, f"could not send {path.name}: {answer.get('error')}")
        else:
            raise mailbox.MailboxError(str(answer.get("error")))
    return len(ops)


def receive_once(queue: Path, peer: Mapping[str, object], node: str, box: mailbox.Mailbox, mine: str,
                 taken: list[str], wait: float) -> list[str]:
    """Remove what was taken last time, wait for files, fetch and check them. What to remove next time."""
    alias = str(peer["alias"])
    ops: list[dict[str, object]] = []
    if taken:
        ops.append({"op": "remove", "box": mine, "names": list(taken)})
    ops.append({"op": "wait", "box": mine, "timeout": wait})
    answers = box.batch(ops)
    for answer in answers:
        if not answer.get("ok"):
            raise mailbox.MailboxError(str(answer.get("error")))
    names = [name for name in answers[-1].get("names") or [] if linkwire.is_file_name(name)][:BATCH]
    if not names:
        return []
    fetched: list[str] = []
    for name, answer in zip(names, box.batch([{"op": "get", "box": mine, "name": name} for name in names])):
        if not answer.get("ok") and answer.get("code"):
            # Refused for good (too large, say): set it aside so it cannot hold up the files after it. Only a
            # note is kept: the mailbox does not hand out a file that size.
            note = {"name": name, "code": answer["code"], "error": str(answer.get("error"))}
            link.spool_write(link.quarantine(queue, alias), name, json.dumps(note).encode("utf-8"))
            _alert(queue, alias, name, f"a file from {alias} was set aside ({answer['code']}): {answer.get('error')}")
            fetched.append(name)
            continue
        if not answer.get("ok"):
            raise mailbox.MailboxError(str(answer.get("error")))
        if answer.get("data") is None:
            continue  # removed meanwhile
        data = mailbox._bytes(answer["data"])
        try:
            linkwire.unwrap(peer, node, data)
            link.spool_write(link.inbound(queue, alias), name, data)
        except Refused as exc:
            link.spool_write(link.quarantine(queue, alias), name, data)
            _alert(queue, alias, name, f"a file from {alias} was set aside ({exc.code}): {exc}")
        fetched.append(name)  # stored here first, so removing it from the mailbox can lose nothing
    return fetched


def _gone(queue: Path, peer: Mapping[str, object], exc: BaseException) -> bool:
    """Whether the machine hosting the mailbox no longer knows this pairing; if so, drop it here too."""
    if not (isinstance(exc, Refused) and exc.code == "unknown_peer" and peer.get("role") == "carrier"):
        return False
    alias = str(peer["alias"])
    # Marked, not dropped: the tower first imports what this process already fetched, then forgets the pairing.
    guard = link.lock(queue)
    try:
        current = link.load_peer(queue, alias)
        if current is not None and current.get("pair") == peer.get("pair"):
            link.save_peer(queue, dict(current, state="gone"))
    finally:
        queue_store.release_owner_lock(guard, remove=False)
    _alert(queue, alias, "unpaired", f"{alias} no longer knows this pairing, so it is dropped here too; "
                                     f"pair again with reins link connect if that was not intended")
    return True


def _describe(exc: BaseException) -> str:
    """An error as the status shows it: the message alone for the kinds that carry their own wording."""
    return str(exc) if isinstance(exc, (mailbox.MailboxError, Refused, OSError)) else f"{type(exc).__name__}: {exc}"


def _pause(stop: threading.Event, back: threading.Event, seconds: float) -> None:
    """Wait out a backoff, but stop at once when told to, or when the other direction just got through."""
    deadline = time.monotonic() + seconds
    while not stop.is_set() and not back.is_set() and time.monotonic() < deadline:
        time.sleep(min(SEND_POLL, max(deadline - time.monotonic(), 0)))
    back.clear()


def _send_loop(queue: Path, peer: Mapping[str, object], box: mailbox.Mailbox, theirs: str,
               stop: threading.Event, status: _Status, back: threading.Event) -> None:
    backoff = BACKOFF_FIRST
    while not stop.is_set():
        try:
            if not send_once(queue, peer, box, theirs):
                stop.wait(SEND_POLL)
                continue
            status.ok("send")
            back.set()  # the mailbox answers again: let the receiving side retry now, not after its backoff
            backoff = BACKOFF_FIRST
        except Exception as exc:  # noqa: BLE001 - a thread that died silently would leave the pair half-carried
            if _gone(queue, peer, exc):
                stop.set()
                return
            status.error("send", _describe(exc))
            stop.wait(backoff)
            backoff = min(backoff * 2, BACKOFF_LIMIT)


def _receive_loop(queue: Path, peer: Mapping[str, object], node: str, box: mailbox.Mailbox, mine: str,
                  stop: threading.Event, status: _Status, back: threading.Event) -> None:
    taken: list[str] = []
    backoff = BACKOFF_FIRST
    while not stop.is_set():
        try:
            taken = receive_once(queue, peer, node, box, mine, taken, RECEIVE_WAIT)
            status.ok("receive")
            backoff = BACKOFF_FIRST
        except Exception as exc:  # noqa: BLE001 - see _send_loop
            if _gone(queue, peer, exc):
                stop.set()
                return
            status.error("receive", _describe(exc))
            _pause(stop, back, backoff)
            backoff = min(backoff * 2, BACKOFF_LIMIT)


def step(queue: Path, alias: str) -> None:
    """One full round for one pairing, in this process: what tests and `--once` daemons use."""
    peer = link.load_peer(queue, alias)
    if peer is None:
        return
    if peer.get("state") == "pending":
        peering.finish_dir(queue, alias)
        return
    guard = link.lock(queue)
    try:
        node = link.node_id(queue)
    finally:
        queue_store.release_owner_lock(guard, remove=False)
    box = peering.open_mailbox(queue, peer)
    try:
        send_once(queue, peer, box, linkwire.box(str(peer["node"])))
        taken = receive_once(queue, peer, node, box, linkwire.box(node), [], 0)
        if taken:
            box.remove(linkwire.box(node), taken)
    except Refused as exc:
        if not _gone(queue, peer, exc):
            raise


def run(queue: Path, alias: str) -> int:
    """The hidden ``reins _link-sync --peer ALIAS``. Returns when the pairing is gone or its daemon is."""
    guard = queue_store.acquire_owner_lock(queue, lock_id(alias), blocking=False)
    if guard is None:
        return 0  # another one already runs
    parent = os.getppid()
    try:
        peer = link.load_peer(queue, alias)
        if peer is None:
            return 0
        status = _Status(queue, alias, str(peer.get("host") or peer.get("path") or alias))
        while peer.get("state") == "pending":
            try:
                if peering.finish_dir(queue, alias) is not None:
                    return 0  # the tower starts the paired one, under its final alias
            except Refused as exc:
                status.error("pairing", str(exc))
            time.sleep(2.0)
            peer = _same_pair(queue, alias, peer["pair"])
            if peer is None or os.getppid() != parent:
                return 0
        guard_node = link.lock(queue)
        try:
            node = link.node_id(queue)
        finally:
            queue_store.release_owner_lock(guard_node, remove=False)
        box = peering.open_mailbox(queue, peer)
        stop = threading.Event()
        back = threading.Event()
        threads = [threading.Thread(target=_send_loop, daemon=True,
                                    args=(queue, peer, box, linkwire.box(str(peer["node"])), stop, status, back)),
                   threading.Thread(target=_receive_loop, daemon=True,
                                    args=(queue, peer, node, box, linkwire.box(node), stop, status, back))]
        for thread in threads:
            thread.start()
        while not stop.is_set():
            time.sleep(1.0)
            # A dead thread ends the process, so the tower starts a whole new one.
            if (_same_pair(queue, alias, peer["pair"]) is None or os.getppid() != parent
                    or not all(thread.is_alive() for thread in threads)):
                stop.set()
        # Both threads stop at their next look. A wait or an SSH call in progress is not waited for: every
        # write is a temporary name plus a rename, so leaving now loses nothing.
        for thread in threads:
            thread.join(timeout=STOP_GRACE)
        return 0
    finally:
        queue_store.release_owner_lock(guard, remove=False)


def problem(queue: Path, alias: str) -> Optional[str]:
    """Why messages to and from ``alias`` do not get through right now, ready to show; None while they do."""
    status = link.load_status(queue, alias)
    parts: list[str] = []
    for direction in ("send", "receive", "pairing"):
        error = status.get(f"{direction}_error")
        if not error:
            continue
        hint = str(status.get(f"{direction}_hint") or diagnose(str(error), alias))
        since = status.get(f"{direction}_since") or status.get(f"{direction}_error_at")
        text = f"{hint} (since {since}; {error})"
        if not any(hint in part for part in parts):  # sending and receiving usually fail for the same reason
            parts.append(f"{direction}: {text}")
    return "; ".join(parts) or None


def problems(queue: Path) -> list[tuple[str, str]]:
    """(alias, what is wrong) for each paired machine whose last move failed, for status and doctor."""
    found = []
    for peer in link.peers(queue):
        text = problem(queue, str(peer.get("alias")))
        if text:
            found.append((str(peer.get("alias")), text))
    return found


# The tower's side -----------------------------------------------------------------

_children: dict[str, subprocess.Popen] = {}
_started: dict[str, float] = {}


def running(queue: Path, alias: str) -> bool:
    lock = queue_store.acquire_owner_lock(queue, lock_id(alias), blocking=False)
    if lock is None:
        return True
    queue_store.release_owner_lock(lock, remove=False)
    return False


def supervise(queue: Path) -> None:
    """Keep one ``_link-sync`` per paired machine, restarting a dead one no more than every few seconds."""
    for alias, child in list(_children.items()):
        if child.poll() is not None:
            del _children[alias]
    for peer in link.peers(queue):
        alias = str(peer.get("alias"))
        if peer.get("state") == "gone" or alias in _children or running(queue, alias):
            continue
        now = time.monotonic()
        if now - _started.get(alias, -RESTART_GAP) < RESTART_GAP:
            continue
        _started[alias] = now
        log = link.status_path(queue, alias).with_suffix(".log")
        try:
            queue_store.private_directory(log.parent)
            with open(log, "ab") as handle:
                _children[alias] = subprocess.Popen(
                    worker_command("_link-sync", "--peer", alias, "--queue-dir", str(queue)),
                    stdin=subprocess.DEVNULL, stdout=handle, stderr=handle, close_fds=True)
        except OSError as exc:
            print(f"reins: warning: could not start the link to {alias}: {exc}", file=sys.stderr)
