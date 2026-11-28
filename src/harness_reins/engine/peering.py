"""Pairing two machines by a handshake, and unpairing them (docs/design/link-protocol.md, 9.3).

Nothing here is written by hand: ``reins link connect`` agrees on both node ids,
the names each side uses for the other, the protocol version, the features and a
pair key, and saves the result as ``link/peers/<alias>.json``.

Over SSH the account itself proves who connects, so one command pairs. A shared
directory can be written by anyone who can reach it, so its pairing code carries
the key that every later file is signed with.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Mapping, Optional

from ..core import linkwire, results
from ..core.messages import Refused
from ..store import link
from ..store import records as queue_store
from . import mailbox

HELLO_TIMEOUT = 60.0
PENDING_PREFIX = "pending-"


def _node(queue: Path) -> str:
    guard = link.lock(queue)
    try:
        return link.node_id(queue)
    finally:
        queue_store.release_owner_lock(guard, remove=False)


def _free_alias(queue: Path, wanted: str, node: Optional[str]) -> str:
    """``wanted``, or ``wanted-2`` and so on while another machine holds it."""
    alias = linkwire.validate_alias(wanted)
    candidate, count = alias, 1
    while True:
        holder = link.load_peer(queue, candidate)
        if holder is None or (node is not None and holder.get("node") == node):
            return candidate
        count += 1
        suffix = f"-{count}"
        candidate = alias[:32 - len(suffix)] + suffix


def _drop(queue: Path, peer: Mapping[str, object], reason: str) -> None:
    """Forget a pairing: its record, its outbox (its files carry the old key) and a hosted mailbox."""
    alias = str(peer["alias"])
    for path in link.spool_files(link.outbox(queue, alias)):
        link.spool_remove(path)
    for record in link.all_sent(queue):
        sent = record["sent"]
        if sent.get("peer") == alias and sent.get("state") == "queued":
            sent.update(state="failed", code="unpaired", reason=reason, at=time.time())
            link.save_sent(queue, record)
    link.remove_peer(queue, alias)
    if peer.get("role") == "host" and linkwire.is_pair_id(peer.get("pair")):
        shutil.rmtree(link.mailbox_root(queue, str(peer["pair"])), ignore_errors=True)


def _replace(queue: Path, node: str, keep: Optional[str] = None) -> Optional[str]:
    """Drop an earlier pairing the same machine abandoned (it handshook again, so it no longer has that key).

    Like any unpairing, what was in flight on it is not delivered, and this side's unsent messages to it are
    marked failed. Returns its alias, so the new pairing keeps the name.
    """
    earlier = link.peer_by_node(queue, node)
    if earlier is None:
        return None
    _drop(queue, earlier, "paired again before it was delivered")
    return str(earlier["alias"]) if keep is None else keep


def _record(*, alias: str, node: Optional[str], pair: str, key: str, protocol: Optional[int], features: list[str],
            transport: str, role: str, state: str, wake: str, **where: object) -> dict[str, object]:
    if wake not in linkwire.WAKE_MODES:
        raise Refused("invalid_record", f"--wake must be one of {', '.join(linkwire.WAKE_MODES)}")
    return {"schema": linkwire.PEER_SCHEMA, "alias": alias, "node": node, "pair": pair, "key": key,
            "protocol": protocol, "features": features, "transport": transport, "role": role, "state": state,
            "wake": wake, "paired_at": results.timestamp(time.time()), **where}


# SSH ------------------------------------------------------------------------------

def connect_ssh(queue: Path, host: str, *, alias: Optional[str] = None, wake: str = "resume") -> dict[str, object]:
    """Pair with ``host`` over SSH. This machine becomes the carrier: it moves files both ways."""
    node = _node(queue)
    for earlier in link.peers(queue):  # checked before the other side changes anything
        if earlier.get("host") == host or (alias is not None and earlier.get("alias") == alias):
            raise Refused("already_paired", f"{host} is already paired as {earlier['alias']}. To pair again, first run: "
                                            f"reins link remove {earlier['alias']}")
    answer = mailbox.ssh_call(host, ["--hello"], linkwire.hello(node, linkwire.own_alias()), HELLO_TIMEOUT)
    if isinstance(answer, dict) and answer.get("error"):
        raise Refused(str(answer.get("code") or "invalid_record"), f"{host}: {answer['error']}")
    welcome = linkwire.check_hello(answer, "welcome")
    pair, key, protocol = welcome.get("pair"), welcome.get("key"), welcome.get("protocol")
    if not linkwire.is_pair_id(pair) or not linkwire.is_key(key) or protocol not in linkwire.PROTOCOLS:
        raise Refused("invalid_record", f"{host} answered the handshake with an unusable pairing")
    features = [item for item in welcome.get("features") or [] if item in linkwire.FEATURES]
    guard = link.lock(queue)
    try:
        if welcome["node"] == node:
            raise Refused("invalid_record", f"{host} is this machine's own Reins")
        kept = _replace(queue, str(welcome["node"]))
        name = _free_alias(queue, alias or kept or linkwire.alias_from(host), None)
        record = _record(alias=name, node=str(welcome["node"]), pair=str(pair), key=str(key), protocol=int(protocol),
                         features=features, transport="ssh", role="carrier", state="active", wake=wake, host=host)
        link.save_peer(queue, record)
        return record
    finally:
        queue_store.release_owner_lock(guard, remove=False)


def serve_hello(queue: Path, request: object) -> dict[str, object]:
    """``_link-box --hello`` on the machine an SSH pair connects to: agree, host the mailbox, answer."""
    try:
        offer = linkwire.check_hello(request, "hello")
        protocol, features = linkwire.agree(offer)
    except Refused as exc:
        return {"error": str(exc), "code": exc.code}
    guard = link.lock(queue)
    try:
        node = link.node_id(queue)
        if offer["node"] == node:
            return {"error": "that is this machine's own Reins", "code": "invalid_record"}
        kept = _replace(queue, str(offer["node"]))
        alias = _free_alias(queue, kept or linkwire.alias_from(str(offer["name"])), None)
        pair, key = linkwire.new_pair_id(), linkwire.new_key()
        root = link.mailbox_root(queue, pair)
        queue_store.private_directory(root.parent)
        queue_store.private_directory(root)
        link.save_peer(queue, _record(alias=alias, node=str(offer["node"]), pair=pair, key=key, protocol=protocol,
                                      features=features, transport="ssh", role="host", state="active", wake="resume"))
    finally:
        queue_store.release_owner_lock(guard, remove=False)
    return {"schema": linkwire.LINK_SCHEMA, "kind": "welcome", "node": node, "name": linkwire.own_alias(),
            "pair": pair, "key": key, "protocol": protocol, "features": features}


# Shared directory -----------------------------------------------------------------

def invite_dir(queue: Path, directory: str, *, alias: Optional[str] = None, wake: str = "resume") -> tuple[dict[str, object], str]:
    """Offer a pairing in ``directory``; the code it returns is what the other machine joins with."""
    base = Path(directory).expanduser()
    if not base.is_dir():
        raise Refused("invalid_record", f"{directory} is not a directory on this machine")
    node = _node(queue)
    pair, key = linkwire.new_pair_id(), linkwire.new_key()
    root = base.absolute() / pair
    offer = linkwire.signed(key, linkwire.hello(node, linkwire.own_alias(), pair=pair))
    root.mkdir()
    mailbox.DirMailbox(root).put("hello", "offer.json", linkwire.canonical(offer))
    guard = link.lock(queue)
    try:
        name = _free_alias(queue, alias, None) if alias else _free_alias(queue, PENDING_PREFIX + pair[2:10], None)
        record = _record(alias=name, node=None, pair=pair, key=key, protocol=None, features=[], transport="dir",
                         role="dir", state="pending", wake=wake, path=str(root), chosen=alias is not None)
        link.save_peer(queue, record)
    finally:
        queue_store.release_owner_lock(guard, remove=False)
    return record, linkwire.pair_code(pair, key)


def _read_hello(root: Path, name: str, key: str, kind: str, pair: str) -> Optional[dict[str, object]]:
    data = mailbox.DirMailbox(root).get("hello", name)
    if data is None:
        return None
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise Refused("invalid_record", f"{root / 'hello' / name} is not JSON") from None
    if not isinstance(record, dict) or not linkwire.verify(key, record) or record.get("pair") != pair:
        raise Refused("identity_mismatch", "the pairing code does not match what is in that directory")
    return linkwire.check_hello(record, kind)


def join_dir(queue: Path, directory: str, code: str, *, alias: Optional[str] = None, wake: str = "resume") -> dict[str, object]:
    """Accept the pairing another machine offered in ``directory`` with ``code``."""
    pair, key = linkwire.parse_pair_code(code)
    root = Path(directory).expanduser().absolute() / pair
    offer = _read_hello(root, "offer.json", key, "hello", pair)
    if offer is None:
        raise Refused("unknown_peer", f"no pairing offer {pair} in {directory}: is it the same shared directory, and "
                                      "has the code been used already?")
    protocol, features = linkwire.agree(offer)
    node = _node(queue)
    if offer["node"] == node:
        raise Refused("invalid_record", "that offer came from this machine's own Reins")
    accept = linkwire.signed(key, {"schema": linkwire.LINK_SCHEMA, "kind": "accept", "node": node,
                                   "name": linkwire.own_alias(), "pair": pair, "protocol": protocol,
                                   "features": features})
    if not mailbox.DirMailbox(root).put("hello", "accept.json", linkwire.canonical(accept)):
        # Someone accepted already. If it was this machine (it stopped before saving), finish now.
        earlier = _read_hello(root, "accept.json", key, "accept", pair)
        if earlier is None or earlier.get("node") != node:
            raise Refused("invalid_record", "this pairing code has been used already")
        protocol, features = int(earlier["protocol"]), list(earlier.get("features") or [])
    guard = link.lock(queue)
    try:
        already = next((peer for peer in link.peers(queue) if peer.get("pair") == pair), None)
        if already is not None:
            return already  # this code was joined here before: the pairing and its outbox stay as they are
        kept = _replace(queue, str(offer["node"]))
        name = _free_alias(queue, alias or kept or linkwire.alias_from(str(offer["name"])), None)
        record = _record(alias=name, node=str(offer["node"]), pair=pair, key=key, protocol=protocol, features=features,
                         transport="dir", role="dir", state="active", wake=wake, path=str(root))
        link.save_peer(queue, record)
        return record
    finally:
        queue_store.release_owner_lock(guard, remove=False)


def finish_dir(queue: Path, alias: str) -> Optional[str]:
    """The offering side: once the other machine accepted, make the pairing usable. Its alias, or None yet."""
    peer = link.load_peer(queue, alias)
    if peer is None or peer.get("state") != "pending":
        return None
    root, key, pair = Path(str(peer["path"])), str(peer["key"]), str(peer["pair"])
    accept = _read_hello(root, "accept.json", key, "accept", pair)
    if accept is None:
        return None
    protocol = accept.get("protocol")
    if protocol not in linkwire.PROTOCOLS:
        raise Refused("unsupported_version", f"the other machine chose protocol {protocol!r}")
    guard = link.lock(queue)
    try:
        current = link.load_peer(queue, alias)
        if current is None or current.get("pair") != pair or current.get("state") != "pending":
            return None
        if link.peer_by_node(queue, str(accept["node"])) is not None:
            _replace(queue, str(accept["node"]))
        name = alias if current.get("chosen") else _free_alias(queue, linkwire.alias_from(str(accept["name"])), None)
        record = dict(current, alias=name, node=str(accept["node"]), protocol=int(protocol), state="active",
                      features=[item for item in accept.get("features") or [] if item in linkwire.FEATURES])
        record.pop("chosen", None)
        if name != alias:
            link.remove_peer(queue, alias)
        link.save_peer(queue, record)
    finally:
        queue_store.release_owner_lock(guard, remove=False)
    mailbox.DirMailbox(root).remove("hello", list(mailbox.HELLO_FILES))  # the code is used up
    return name


# Unpairing --------------------------------------------------------------------------

def open_mailbox(queue: Path, peer: Mapping[str, object]) -> mailbox.Mailbox:
    if peer.get("transport") == "dir":
        return mailbox.DirMailbox(Path(str(peer["path"])))
    if peer.get("role") == "host":
        return mailbox.DirMailbox(link.mailbox_root(queue, str(peer["pair"])))
    return mailbox.SshMailbox(str(peer["host"]), str(peer["pair"]))


def remove(queue: Path, alias: str) -> dict[str, object]:
    """Unpair: forget the key, then tell the other machine.

    A carrier is told with a bye in the mailbox. A machine that hosts the mailbox just deletes it: the carrier's
    next call then finds no such pairing and drops its own, so there is no bye to lose.
    """
    guard = link.lock(queue)
    try:
        peer = link.load_peer(queue, alias)
        if peer is None:
            raise Refused("unknown_peer", f"no machine is paired as {alias}")
        node = link.node_id(queue)
        # The record goes first: from now on this side sends nothing more, even if it crashes below.
        _drop(queue, peer, "the machine was unpaired")
    finally:
        queue_store.release_owner_lock(guard, remove=False)
    if peer.get("role") == "host":
        return {"alias": alias, "told": "next"}
    told = False
    if peer.get("state") == "active" and linkwire.is_node_id(peer.get("node")):
        try:
            open_mailbox(queue, peer).put(linkwire.box(str(peer["node"])), f"b-{int(time.time() * 1000)}.json",
                                          linkwire.encode(linkwire.wrap(peer, node, "bye", {"at": time.time()})))
            told = True
        except (mailbox.MailboxError, OSError, Refused):
            told = False
    return {"alias": alias, "told": told}


def forget(queue: Path, alias: str) -> None:
    """The other machine said bye: drop the pairing here too. Messages already received stay."""
    guard = link.lock(queue)
    try:
        peer = link.load_peer(queue, alias)
        if peer is not None:
            _drop(queue, peer, "the other machine unpaired")
    finally:
        queue_store.release_owner_lock(guard, remove=False)
