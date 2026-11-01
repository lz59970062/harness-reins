"""Native tool-output transport, ownership and durable receipt semantics."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from harness_reins import cli
from harness_reins.commands import callbacks, work
from harness_reins.core import capabilities
from harness_reins.engine import delivery, dispatch, native, routes
from harness_reins.harness.codex import app_server
from harness_reins.store import layout, records
from harness_reins.install import diagnostics
import reins_records


class NativeCallbacksTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="reins-native-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.root / "profile"),
            "REINS_HOME": str(self.root / "reins"),
            "REINS_TARGET_LOCK_DIR": str(self.root / "locks"),
        }, clear=True))
        # RPC/route tests also run on Windows; the real socket test is POSIX.
        self.request = reins_records.callback_record(
            "native-c1", str(self.root), agent="codex", callback_mode="app-server",
            callback_socket=str(self.root / "owner.sock"),
            target={"kind": "session", "session": "original"}, prompt="Inspect native-c1 and ACK.",
            queue_dir=str(self.root / "queue"), target_source="explicit", attempts=0,
        )
        self.queue = self.root / "queue"
        records.ensure_daemon_dirs(self.queue)
        self.payload = {"request": self.request, "queue_dir": str(self.queue),
                        "cwd": str(self.root), "prompt": self.request["prompt"], "timeout": 4,
                        "ack_path": str(layout.ack_path(self.queue, "native-c1")),
                        "canceled_path": str(layout.request_path(self.queue, "canceled", "native-c1"))}

    def client(self, *, loaded=True, active=False, start=None):
        client = mock.Mock()
        def request(method, params):
            if method == "initialize": return {}
            if method == "thread/loaded/list": return {"data": ["original"] if loaded else []}
            if method == "thread/resume":
                return {"thread": {"id": "original", "status": {"type": "active" if active else "idle"}}}
            if method == "turn/start":
                self.assertTrue(delivery.desktop_submission_pending(self.queue, self.request))
                if isinstance(start, Exception): raise start
                return {"turn": {"id": "turn-1"}} if start is None else start
            raise AssertionError(method)
        client.request.side_effect = request
        return client

    def start(self, client):
        # Exercise native RPC logic on every CI OS, independent of socket type.
        with mock.patch.object(native.sys, "platform", "linux"), \
                mock.patch.object(app_server, "AppServerConnection", return_value=client):
            return delivery.start_desktop_app_server_turn(self.payload)

    def test_cli_and_desktop_auto_selection_unchanged(self):
        args = argparse.Namespace(agent="codex", callback_mode="auto")
        self.assertEqual(routes.selection(args)["callback_mode"], "cli")
        with mock.patch.dict(os.environ, {"CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop"}):
            self.assertEqual(routes.selection(args)["callback_mode"], "codex-queue")
        command = delivery.codex_resume_command(dict(self.request, callback_mode="cli"))
        self.assertEqual(command[:3], ["codex", "exec", "resume"])

    def test_selection_freezes_socket_and_capability(self):
        args = argparse.Namespace(agent="codex", callback_mode="app-server", app_server_socket=str(self.root / "owner.sock"))
        route = routes.selection(args)
        self.assertEqual(route["callback_socket"], str(self.root / "owner.sock"))
        self.assertEqual(capabilities.required_for(route), [capabilities.CODEX_TOOL_OUTPUT])
        self.assertIn("callback_socket", routes.ROUTE_FIELDS)

    def test_cli_entrypoints_preserve_explicit_native_socket(self):
        endpoint = str(self.root / "owner.sock")
        for argv, module, name in (
            (["run", "--session", "original", "--callback-mode", "app-server", "--app-server-socket", endpoint, "--", "echo", "fixture"], work, "run"),
            (["doctor", "--session", "original", "--callback-mode", "app-server", "--app-server-socket", endpoint], diagnostics, "doctor"),
            (["retry", "--id", "native-c1", "--callback-mode", "app-server", "--app-server-socket", endpoint], callbacks, "retry_callback"),
        ):
            with self.subTest(argv=argv), mock.patch.object(sys, "argv", ["reins", *argv]), \
                    mock.patch.object(module, name, return_value=0) as handler, \
                    mock.patch.object(diagnostics, "emit_if_needed"):
                self.assertEqual(cli.main(), 0)
                self.assertEqual(handler.call_args.args[0].app_server_socket, endpoint)

    def test_retry_keeps_socket_and_does_not_relaunch_task(self):
        failed = self.queue / "failed/native-c1.json"
        records.write_request(failed, self.request)
        args = argparse.Namespace(id="native-c1", queue_dir=str(self.queue), callback_mode="app-server", app_server_socket=None)
        with mock.patch.object(routes, "inspect_route", return_value={"status": "owner_reachable"}), \
                mock.patch.object(dispatch, "submit_managed_run") as launch:
            self.assertEqual(callbacks.retry_callback(args), 0)
        launch.assert_not_called()
        saved = dispatch.load_request(self.queue / "pending/native-c1.json")
        self.assertEqual(saved["callback_socket"], self.request["callback_socket"])
        self.assertEqual(saved["target"], self.request["target"])

    def test_missing_relative_socket_and_wrong_agent_are_rejected(self):
        for value in (None, "", "relative.sock"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                routes.selection(argparse.Namespace(agent="codex", callback_mode="app-server", app_server_socket=value))
        with self.assertRaises(ValueError):
            routes.selection(argparse.Namespace(agent="pi", callback_mode="app-server", app_server_socket=str(self.root / "x")))
        with self.assertRaises(ValueError):
            routes.selection(argparse.Namespace(agent="codex", callback_mode="cli", app_server_socket=str(self.root / "x")))

    def test_durable_record_rejects_missing_native_capability(self):
        path = self.queue / "pending/native-c1.json"
        records.write_request(path, self.request)
        self.assertEqual(dispatch.load_request(path)["callback_socket"], self.request["callback_socket"])
        records.write_request(path, dict(self.request, requires=[]))
        with self.assertRaisesRegex(ValueError, "capability"):
            dispatch.load_request(path)

    def test_probe_is_read_only_and_does_not_claim_receipt(self):
        client = self.client()
        with mock.patch.object(native.sys, "platform", "linux"), \
                mock.patch.object(app_server, "AppServerConnection", return_value=client):
            result = routes.inspect_route(self.request)
        self.assertEqual(result["status"], "owner_reachable")
        self.assertFalse(result["end_to_end_verified"])
        self.assertEqual([c.args[0] for c in client.request.call_args_list], ["initialize", "thread/loaded/list"])
        client.close.assert_called_once()

    def test_wrong_owner_never_resumes_or_submits(self):
        client = self.client(loaded=False)
        with self.assertRaises(app_server.CallbackTransportBlocked):
            self.start(client)
        self.assertEqual([c.args[0] for c in client.request.call_args_list], ["initialize", "thread/loaded/list"])
        self.assertFalse(delivery.desktop_submission_pending(self.queue, self.request))

    def test_idle_and_active_turns_receive_tool_output_without_policy_override(self):
        for active in (False, True):
            with self.subTest(active=active):
                client = self.client(active=active)
                result = self.start(client)
                self.assertEqual(result.turn_id, "turn-1")
                params = client.request.call_args.args[1]
                self.assertEqual(params, {"threadId": "original", "input": [], "toolOutput": {
                    "name": "reins_task_completed", "namespace": None, "output": self.request["prompt"]}})
                result.close()
                delivery.release_retained_target_lease(self.queue, self.request)

    def test_unsupported_rpc_blocks_without_fallback_and_releases_intent(self):
        client = self.client(start=app_server.AppServerRpcError("unsupported toolOutput"))
        with self.assertRaisesRegex(app_server.CallbackTransportBlocked, "CLI fallback disabled"):
            self.start(client)
        self.assertFalse(delivery.desktop_submission_pending(self.queue, self.request))

    def test_lost_response_and_missing_turn_id_retain_intent(self):
        for response in (app_server.AppServerProtocolError("lost response"), {}):
            with self.subTest(response=response):
                client = self.client(start=response)
                result = self.start(client)
                self.assertIsNone(result.turn_id)
                self.assertTrue(delivery.desktop_submission_pending(self.queue, self.request))
                waited = delivery.wait_for_remote_delivery(self.payload, result, 4)
                self.assertEqual(waited["returncode"], 125)
                self.assertTrue(waited["manual_recovery_required"])
                delivery.release_retained_target_lease(self.queue, self.request)

    def test_completion_uses_one_event_wait_and_requires_ack(self):
        client = self.client()
        result = self.start(client)
        client.wait_for_turn_completion.return_value = True
        waited = delivery.wait_for_remote_delivery(self.payload, result, 20)
        self.assertEqual(waited["returncode"], 1)
        client.wait_for_turn_completion.assert_called_once_with("original", "turn-1", 20)
        self.assertFalse(delivery.desktop_submission_pending(self.queue, self.request))

    def test_ack_on_completion_succeeds(self):
        client = self.client()
        result = self.start(client)
        def complete(*_):
            Path(self.payload["ack_path"]).touch()
            return True
        client.wait_for_turn_completion.side_effect = complete
        waited = delivery.wait_for_remote_delivery(self.payload, result, 20)
        self.assertEqual(waited["returncode"], 0)
        self.assertFalse(delivery.desktop_submission_pending(self.queue, self.request))

    def test_connection_loss_or_deadline_preserves_intent(self):
        for response in (False, app_server.AppServerProtocolError("disconnected")):
            with self.subTest(response=response):
                client = self.client()
                result = self.start(client)
                if isinstance(response, Exception): client.wait_for_turn_completion.side_effect = response
                else: client.wait_for_turn_completion.return_value = response
                waited = delivery.wait_for_remote_delivery(self.payload, result, 20)
                self.assertEqual(waited["returncode"], 125)
                self.assertTrue(delivery.desktop_submission_pending(self.queue, self.request))
                client.wait_for_turn_completion.assert_called_once()
                delivery.release_retained_target_lease(self.queue, self.request)


if __name__ == "__main__":
    unittest.main()
