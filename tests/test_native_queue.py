"""Public CLI queue transport: frozen identity, one publication and receiver ACK."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from harness_reins.commands import callbacks
from harness_reins.core import capabilities
from harness_reins.engine import delivery, dispatch, native_queue, routes
from harness_reins.store import layout, records
import reins_records


THREAD = "01a15842-e8a7-7630-9fee-869784c02260"
MESSAGE = "01a19796-8012-7ea3-bd98-60b2b6acbfbc"


class NativeQueueTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="reins-queue-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(os.environ, {
            "CODEX_HOME": str(self.root / "profile"), "REINS_HOME": str(self.root / "reins"),
            "REINS_TARGET_LOCK_DIR": str(self.root / "locks"),
            # An executable that exists: Windows resolves it before the mocked run.
            "REINS_CODEX_BIN": sys.executable,
        }, clear=True))
        self.queue = self.root / "queue"
        records.ensure_daemon_dirs(self.queue)
        self.request = reins_records.callback_record("native-c1", str(self.root),
            target={"kind": "session", "session": THREAD}, prompt="inspect and ACK native-c1",
            agent="codex", callback_mode="codex-queue", queue_dir=str(self.queue), attempts=0,
            **native_queue.route_fields())
        self.payload = {"request": self.request, "queue_dir": str(self.queue), "timeout": 10, "prompt": self.request["prompt"]}

    def publish(self, result=None):
        result = result or subprocess.CompletedProcess([], 0, f"Queued message {MESSAGE} for thread {THREAD}.\n", "")
        with mock.patch.object(native_queue.subprocess, "run", return_value=result) as run:
            value = native_queue.publish(self.payload)
        return value, run

    def test_auto_desktop_uses_queue_but_server_cli_keeps_resume(self):
        args = argparse.Namespace(agent="codex", callback_mode="auto")
        self.assertEqual(routes.selection(args)["callback_mode"], "cli")
        with mock.patch.dict(os.environ, {"CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop"}):
            route = routes.selection(args)
        self.assertEqual(route["callback_mode"], "codex-queue")
        self.assertEqual(capabilities.required_for(route), [capabilities.CODEX_QUEUE])
        self.assertEqual(route["callback_codex_home"], str(self.root / "profile"))

    def test_preflight_only_checks_public_cli_capability(self):
        with mock.patch.object(native_queue.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "--thread --message", "")) as run:
            result = routes.inspect_route(self.request)
        self.assertEqual(result["status"], "available")
        self.assertFalse(result["end_to_end_verified"])
        self.assertEqual(run.call_args.args[0][-2:], ["queue", "--help"])

    def test_unsupported_cli_is_blocked(self):
        with mock.patch.object(native_queue.subprocess, "run", return_value=subprocess.CompletedProcess([], 2, "", "unknown queue")):
            self.assertEqual(routes.inspect_route(self.request)["status"], "blocked")

    def test_publish_is_native_queue_without_resume_and_freezes_profile(self):
        value, run = self.publish()
        self.assertEqual(value["returncode"], native_queue.PUBLISHED_RETURN_CODE)
        command = run.call_args.args[0]
        self.assertEqual(command[1:], ["queue", "--thread", THREAD, "--message", self.request["prompt"]])
        self.assertEqual(run.call_args.kwargs["env"]["CODEX_HOME"], self.request["callback_codex_home"])
        self.assertFalse(delivery.desktop_submission_pending(self.queue, self.request))
        receipt = json.loads((self.queue / "native-queue/native-c1.json").read_text())
        self.assertEqual(receipt["message_id"], MESSAGE)
        self.assertEqual(receipt["state"], "queued")

    def test_second_publication_never_calls_codex(self):
        self.publish()
        value, run = self.publish()
        self.assertEqual(value["returncode"], 125)
        run.assert_not_called()

    def test_timeout_retains_intent_and_prevents_replay(self):
        with mock.patch.object(native_queue.subprocess, "run", side_effect=subprocess.TimeoutExpired("queue", 10)):
            self.assertEqual(native_queue.publish(self.payload)["returncode"], 125)
        self.assertTrue(delivery.desktop_submission_pending(self.queue, self.request))
        _, run = self.publish()
        run.assert_not_called()

    def test_spawn_failure_is_repairable_without_claiming_publication(self):
        with mock.patch.object(native_queue.subprocess, "run", side_effect=FileNotFoundError("codex")):
            self.assertEqual(native_queue.publish(self.payload)["returncode"], 126)
        self.assertFalse(delivery.desktop_submission_pending(self.queue, self.request))
        self.assertFalse((self.queue / "native-queue/native-c1.json").exists())

    def test_unresolvable_executable_is_repairable_without_claiming_publication(self):
        with mock.patch.object(native_queue.shell, "process_command", side_effect=FileNotFoundError("codex")):
            self.assertEqual(native_queue.publish(self.payload)["returncode"], 126)
        self.assertFalse(delivery.desktop_submission_pending(self.queue, self.request))
        self.assertFalse((self.queue / "native-queue/native-c1.json").exists())

    def test_unconfirmed_or_wrong_target_response_is_not_success(self):
        for result in (subprocess.CompletedProcess([], 0, "", ""),
                       subprocess.CompletedProcess([], -9, "", ""),
                       subprocess.CompletedProcess([], 0, f"Queued message {MESSAGE} for thread deadbeef.", "")):
            with self.subTest(result=result):
                value, _ = self.publish(result)
                self.assertEqual(value["returncode"], 125)
                (self.queue / "native-queue/native-c1.json").unlink()
                delivery.release_retained_target_lease(self.queue, self.request)

    def test_cli_failure_is_repairable_and_can_be_retried(self):
        path = self.queue / "failed/native-c1.json"
        records.write_request(path, self.request)
        value, _ = self.publish(subprocess.CompletedProcess([], 1, "", "no rollout found for thread id"))
        self.assertEqual(value["returncode"], 126)
        self.assertFalse(delivery.desktop_submission_pending(self.queue, self.request))
        self.assertFalse((self.queue / "native-queue/native-c1.json").exists())
        args = argparse.Namespace(id="native-c1", queue_dir=str(self.queue), callback_mode=None)
        with mock.patch.object(routes, "inspect_route", return_value={"status": "available"}):
            self.assertEqual(callbacks.retry_callback(args), 0)
        self.assertTrue((self.queue / "pending/native-c1.json").is_file())

    def test_publish_runs_with_the_submitting_session_environment(self):
        self.payload["environment"] = {"PATH": "/session/node/bin", "CODEX_HOME": "/somewhere/else"}
        _, run = self.publish()
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["PATH"], "/session/node/bin")
        self.assertEqual(env["CODEX_HOME"], self.request["callback_codex_home"])

    def test_unanswered_ack_does_not_hold_back_later_callbacks(self):
        first = self.queue / "pending/native-c1.json"
        records.write_request(first, self.request)
        def send(*_):
            self.publish()
            return subprocess.CompletedProcess([], native_queue.PUBLISHED_RETURN_CODE), False, False
        with mock.patch.object(delivery, "run_resume_until_exit_or_ack", side_effect=send):
            self.assertTrue(delivery.process_one(self.queue, argparse.Namespace()))
        later = self.queue / "pending/native-c2.json"
        records.write_request(later, dict(self.request, id="native-c2"))
        self.assertEqual(delivery.select_pending(self.queue, 1e12), later)
        self.assertEqual(dispatch.load_request(first)["delivery_state"], "queued_awaiting_ack")

    def test_published_is_pending_until_ack_and_survives_restart_without_resend(self):
        path = self.queue / "pending/native-c1.json"
        records.write_request(path, self.request)
        def send(*_):
            self.publish()
            return subprocess.CompletedProcess([], native_queue.PUBLISHED_RETURN_CODE), False, False
        with mock.patch.object(delivery, "run_resume_until_exit_or_ack", side_effect=send):
            self.assertTrue(delivery.process_one(self.queue, argparse.Namespace()))
        self.assertEqual(dispatch.load_request(path)["delivery_state"], "queued_awaiting_ack")
        self.assertFalse((self.queue / "done/native-c1.json").exists())
        delivery.recover_running(self.queue)
        self.assertIsNone(delivery.select_pending(self.queue, 1e12))
        self.assertEqual(callbacks.ack(argparse.Namespace(queue_dir=str(self.queue), id="native-c1", message="received")), 0)
        self.assertTrue((self.queue / "done/native-c1.json").is_file())
        self.assertFalse(delivery.retained_target_lease_is_held(self.request))

    def test_receipt_instructions_describe_native_permissions_and_no_replay(self):
        text = dispatch.build_acknowledgement_text("reins ack native-c1", callback_mode="codex-queue")
        self.assertIn("original thread keeps its existing permissions", text)
        self.assertIn("published once", text)
        self.assertNotIn("retry this callback", text)

    def test_frozen_route_survives_task_to_callback(self):
        path = self.queue / "pending/native-c1.json"
        records.write_request(path, self.request)
        saved = dispatch.load_request(path)
        with mock.patch.dict(os.environ, {"REINS_CODEX_BIN": "/different/codex"}):
            self.assertEqual(saved["callback_codex_bin"], self.request["callback_codex_bin"])
        self.assertTrue(set(native_queue.route_fields()) <= set(routes.ROUTE_FIELDS))


if __name__ == "__main__":
    unittest.main()
