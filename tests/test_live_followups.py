from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from harness_reins.commands import callbacks
from harness_reins.engine import delivery as delivery_engine
from harness_reins.engine import dispatch
from harness_reins.engine import messages as message_engine
from harness_reins.harness.claude import session as claude_session
from harness_reins.install import skills
from harness_reins.store import layout
from harness_reins.store import records as queue_store
from harness_reins.system import files

SESSION = "5b2e7c10-claude-session"


class LiveCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "reins" / "queue"
        self.claude = self.directory / "claude"
        patcher = mock.patch.dict(os.environ, {
            "REINS_HOME": str(self.directory / "reins"), "CLAUDE_CONFIG_DIR": str(self.claude),
            layout.TARGET_LOCK_DIR_ENV: str(self.directory / "target-locks"), "CLAUDE_CODE_ENTRYPOINT": "cli",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        queue_store.ensure_daemon_dirs(self.root)

    def enqueue(self, ident: str, **fields: object) -> Path:
        args = argparse.Namespace(
            agent="claude", session=SESSION, last=False, task="train model", cwd=str(self.directory),
            command="python train.py", exit_code=0, message=None, queue_dir=str(self.root), _callback_id=ident,
        )
        prompt = dispatch.build_prompt(args, duration=12.0)
        request = dispatch.make_request(args, prompt)
        request.update(fields)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dispatch.enqueue_existing_request(self.root, request, prompt), 0)
        return layout.request_path(self.root, "pending", ident)

    def run_hook(self, expects=None) -> tuple[int, str]:
        stderr = io.StringIO()
        args = argparse.Namespace(queue_dir=str(self.root), poll_interval=0.02)
        with mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": SESSION}))), \
                contextlib.redirect_stderr(stderr):
            code = claude_session.hook(args, expects)
        return code, stderr.getvalue()


class WindowClosedTests(LiveCase):
    def test_a_callback_shown_to_a_window_that_closed_is_due_at_once(self) -> None:
        now = time.time()
        path = self.enqueue("cb-shown", live_delivered_at=now, next_attempt_at=now + 1800, live_window_pid=4242)
        with mock.patch.object(claude_session, "open_window", return_value=4242):
            self.assertIsNone(delivery_engine.select_pending(self.root, time.time()))
        with mock.patch.object(claude_session, "open_window", return_value=None):
            self.assertEqual(delivery_engine.select_pending(self.root, time.time()), path)

    def test_the_daemon_delivers_it_rather_than_putting_it_back(self) -> None:
        now = time.time()
        path = self.enqueue("cb-shown", live_delivered_at=now, next_attempt_at=now + 1800, live_window_pid=4242)
        seen = []

        def resume(root, request_id, request, args):
            seen.append(dict(request))
            return subprocess.CompletedProcess(["claude"], 124), False, False

        args = argparse.Namespace(retries=3, retry_delay=30.0, retry_backoff=2.0)
        with mock.patch.object(claude_session, "open_window", return_value=None), \
                mock.patch.object(delivery_engine, "run_resume_until_exit_or_ack", side_effect=resume), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(delivery_engine.process_one(self.root, args))
            self.assertEqual(len(seen), 1)
            self.assertNotIn("live_window_pid", seen[0])
            # The retry hold the failed resume left is kept, not skipped as a window hold.
            self.assertIsNone(delivery_engine.select_pending(self.root, time.time()))
        request = json.loads(path.read_text(encoding="utf-8"))
        self.assertGreater(request["next_attempt_at"], time.time())
        self.assertEqual(request["attempts"], 1)

    def test_a_window_reins_could_not_see_keeps_the_grace(self) -> None:
        now = time.time()
        self.enqueue("cb-unseen", live_delivered_at=now, next_attempt_at=now + 1800, live_window_pid=None)
        with mock.patch.object(claude_session, "open_window", return_value=None):
            self.assertIsNone(delivery_engine.select_pending(self.root, time.time()))

    def test_an_ordinary_retry_hold_is_still_kept(self) -> None:
        self.enqueue("cb-retry", next_attempt_at=time.time() + 1800)
        with mock.patch.object(claude_session, "open_window", return_value=None):
            self.assertIsNone(delivery_engine.select_pending(self.root, time.time()))


class ReminderTests(LiveCase):
    def test_an_ignored_callback_is_raised_again_a_bounded_number_of_times(self) -> None:
        now = time.time()
        self.assertTrue(claude_session._due_for_reminder({}, now))
        self.assertFalse(claude_session._due_for_reminder({"live_delivered_at": now}, now))
        stale = now - claude_session.LIVE_REMIND_SECONDS
        self.assertTrue(claude_session._due_for_reminder({"live_delivered_at": stale}, now))
        self.assertFalse(claude_session._due_for_reminder(
            {"live_delivered_at": stale, "live_reminders": claude_session.LIVE_REMINDERS}, now))

    def test_the_hook_repeats_it_and_counts_the_repeat(self) -> None:
        path = self.enqueue("cb-ignored")
        with mock.patch.object(claude_session, "open_window", return_value=4242):
            code, text = self.run_hook()
        self.assertEqual(code, 2)
        self.assertIn("cb-ignored", text)
        request = json.loads(path.read_text(encoding="utf-8"))
        request["live_delivered_at"] -= claude_session.LIVE_REMIND_SECONDS
        queue_store.write_request(path, request)
        with mock.patch.object(claude_session, "open_window", return_value=4242):
            code, text = self.run_hook()
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["live_reminders"], 1)


class HookRenewalTests(LiveCase):
    def renew(self, expects: bool) -> tuple[int, str]:
        with mock.patch.object(claude_session, "HOOK_TIMEOUT_SECONDS", 0.2), \
                mock.patch.object(claude_session, "HOOK_RENEW_MARGIN_SECONDS", 0.0), \
                mock.patch.object(claude_session, "open_window", return_value=4242):
            return self.run_hook(lambda _root, _session: expects)

    def test_the_hook_wakes_its_window_to_be_restarted_when_work_is_out(self) -> None:
        code, text = self.renew(True)
        self.assertEqual(code, 2)
        self.assertIn("restarts its hook", text)

    def test_the_hook_leaves_quietly_when_nothing_can_arrive(self) -> None:
        self.assertEqual(self.renew(False), (0, ""))

    def test_a_running_task_or_a_registered_name_counts_as_work_out(self) -> None:
        tasks = layout.managed_tasks_root(self.root)
        (tasks / "t1").mkdir(parents=True)
        task = {"id": "t1", "agent": "claude", "state": "completed", "target": {"kind": "session", "session": SESSION}}
        (tasks / "t1" / "task.json").write_text(json.dumps(task), encoding="utf-8")
        self.assertFalse(message_engine.expects_callbacks(self.root, "claude", SESSION))
        task["state"] = "running"
        (tasks / "t1" / "task.json").write_text(json.dumps(task), encoding="utf-8")
        self.assertTrue(message_engine.expects_callbacks(self.root, "claude", SESSION))
        self.assertFalse(message_engine.expects_callbacks(self.root, "claude", "another-session"))

    def test_an_active_goal_counts_as_work_out(self) -> None:
        goal = {"id": "g1", "agent": "claude", "state": "active", "target": {"kind": "session", "session": SESSION}}
        (self.root / "goals").mkdir(exist_ok=True)
        (self.root / "goals" / "g1.json").write_text(json.dumps(goal), encoding="utf-8")
        self.assertTrue(message_engine.expects_callbacks(self.root, "claude", SESSION))
        goal["state"] = "completed"
        (self.root / "goals" / "g1.json").write_text(json.dumps(goal), encoding="utf-8")
        self.assertFalse(message_engine.expects_callbacks(self.root, "claude", SESSION))


class OrphanWaiterTests(LiveCase):
    def run_wait(self, windows: list[object]) -> tuple[int, str]:
        stderr = io.StringIO()
        args = argparse.Namespace(queue_dir=str(self.root), session=SESSION, task=None, timeout=5.0,
                                  poll_interval=0.02, agent="claude")
        with mock.patch.object(claude_session, "WAIT_WINDOW_CHECK_SECONDS", 0.05), \
                mock.patch.object(claude_session, "open_window", side_effect=windows + [None] * 500), \
                contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            return claude_session.wait(args), stderr.getvalue()

    def test_a_wait_whose_window_closed_lets_go(self) -> None:
        code, text = self.run_wait([4242, 4242])
        self.assertEqual(code, 0)
        self.assertIn("has closed", text)
        self.assertFalse(claude_session.live_watcher_is_held({"agent": "claude",
                                                             "target": {"kind": "session", "session": SESSION}}))

    def test_a_wait_that_never_saw_its_window_keeps_waiting(self) -> None:
        stderr = io.StringIO()
        args = argparse.Namespace(queue_dir=str(self.root), session=SESSION, task=None, timeout=0.3,
                                  poll_interval=0.02, agent="claude")
        with mock.patch.object(claude_session, "WAIT_WINDOW_CHECK_SECONDS", 0.05), \
                mock.patch.object(claude_session, "open_window", return_value=None), \
                contextlib.redirect_stderr(stderr):
            self.assertEqual(claude_session.wait(args), 3)


class StatusTests(LiveCase):
    def test_status_shows_failed_callbacks_with_what_to_do(self) -> None:
        path = self.enqueue("cb-gave-up", last_error="missing acknowledgement marker after exit 1")
        queue_store.move_request(path, self.root / "failed")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            callbacks.status(argparse.Namespace(queue_dir=str(self.root), state=None, limit=5, quiet_empty=False))
        self.assertIn("[failed]", output.getvalue())
        self.assertIn("reins retry --id cb-gave-up", output.getvalue())


class HookWordingTests(LiveCase):
    def test_no_reins_wait_is_asked_for_where_the_hook_delivers(self) -> None:
        self.assertFalse(claude_session.hook_delivers())
        skills.configure_claude_hook("reins _claude-hook", self.claude / "settings.json")
        self.assertTrue(claude_session.hook_installed(self.claude / "settings.json"))
        self.assertTrue(claude_session.hook_delivers())
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_ENTRYPOINT": claude_session.HEADLESS_ENTRYPOINT}):
            self.assertFalse(claude_session.hook_delivers())
        self.assertFalse(claude_session.hook_delivers("dsh"))


class WindowsReadRetryTests(unittest.TestCase):
    def test_a_read_during_a_replace_is_retried(self) -> None:
        attempts = []

        def flaky(_path: Path, encoding: str = "utf-8") -> str:
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError(13, "being replaced")
            return "{}"

        with mock.patch.object(files, "os", SimpleNamespace(name="nt")), \
                mock.patch.object(Path, "read_text", flaky):
            self.assertEqual(files.read_text(Path("task.json")), "{}")
        self.assertEqual(len(attempts), 3)

    def test_a_lasting_permission_error_still_raises(self) -> None:
        with mock.patch.object(files, "os", SimpleNamespace(name="nt")), \
                mock.patch.object(files.windows_io, "SHARING_RETRY_SECONDS", 0.05), \
                mock.patch.object(Path, "read_text", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(PermissionError):
                files.read_text(Path("task.json"))


if __name__ == "__main__":
    unittest.main()
