"""Codex app-server JSON-RPC client used to reach a loaded Desktop thread."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import struct
import sys
import time
from pathlib import Path
from ...owners import macos as macos_platform
from ...store import layout
from ...system import shell


DESKTOP_APP_SERVER_ENV = "REINS_DESKTOP_APP_SERVER"
APP_SERVER_SOCKET_ENV = "REINS_APP_SERVER_SOCKET"
ALLOW_APP_SERVER_SOCKET_OVERRIDE_ENV = "REINS_ALLOW_APP_SERVER_SOCKET_OVERRIDE"


class AppServerProtocolError(RuntimeError):
    """The local Codex App Server did not accept a desktop callback."""


class AppServerTimeout(AppServerProtocolError):
    """The local Codex App Server did not respond before the deadline."""


class AppServerRpcError(AppServerProtocolError):
    """The local Codex App Server explicitly rejected a JSON-RPC request."""


class CallbackTransportBlocked(AppServerProtocolError):
    """Definite pre-submission failure requiring route repair, not blind retry."""


class CallbackTargetBusy(RuntimeError):
    """The correct Core still has an active turn; wait without consuming a retry."""


def desktop_route_repair(request: dict[str, object]) -> str:
    """The command that moves a saved `desktop` record onto the native queue."""
    return f"reins retry --id {request.get('id') or '<callback-id>'} --callback-mode codex-queue"


def desktop_app_server_socket(request: dict[str, object]) -> Path | None:
    target = request.get("target")
    if request.get("callback_mode") == "desktop" and sys.platform in ("darwin", "win32"):
        # Only the retired launcher's shared Core could serve these records here.
        raise CallbackTransportBlocked(f"the Desktop launcher route is retired; run {desktop_route_repair(request)}")
    if DESKTOP_APP_SERVER_ENV in os.environ and not shell.truthy_env(DESKTOP_APP_SERVER_ENV):
        if request.get("callback_mode") == "desktop":
            raise CallbackTransportBlocked("Desktop App Server delivery is explicitly disabled")
        return None
    if (
        request.get("callback_mode") == "manual"
        or (not shell.truthy_env(DESKTOP_APP_SERVER_ENV) and request.get("callback_mode") not in ("cli", "desktop"))
        or not isinstance(target, dict)
        or target.get("kind") != "session"
        or not isinstance(target.get("session"), str)
        or not target["session"]
    ):
        return None
    if os.name == "nt":
        return None
    configured = os.environ.get(APP_SERVER_SOCKET_ENV)
    if configured and shell.truthy_env(ALLOW_APP_SERVER_SOCKET_OVERRIDE_ENV):
        return Path(configured).expanduser()
    return layout.codex_home() / "app-server-control" / "app-server-control.sock"


def desktop_sandbox_policy(request: dict[str, object]) -> dict[str, object] | None:
    if request.get("sandbox_mode") != "workspace-write":
        return None
    queue_root = request.get("queue_dir")
    if not isinstance(queue_root, str) or not queue_root:
        return None
    root = Path(queue_root).expanduser()
    if not root.is_absolute():
        return None
    return {"type": "workspaceWrite", "writableRoots": [str(root)]}


class AppServerConnection:
    def __init__(self, path: Path, timeout: float) -> None:
        self.path = path
        self.timeout = timeout
        self.socket: socket.socket | None = None
        self.buffer = b""
        self.fragment: bytearray | None = None
        self.request_id = 0
        self.notifications: list[dict[str, object]] = []

    def connect(self) -> None:
        deadline = time.monotonic() + self.timeout
        if not self.path.is_socket():
            raise AppServerProtocolError(f"control socket unavailable at {self.path}")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(max(0.1, deadline - time.monotonic()))
            connection.connect(str(self.path))
            self._verify_peer_uid(connection)
            key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
            request = (
                "GET / HTTP/1.1\r\n"
                "Host: localhost\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            )
            connection.sendall(request.encode("ascii"))
            self.socket = connection
            header = self._read_headers(deadline)
            lines = header.decode("iso-8859-1").split("\r\n")
            expected_accept = base64.b64encode(
                hashlib.sha1(f"{key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11".encode("ascii")).digest()
            ).decode("ascii")
            headers = {
                name.strip().lower(): value.strip()
                for line in lines[1:]
                if ":" in line
                for name, value in [line.split(":", 1)]
            }
            if (
                not lines
                or lines[0] != "HTTP/1.1 101 Switching Protocols"
                or headers.get("sec-websocket-accept") != expected_accept
                or not self._header_has_token(headers, "connection", "upgrade")
                or not self._header_has_token(headers, "upgrade", "websocket")
            ):
                raise AppServerProtocolError("control socket rejected WebSocket upgrade")
        except BaseException:
            connection.close()
            self.socket = None
            raise

    def close(self) -> None:
        if self.socket is not None:
            self.socket.close()
            self.socket = None
        self.buffer = b""
        self.fragment = None

    def request(self, method: str, params: dict[str, object]) -> object:
        self.request_id += 1
        request_id = self.request_id
        self._send_frame(0x1, json.dumps({"id": request_id, "method": method, "params": params}))
        deadline = time.monotonic() + self.timeout
        while True:
            response = self._receive_json(deadline)
            if "method" in response:
                self.notifications.append(response)
                continue
            if response.get("id") != request_id:
                raise AppServerProtocolError("control socket returned an unexpected response id")
            if "error" in response:
                raise AppServerRpcError(f"App Server {method} error: {response['error']}")
            if "result" not in response:
                raise AppServerProtocolError(f"App Server {method} returned no result")
            return response["result"]

    def notify(self, method: str, params: dict[str, object]) -> None:
        self._send_frame(0x1, json.dumps({"method": method, "params": params}))

    def wait_for_turn_completion(self, thread_id: str, turn_id: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if self.notifications:
                message = self.notifications.pop(0)
            else:
                try:
                    message = self._receive_json(deadline)
                except AppServerTimeout:
                    return False
            params = message.get("params")
            if (
                message.get("method") == "turn/completed"
                and isinstance(params, dict)
                and params.get("threadId") == thread_id
                and isinstance(params.get("turn"), dict)
                and params["turn"].get("id") == turn_id
            ):
                return True

    @staticmethod
    def _header_has_token(headers: dict[str, str], name: str, expected: str) -> bool:
        return expected in {token.strip().lower() for token in headers.get(name, "").split(",")}

    @staticmethod
    def _verify_peer_uid(connection: socket.socket) -> None:
        try:
            if sys.platform == "darwin":
                peer_uid = macos_platform.peer_uid(connection.fileno())
            elif hasattr(socket, "SO_PEERCRED"):
                credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                _peer_pid, peer_uid, _peer_gid = struct.unpack("3i", credentials)
            else:
                raise AppServerProtocolError("control-socket peer verification is unsupported on this platform")
        except OSError as exc:
            raise AppServerProtocolError(f"could not verify control-socket peer: {exc}") from exc
        if peer_uid != os.geteuid():
            raise AppServerProtocolError("control socket peer does not belong to this user")

    def _read_headers(self, deadline: float) -> bytes:
        while b"\r\n\r\n" not in self.buffer:
            if len(self.buffer) >= 65_536:
                raise AppServerProtocolError("control socket response headers exceed 64 KiB")
            self.buffer += self._receive_bytes(4096, deadline)
        header, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        return header

    def _receive_json(self, deadline: float) -> dict[str, object]:
        opcode, payload = self._receive_message(deadline)
        if opcode == 0x8:
            raise AppServerProtocolError("control socket closed during request")
        if opcode != 0x1:
            raise AppServerProtocolError("control socket sent a non-text message")
        try:
            message = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AppServerProtocolError(f"invalid control-socket response: {exc}") from exc
        if not isinstance(message, dict):
            raise AppServerProtocolError("control socket response was not a JSON object")
        if "method" in message and not isinstance(message["method"], str):
            raise AppServerProtocolError("control socket notification method was not a string")
        if "id" not in message and "method" not in message:
            raise AppServerProtocolError("control socket response had neither id nor method")
        return message

    def _receive_message(self, deadline: float) -> tuple[int, bytes]:
        # Both frame and message state must survive short completion polls.
        while True:
            final, opcode, payload = self._receive_frame(deadline)
            if opcode in (0x9, 0xA):
                if opcode == 0x9:
                    self._send_frame(0xA, payload)
                continue
            if opcode == 0x8:
                self.fragment = None
                return opcode, payload
            if self.fragment is None:
                if opcode == 0x0:
                    raise AppServerProtocolError("control socket sent an unexpected continuation")
                if final:
                    return opcode, payload
                if opcode != 0x1:
                    raise AppServerProtocolError("control socket started an unsupported fragmented message")
                self.fragment = bytearray(payload)
                continue
            if opcode != 0x0:
                raise AppServerProtocolError("control socket interrupted a fragmented message")
            if len(self.fragment) + len(payload) > 1_048_576:
                raise AppServerProtocolError("control socket message exceeds 1 MiB")
            self.fragment.extend(payload)
            if final:
                message = bytes(self.fragment)
                self.fragment = None
                return 0x1, message

    def _receive_frame(self, deadline: float) -> tuple[bool, int, bytes]:
        header = self._peek_exact(2, deadline)
        final = bool(header[0] & 0x80)
        if header[0] & 0x70:
            raise AppServerProtocolError("control socket frame used unsupported RSV bits")
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F
        offset = 2
        if length == 126:
            length = struct.unpack("!H", self._peek_exact(4, deadline)[2:4])[0]
            offset = 4
        elif length == 127:
            length = struct.unpack("!Q", self._peek_exact(10, deadline)[2:10])[0]
            offset = 10
        if opcode >= 0x8 and (not final or length > 125):
            raise AppServerProtocolError("control socket sent an invalid control frame")
        if length > 1_048_576:
            raise AppServerProtocolError("control socket frame exceeds 1 MiB")
        # RFC 6455 servers normally send unmasked frames. Some Codex Desktop
        # App Server control-socket versions have emitted masked frames,
        # however. The peer is a same-UID Unix socket verified during connect,
        # so decode those frames for local compatibility instead of stranding
        # a successfully started callback turn.
        mask_size = 4 if masked else 0
        frame = self._peek_exact(offset + mask_size + length, deadline)
        mask = frame[offset:offset + mask_size] if masked else None
        payload = frame[offset + mask_size:]
        self.buffer = self.buffer[len(frame):]
        if mask is not None:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return final, opcode, payload

    def _send_frame(self, opcode: int, text: str | bytes) -> None:
        if self.socket is None:
            raise AppServerProtocolError("control socket is not connected")
        payload = text.encode("utf-8") if isinstance(text, str) else text
        if len(payload) > 1_048_576:
            raise AppServerProtocolError("control socket request exceeds 1 MiB")
        if len(payload) < 126:
            header = bytes([0x80 | opcode, 0x80 | len(payload)])
        elif len(payload) <= 0xFFFF:
            header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack("!H", len(payload))
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack("!Q", len(payload))
        mask = secrets.token_bytes(4)
        encoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.socket.sendall(header + mask + encoded)

    def _read_exact(self, size: int, deadline: float) -> bytes:
        value = self._peek_exact(size, deadline)
        self.buffer = self.buffer[size:]
        return value

    def _peek_exact(self, size: int, deadline: float) -> bytes:
        while len(self.buffer) < size:
            self.buffer += self._receive_bytes(size - len(self.buffer), deadline)
        return self.buffer[:size]

    def _receive_bytes(self, size: int, deadline: float) -> bytes:
        if self.socket is None:
            raise AppServerProtocolError("control socket is not connected")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppServerTimeout("control socket request timed out")
        self.socket.settimeout(remaining)
        try:
            value = self.socket.recv(size)
        except socket.timeout as exc:
            raise AppServerTimeout("control socket request timed out") from exc
        except OSError as exc:
            raise AppServerProtocolError(f"control socket read failed: {exc}") from exc
        if not value:
            raise AppServerProtocolError("control socket closed unexpectedly")
        return value


class DesktopAppServerDelivery:
    def __init__(self, connection: AppServerConnection | None, thread_id: str, turn_id: str | None) -> None:
        self.connection = connection
        self.thread_id = thread_id
        self.turn_id = turn_id

    def wait_for_completion(self, timeout: float) -> bool:
        if self.connection is None or self.turn_id is None:
            time.sleep(timeout)
            return False
        try:
            return self.connection.wait_for_turn_completion(self.thread_id, self.turn_id, timeout)
        except AppServerProtocolError as exc:
            print(
                f"reins: desktop App Server completion stream unavailable: {exc}; "
                "waiting for ACK or timeout",
                file=sys.stderr,
            )
            self.close()
            return False

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None


def desktop_delivery_context(payload: dict[str, object]) -> tuple[Path, dict[str, object]] | None:
    """Return the durable queue identity supplied by a real delivery worker."""
    request = payload.get("request")
    root = payload.get("queue_dir")
    if (isinstance(request, dict) and isinstance(request.get("id"), str)
            and isinstance(root, str) and root):
        return Path(root), request
    return None
