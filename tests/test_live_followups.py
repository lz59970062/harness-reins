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

import yaml

from harness_reins.commands import callbacks
from harness_reins.commands import goals as goal_commands
from harness_reins.core import capabilities
from harness_reins.engine import goals
from harness_reins.engine import delivery as delivery_engine
from harness_reins.engine import dispatch
from harness_reins.engine import messages as message_engine
from harness_reins.engine import tasks as managed_tasks
from harness_reins.harness.claude import session as claude_session
from harness_reins.harness.pi import channel as pi_channel
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


class GoalRouteTests(LiveCase):
    PLAN = ("version: 1\nrevision: 1\ngoal: Ship it\npath:\n  - id: build\n    title: Build\n    status: pending\n"
            "amendments:\n  - revision: 1\n    reason: Initial path\n")

    def start_goal(self, agent: str) -> dict[str, object]:
        plan = self.directory / "plan.yaml"
        plan.write_text(self.PLAN, encoding="utf-8")
        args = argparse.Namespace(queue_dir=str(self.root), id="g1", session="thread-1", agent=agent, last=False,
                                  cwd=str(self.directory), task="ship it", plan_file=str(plan), idle_seconds=60.0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(goal_commands.goal_start(args), 0)
        goal = goals.load_goal(self.root, "g1")
        goal["last_external_callback_at"] = time.time() - 120
        goals.write_goal(self.root, goal)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(goals.process_goal_reminders(self.root))
        (reminder,) = (self.root / "pending").glob("*.json")
        return dispatch.load_request(reminder)

    def test_a_reminder_takes_the_route_the_sessions_results_take(self) -> None:
        with mock.patch.dict(os.environ, {"CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop"}):
            reminder = self.start_goal("codex")
        self.assertEqual(reminder["callback_mode"], "codex-queue")
        self.assertIn(capabilities.CODEX_QUEUE, reminder["requires"])

    def test_a_cli_session_keeps_the_cli_route(self) -> None:
        reminder = self.start_goal("claude")
        self.assertEqual(reminder["callback_mode"], "cli")
        self.assertEqual(reminder["requires"], [capabilities.CLI_RESUME])

    def test_a_goal_from_before_routes_were_recorded_still_reminds_through_the_cli(self) -> None:
        plan = self.directory / "plan.yaml"
        plan.write_text(self.PLAN, encoding="utf-8")
        args = argparse.Namespace(queue_dir=str(self.root), id="g1", session="thread-1", agent="codex", last=False,
                                  cwd=str(self.directory), task="ship it", plan_file=str(plan), idle_seconds=60.0)
        with contextlib.redirect_stdout(io.StringIO()):
            goal_commands.goal_start(args)
        goal = goals.load_goal(self.root, "g1")
        goal.pop("route")
        goal["last_external_callback_at"] = time.time() - 120
        goals.write_goal(self.root, goal)
        with contextlib.redirect_stderr(io.StringIO()):
            goals.process_goal_reminders(self.root)
        (reminder,) = (self.root / "pending").glob("*.json")
        self.assertEqual(dispatch.load_request(reminder)["callback_mode"], "cli")


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


class SkillTextTests(unittest.TestCase):
    def test_every_skill_names_its_own_session_and_has_no_unfilled_placeholder(self) -> None:
        skills_dir = Path(skills.__file__).parents[1] / "assets" / "skills"
        for agent, variable in (("claude", "CLAUDE_CODE_SESSION_ID"), ("codex", "CODEX_THREAD_ID"),
                                ("pi", "PI_SESSION_ID"), ("dsh", "DSH_SESSION_ID")):
            text = (skills_dir / agent / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("## Several steps: start a goal", text, agent)
            self.assertIn(f'--session "${variable}"', text, agent)
            self.assertNotIn("{'", text, agent)

    def test_every_description_asks_for_a_goal_on_work_of_several_steps(self) -> None:
        skills_dir = Path(skills.__file__).parents[1] / "assets" / "skills"
        for agent in ("claude", "codex", "pi", "dsh"):
            text = (skills_dir / agent / "SKILL.md").read_text(encoding="utf-8")
            description = yaml.safe_load(text.split("---", 2)[1])["description"]
            self.assertIn("start a Reins goal before the first job", description, agent)
            self.assertLessEqual(len(description), 1024, agent)

    def test_the_claude_description_does_not_ask_for_a_wait_the_hook_makes_needless(self) -> None:
        text = (Path(skills.__file__).parents[1] / "assets" / "skills" / "claude" / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("reins wait", yaml.safe_load(text.split("---", 2)[1])["description"])


class SetupSkillRefreshTests(unittest.TestCase):
    def install(self, home: Path, **fields: object) -> None:
        args = argparse.Namespace(**{"path": None, "force": True, "keep_existing": False, "target": "both",
                                     "claude_hook": None, "refresh_installed": True, **fields})
        environment = {"HOME": str(home), "CODEX_HOME": str(home / "codex"), "CLAUDE_CONFIG_DIR": str(home / "claude"),
                       pi_channel.PROFILE_ENV: str(home / "pi"), "DSH_HOME": str(home / "dsh")}
        with mock.patch.dict(os.environ, environment), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(skills.install_skill(args), 0)

    def old_copy(self, home: Path, agent: str) -> Path:
        path = home / agent / "skills" / "harness-reins" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("an older version", encoding="utf-8")
        return path

    def test_setup_refreshes_a_copy_it_installed_earlier(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            copies = [self.old_copy(home, "pi"), self.old_copy(home, "dsh")]
            self.install(home)
            for path in copies:
                self.assertIn("## Several steps: start a goal", path.read_text(encoding="utf-8"), path)

    def test_setup_does_not_install_for_an_agent_that_never_had_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.install(home)
            self.assertFalse((home / "pi" / "skills" / "harness-reins").exists())
            self.assertFalse((home / "dsh" / "skills" / "harness-reins").exists())

    def test_a_kept_skill_stays_and_so_does_one_outside_setup(self) -> None:
        for fields in ({"keep_existing": True}, {"refresh_installed": False}):
            with self.subTest(fields=fields), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                path = self.old_copy(home, "pi")
                self.install(home, **fields)
                self.assertEqual(path.read_text(encoding="utf-8"), "an older version")


class GoalHintTests(LiveCase):
    HINT = "start one with `reins goal start`"

    def submit(self, session: str = SESSION, goal_id: str | None = None) -> str:
        args = argparse.Namespace(
            cwd=str(self.directory), task="step", command=None, exit_code=None, message=None, session=session,
            last=False, via_daemon=False, queue_dir=str(self.root), approvals_reviewer="auto_review",
            approval_policy="on-request", sandbox_mode="workspace-write", dry_run=False, strict=False,
            wrapped_command=["true"], agent="codex", goal_id=goal_id)
        errors = io.StringIO()
        with mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"), \
                contextlib.redirect_stderr(errors):
            self.assertEqual(dispatch.submit_managed_run(args), 0)
        return errors.getvalue()

    def start_goal(self, session: str = SESSION) -> None:
        plan = self.directory / "plan.yaml"
        plan.write_text(GoalRouteTests.PLAN, encoding="utf-8")
        args = argparse.Namespace(queue_dir=str(self.root), id="g1", session=session, agent="codex", last=False,
                                  cwd=str(self.directory), task="ship it", plan_file=str(plan), idle_seconds=60.0)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(goal_commands.goal_start(args), 0)

    def test_the_second_job_of_a_session_hears_about_goals_and_the_first_does_not(self) -> None:
        self.assertNotIn(self.HINT, self.submit())
        self.assertIn(self.HINT, self.submit())

    def test_another_sessions_job_does_not_count(self) -> None:
        self.submit("another-session")
        self.assertNotIn(self.HINT, self.submit())

    def test_a_session_with_an_active_goal_hears_nothing(self) -> None:
        self.submit()
        self.start_goal()
        self.assertNotIn(self.HINT, self.submit())
        self.assertNotIn(self.HINT, self.submit(goal_id="g1"))

    def test_another_sessions_goal_does_not_silence_it(self) -> None:
        self.start_goal("another-session")
        self.submit()
        self.assertIn(self.HINT, self.submit())

    def test_a_failure_to_look_does_not_fail_a_submitted_task(self) -> None:
        self.submit()
        with mock.patch.object(goals, "has_active_goal", side_effect=OSError("gone")):
            self.assertNotIn(self.HINT, self.submit())


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
