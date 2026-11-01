"""The Desktop launcher is gone: nothing makes one, and saved `desktop` records have a way out."""
import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import unittest
from unittest import mock

from harness_reins import cli
from harness_reins.commands import callbacks
from harness_reins.core import capabilities
from harness_reins.engine import delivery, dispatch, native_queue, routes
from harness_reins.harness.codex import app_server
from harness_reins.install import services
from harness_reins.store import layout, records
import reins_records

THREAD = "01a15842-e8a7-7630-9fee-869784c02260"
MESSAGE = "01a19796-8012-7ea3-bd98-60b2b6acbfbc"


class RetiredDesktopRouteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="reins-retired-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.root / "profile"), "REINS_HOME": str(self.root / "reins-home"),
            "REINS_TARGET_LOCK_DIR": str(self.root / "target-locks"), "PATH": os.environ.get("PATH", ""),
            **{key: os.environ[key] for key in ("SystemRoot", "USERPROFILE") if key in os.environ},
        }, clear=True))
        self.errors = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stderr(self.errors))
        self.queue = self.root / "queue"
        records.ensure_daemon_dirs(self.queue)

    def request(self):
        return reins_records.callback_record(
            "saved", str(self.root), target={"kind": "session", "session": THREAD}, prompt="inspect the result",
            agent="codex", target_source="fixture", callback_mode="desktop",
            callback_bridge_file=str(self.root / "profile/harness-reins/desktop/bridge.json"),
            requires=[capabilities.CODEX_DESKTOP], sandbox_mode="workspace-write", queue_dir=str(self.queue))

    def retry(self, mode=None):
        return callbacks.retry_callback(argparse.Namespace(queue_dir=str(self.queue), id="saved", callback_mode=mode))

    def test_new_work_cannot_select_desktop(self):
        for platform in ("darwin", "win32", "linux"):
            with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform), \
                    self.assertRaisesRegex(ValueError, "codex-queue"):
                routes.selection(argparse.Namespace(agent="codex", callback_mode="desktop"))

    def test_retry_cannot_move_a_callback_back_to_desktop(self):
        request = reins_records.callback_record("saved", str(self.root), target={"kind": "session", "session": "original"},
                                                prompt="inspect the result", agent="codex", target_source="fixture",
                                                callback_mode="cli")
        records.write_request(self.queue / "failed/saved.json", request)
        self.assertEqual(self.retry("desktop"), 2)
        self.assertIn("codex-queue", self.errors.getvalue())
        self.assertTrue((self.queue / "failed/saved.json").is_file())

    def test_help_offers_only_live_modes(self):
        with mock.patch.object(sys, "argv", ["reins", "run", "--help"]), contextlib.redirect_stdout(io.StringIO()) as out, \
                self.assertRaises(SystemExit):
            cli.main()
        self.assertNotIn("desktop", out.getvalue().split("Callback route")[0].split("--callback-mode")[1])

    def test_saved_record_is_blocked_with_the_exact_command_on_mac_and_windows(self):
        for platform in ("darwin", "win32"):
            with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform):
                with self.assertRaisesRegex(app_server.CallbackTransportBlocked,
                                            "reins retry --id saved --callback-mode codex-queue"):
                    delivery.start_desktop_app_server_turn({"request": self.request(), "timeout": 2})
                report = routes.inspect_route(self.request())
                self.assertEqual(report["status"], "blocked")
                self.assertIn("reins retry --id saved --callback-mode codex-queue", report["action"])

    def test_daemon_retains_the_blocked_record_and_names_the_repair(self):
        records.write_request(self.queue / "pending/saved.json", self.request())
        with mock.patch.object(delivery, "run_resume_until_exit_or_ack",
                               return_value=(mock.Mock(returncode=126), False, False)):
            self.assertTrue(delivery.process_one(self.queue, argparse.Namespace(retries=10)))
        saved = dispatch.load_request(self.queue / "failed/saved.json")
        self.assertEqual(saved["delivery_state"], "blocked")
        self.assertIn("reins retry --id saved --callback-mode codex-queue", saved["last_error"])

    def test_retry_without_a_new_mode_repeats_the_command(self):
        records.write_request(self.queue / "failed/saved.json", self.request())
        with mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(self.retry(), 2)
        self.assertIn("reins retry --id saved --callback-mode codex-queue", self.errors.getvalue())
        self.assertTrue((self.queue / "failed/saved.json").is_file())

    @unittest.skipUnless(os.name == "posix", "the fixture Codex is a shell script")
    def test_retry_moves_the_record_to_the_queue_and_publishes_once(self):
        calls = self.root / "codex-calls"
        codex = self.root / "codex"
        codex.write_text(
            '#!/bin/sh\nif [ "$2" = "--help" ]; then echo "--thread --message"; exit 0; fi\n'
            f'echo "$@" >> "{calls}"\n'
            f'echo "Queued message {MESSAGE} for thread $3."\n',
            encoding="utf-8")
        codex.chmod(0o700)
        records.write_request(self.queue / "failed/saved.json", self.request())
        with mock.patch.dict(os.environ, {"REINS_CODEX_BIN": str(codex)}), mock.patch.object(sys, "platform", "darwin"):
            self.assertEqual(self.retry("codex-queue"), 0, self.errors.getvalue())
        saved = dispatch.load_request(self.queue / "pending/saved.json")
        self.assertEqual(saved["callback_mode"], "codex-queue")
        self.assertNotIn("callback_bridge_file", saved)
        self.assertEqual(saved["requires"], [capabilities.CODEX_QUEUE])
        self.assertEqual(saved["target"], {"kind": "session", "session": THREAD})
        self.assertFalse((self.queue / "failed/saved.json").exists())

        result = native_queue.publish({"request": saved, "queue_dir": str(self.queue), "timeout": 10, "prompt": saved["prompt"]})
        self.assertEqual(result["returncode"], native_queue.PUBLISHED_RETURN_CODE)
        self.assertEqual(calls.read_text(encoding="utf-8").splitlines(),
                         [f"queue --thread {THREAD} --message inspect the result"])

    @unittest.skipUnless(os.name == "posix", "Windows has no local control socket")
    def test_linux_keeps_delivering_saved_records_through_the_control_socket(self):
        with mock.patch.object(sys, "platform", "linux"), \
                mock.patch.object(layout, "codex_home", return_value=Path("/fixture/profile")):
            self.assertEqual(app_server.desktop_app_server_socket(self.request()),
                             Path("/fixture/profile/app-server-control/app-server-control.sock"))

    def test_setup_creates_no_launcher_and_reports_a_stale_one(self):
        state = self.root / "profile/harness-reins"
        args = argparse.Namespace(
            name="harness-reins", queue_dir=None, interval=2.0, retries=3, retry_delay=30.0, retry_backoff=2.0,
            restart_sec=5.0, exec_start="/fixture/reins", codex_bin=None, path=None, force=False, enable=False,
            now=False, skill_path=None, callback_only=True)
        for platform, service, installer in (("darwin", "launchd", "install_launchd"),
                                             ("win32", "windows-task", "install_windows_task")):
            with self.subTest(platform=platform), mock.patch.object(sys, "platform", platform), \
                    mock.patch.object(services, "select_daemon_service", return_value=service), \
                    mock.patch("harness_reins.install.canonical.ensure", return_value={"version": "x", "build": "y"}), \
                    mock.patch.object(services.skill_install, "install_skill", return_value=0), \
                    mock.patch.object(services.reminder_policy, "ensure_callback_hook_file"), \
                    mock.patch.object(services, "report_claude_agent_readiness"), \
                    mock.patch.object(services, installer, return_value=0), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(services.setup(args), 0)
                self.assertEqual(list(state.glob("*")) if state.exists() else [], [])
        self.assertNotIn("Desktop launcher", self.errors.getvalue())
        state.mkdir(parents=True)
        stale = state / "Start Reins Desktop.command"
        stale.write_text("#!/bin/sh\n", encoding="utf-8")
        services.report_retired_desktop_launcher()
        self.assertIn(str(stale), self.errors.getvalue())
        self.assertTrue(stale.is_file())

    def test_a_stale_launcher_reaches_a_clear_message(self):
        with mock.patch.object(sys, "argv", ["reins", "desktop", "launch", "--check-only"]):
            self.assertEqual(cli.main(), 2)
        self.assertIn("open it normally", self.errors.getvalue())
        self.assertIn("--callback-mode codex-queue", self.errors.getvalue())

    def test_setup_still_accepts_the_old_opt_out_flag(self):
        with mock.patch.object(sys, "argv", ["reins", "setup", "--no-desktop-launcher"]), \
                mock.patch.object(services, "setup", return_value=0) as setup:
            self.assertEqual(cli.main(), 0)
        setup.assert_called_once()


@unittest.skipUnless(os.name == "posix", "App Server callbacks use Unix sockets")
class SocketRpcTests(unittest.TestCase):
    def test_websocket_rpc_answers_ping_and_reports_completion(self):
        directory = tempfile.TemporaryDirectory(prefix="reins-rpc-", dir="/tmp")
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "core.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(path))
        server.listen(1)
        server.settimeout(3)
        errors, methods = [], []

        def exact(peer, count):
            data = b""
            while len(data) < count:
                part = peer.recv(count - len(data))
                if not part:
                    raise EOFError("client closed")
                data += part
            return data

        def frame(peer):
            first, second = exact(peer, 2)
            size = second & 127
            if size == 126:
                size = struct.unpack("!H", exact(peer, 2))[0]
            elif size == 127:
                size = struct.unpack("!Q", exact(peer, 8))[0]
            mask, data = exact(peer, 4), exact(peer, size)
            return first & 15, bytes(value ^ mask[index % 4] for index, value in enumerate(data))

        def send(peer, message):
            value = json.dumps(message).encode()
            peer.sendall(bytes([129, len(value)]) + value)

        def serve():
            try:
                peer, _ = server.accept()
                with peer:
                    peer.settimeout(3)
                    header = b""
                    while not header.endswith(b"\r\n\r\n"):
                        header += exact(peer, 1)
                    key = next(line.split(b":", 1)[1].strip() for line in header.split(b"\r\n")
                               if line.lower().startswith(b"sec-websocket-key:"))
                    accepted = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                    peer.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                                 b"Sec-WebSocket-Accept: " + accepted + b"\r\n\r\n")
                    for expected in ("initialize", "initialized", "thread/resume", "turn/start"):
                        opcode, value = frame(peer)
                        message = json.loads(value)
                        methods.append((opcode, message["method"]))
                        if "id" in message:
                            if expected == "initialize":
                                peer.sendall(b"\x8a\x00\x89\x01p")  # An unsolicited pong, then a ping.
                                if frame(peer) != (10, b"p"):
                                    raise AssertionError("ping was not answered")
                            send(peer, {"id": message["id"], "result": {"turn": {"id": "turn-1"}}})
                    send(peer, {"method": "turn/completed", "params": {"threadId": "original", "turn": {"id": "turn-1"}}})
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=serve)
        thread.start()
        payload = {"request": {"target": {"kind": "session", "session": "original"}, "callback_mode": "cli",
                               "sandbox_mode": "workspace-write", "queue_dir": str(Path(directory.name) / "queue")},
                   "cwd": directory.name, "prompt": "fixture callback", "timeout": 2}
        with mock.patch.object(app_server, "desktop_app_server_socket", return_value=path):
            callback = delivery.start_desktop_app_server_turn(payload)
        self.assertIsNotNone(callback)
        try:
            self.assertTrue(callback.wait_for_completion(2))
        finally:
            callback.close()
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(methods, [(1, "initialize"), (1, "initialized"), (1, "thread/resume"), (1, "turn/start")])


if __name__ == "__main__":
    unittest.main()
