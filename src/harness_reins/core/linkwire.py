"""Paired mailboxes between machines: addresses, the signed files in a mailbox and the receipt order.

Two paired nodes share one mailbox (docs/design/link-protocol.md, section 9). Every
file in it is a ``reins.link/1`` record signed with the pair's key, so a file that
anyone else put there fails its check and is never imported. Nothing here touches
the disk or the network.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import socket
from typing import Mapping, Optional

from .messages import Refused, validate_name

LINK_SCHEMA = "reins.link/1"
PEER_SCHEMA = "reins.peer/1"
PROTOCOLS = (1,)
FEATURES = ("message", "receipt", "names")
FILE_LIMIT = 1024 * 1024  # bytes, for one file in a mailbox
KINDS = ("message", "receipt", "names", "bye")
HELLO_KINDS = ("hello", "welcome", "accept")
WAKE_MODES = ("never", "resume")
CODE_PREFIX = "reins-pair"

_PAIR_ID = re.compile(r"p-[0-9a-f]{16}")
_NODE_ID = re.compile(r"n-[0-9a-f]{16}")
_KEY = re.compile(r"[0-9a-f]{64}")
_BOX = re.compile(r"to-n-[0-9a-f]{16}")
# m-<message id>, r-<message id>-<state>-<ms>, n-<ms>, b-<ms>; the receiver checks it again after reading.
_FILE_NAME = re.compile(r"(?:m-[0-9a-f]{16}|r-m-[0-9a-f]{16}-[a-z]{1,16}-[0-9]{1,16}|[nb]-[0-9]{1,16})\.json")

# A receipt may only move a sent message forward. Same-rank states replace each other by the receiver's time.
RANK = {"queued": 0, "forwarded": 1, "published": 2, "failed": 2, "unknown": 2,
        "acked": 3, "rejected": 3, "expired": 3, "canceled": 3}
TERMINAL = ("acked", "rejected", "expired", "canceled")
RECEIPT_STATES = tuple(state for state in RANK if state != "queued")


def new_pair_id() -> str:
    return f"p-{secrets.token_hex(8)}"


def new_key() -> str:
    return secrets.token_hex(32)


def is_pair_id(value: object) -> bool:
    return isinstance(value, str) and _PAIR_ID.fullmatch(value) is not None


def is_node_id(value: object) -> bool:
    return isinstance(value, str) and _NODE_ID.fullmatch(value) is not None


def is_key(value: object) -> bool:
    return isinstance(value, str) and _KEY.fullmatch(value) is not None


def box(node: str) -> str:
    """The mailbox compartment that holds files for ``node``."""
    if not is_node_id(node):
        raise Refused("invalid_record", f"{node!r} is not a node id")
    return f"to-{node}"


def is_box(value: object) -> bool:
    return isinstance(value, str) and (value == "hello" or _BOX.fullmatch(value) is not None)


def is_file_name(value: object) -> bool:
    return isinstance(value, str) and _FILE_NAME.fullmatch(value) is not None


def validate_alias(alias: object) -> str:
    try:
        return validate_name(alias)
    except Refused:
        raise Refused("invalid_record", f"{alias!r} is not a usable machine name: use 1-32 lowercase letters, "
                                        "digits, '-' or '_', starting with a letter or digit") from None


def alias_from(text: str) -> str:
    """A machine name from a host name or an SSH destination: ``user@gpu1.lab`` -> ``gpu1``."""
    host = text.rsplit("@", 1)[-1].strip().lower()
    if not re.fullmatch(r"[0-9.]+", host):  # an IP address keeps all its parts
        host = host.split(".", 1)[0]
    alias = re.sub(r"[^a-z0-9_-]+", "-", host).strip("-_")[:32].strip("-_")
    return alias or "peer"


def own_alias() -> str:
    return alias_from(socket.gethostname() or "peer")


def parse_address(address: str) -> tuple[str, Optional[str]]:
    """``name`` or ``name@machine``; the machine is a paired alias, resolved by the caller, never guessed."""
    name, separator, alias = address.partition("@")
    if not separator:
        return validate_name(address), None
    return validate_name(name), validate_alias(alias)


def canonical(record: Mapping[str, object]) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _mac(key: str, record: Mapping[str, object]) -> str:
    return hmac.new(bytes.fromhex(key), canonical({k: v for k, v in record.items() if k != "mac"}),
                    hashlib.sha256).hexdigest()


def signed(key: str, record: Mapping[str, object]) -> dict[str, object]:
    out = dict(record)
    out["mac"] = _mac(key, out)
    return out


def verify(key: str, record: Mapping[str, object]) -> bool:
    mac = record.get("mac")
    return isinstance(mac, str) and hmac.compare_digest(mac, _mac(key, record))


def encode(record: Mapping[str, object]) -> bytes:
    data = canonical(record)
    if len(data) > FILE_LIMIT:
        raise Refused("invalid_record", f"a mailbox file is at most {FILE_LIMIT // 1024} KiB")
    return data


def wrap(peer: Mapping[str, object], own_node: str, kind: str, body: Mapping[str, object]) -> dict[str, object]:
    """A signed mailbox file for ``peer``."""
    if kind not in KINDS:
        raise Refused("invalid_record", f"unknown mailbox file kind {kind!r}")
    return signed(str(peer["key"]), {"schema": LINK_SCHEMA, "v": int(peer["protocol"]), "kind": kind,
                                      "pair": peer["pair"], "from": own_node, "to": peer["node"], "body": dict(body)})


def unwrap(peer: Mapping[str, object], own_node: str, data: bytes) -> dict[str, object]:
    """The checked record of one mailbox file from ``peer``; Refused names why it is not usable."""
    if len(data) > FILE_LIMIT:
        raise Refused("invalid_record", "the file is larger than the mailbox limit")
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise Refused("invalid_record", "the file is not usable JSON") from None
    if not isinstance(record, dict) or record.get("schema") != LINK_SCHEMA:
        raise Refused("invalid_record", "the file is not a reins.link/1 record")
    if not verify(str(peer["key"]), record):
        raise Refused("identity_mismatch", "the file is not signed with this pair's key")
    if record.get("pair") != peer["pair"] or record.get("from") != peer["node"] or record.get("to") != own_node:
        raise Refused("identity_mismatch", "the file names another pair or node")
    if record.get("v") != peer["protocol"]:
        raise Refused("unsupported_version", f"the file uses protocol {record.get('v')!r}, the pair agreed on "
                                             f"{peer['protocol']}")
    if record.get("kind") not in KINDS or not isinstance(record.get("body"), dict):
        raise Refused("invalid_record", "unknown mailbox file kind")
    return record


def hello(node: str, name: str, **extra: object) -> dict[str, object]:
    return {"schema": LINK_SCHEMA, "kind": "hello", "protocols": list(PROTOCOLS), "features": list(FEATURES),
            "node": node, "name": name, **extra}


def agree(offer: Mapping[str, object]) -> tuple[int, list[str]]:
    """The highest protocol both sides speak and the features both have."""
    theirs = offer.get("protocols")
    common = [version for version in PROTOCOLS if isinstance(theirs, list) and version in theirs]
    if not common:
        raise Refused("unsupported_version", f"the other machine speaks protocols {theirs!r}, this one {list(PROTOCOLS)}")
    features = offer.get("features") if isinstance(offer.get("features"), list) else []
    return max(common), [feature for feature in FEATURES if feature in features]


def check_hello(record: object, kind: str) -> dict[str, object]:
    if not isinstance(record, dict) or record.get("schema") != LINK_SCHEMA or record.get("kind") != kind:
        raise Refused("invalid_record", f"expected a {kind} from the other machine")
    if not is_node_id(record.get("node")):
        raise Refused("invalid_record", "the other machine sent no usable node id")
    if not isinstance(record.get("name"), str):
        raise Refused("invalid_record", "the other machine sent no name")
    return record


def pair_code(pair: str, key: str) -> str:
    return f"{CODE_PREFIX}:{pair}:{key}"


def parse_pair_code(code: str) -> tuple[str, str]:
    prefix, _, rest = code.strip().partition(":")
    pair, _, key = rest.partition(":")
    if prefix != CODE_PREFIX or not is_pair_id(pair) or not is_key(key):
        raise Refused("invalid_record", "that is not a pairing code; copy the whole line `reins link connect --dir` printed")
    return pair, key


def local_root(node: str, message_id: str) -> str:
    """The id a message from another node gets here, so two nodes can never claim the same record."""
    return "m-" + hashlib.sha256(f"{node}:{message_id}".encode("utf-8")).hexdigest()[:16]


def digest(message: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical(message)).hexdigest()


def receipt_name(message_id: str, state: str, at_ms: int) -> str:
    return f"r-{message_id}-{state}-{at_ms}.json"


def moves_forward(current: Mapping[str, object], state: str, at: float) -> bool:
    """Whether a receipt saying ``state`` at the receiver's time ``at`` should replace what is recorded."""
    old = str(current.get("state") or "queued")
    if state not in RANK or old in TERMINAL:
        return False
    if RANK[state] != RANK.get(old, 0):
        return RANK[state] > RANK.get(old, 0)
    previous = current.get("at")
    return state != old and (not isinstance(previous, (int, float)) or at > previous)
