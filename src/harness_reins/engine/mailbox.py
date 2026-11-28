"""The mailbox two paired machines share, behind one small interface (docs/design/link-protocol.md, 9.1).

``DirMailbox`` is a directory: on a shared disk both machines use it directly, and
the machine an SSH pair connects to keeps one in ``~/.reins/link/mailboxes``.
``SshMailbox`` reaches that directory through ``ssh HOST reins _link-box``, running
a whole batch of operations in one round trip. Everything above this module
(handshake, signatures, dedup, receipts) is written once against ``Mailbox``.

Rules for a directory another machine also writes: a file has one writer, a
write is a temporary name and a rename in the same directory, nothing is
locked, nothing waits on inotify and no decision uses a file's mtime.
"""
from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from ..core import linkwire
from ..core.messages import Refused

WAIT_LIMIT = 50.0      # seconds one wait may block
POLL = 0.5             # seconds between looks at a directory while waiting
SSH_SLACK = 60.0       # seconds an SSH round trip may take beyond its waits
SSH_ENV = "REINS_SSH"  # the ssh command line, for tests and unusual setups; default "ssh"
HELLO_FILES = ("offer.json", "accept.json")
REMOTE_REINS = 'r="$HOME/.reins/bin/reins"; [ -x "$r" ] || r=reins; exec "$r" _link-box'


class MailboxError(RuntimeError):
    """The mailbox could not be reached or answered wrongly; the caller retries later."""


def _check(box: str, name: Optional[str] = None) -> None:
    if not linkwire.is_box(box):
        raise Refused("invalid_record", f"{box!r} is not a mailbox compartment")
    if name is not None and not (linkwire.is_file_name(name) or (box == "hello" and name in HELLO_FILES)):
        raise Refused("invalid_record", f"{name!r} is not a mailbox file name")


class Mailbox:
    """put, list, get, remove and wait; ``batch`` runs a list of them and returns one result each."""

    def batch(self, ops: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
        return [self._one(op) for op in ops]

    def _one(self, op: Mapping[str, object]) -> dict[str, object]:
        try:
            kind = op.get("op")
            box = str(op.get("box"))
            if kind == "put":
                return {"ok": True, "created": self.put(box, str(op["name"]), _bytes(op["data"]))}
            if kind == "list":
                return {"ok": True, "names": self.list(box)}
            if kind == "get":
                data = self.get(box, str(op["name"]))
                return {"ok": True, "data": None if data is None else _text(data)}
            if kind == "remove":
                self.remove(box, [str(name) for name in op.get("names") or []])
                return {"ok": True}
            if kind == "wait":
                return {"ok": True, "names": self.wait(box, float(op.get("timeout") or 0))}
            return {"ok": False, "error": f"unknown operation {kind!r}"}
        except Refused as exc:
            return {"ok": False, "error": str(exc), "code": exc.code}
        except MailboxError as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    # Single operations raise; the batch above turns them into results.
    def put(self, box: str, name: str, data: bytes) -> bool:
        return _result(self.batch([{"op": "put", "box": box, "name": name, "data": _text(data)}])[0])["created"]

    def list(self, box: str) -> list[str]:
        return _result(self.batch([{"op": "list", "box": box}])[0])["names"]

    def get(self, box: str, name: str) -> Optional[bytes]:
        data = _result(self.batch([{"op": "get", "box": box, "name": name}])[0])["data"]
        return None if data is None else _bytes(data)

    def remove(self, box: str, names: Iterable[str]) -> None:
        _result(self.batch([{"op": "remove", "box": box, "names": list(names)}])[0])

    def wait(self, box: str, timeout: float) -> list[str]:
        return _result(self.batch([{"op": "wait", "box": box, "timeout": timeout}])[0])["names"]


def _text(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _bytes(text: object) -> bytes:
    if not isinstance(text, str):
        raise ValueError("mailbox data must be base64 text")
    return base64.b64decode(text.encode("ascii"), validate=True)


def _result(result: Mapping[str, object]) -> dict[str, object]:
    if not result.get("ok"):
        if result.get("code"):
            raise Refused(str(result["code"]), str(result.get("error")))
        raise MailboxError(str(result.get("error") or "the mailbox refused the operation"))
    return dict(result)


class DirMailbox(Mailbox):
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _box(self, box: str) -> Path:
        _check(box)
        if not self.root.is_dir():
            # Never created here: on an unmounted shared directory that would write to the disk underneath.
            raise MailboxError(f"the shared directory {self.root} is not there")
        return self.root / box

    def put(self, box: str, name: str, data: bytes) -> bool:
        _check(box, name)
        if len(data) > linkwire.FILE_LIMIT:
            raise Refused("invalid_record", "the file is larger than the mailbox limit")
        directory = self._box(box)
        directory.mkdir(exist_ok=True)
        path = directory / name
        if path.exists():
            return False  # the writer's own earlier attempt: a name never carries two contents
        temporary = directory / f".tmp-{uuid.uuid4().hex}-{name}"
        try:
            with open(temporary, "xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise
        return True

    def list(self, box: str) -> list[str]:
        try:
            # Only names the protocol allows: a stray file must not wake every wait or block a remove.
            return sorted(path.name for path in self._box(box).iterdir()
                          if linkwire.is_file_name(path.name) or (box == "hello" and path.name in HELLO_FILES))
        except FileNotFoundError:
            return []

    def get(self, box: str, name: str) -> Optional[bytes]:
        _check(box, name)
        path = self._box(box) / name
        try:
            if path.stat().st_size > linkwire.FILE_LIMIT:
                raise Refused("invalid_record", f"{name} is larger than the mailbox limit")
            return path.read_bytes()
        except FileNotFoundError:
            return None

    def remove(self, box: str, names: Iterable[str]) -> None:
        for name in names:
            _check(box, name)
            try:
                (self._box(box) / name).unlink()
            except FileNotFoundError:
                pass

    def wait(self, box: str, timeout: float) -> list[str]:
        deadline = time.monotonic() + min(max(timeout, 0.0), WAIT_LIMIT)
        while True:
            names = self.list(box)
            if names or time.monotonic() >= deadline:
                return names
            time.sleep(POLL)


def ssh_command() -> list[str]:
    return shlex.split(os.environ.get(SSH_ENV, "").strip() or "ssh")


def ssh_call(host: str, arguments: Sequence[str], request: Mapping[str, object], timeout: float) -> object:
    """Run ``reins _link-box ARGS`` on ``host`` with ``request`` on stdin; its JSON answer."""
    remote = REMOTE_REINS + "".join(" " + shlex.quote(word) for word in arguments)
    command = [*ssh_command(), "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", host, remote]
    try:
        done = subprocess.run(command, input=json.dumps(request).encode("utf-8"), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MailboxError(f"ssh {host}: {exc}") from exc
    if done.returncode != 0:
        detail = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise MailboxError(f"ssh {host} exited {done.returncode}: {detail[-1] if detail else 'no output'}")
    try:
        return json.loads(done.stdout.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise MailboxError(f"ssh {host} answered something that is not JSON: {exc}") from exc


class SshMailbox(Mailbox):
    def __init__(self, host: str, pair: str) -> None:
        self.host = host
        self.pair = pair

    def batch(self, ops: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
        waits = sum(min(float(op.get("timeout") or 0), WAIT_LIMIT) for op in ops if op.get("op") == "wait")
        answer = ssh_call(self.host, [], {"pair": self.pair, "ops": list(ops)}, waits + SSH_SLACK)
        if isinstance(answer, dict) and answer.get("code") and not isinstance(answer.get("results"), list):
            raise Refused(str(answer["code"]), f"{self.host}: {answer.get('error')}")
        if not isinstance(answer, dict) or not isinstance(answer.get("results"), list) \
                or len(answer["results"]) != len(ops):
            error = answer.get("error") if isinstance(answer, dict) else None
            raise MailboxError(f"ssh {self.host}: {error or 'the answer does not match the request'}")
        return [result if isinstance(result, dict) else {"ok": False, "error": "bad result"}
                for result in answer["results"]]


def serve(root: Path, request: object) -> dict[str, object]:
    """``_link-box`` on the machine that hosts a mailbox: run the batch a carrier sent."""
    if not isinstance(request, dict) or not isinstance(request.get("ops"), list):
        return {"error": "the request is not a batch"}
    if len(request["ops"]) > 1000:
        return {"error": "too many operations in one batch"}
    return {"results": DirMailbox(root).batch(request["ops"])}
