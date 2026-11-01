from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from harness_reins.engine import routes as transport
from harness_reins import cli
from harness_reins.commands import callbacks as callback_commands
from harness_reins.engine import dispatch
from harness_reins.engine import tasks as managed_tasks
from harness_reins.engine import delivery as delivery_engine
from harness_reins.harness.codex import app_server
from harness_reins.store import records as queue_store
from harness_reins.store import layout
import reins_records


class CallbackModeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="reins-modes-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.root / "profile"), "REINS_HOME": str(self.root / "profile" / "reins-home"),
            "REINS_TARGET_LOCK_DIR": str(self.root / "target-locks"),
        }, clear=True))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))

    def request(self, mode="manual"):
        return reins_records.callback_record("fixture", str(self.root), target={"kind": "session", "session": "original"}, prompt="inspect the saved fixture", agent="codex", target_source="fixture", callback_mode=mode, attempts=1)

    def test_auto_desktop_is_distinct_from_cli_and_manual(self):
        args = argparse.Namespace(agent="codex", callback_mode="auto")
        self.assertEqual(transport.selection(args)["callback_mode"], "cli")
        with mock.patch.dict(os.environ, {"CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop"}):
            selected = transport.selection(args)
            self.assertEqual(selected["callback_mode"], "codex-queue")
            self.assertTrue(Path(selected["callback_codex_bin"]).is_absolute())
            args.callback_mode = "manual"
            self.assertEqual(transport.selection(args)["callback_mode"], "manual")

    def test_fresh_cli_child_does_not_inherit_parent_desktop_origin(self):
        original = {"CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop", "CODEX_THREAD_ID": "parent", "PATH": "keep"}
        child = managed_tasks.child_agent_environment(original)
        self.assertNotIn("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", child)
        self.assertNotIn("CODEX_THREAD_ID", child)
        self.assertEqual(child["PATH"], "keep")
        self.assertEqual(original["CODEX_INTERNAL_ORIGINATOR_OVERRIDE"], "Codex Desktop")

    def test_windows_desktop_default_matches_existing_launcher(self):
        with mock.patch.object(transport.sys, "platform", "win32"):
            self.assertEqual(transport.bridge_file(self.root), self.root / "harness-reins/desktop-bridge.json")

    def test_unconfigured_desktop_refuses_before_workload_admission(self):
        args = argparse.Namespace(agent="codex", callback_mode="desktop", session="original", last=False)
        with mock.patch.object(managed_tasks, "select_execution_backend") as backend, mock.patch.object(layout, "managed_task_dir") as directory:
            self.assertEqual(dispatch.submit_managed_run(args), 125)
        backend.assert_not_called()
        directory.assert_not_called()
        self.assertEqual(args._callback_capability["status"], "blocked")

    def test_probe_only_reads_and_follows_pagination(self):
        client = mock.Mock()
        client.request.side_effect = [{}, {"data": ["other"], "nextCursor": "next"}, {"data": ["original"]}]
        with mock.patch.object(app_server, "desktop_app_server_socket", return_value=self.root / "socket"), \
                mock.patch.object(app_server, "AppServerConnection", return_value=client):
            result = transport.inspect_route(self.request("desktop"))
        self.assertEqual(result["status"], "owner_reachable")
        self.assertFalse(result["end_to_end_verified"])
        self.assertEqual([c.args[0] for c in client.request.call_args_list],
                         ["initialize", "thread/loaded/list", "thread/loaded/list"])
        client.close.assert_called_once()

    def test_manual_callback_is_never_selected_and_ack_finishes_it(self):
        queue = self.root / "queue"
        queue_store.ensure_daemon_dirs(queue)
        queue_store.write_request(queue / "pending/fixture.json", self.request())
        self.assertIsNone(delivery_engine.select_pending(queue, 10**12))
        self.assertEqual(callback_commands.ack(argparse.Namespace(queue_dir=str(queue), id="fixture", message=None)), 0)
        self.assertTrue((queue / "done/fixture.json").is_file())
        self.assertFalse((queue / "pending/fixture.json").exists())

    def test_owner_conflict_does_not_fall_back_to_cli(self):
        request = self.request("cli")
        request.update(sandbox_mode="workspace-write", queue_dir=str(self.root))
        client = mock.Mock()
        client.request.side_effect = [{}, app_server.AppServerRpcError("thread original already has an active writer")]
        with mock.patch.object(app_server, "desktop_app_server_socket", return_value=self.root / "socket"), \
                mock.patch.object(app_server, "AppServerConnection", return_value=client):
            with self.assertRaises(app_server.CallbackTransportBlocked):
                delivery_engine.start_desktop_app_server_turn({"request": request, "timeout": 2})
        self.assertEqual(len(client.request.call_args_list), 2)

    def test_explicit_desktop_never_falls_back_even_for_a_plain_socket(self):
        request = self.request("desktop")
        request.update(sandbox_mode="workspace-write", queue_dir=str(self.root))
        client = mock.Mock()
        client.connect.side_effect = OSError("socket unavailable")
        with mock.patch.object(app_server, "desktop_app_server_socket", return_value=self.root / "socket"), \
                mock.patch.object(app_server, "AppServerConnection", return_value=client):
            with self.assertRaises(app_server.CallbackTransportBlocked):
                delivery_engine.start_desktop_app_server_turn({"request": request, "timeout": 2})
        client.request.assert_not_called()

    def test_active_turn_is_deferred_before_sending_callback(self):
        request = self.request("desktop")
        request.update(sandbox_mode="workspace-write", queue_dir=str(self.root))
        client = mock.Mock()
        client.request.side_effect = [{}, {"thread": {"status": {"type": "active"}}}]
        with mock.patch.object(app_server, "desktop_app_server_socket", return_value=self.root / "socket"), \
                mock.patch.object(app_server, "AppServerConnection", return_value=client):
            with self.assertRaises(app_server.CallbackTargetBusy):
                delivery_engine.start_desktop_app_server_turn({"request": request, "timeout": 2})
        self.assertEqual(len(client.request.call_args_list), 2)

    def test_busy_delivery_waits_without_spending_retry_budget(self):
        queue = self.root / "queue"
        queue_store.ensure_daemon_dirs(queue)
        request = self.request("cli")
        queue_store.write_request(queue / "pending/fixture.json", request)
        with mock.patch.object(delivery_engine, "run_resume_until_exit_or_ack", return_value=(subprocess.CompletedProcess([], 123), False, False)):
            self.assertTrue(delivery_engine.process_one(queue, argparse.Namespace(retries=0)))
        saved = dispatch.load_request(queue / "pending/fixture.json")
        self.assertEqual(saved["attempts"], request["attempts"])
        self.assertIn("next_attempt_at", saved)
        self.assertFalse((queue / "failed/fixture.json").exists())

    def test_definite_block_is_retained_without_automatic_retries(self):
        queue = self.root / "queue"
        queue_store.ensure_daemon_dirs(queue)
        queue_store.write_request(queue / "pending/fixture.json", self.request("cli"))
        with mock.patch.object(delivery_engine, "run_resume_until_exit_or_ack", return_value=(subprocess.CompletedProcess([], 126), False, False)):
            self.assertTrue(delivery_engine.process_one(queue, argparse.Namespace(retries=10)))
        record = dispatch.load_request(queue / "failed/fixture.json")
        self.assertEqual(record["delivery_state"], "blocked")
        self.assertFalse((queue / "pending/fixture.json").exists())

    def test_retry_preserves_target_and_rejects_unknown_outcome(self):
        queue = self.root / "queue"
        queue_store.ensure_daemon_dirs(queue)
        request = self.request("cli")
        request.update(retain_target_lease=True)
        queue_store.write_request(queue / "failed/fixture.json", request)
        args = argparse.Namespace(queue_dir=str(queue), id="fixture", callback_mode="manual")
        self.assertEqual(callback_commands.retry_callback(args), 2)
        request.pop("retain_target_lease")
        request["last_error"] = "original failure"
        queue_store.write_request(queue / "failed/fixture.json", request)
        with mock.patch.object(dispatch, "submit_managed_run") as launch:
            self.assertEqual(callback_commands.retry_callback(args), 0)
        launch.assert_not_called()
        saved = dispatch.load_request(queue / "pending/fixture.json")
        self.assertEqual(saved["target"], request["target"])
        self.assertEqual(saved["id"], "fixture")
        self.assertEqual(saved["callback_mode"], "manual")
        self.assertEqual(saved["retry_history"][0]["last_error"], "original failure")


if __name__ == "__main__":
    unittest.main()
