from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import io
import json
import os
import shlex
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from harness_reins import cli
from harness_reins.commands import callbacks as callback_commands
from harness_reins.commands import goals as goal_commands
from harness_reins.commands import work as work_commands
from harness_reins.install import services
from harness_reins.install import skills as skill_install
from harness_reins.tower import environment as tower_environment
from harness_reins.tower import main as tower
from harness_reins.engine import dispatch
from harness_reins.engine import goals
from harness_reins.engine import recovery
from harness_reins.engine import tasks as managed_tasks
from harness_reins.engine import worker as task_worker
from harness_reins.engine import delivery as delivery_engine
from harness_reins.engine import reminders as reminder_policy
from harness_reins.engine import resume_environment
from harness_reins.harness.codex import app_server
from harness_reins.owners import host
from harness_reins import runtime as reins_runtime
from harness_reins.store import records as queue_store
from harness_reins.system import shell
from harness_reins.store import layout
from harness_reins.core import ids
import reins_records


def assert_private_file(test: unittest.TestCase, path: Path) -> None:
    if os.name == "nt":
        # Use actual DACL inspection, not chmod's unrelated Windows mode bits.
        from test_windows_io import WindowsIOTests
        WindowsIOTests().assert_private_acl(path)
    else:
        test.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


_PYTHON_FIXTURES: set[str] = set()


def executable_python_fixture(script: Path) -> Path:
    """Make the same local, model-free Python fixture executable on both OSes."""
    if os.name == "nt":
        _PYTHON_FIXTURES.add(str(script))
        return script
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def patch_fixture_commands(test: unittest.TestCase) -> None:
    if os.name != "nt":
        return
    for owner, name in ((managed_tasks, "agent_wrapped_command"), (delivery_engine, "resume_command")):
        original = getattr(owner, name)

        def fixture_command(*args, _original=original, **kwargs):
            command = _original(*args, **kwargs)
            return [sys.executable, *command] if command[0] in _PYTHON_FIXTURES else command

        patcher = mock.patch.object(owner, name, side_effect=fixture_command)
        patcher.start()
        test.addCleanup(patcher.stop)


def patch_inprocess_delivery_worker(test: unittest.TestCase) -> None:
    if os.name == "nt":
        # These tests invoke the worker in the test runner, using local pipe fds.
        # Real inherited native handles and Jobs are covered by process tests.
        for patcher in (
            mock.patch.object(cli.windows_process, "inherited_lock_fds", side_effect=lambda:
                              json.loads(os.environ.get("CODEX_LONG_TASK_DELIVERY_LOCK_FDS", "[]"))),
            mock.patch.object(cli.windows_process, "enter_worker_job"),
        ):
            patcher.start()
            test.addCleanup(patcher.stop)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        # This legacy fixture models screen selection; Windows IO and process
        # adapters still run natively because os.name remains unchanged.
        if os.name == "nt":
            platform = mock.patch.object(sys, "platform", "linux")
            platform.start()
            self.addCleanup(platform.stop)
        patch_fixture_commands(self)
        patch_inprocess_delivery_worker(self)

    def tearDown(self) -> None:
        for delivery in list(delivery_engine._BACKGROUND_RESUMES.values()):
            delivery_engine.stop_resume_process(delivery.process)
        delivery_engine._BACKGROUND_RESUMES.clear()

    @staticmethod
    def _env_without_claude(**extra: str) -> "mock._patch_dict":
        """Simulate a Codex-only environment even when tests run inside Claude Code or PI."""
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID",
                           "PI_SESSION_ID", "PI_SESSION_FILE", "PI_CODING_AGENT")
        }
        env.update(extra)
        return mock.patch.dict(os.environ, env, clear=True)

    @staticmethod
    def _goal_plan(directory: str, *, status: str = "pending", name: str = "GOAL.yaml") -> Path:
        path = Path(directory) / name
        path.write_text(
            textwrap.dedent(
                f"""\
                version: 1
                revision: 1
                goal: Finish the test goal
                path:
                  - id: finish
                    title: Finish and verify the work
                    status: {status}
                """
            ),
            encoding="utf-8",
        )
        return path

    def test_run_defaults_to_durable_submission_without_starting_task_in_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])

            with (
                mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"),
                mock.patch.object(cli.subprocess, "run") as wrapped,
            ):
                self.assertEqual(work_commands.run(args), 0)

            wrapped.assert_not_called()
            manifests = list(layout.managed_tasks_root(root).glob("*/task.json"))
            self.assertEqual(len(manifests), 1)
            task = managed_tasks.load_managed_task(manifests[0])
            self.assertEqual(task["state"], "submitted")
            self.assertEqual(task["wrapped_command"], args.wrapped_command)
            self.assertEqual(task["id"], task["screen_session"].removeprefix("reins-"))
            self.assertTrue(Path(str(task["environment_path"])).exists())
            self.assertFalse(list((root / "pending").glob("*.json")))

    def test_default_run_requires_screen_and_does_not_persist_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])

            with mock.patch.object(managed_tasks, "screen_binary", return_value=None):
                self.assertEqual(work_commands.run(args), 125)

            self.assertFalse(layout.managed_tasks_root(root).exists())

    def test_done_defaults_to_daemon_queue_without_requiring_screen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])
            args.exit_code = 0

            with (
                mock.patch.object(managed_tasks, "screen_binary", return_value=None),
                mock.patch.object(cli.subprocess, "run") as direct_resume,
            ):
                self.assertEqual(work_commands.done(args), 0)

            direct_resume.assert_not_called()
            callbacks = list((root / "pending").glob("*.json"))
            self.assertEqual(len(callbacks), 1)
            callback = dispatch.load_request(callbacks[0])
            self.assertEqual(callback["target"], {"kind": "session", "session": "test-thread"})
            self.assertEqual(callbacks[0].stem, callback["id"])

    def test_run_dry_run_previews_without_screen_or_queue_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])
            args.dry_run = True
            output = io.StringIO()

            with (
                mock.patch.object(managed_tasks, "screen_binary", return_value=None),
                mock.patch.object(cli.subprocess, "run") as wrapped,
                mock.patch("sys.stdout", output),
            ):
                self.assertEqual(work_commands.run(args), 0)

            wrapped.assert_not_called()
            self.assertFalse(layout.managed_tasks_root(root).exists())
            self.assertIn("daemon -> auto backend -> independent worker -> task", output.getvalue())

    def test_agent_parser_matches_run_style_and_preserves_prompt(self) -> None:
        with (
            mock.patch.object(
                sys,
                "argv",
                [
                    "reins",
                    "agent",
                    "claude",
                    "--session",
                    "parent-thread",
                    "--task",
                    "review parser",
                    "--",
                    "Inspect parser.py and report defects.",
                ],
            ),
            mock.patch.object(work_commands, "agent", return_value=0) as parsed_agent,
        ):
            self.assertEqual(cli.main(), 0)

        args = parsed_agent.call_args.args[0]
        self.assertEqual(args.agent_worker, "claude")
        self.assertEqual(args.task, "review parser")
        self.assertEqual(args.agent_prompt, ["--", "Inspect parser.py and report defects."])

    def test_codex_agent_submission_owns_prompt_and_result_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._agent_args(tmp, root, "codex", "Fix the confirmed parser defect.")
            with (
                mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"),
                mock.patch.dict(
                    os.environ,
                    {"REINS_CODEX_BIN": "/opt/codex/bin/codex"},
                    clear=False,
                ),
            ):
                self.assertEqual(work_commands.agent(args), 0)

            task_path = next(layout.managed_tasks_root(root).glob("*/task.json"))
            task = managed_tasks.load_managed_task(task_path)
            prompt_path = Path(str(task["agent_prompt_path"]))
            result_path = Path(str(task["agent_result_path"]))
            self.assertEqual(task["task_kind"], "agent")
            self.assertEqual(task["agent_worker"], "codex")
            self.assertEqual(prompt_path.read_text(encoding="utf-8"), reins_records.agent_prompt("Fix the confirmed parser defect.", task))
            assert_private_file(self, prompt_path)
            self.assertEqual(result_path.parent, task_path.parent)
            self.assertEqual(
                task["wrapped_command"],
                [
                    "/opt/codex/bin/codex",
                    "exec",
                    "--json",
                    "--skip-git-repo-check",
                    "-C",
                    tmp,
                    "-s",
                    "workspace-write",
                    "-c",
                    'approvals_reviewer="auto_review"',
                    "-c",
                    'approval_policy="on-request"',
                    "-o",
                    str(result_path),
                    "-",
                ],
            )
            self.assertNotIn("Fix the confirmed parser defect.", task_path.read_text(encoding="utf-8"))

    def test_codex_agent_worker_reads_prompt_and_saves_final_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            fake_codex = self._fake_child_agent(Path(tmp))
            args = self._agent_args(tmp, root, "codex", "Implement the parser fix and run tests.")
            with (
                mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"),
                mock.patch.dict(
                    os.environ,
                    {
                        "REINS_CODEX_BIN": str(fake_codex),
                        "CODEX_THREAD_ID": "parent-codex-thread",
                        "CLAUDE_CODE_SESSION_ID": "stale-claude-thread",
                        "CLAUDECODE": "1",
                        "REINS_TEST_SENTINEL": "codex-submission-environment",
                    },
                    clear=False,
                ),
            ):
                self.assertEqual(work_commands.agent(args), 0)

            task_path = next(layout.managed_tasks_root(root).glob("*/task.json"))
            task = managed_tasks.load_managed_task(task_path)
            task["state"] = "launching"
            managed_tasks.write_managed_task(root, task)
            worker = argparse.Namespace(task_file=str(task_path), token=task["token"])
            with mock.patch.dict(os.environ, {"STY": "screen-session"}, clear=True):
                self.assertEqual(task_worker.run_screen_worker(worker), 0)

            task = managed_tasks.load_managed_task(task_path)
            result_path = Path(str(task["agent_result_path"]))
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["prompt"], reins_records.agent_prompt("Implement the parser fix and run tests.", task))
            self.assertEqual(result["sentinel"], "codex-submission-environment")
            self.assertIsNone(result["codex_thread_id"])
            self.assertIsNone(result["claude_session_id"])
            self.assertIsNone(result["claudecode"])
            assert_private_file(self, result_path)
            callback = dispatch.load_request(root / "pending" / f"{ids.callback_id(task['id'])}.json")
            self.assertEqual(callback["agent_worker"], "codex")
            self.assertIn("Child agent: Codex", Path(callback["prompt_details_path"]).read_text())
            self.assertIn(result_path.name, callback["prompt"])
            self.assertIn(str(result_path.parent), callback["prompt"])

    def test_claude_agent_injects_submitter_environment_without_parent_session_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            fake_claude = self._fake_child_agent(Path(tmp))
            args = self._agent_args(tmp, root, "claude", "Audit the parser and return evidence.")
            submit_environment = {
                "REINS_CLAUDE_BIN": str(fake_claude),
                "CODEX_THREAD_ID": "codex-parent",
                "CLAUDE_CODE_SESSION_ID": "claude-parent",
                "CLAUDECODE": "1",
                "CLAUDE_CONFIG_DIR": str(Path(tmp) / "claude-config"),
                "ANTHROPIC_API_KEY": "secret-sentinel-value",
                "ANTHROPIC_BASE_URL": "https://example.invalid/anthropic",
                "REINS_TEST_SENTINEL": "submission-environment",
            }
            with (
                mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"),
                mock.patch.dict(os.environ, submit_environment, clear=False),
            ):
                self.assertEqual(work_commands.agent(args), 0)

            task_path = next(layout.managed_tasks_root(root).glob("*/task.json"))
            task = managed_tasks.load_managed_task(task_path)
            environment_path = Path(str(task["environment_path"]))
            assert_private_file(self, environment_path)
            self.assertNotIn("--bare", task["wrapped_command"])
            self.assertEqual(
                task["wrapped_command"],
                [
                    *([sys.executable] if os.name == "nt" else []), str(fake_claude),
                    "-p",
                    "--session-id",
                    task["node"]["planned_session"],
                    "--permission-mode",
                    "auto",
                    "--output-format",
                    "text",
                ],
            )
            task["state"] = "launching"
            managed_tasks.write_managed_task(root, task)
            worker = argparse.Namespace(task_file=str(task_path), token=task["token"])
            output = io.StringIO()
            with (
                mock.patch.dict(
                    os.environ,
                    {"STY": "screen-session", "CLAUDECODE": "daemon-environment-must-not-leak"},
                    clear=True,
                ),
                mock.patch("sys.stdout", output),
            ):
                self.assertEqual(task_worker.run_screen_worker(worker), 0)

            task = managed_tasks.load_managed_task(task_path)
            result_path = Path(str(task["agent_result_path"]))
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["prompt"], reins_records.agent_prompt("Audit the parser and return evidence.", task))
            self.assertEqual(result["sentinel"], "submission-environment")
            self.assertTrue(result["api_key_present"])
            self.assertEqual(result["base_url"], "https://example.invalid/anthropic")
            self.assertEqual(result["config_dir"], str(Path(tmp) / "claude-config"))
            self.assertIsNone(result["codex_thread_id"])
            self.assertIsNone(result["claude_session_id"])
            self.assertIsNone(result["claudecode"])
            assert_private_file(self, result_path)
            self.assertFalse(environment_path.exists())
            self.assertNotIn("secret-sentinel-value", output.getvalue())
            callback = dispatch.load_request(root / "pending" / f"{ids.callback_id(task['id'])}.json")
            self.assertEqual(callback["agent"], "codex")
            self.assertEqual(callback["agent_worker"], "claude")
            details = Path(callback["prompt_details_path"]).read_text()
            self.assertIn(f"Agent result: {result_path}", details)
            self.assertIn("Child agent: Claude Code", details)
            self.assertIn(result_path.name, callback["prompt"])
            self.assertIn(str(result_path.parent), callback["prompt"])
            self.assertNotIn("secret-sentinel-value", details)
            self.assertNotIn("secret-sentinel-value", json.dumps(callback))

    def test_via_daemon_is_hidden_compatibility_flag(self) -> None:
        help_output = io.StringIO()
        with (
            mock.patch.object(sys, "argv", ["reins", "run", "--help"]),
            mock.patch("sys.stdout", help_output),
            self.assertRaises(SystemExit) as help_exit,
        ):
            cli.main()
        self.assertEqual(help_exit.exception.code, 0)
        self.assertNotIn("--via-daemon", help_output.getvalue())

        with (
            mock.patch.object(
                sys,
                "argv",
                ["reins", "run", "--via-daemon", "--session", "compat-session", "--", "true"],
            ),
            mock.patch.object(work_commands, "run", return_value=0) as parsed_run,
        ):
            self.assertEqual(cli.main(), 0)
        self.assertTrue(parsed_run.call_args.args[0].via_daemon)

    def test_screen_worker_parser_accepts_dash_prefixed_token(self) -> None:
        with (
            mock.patch.object(
                sys,
                "argv",
                ["reins", "_screen-worker", "--task-file", "/tmp/task.json", "--token=-abc"],
            ),
            mock.patch.object(task_worker, "run_screen_worker", return_value=0) as worker,
        ):
            self.assertEqual(cli.main(), 0)

        self.assertEqual(worker.call_args.args[0].token, "-abc")

    def test_submitted_worker_token_has_safe_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])
            with (
                mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"),
                mock.patch.object(cli.secrets, "token_urlsafe", return_value="-abc"),
            ):
                self.assertEqual(work_commands.run(args), 0)

            task_path = next(layout.managed_tasks_root(root).glob("*/task.json"))
            self.assertEqual(managed_tasks.load_managed_task(task_path)["token"], "reins_-abc")

    def test_daemon_launches_submitted_task_through_screen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])
            with mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"):
                self.assertEqual(work_commands.run(args), 0)
            task_path = next(layout.managed_tasks_root(root).glob("*/task.json"))
            task = managed_tasks.load_managed_task(task_path)
            task["token"] = "-abc"
            managed_tasks.write_managed_task(root, task)

            completed = subprocess.CompletedProcess(["screen"], 0)
            with (
                mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"),
                mock.patch.object(managed_tasks, "screen_owner_state", return_value=cli.OwnerState.ABSENT),
                mock.patch("harness_reins.owners.screen.screen_binary", return_value="/usr/bin/screen"),
                mock.patch.object(reins_runtime, "console_script_path", return_value="/usr/bin/reins"),
                mock.patch.object(cli.subprocess, "run", return_value=completed) as launch,
            ):
                self.assertEqual(managed_tasks.recover_managed_tasks(root), 1)

            command = launch.call_args.args[0]
            self.assertEqual(command[0], "/usr/bin/screen")
            self.assertEqual(command[command.index("-dmS") + 1], f"reins-{task_path.parent.name}")
            self.assertEqual("-Logfile" in command, sys.platform != "darwin")
            self.assertIn("_screen-worker", command)
            self.assertIn("--token=-abc", command)
            self.assertNotIn("--token", command)
            task = managed_tasks.load_managed_task(task_path)
            self.assertEqual(task["state"], "launching")
            self.assertEqual(task["launch_attempt_count"], 1)

    def test_launching_task_waits_for_worker_handshake_before_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])
            with mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"):
                self.assertEqual(work_commands.run(args), 0)
            task_path = next(layout.managed_tasks_root(root).glob("*/task.json"))

            completed = subprocess.CompletedProcess(["screen"], 0)
            with (
                mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"),
                mock.patch.object(managed_tasks, "screen_owner_state", return_value=cli.OwnerState.ABSENT),
                mock.patch("harness_reins.owners.screen.screen_binary", return_value="/usr/bin/screen"),
                mock.patch.object(cli.subprocess, "run", return_value=completed) as launch,
                mock.patch.object(managed_tasks, "MANAGED_WORKER_HANDSHAKE_SECONDS", 60.0),
            ):
                self.assertEqual(managed_tasks.recover_managed_tasks(root), 1)
                self.assertEqual(managed_tasks.recover_managed_tasks(root), 0)

            self.assertEqual(launch.call_count, 1)
            task = managed_tasks.load_managed_task(task_path)
            self.assertEqual(task["state"], "launching")
            self.assertEqual(task["launch_attempt_count"], 1)

    def test_worker_handshake_failure_has_bounded_retries_and_one_callback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])
            with mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"):
                self.assertEqual(work_commands.run(args), 0)
            task_path = next(layout.managed_tasks_root(root).glob("*/task.json"))

            completed = subprocess.CompletedProcess(["screen"], 0)
            with (
                mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"),
                mock.patch.object(managed_tasks, "screen_owner_state", return_value=cli.OwnerState.ABSENT),
                mock.patch("harness_reins.owners.screen.screen_binary", return_value="/usr/bin/screen"),
                mock.patch.object(cli.subprocess, "run", return_value=completed) as launch,
                mock.patch.object(managed_tasks, "MANAGED_WORKER_HANDSHAKE_SECONDS", 0.0),
                mock.patch.object(managed_tasks, "MANAGED_LAUNCH_MAX_ATTEMPTS", 2),
            ):
                self.assertEqual(managed_tasks.recover_managed_tasks(root), 1)
                self.assertEqual(managed_tasks.recover_managed_tasks(root), 1)

                task = managed_tasks.load_managed_task(task_path)
                self.assertEqual(task["state"], "submitted")
                task["next_launch_at"] = 0.0
                managed_tasks.write_managed_task(root, task)

                self.assertEqual(managed_tasks.recover_managed_tasks(root), 1)
                self.assertEqual(managed_tasks.recover_managed_tasks(root), 1)
                self.assertEqual(managed_tasks.recover_managed_tasks(root), 0)

            self.assertEqual(launch.call_count, 2)
            task = managed_tasks.load_managed_task(task_path)
            self.assertEqual(task["state"], "launch_failed")
            self.assertEqual(task["launch_attempt_count"], 2)
            self.assertIn("worker_start_handshake_failed_after_2_attempts", task["recovery_reason"])
            self.assertNotIn("token", task)
            self.assertFalse(Path(str(task["environment_path"])).exists())
            callbacks = list((root / "pending").glob("*.json"))
            self.assertEqual(len(callbacks), 1)
            callback = dispatch.load_request(callbacks[0])
            self.assertEqual(callback["managed_task_state"], "launch_failed")
            self.assertIn("not automatically restarted", callback["prompt"])

    def test_legacy_launching_task_is_not_automatically_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])
            with mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"):
                self.assertEqual(work_commands.run(args), 0)
            task_path = next(layout.managed_tasks_root(root).glob("*/task.json"))
            task = managed_tasks.load_managed_task(task_path)
            task.update(
                {
                    "state": "launching",
                    "token": "-legacy-token",
                    "screen_submitted_at": 0.0,
                }
            )
            task.pop("launch_attempt_count", None)
            managed_tasks.write_managed_task(root, task)

            with (
                mock.patch.object(host, "current_machine_id", return_value=str(task["machine_id"])),
                mock.patch.object(managed_tasks, "screen_owner_state", return_value=cli.OwnerState.ABSENT),
                mock.patch.object(cli.subprocess, "run") as relaunch,
            ):
                self.assertEqual(managed_tasks.recover_managed_tasks(root), 1)
                self.assertEqual(managed_tasks.recover_managed_tasks(root), 0)

            relaunch.assert_not_called()
            task = managed_tasks.load_managed_task(task_path)
            self.assertEqual(task["state"], "launch_failed")
            self.assertIn("legacy_attempt_count_unknown", task["recovery_reason"])
            self.assertNotIn("token", task)
            self.assertEqual(len(list((root / "pending").glob("*.json"))), 1)

    def test_screen_worker_records_result_and_queues_same_id_callback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            run_args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])
            with mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"):
                self.assertEqual(work_commands.run(run_args), 0)
            task_path = next(layout.managed_tasks_root(root).glob("*/task.json"))
            task = managed_tasks.load_managed_task(task_path)
            task["state"] = "launching"
            managed_tasks.write_managed_task(root, task)
            worker = argparse.Namespace(task_file=str(task_path), token=task["token"])

            def complete(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                running = managed_tasks.load_managed_task(task_path)
                self.assertEqual(running["state"], "running")
                self.assertIn("worker_ready_at", running)
                return subprocess.CompletedProcess(command, 7)

            with (
                mock.patch.dict(os.environ, {"STY": "screen-session"}, clear=True),
                mock.patch.object(cli.subprocess, "run", side_effect=complete),
            ):
                self.assertEqual(task_worker.run_screen_worker(worker), 7)

            task = managed_tasks.load_managed_task(task_path)
            self.assertEqual(task["state"], "completed")
            self.assertEqual(task["exit_code"], 7)
            self.assertNotIn("token", task)
            self.assertFalse(Path(str(task["environment_path"])).exists())
            callback = dispatch.load_request(root / "pending" / f"{ids.callback_id(task['id'])}.json")
            self.assertEqual(callback["id"], ids.callback_id(task["id"]))
            self.assertEqual(callback["exit_code"], 7)
            self.assertIn(f"Task log: {task['log_path']}", Path(callback["prompt_details_path"]).read_text())
            self.assertIn(Path(task["log_path"]).name, callback["prompt"])
            self.assertIn(str(Path(task["log_path"]).parent), callback["prompt"])

    def test_host_reboot_recovers_original_agent_without_restarting_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            run_args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])
            run_args.agent = "claude"
            with mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"):
                self.assertEqual(work_commands.run(run_args), 0)
            task_path = next(layout.managed_tasks_root(root).glob("*/task.json"))
            task = managed_tasks.load_managed_task(task_path)
            task.update({"state": "running", "boot_id": "old-boot", "agent": "claude"})
            managed_tasks.write_managed_task(root, task)

            with (
                mock.patch.object(host, "current_boot_id", return_value="new-boot"),
                mock.patch.object(host, "current_machine_id", return_value=str(task["machine_id"])),
                mock.patch.object(managed_tasks, "screen_owner_state", return_value=cli.OwnerState.ABSENT),
                mock.patch.object(cli.subprocess, "run") as restart,
            ):
                self.assertEqual(managed_tasks.recover_managed_tasks(root), 1)

            restart.assert_not_called()
            task = managed_tasks.load_managed_task(task_path)
            self.assertEqual(task["recovery_reason"], "interrupted_by_host_reboot")
            callback = dispatch.load_request(root / "pending" / f"{ids.callback_id(task['id'])}.json")
            self.assertEqual(callback["agent"], "claude")
            self.assertEqual(callback["target"], {"kind": "session", "session": "test-thread"})
            self.assertIn("not automatically restarted", callback["prompt"])
            self.assertIn("checkpoints", Path(callback["prompt_details_path"]).read_text())
            self.assertIn("read before acting", callback["prompt"])

    def test_daemon_restart_reconnects_live_screen_without_duplicate_launch_or_callback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            run_args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])
            with mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"):
                self.assertEqual(work_commands.run(run_args), 0)
            task_path = next(layout.managed_tasks_root(root).glob("*/task.json"))
            task = managed_tasks.load_managed_task(task_path)
            task.update({"state": "running", "boot_id": host.current_boot_id()})
            managed_tasks.write_managed_task(root, task)

            with (
                mock.patch.object(host, "current_machine_id", return_value=str(task["machine_id"])),
                mock.patch.object(managed_tasks, "screen_owner_state", return_value=cli.OwnerState.ALIVE),
                mock.patch.object(cli.subprocess, "run") as relaunch,
            ):
                self.assertEqual(managed_tasks.recover_managed_tasks(root), 0)

            relaunch.assert_not_called()
            self.assertFalse((root / "pending" / f"{ids.callback_id(task['id'])}.json").exists())

    def test_cross_host_record_is_preserved_without_launch_or_callback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            run_args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])
            with mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"):
                self.assertEqual(work_commands.run(run_args), 0)
            task_path = next(layout.managed_tasks_root(root).glob("*/task.json"))

            with (
                mock.patch.object(host, "current_machine_id", return_value="different-machine"),
                mock.patch.object(cli.subprocess, "run") as launch,
            ):
                self.assertEqual(managed_tasks.recover_managed_tasks(root), 1)

            launch.assert_not_called()
            task = managed_tasks.load_managed_task(task_path)
            self.assertEqual(task["state"], "foreign_host")
            self.assertEqual(task["recovery_reason"], "cross_host_recovery_unsupported")
            self.assertFalse((root / "pending" / f"{ids.callback_id(task['id'])}.json").exists())

    def test_reboot_recovery_callback_ack_does_not_complete_bound_goal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            start = argparse.Namespace(
                queue_dir=str(root),
                id="stage-goal",
                session="test-thread",
                last=False,
                agent="codex",
                cwd=tmp,
                task="finish training stage",
                plan_file=str(self._goal_plan(tmp)),
                idle_seconds=1.0,
            )
            self.assertEqual(goal_commands.goal_start(start), 0)
            run_args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])
            run_args.goal_id = "stage-goal"
            with mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"):
                self.assertEqual(work_commands.run(run_args), 0)
            task_path = next(layout.managed_tasks_root(root).glob("*/task.json"))
            task = managed_tasks.load_managed_task(task_path)
            task.update({"state": "running", "boot_id": "old-boot"})
            managed_tasks.write_managed_task(root, task)

            with (
                mock.patch.object(host, "current_boot_id", return_value="new-boot"),
                mock.patch.object(host, "current_machine_id", return_value=str(task["machine_id"])),
                mock.patch.object(managed_tasks, "screen_owner_state", return_value=cli.OwnerState.ABSENT),
            ):
                self.assertEqual(managed_tasks.recover_managed_tasks(root), 1)

            callback = dispatch.load_request(root / "pending" / f"{ids.callback_id(task['id'])}.json")
            self.assertEqual(callback["goal_id"], "stage-goal")
            self.assertEqual(
                callback_commands.ack(argparse.Namespace(queue_dir=str(root), id=callback["id"], message="recovery inspected")),
                0,
            )
            goal = goals.load_goal(root, "stage-goal")
            self.assertEqual(goal["state"], "active")
            reminder_time = float(callback["created_at"]) + 2.0
            with mock.patch.object(cli.time, "time", return_value=reminder_time):
                self.assertTrue(goals.process_goal_reminders(root))
            reminders = [
                dispatch.load_request(path)
                for path in (root / "pending").glob("*.json")
                if path.stem != ids.callback_id(task["id"])
            ]
            self.assertEqual(len(reminders), 1)
            self.assertEqual(reminders[0]["id"], "g-stage-goal-c1")
            self.assertTrue(reminders[0]["goal_reminder"])

    def test_active_owner_lock_blocks_premature_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])
            request = recovery.make_active_request(args, time.time())
            request["launch_phase"] = "launch_committed"
            request_id = str(request["id"])
            lock = queue_store.acquire_owner_lock(root, request_id, blocking=True)
            self.assertIsNotNone(lock)
            queue_store.write_request(layout.request_path(root, layout.ACTIVE_STATE, request_id), request)

            self.assertEqual(recovery.recover_active(root), 0)
            self.assertTrue(layout.request_path(root, layout.ACTIVE_STATE, request_id).exists())

            queue_store.release_owner_lock(lock, remove=False)
            self.assertEqual(recovery.recover_active(root), 1)
            recovered = json.loads(layout.request_path(root, "pending", request_id).read_text(encoding="utf-8"))
            self.assertEqual(recovered["outcome"], "unknown")
            details = Path(recovered["prompt_details_path"]).read_text()
            self.assertIn("may still be running", details)
            self.assertIn("exit status are unknown", details)
            self.assertIn("unknown", recovered["prompt"])
            self.assertIn("read before acting", recovered["prompt"])

    def test_daemon_singleton_uses_stable_non_removed_lock_inode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            first = queue_store.acquire_owner_lock(root, "daemon-singleton", blocking=False)
            self.assertIsNotNone(first)
            self.assertIsNone(queue_store.acquire_owner_lock(root, "daemon-singleton", blocking=False))
            lock_path = layout.owner_lock_path(root, "daemon-singleton")

            queue_store.release_owner_lock(first, remove=False)
            self.assertTrue(lock_path.exists())
            handoff = queue_store.acquire_owner_lock(root, "daemon-singleton", blocking=False)
            self.assertIsNotNone(handoff)
            queue_store.release_owner_lock(handoff, remove=False)
            self.assertTrue(lock_path.exists())

    def test_submission_persistence_failure_refuses_to_launch_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])

            with (
                mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"),
                mock.patch.object(queue_store, "write_request", side_effect=OSError("disk unavailable")),
                mock.patch.object(cli.subprocess, "run") as wrapped,
            ):
                self.assertEqual(dispatch.submit_managed_run(args), 125)

            wrapped.assert_not_called()

    def test_claude_submission_reserves_its_callback_for_the_session_waiter(self) -> None:
        claude_session = cli.claude_session
        inside = {"CLAUDE_CODE_SESSION_ID": "test-thread", "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "",
                  claude_session.LIVE_WAIT_GRACE_ENV: ""}
        cases = ((inside, True), (dict(inside, CLAUDE_CODE_DISABLE_BACKGROUND_TASKS="1"), False),
                 (dict(inside, **{claude_session.LIVE_WAIT_GRACE_ENV: "0"}), False),
                 # A script that names the session with --session gets the daemon at once.
                 (dict(inside, CLAUDE_CODE_SESSION_ID=""), False),
                 (dict(inside, CLAUDE_CODE_SESSION_ID="other-session"), False))
        for extra, reserved in cases:
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "queue"
                args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])
                args.agent = "claude"
                errors = io.StringIO()
                with mock.patch.dict(os.environ, dict(extra, **{layout.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "locks")})), \
                        mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"), \
                        contextlib.redirect_stderr(errors):
                    self.assertEqual(dispatch.submit_managed_run(args), 0)
                    held = claude_session.live_watcher_is_held(
                        {"agent": "claude", "target": {"kind": "session", "session": "test-thread"}})
                self.assertEqual(held, reserved)
                self.assertEqual("then resumed headless" in errors.getvalue(), reserved)

    def test_screen_worker_launch_failure_queues_unknown_callback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._run_args(tmp, root, ["missing-command"])
            with mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"):
                self.assertEqual(dispatch.submit_managed_run(args), 0)
            task_path = next(layout.managed_tasks_root(root).glob("*/task.json"))
            task = managed_tasks.load_managed_task(task_path)
            task["state"] = "launching"
            managed_tasks.write_managed_task(root, task)
            worker = argparse.Namespace(task_file=str(task_path), token=task["token"])

            with (
                mock.patch.dict(os.environ, {"STY": "screen-session"}, clear=True),
                mock.patch.object(cli.subprocess, "run", side_effect=OSError("exec failed")),
            ):
                self.assertEqual(task_worker.run_screen_worker(worker), 127)

            task = managed_tasks.load_managed_task(task_path)
            self.assertEqual(task["state"], "interrupted")
            callback = dispatch.load_request(root / "pending" / f"{ids.callback_id(task['id'])}.json")
            self.assertEqual(callback["outcome"], "unknown")
            self.assertIn("wrapped_command_launch_failed", callback["prompt"])

    def run_command_task(self, tmp: str, root: Path, command: list[str], stdin) -> dict:
        args = self._run_args(tmp, root, command)
        with mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"), \
                mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dispatch.submit_managed_run(args), 0)
        task_path = next(layout.managed_tasks_root(root).glob("*/task.json"))
        task = managed_tasks.load_managed_task(task_path)
        task["state"] = "launching"
        managed_tasks.write_managed_task(root, task)
        worker = argparse.Namespace(task_file=str(task_path), token=task["token"])
        # A real file: on Windows the worker hands its own stdout to the task.
        with tempfile.TemporaryFile("w+") as log, mock.patch.dict(os.environ, {"STY": "screen-session"}, clear=True), \
                contextlib.redirect_stdout(log):
            self.assertEqual(task_worker.run_screen_worker(worker), 0)
        return managed_tasks.load_managed_task(task_path)

    @unittest.skipIf(os.name == "nt", "piped_input does not read Windows pipes yet (no select())")
    def test_input_piped_into_atc_run_becomes_the_task_stdin(self) -> None:
        # reins run -- python3 - <<EOF: the heredoc reaches reins, so reins must hand it to the task.
        read_end, write_end = os.pipe()
        with tempfile.TemporaryDirectory() as tmp, os.fdopen(read_end, "rb") as piped:
            output = Path(tmp) / "out.txt"
            os.write(write_end, f"open({str(output)!r}, 'w').write(str(6 * 7))\n".encode())
            os.close(write_end)
            task = self.run_command_task(tmp, Path(tmp) / "queue", [sys.executable, "-"], piped)
            self.assertEqual(output.read_text(), "42")
            assert_private_file(self, Path(task["stdin_path"]))

    def test_task_without_piped_input_reads_an_empty_stdin_instead_of_waiting(self) -> None:
        read_end, write_end = os.pipe()  # open and silent, like an agent's tool shell
        try:
            with tempfile.TemporaryDirectory() as tmp, os.fdopen(read_end, "rb") as silent:
                output = Path(tmp) / "out.txt"
                script = f"import sys; open({str(output)!r}, 'w').write(repr(sys.stdin.read()))"
                task = self.run_command_task(tmp, Path(tmp) / "queue", [sys.executable, "-c", script], silent)
                self.assertNotIn("stdin_path", task)
                self.assertEqual(output.read_text(), "''")
        finally:
            os.close(write_end)

    def test_recover_active_preserves_stranded_completed_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._run_args(tmp, root, [sys.executable, "-c", "raise SystemExit(9)"])
            args.command = shlex.join(args.wrapped_command)
            args.exit_code = 9
            request = recovery.make_active_request(args, time.time() - 3.0)
            request.update(
                {
                    "launch_phase": "launch_committed",
                    "outcome": "completed",
                    "exit_code": 9,
                    "completed_at": time.time(),
                }
            )
            dispatch.prepare_request_for_queue(root, request, dispatch.build_prompt(args, 3.0))
            request_id = str(request["id"])
            queue_store.write_request(layout.request_path(root, layout.ACTIVE_STATE, request_id), request)

            self.assertEqual(recovery.recover_active(root), 1)
            recovered = json.loads(layout.request_path(root, "pending", request_id).read_text(encoding="utf-8"))
            self.assertEqual(recovered["outcome"], "completed")
            self.assertEqual(recovered["exit_code"], 9)
            self.assertIn("Exit code: 9", Path(recovered["prompt_details_path"]).read_text())
            self.assertIn("Result: exited 9", recovered["prompt"])

    def test_recover_active_discards_definitely_unlaunched_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])
            request = recovery.make_active_request(args, time.time())
            request_id = str(request["id"])
            queue_store.write_request(layout.request_path(root, layout.ACTIVE_STATE, request_id), request)

            self.assertEqual(recovery.recover_active(root), 0)
            self.assertFalse(layout.request_path(root, layout.ACTIVE_STATE, request_id).exists())
            self.assertFalse(layout.request_path(root, "pending", request_id).exists())

    def test_make_request_binds_codex_thread_id_by_default(self) -> None:
        args = argparse.Namespace(
            cwd="/tmp",
            session=None,
            last=False,
            approvals_reviewer="auto_review",
            approval_policy="on-request",
            sandbox_mode="workspace-write",
        )

        with self._env_without_claude(CODEX_THREAD_ID="thread-from-env"):
            request = dispatch.make_request(args, "wake up")

        self.assertEqual(request["target"], {"kind": "session", "session": "thread-from-env"})
        self.assertEqual(request["origin"]["binding"], "CODEX_THREAD_ID")

    def test_make_request_refuses_ambiguous_target(self) -> None:
        args = argparse.Namespace(
            cwd="/tmp",
            session=None,
            last=False,
            approvals_reviewer="auto_review",
            approval_policy="on-request",
            sandbox_mode="workspace-write",
        )

        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "CODEX_THREAD_ID is unset"):
                dispatch.make_request(args, "wake up")

    def test_explicit_session_overrides_environment(self) -> None:
        args = argparse.Namespace(
            cwd="/tmp",
            session="explicit-thread",
            last=False,
            approvals_reviewer="auto_review",
            approval_policy="on-request",
            sandbox_mode="workspace-write",
        )

        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": "thread-from-env"}, clear=False):
            request = dispatch.make_request(args, "wake up")

        self.assertEqual(request["target"], {"kind": "session", "session": "explicit-thread"})
        self.assertEqual(request["origin"]["binding"], "--session")

    def test_bound_target_is_snapshotted_before_environment_changes(self) -> None:
        args = argparse.Namespace(
            cwd="/tmp",
            session=None,
            last=False,
            approvals_reviewer="auto_review",
            approval_policy="on-request",
            sandbox_mode="workspace-write",
        )

        with self._env_without_claude(CODEX_THREAD_ID="launch-thread"):
            dispatch.bind_target(args)
        with self._env_without_claude(CODEX_THREAD_ID="later-thread"):
            request = dispatch.make_request(args, "wake up")

        self.assertEqual(request["target"], {"kind": "session", "session": "launch-thread"})
        self.assertEqual(request["origin"]["binding"], "CODEX_THREAD_ID")

    def test_resume_command_uses_auto_review_by_default(self) -> None:
        command = delivery_engine.resume_command(
            {
                "target": {"kind": "session", "session": "session-1"},
                "cwd": "/tmp",
                "prompt": "hello",
            }
        )

        self.assertIn("-c", command)
        self.assertIn('approvals_reviewer="auto_review"', command)
        self.assertIn('approval_policy="on-request"', command)
        self.assertIn('sandbox_mode="workspace-write"', command)
        # A session started outside a git repo must still resume.
        self.assertIn("--skip-git-repo-check", command)
        self.assertEqual(command[-2:], ["session-1", "-"])

    def test_enqueue_adds_acknowledgement_instruction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                cwd=tmp,
                task="train",
                command=None,
                exit_code=0,
                message=None,
                session="session-1",
                last=False,
                queue_dir=tmp,
                approvals_reviewer="auto_review",
                approval_policy="on-request",
                sandbox_mode="workspace-write",
            )

            self.assertEqual(dispatch.enqueue_request(args, dispatch.build_prompt(args)), 0)
            queued = list((Path(tmp) / "pending").glob("*.json"))
            self.assertEqual(len(queued), 1)
            request = json.loads(queued[0].read_text(encoding="utf-8"))

            self.assertRegex(request["prompt"], r"(reins|harness-reins|_entry\.py)")
            self.assertIn(" 'ack' " if os.name == "nt" else " ack ", request["prompt"])
            details = Path(request["prompt_details_path"]).read_text()
            self.assertRegex(details, r"Callback time: .+[+-]\d{2}:\d{2}")
            self.assertIn(str(request["id"]), request["prompt"])
            self.assertIn("Bound session: session-1", details)
            self.assertIn("Binding source: --session", details)
            self.assertIn("never redirect this callback to another thread", details)
            self.assertIn("Session: session-1 (only)", request["prompt"])
            self.assertEqual(request["queue_dir"], tmp)

    def test_there_is_no_most_recent_session_fallback(self) -> None:
        for argv in (["run", "--last", "--", "true"], ["doctor", "--last"]):
            with self.subTest(argv=argv), mock.patch.object(sys, "argv", ["reins", *argv]), \
                    contextlib.redirect_stderr(io.StringIO()) as errors, self.assertRaises(SystemExit) as caught:
                cli.main()
            self.assertEqual(caught.exception.code, 2)
            self.assertIn("unrecognized arguments: --last", errors.getvalue())

    def test_resume_command_makes_queue_dir_writable(self) -> None:
        command = delivery_engine.resume_command(
            {
                "target": {"kind": "session", "session": "codex-session-1"},
                "cwd": "/tmp",
                "prompt": "hello",
                "queue_dir": "/tmp/callback-queue",
            }
        )

        self.assertIn('sandbox_workspace_write.writable_roots=["/tmp/callback-queue"]', command)
        self.assertEqual(command[-2:], ["codex-session-1", "-"])

    def test_resolve_agent_detects_claude_environment(self) -> None:
        env = {key: value for key, value in os.environ.items()
               if key not in ("CODEX_THREAD_ID", "PI_SESSION_ID", "PI_SESSION_FILE", "PI_CODING_AGENT")}
        env["CLAUDECODE"] = "1"
        env["CLAUDE_CODE_SESSION_ID"] = "claude-session-1"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(dispatch.resolve_agent(), "claude")

    def test_resolve_agent_prefers_explicit_flag(self) -> None:
        args = argparse.Namespace(agent="claude")
        with self._env_without_claude(CODEX_THREAD_ID="codex-thread"):
            self.assertEqual(dispatch.resolve_agent(args), "claude")
        args = argparse.Namespace(agent="codex")
        with mock.patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s"}, clear=False):
            self.assertEqual(dispatch.resolve_agent(args), "codex")

    def test_make_request_binds_claude_session_id_by_default(self) -> None:
        args = argparse.Namespace(
            cwd="/tmp",
            session=None,
            last=False,
            agent=None,
            approvals_reviewer="auto_review",
            approval_policy="on-request",
            sandbox_mode="workspace-write",
            permission_mode="auto",
        )
        env = {key: value for key, value in os.environ.items()
               if key not in ("CODEX_THREAD_ID", "PI_SESSION_ID", "PI_SESSION_FILE", "PI_CODING_AGENT")}
        env["CLAUDECODE"] = "1"
        env["CLAUDE_CODE_SESSION_ID"] = "claude-session-1"
        with mock.patch.dict(os.environ, env, clear=True):
            request = dispatch.make_request(args, "wake up")

        self.assertEqual(request["agent"], "claude")
        self.assertEqual(request["target"], {"kind": "session", "session": "claude-session-1"})
        self.assertEqual(request["origin"]["binding"], "CLAUDE_CODE_SESSION_ID")
        self.assertEqual(request["permission_mode"], "auto")

    def test_claude_resume_command_shape(self) -> None:
        with mock.patch.object(cli.claude_session, "discover_executable", return_value=None):
            command = delivery_engine.resume_command(
                {
                    "agent": "claude",
                    "target": {"kind": "session", "session": "claude-session-1"},
                    "cwd": "/tmp",
                    "prompt": "hello",
                    "queue_dir": "/tmp/callback-queue",
                }
            )

        self.assertEqual(
            command,
            [
                "claude",
                "-p",
                "--permission-mode",
                "auto",
                "--add-dir",
                "/tmp/callback-queue",
                "--resume",
                "claude-session-1",
            ],
        )

    def test_claude_resume_command_honors_overrides(self) -> None:
        request = {
            "agent": "claude",
            "target": {"kind": "session", "session": "claude-session-1"},
            "cwd": "/tmp",
            "prompt": "hello",
            "permission_mode": "acceptEdits",
        }
        with mock.patch.dict(os.environ, {"REINS_CLAUDE_BIN": "/opt/claude"}, clear=False):
            command = delivery_engine.resume_command(request)

        self.assertEqual(command, ["/opt/claude", "-p", "--permission-mode", "acceptEdits", "--resume", "claude-session-1"])

    def test_claude_prompt_and_acknowledgement_text(self) -> None:
        args = argparse.Namespace(
            task="train",
            cwd="/tmp",
            command="python train.py",
            exit_code=0,
            message=None,
            agent="claude",
        )
        prompt = dispatch.build_prompt(args)
        self.assertIn("called back into Claude Code", prompt)
        ack_text = dispatch.build_acknowledgement_text("reins ack --queue-dir /q --id abc", agent="claude")
        self.assertIn("Claude Code headless", ack_text)
        self.assertIn("reins ack --queue-dir /q --id abc", ack_text)

    def test_desktop_delivery_skipped_for_claude_requests(self) -> None:
        payload = {
            "request": {
                "agent": "claude",
                "target": {"kind": "session", "session": "claude-session-1"},
                "sandbox_mode": "workspace-write",
            },
            "timeout": 1.0,
        }
        self.assertIsNone(delivery_engine.start_desktop_app_server_turn(payload))

    def test_daemon_marks_done_after_fake_claude_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "claude-acked", tmp)
            pending = root / "pending" / "claude-acked.json"
            request = json.loads(pending.read_text(encoding="utf-8"))
            request["agent"] = "claude"
            request["queue_dir"] = str(root)
            pending.write_text(json.dumps(request) + "\n", encoding="utf-8")
            fake_claude = self._fake_codex(Path(tmp), ack=True, queue=root, request_id="claude-acked")
            args = argparse.Namespace(retries=3, retry_delay=0.0, retry_backoff=2.0)

            with mock.patch.dict(os.environ, {"REINS_CLAUDE_BIN": str(fake_claude)}):
                self.assertTrue(delivery_engine.process_one(root, args))

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not (root / "done" / "claude-acked.json").exists():
                delivery_engine.reap_background_resumes()
                delivery_engine.recover_running(root)
                time.sleep(0.02)

            self.assertTrue((root / "done" / "claude-acked.json").exists())
            self.assertTrue((root / "acks" / "claude-acked.json").exists())

    def test_install_skill_installs_for_both_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            claude_home = Path(tmp) / "claude-home"
            args = argparse.Namespace(path=None, target="both", force=False)
            with mock.patch.dict(
                os.environ,
                {"CODEX_HOME": str(codex_home), "CLAUDE_CONFIG_DIR": str(claude_home)},
                clear=False,
            ):
                self.assertEqual(skill_install.install_skill(args), 0)

            codex_skill = codex_home / "skills" / "harness-reins"
            claude_skill = claude_home / "skills" / "harness-reins"
            self.assertTrue((codex_skill / "SKILL.md").exists())
            self.assertTrue((codex_skill / "agents" / "openai.yaml").exists())
            self.assertTrue((claude_skill / "SKILL.md").exists())
            self.assertFalse((claude_skill / "agents").exists())
            self.assertIn("reins wait", (claude_skill / "SKILL.md").read_text(encoding="utf-8"))
            self.assertIn("End your turn", (codex_skill / "SKILL.md").read_text(encoding="utf-8"))
            self.assertEqual(
                (claude_skill / "REFERENCE.md").read_text(encoding="utf-8"),
                (codex_skill / "REFERENCE.md").read_text(encoding="utf-8"),
            )

    def test_install_skill_single_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-home"
            claude_home = Path(tmp) / "claude-home"
            args = argparse.Namespace(path=None, target="claude", force=False)
            with mock.patch.dict(
                os.environ,
                {"CODEX_HOME": str(codex_home), "CLAUDE_CONFIG_DIR": str(claude_home)},
                clear=False,
            ):
                self.assertEqual(skill_install.install_skill(args), 0)

            self.assertFalse((codex_home / "skills").exists())
            self.assertTrue((claude_home / "skills" / "harness-reins" / "SKILL.md").exists())

    def test_install_skill_all_keeps_each_agents_delivery_to_itself(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            homes = {"CODEX_HOME": Path(tmp) / "codex", "CLAUDE_CONFIG_DIR": Path(tmp) / "claude",
                     "PI_CODING_AGENT_DIR": Path(tmp) / "pi"}
            with mock.patch.dict(os.environ, {name: str(path) for name, path in homes.items()}, clear=False):
                self.assertEqual(skill_install.install_skill(argparse.Namespace(path=None, target="all", force=False)), 0)
            skills = {name: (path / "skills" / "harness-reins") for name, path in homes.items()}
            codex = (skills["CODEX_HOME"] / "SKILL.md").read_text(encoding="utf-8")
            claude = (skills["CLAUDE_CONFIG_DIR"] / "SKILL.md").read_text(encoding="utf-8")
            pi = (skills["PI_CODING_AGENT_DIR"] / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("steer", pi)
            self.assertNotIn("reins wait", pi)
            self.assertNotIn("steer", claude)
            # Codex names `reins wait` only to rule it out; it never runs Claude Code background commands.
            self.assertIn("End your turn", codex)
            self.assertNotIn("run_in_background", codex)
            self.assertNotIn("install-pi-extension", codex)
            self.assertEqual((skills["PI_CODING_AGENT_DIR"] / "REFERENCE.md").read_text(encoding="utf-8"),
                             (skills["CODEX_HOME"] / "REFERENCE.md").read_text(encoding="utf-8"))
            self.assertFalse((skills["PI_CODING_AGENT_DIR"] / "agents").exists())

    def test_daemon_requeues_when_agent_does_not_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "missing-ack", tmp)
            fake_codex = self._fake_codex(Path(tmp), ack=False, queue=root, request_id="missing-ack")
            args = argparse.Namespace(retries=3, retry_delay=0.0, retry_backoff=2.0)

            with mock.patch.dict(os.environ, {"REINS_CODEX_BIN": str(fake_codex)}):
                self.assertTrue(delivery_engine.process_one(root, args))

            pending = root / "pending" / "missing-ack.json"
            self.assertTrue(pending.exists())
            request = json.loads(pending.read_text(encoding="utf-8"))
            self.assertEqual(request["attempts"], 1)
            self.assertIn("last_error", request)
            self.assertFalse((root / "done" / "missing-ack.json").exists())

    def test_daemon_marks_done_only_after_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "acked", tmp)
            fake_codex = self._fake_codex(Path(tmp), ack=True, queue=root, request_id="acked")
            args = argparse.Namespace(retries=3, retry_delay=0.0, retry_backoff=2.0)

            with mock.patch.dict(os.environ, {"REINS_CODEX_BIN": str(fake_codex)}):
                self.assertTrue(delivery_engine.process_one(root, args))

            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not (root / "done" / "acked.json").exists():
                delivery_engine.reap_background_resumes()
                delivery_engine.recover_running(root)
                time.sleep(0.02)
            self.assertTrue((root / "done" / "acked.json").exists())
            self.assertTrue((root / "acks" / "acked.json").exists())
            self.assertFalse((root / "pending" / "acked.json").exists())

    def test_daemon_reconciles_pending_request_with_existing_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "late-ack", tmp)
            queue_store.ensure_daemon_dirs(root)
            (root / "acks" / "late-ack.json").write_text("{}\n", encoding="utf-8")
            args = argparse.Namespace(retries=3, retry_delay=0.0, retry_backoff=2.0)

            with mock.patch.object(cli.subprocess, "run") as run:
                self.assertTrue(delivery_engine.process_one(root, args))

            run.assert_not_called()
            self.assertTrue((root / "done" / "late-ack.json").exists())

    def test_daemon_times_out_stuck_resume_and_requeues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "stuck", tmp)
            args = argparse.Namespace(retries=3, retry_delay=0.0, retry_backoff=2.0, resume_timeout=1.0)

            timed_out = subprocess.CompletedProcess(["codex"], 124)
            with mock.patch.object(delivery_engine, "run_resume_until_exit_or_ack", return_value=(timed_out, False, False)):
                self.assertTrue(delivery_engine.process_one(root, args))

            request = json.loads((root / "pending" / "stuck.json").read_text(encoding="utf-8"))
            self.assertEqual(request["last_error"], "agent resume timed out or stopped making progress")

    def test_daemon_recovers_stale_running_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "unacked", tmp)
            queue_store.ensure_daemon_dirs(root)
            os.replace(root / "pending" / "unacked.json", root / "running" / "unacked.json")
            self._write_request(root, "acked", tmp)
            os.replace(root / "pending" / "acked.json", root / "running" / "acked.json")
            (root / "acks" / "acked.json").write_text("{}\n", encoding="utf-8")

            delivery_engine.recover_running(root)

            self.assertTrue((root / "pending" / "unacked.json").exists())
            self.assertTrue((root / "done" / "acked.json").exists())

    def test_move_request_collision_is_idempotent_only_for_same_valid_id(self) -> None:
        def payload(request_id: str, cwd: str, prompt: str) -> dict[str, object]:
            return reins_records.callback_record(request_id, cwd, target={"kind": "session", "session": "fixture-session"}, prompt=prompt, created_at=1.0)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            queue_store.ensure_daemon_dirs(root)
            source = layout.request_path(root, layout.ACTIVE_STATE, "same-id")
            destination = layout.request_path(root, "pending", "same-id")
            queue_store.write_request(source, payload("same-id", tmp, "identical"))
            queue_store.write_request(destination, payload("same-id", tmp, "identical"))

            self.assertEqual(queue_store.move_request(source, destination.parent), destination)
            self.assertFalse(source.exists())
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["prompt"], "identical")

            source = layout.request_path(root, layout.ACTIVE_STATE, "divergent")
            destination = layout.request_path(root, "pending", "divergent")
            queue_store.write_request(source, payload("divergent", tmp, "new completed source"))
            queue_store.write_request(destination, payload("divergent", tmp, "stale destination"))

            with self.assertRaises(FileExistsError):
                queue_store.move_request(source, destination.parent)
            self.assertTrue(source.exists())
            self.assertTrue(destination.exists())

            source = layout.request_path(root, layout.ACTIVE_STATE, "collision")
            destination = layout.request_path(root, "pending", "collision")
            queue_store.write_request(source, payload("collision", tmp, "source"))
            queue_store.write_request(destination, payload("different-id", tmp, "destination"))

            with self.assertRaises(FileExistsError):
                queue_store.move_request(source, destination.parent)
            self.assertTrue(source.exists())
            self.assertTrue(destination.exists())

            destination.write_text("not json\n", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                queue_store.move_request(source, destination.parent)
            self.assertTrue(source.exists())

    def test_ack_is_monotonic_and_prevents_redelivery_after_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "monotonic-ack", tmp)
            ack_args = argparse.Namespace(queue_dir=str(root), id="monotonic-ack", message="received")

            self.assertEqual(callback_commands.ack(ack_args), 0)
            first = json.loads(layout.ack_path(root, "monotonic-ack").read_text(encoding="utf-8"))
            self.assertEqual(callback_commands.ack(ack_args), 0)
            second = json.loads(layout.ack_path(root, "monotonic-ack").read_text(encoding="utf-8"))
            self.assertGreaterEqual(second["marked_at"], first["marked_at"])

            args = argparse.Namespace(retries=3, retry_delay=0.0, retry_backoff=2.0)
            with mock.patch.object(delivery_engine, "run_resume_until_exit_or_ack") as resume:
                self.assertTrue(delivery_engine.process_one(root, args))
            resume.assert_not_called()
            self.assertTrue(layout.request_path(root, "done", "monotonic-ack").exists())
            self.assertTrue(layout.ack_path(root, "monotonic-ack").exists())

    def test_normal_running_ack_does_not_require_target_lock_directory_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            target_locks = Path(tmp) / "target-locks"
            self._write_request(
                root,
                "queue-only-ack",
                tmp,
                target={"kind": "session", "session": "queue-only-session"},
            )
            queue_store.ensure_daemon_dirs(root)
            queue_store.move_request(layout.request_path(root, "pending", "queue-only-ack"), root / "running")
            args = argparse.Namespace(queue_dir=str(root), id="queue-only-ack", message="inspected")

            with mock.patch.dict(os.environ, {layout.TARGET_LOCK_DIR_ENV: str(target_locks)}, clear=False):
                self.assertEqual(callback_commands.ack(args), 0)

            self.assertTrue(layout.ack_path(root, "queue-only-ack").exists())
            self.assertFalse(target_locks.exists())

    def test_daemon_reconciles_retained_lease_after_sandboxed_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            target_locks = Path(tmp) / "target-locks"
            self._write_request(
                root,
                "deferred-lease-ack",
                tmp,
                target={"kind": "session", "session": "deferred-lease-session"},
            )
            queue_store.ensure_daemon_dirs(root)
            queue_store.move_request(layout.request_path(root, "pending", "deferred-lease-ack"), root / "failed")
            failed_path = layout.request_path(root, "failed", "deferred-lease-ack")
            failed = dispatch.load_request(failed_path)
            failed["retain_target_lease"] = True
            queue_store.write_request(failed_path, failed)
            args = argparse.Namespace(queue_dir=str(root), id="deferred-lease-ack", message=None)

            with mock.patch.dict(os.environ, {layout.TARGET_LOCK_DIR_ENV: str(target_locks)}, clear=False):
                delivery_engine.retain_target_lease(root, failed)
                lease_path = delivery_engine.retained_target_lease_path(failed)
                self.assertIsNotNone(lease_path)
                with mock.patch.object(
                    delivery_engine,
                    "acquire_retained_target_lease_lock",
                    side_effect=OSError("read-only target-locks"),
                ):
                    self.assertEqual(callback_commands.ack(args), 0)
                self.assertTrue(lease_path.exists())
                delivery_engine.reconcile_acknowledged_retained_leases(root)
                self.assertFalse(lease_path.exists())

            self.assertTrue(layout.request_path(root, "done", "deferred-lease-ack").exists())
            self.assertTrue(layout.ack_path(root, "deferred-lease-ack").exists())

    def test_late_ack_reconciles_failed_request_to_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            target = {"kind": "session", "session": "late-ack-thread"}
            self._write_request(root, "late-failed-ack", tmp, target=target)
            queue_store.ensure_daemon_dirs(root)
            queue_store.move_request(
                layout.request_path(root, "pending", "late-failed-ack"),
                root / "failed",
            )
            failed = dispatch.load_request(layout.request_path(root, "failed", "late-failed-ack"))
            failed["retain_target_lease"] = True
            queue_store.write_request(layout.request_path(root, "failed", "late-failed-ack"), failed)
            args = argparse.Namespace(queue_dir=str(root), id="late-failed-ack", message="late delivery completed")

            with mock.patch.dict(os.environ, {layout.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks")}, clear=False):
                delivery_engine.retain_target_lease(root, failed)
                self.assertEqual(callback_commands.ack(args), 0)
                self.assertFalse(layout.request_path(root, "failed", "late-failed-ack").exists())
                self.assertTrue(layout.request_path(root, "done", "late-failed-ack").exists())
                self.assertTrue(layout.ack_path(root, "late-failed-ack").exists())
                self.assertFalse(delivery_engine.retained_target_lease_is_held(failed))

    def test_late_ack_releases_manual_recovery_lease_across_queue_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_a = Path(tmp) / "queue-a"
            root_b = Path(tmp) / "queue-b"
            target = {"kind": "session", "session": "late-ack-thread"}
            self._write_request(root_a, "late-failed-ack", tmp, target=target)
            queue_store.ensure_daemon_dirs(root_a)
            queue_store.move_request(layout.request_path(root_a, "pending", "late-failed-ack"), root_a / "failed")
            failed = dispatch.load_request(layout.request_path(root_a, "failed", "late-failed-ack"))
            failed["retain_target_lease"] = True
            queue_store.write_request(layout.request_path(root_a, "failed", "late-failed-ack"), failed)
            self._write_request(root_b, "same-session", tmp, target=target)
            args = argparse.Namespace(queue_dir=str(root_a), id="late-failed-ack", message=None)

            with mock.patch.dict(os.environ, {layout.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks")}, clear=False):
                delivery_engine.retain_target_lease(root_a, failed)
                self.assertIsNone(delivery_engine.select_pending(root_b, time.time()))
                self.assertEqual(callback_commands.ack(args), 0)
                self.assertEqual(delivery_engine.select_pending(root_b, time.time()).stem, "same-session")

    def test_restart_reconciles_retained_marker_after_acknowledged_done_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_a = Path(tmp) / "queue-a"
            root_b = Path(tmp) / "queue-b"
            target = {"kind": "session", "session": "restart-thread"}
            self._write_request(root_a, "reconciled", tmp, target=target)
            queue_store.ensure_daemon_dirs(root_a)
            queue_store.move_request(layout.request_path(root_a, "pending", "reconciled"), root_a / "done")
            completed = dispatch.load_request(layout.request_path(root_a, "done", "reconciled"))
            self._write_request(root_b, "same-session", tmp, target=target)

            with mock.patch.dict(os.environ, {layout.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks")}, clear=False):
                delivery_engine.retain_target_lease(root_a, completed)
                queue_store.write_request(layout.ack_path(root_a, "reconciled"), {"id": "reconciled", "marked_at": time.time()})
                self.assertEqual(delivery_engine.select_pending(root_b, time.time()).stem, "same-session")
                self.assertFalse(delivery_engine.retained_target_lease_is_held(completed))

    def test_marker_reconciliation_blocks_while_transaction_lock_is_held(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            target = {"kind": "session", "session": "reconcile-thread"}
            self._write_request(root, "reconciled", tmp, target=target)
            request = dispatch.load_request(layout.request_path(root, "pending", "reconciled"))

            with mock.patch.dict(os.environ, {layout.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks")}, clear=False):
                delivery_engine.retain_target_lease(root, request)
                queue_store.write_request(layout.ack_path(root, "reconciled"), {"id": "reconciled", "marked_at": time.time()})
                lock = delivery_engine.acquire_retained_target_lease_lock(request, blocking=False)
                self.assertIsNotNone(lock)
                try:
                    self.assertTrue(delivery_engine.retained_target_lease_is_held(request))
                finally:
                    queue_store.release_owner_lock(lock, remove=False)
                self.assertFalse(delivery_engine.retained_target_lease_is_held(request))

    def test_ack_releases_timeout_marker_while_parent_still_marks_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            target = {"kind": "session", "session": "timeout-thread"}
            self._write_request(root, "timeout-ack", tmp, target=target)
            queue_store.ensure_daemon_dirs(root)
            queue_store.move_request(layout.request_path(root, "pending", "timeout-ack"), root / "running")
            request = dispatch.load_request(layout.request_path(root, "running", "timeout-ack"))
            args = argparse.Namespace(queue_dir=str(root), id="timeout-ack", message=None)

            with mock.patch.dict(os.environ, {layout.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks")}, clear=False):
                delivery_engine.retain_target_lease(root, request)
                self.assertEqual(callback_commands.ack(args), 0)
                self.assertTrue(layout.request_path(root, "running", "timeout-ack").exists())
                self.assertFalse(delivery_engine.retained_target_lease_is_held(request))

    def test_ack_racing_final_failed_transition_converges_to_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            request_id = "final-transition-ack"
            self._write_request(root, request_id, tmp, target={"kind": "session", "session": "race-thread"})
            args = argparse.Namespace(retries=0, retry_delay=0.0, retry_backoff=2.0)
            original_move = queue_store.move_request

            def move_with_late_ack(source: Path, destination: Path) -> Path:
                if source.parent.name == "running" and destination == root / "failed":
                    delivery_engine.retain_target_lease(root, dispatch.load_request(source))
                    queue_store.write_request(
                        layout.ack_path(root, request_id),
                        {"id": request_id, "marked_at": time.time()},
                    )
                return original_move(source, destination)

            completed = subprocess.CompletedProcess(["codex"], 125)
            with (
                mock.patch.dict(os.environ, {layout.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks")}, clear=False),
                mock.patch.object(delivery_engine, "run_resume_until_exit_or_ack", return_value=(completed, False, False)),
                mock.patch.object(queue_store, "move_request", side_effect=move_with_late_ack),
            ):
                self.assertTrue(delivery_engine.process_one(root, args))
                request = dispatch.load_request(layout.request_path(root, "done", request_id))
                self.assertFalse(delivery_engine.retained_target_lease_is_held(request))

            self.assertTrue(layout.ack_path(root, request_id).exists())
            self.assertTrue(layout.request_path(root, "done", request_id).exists())
            self.assertFalse(layout.request_path(root, "failed", request_id).exists())
            self.assertFalse(layout.request_path(root, "running", request_id).exists())

    def test_ack_releases_queue_for_other_session_but_keeps_same_session_lease(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "a-running", tmp, target={"kind": "session", "session": "session-a"})
            fake_codex = self._fake_codex(
                Path(tmp),
                ack=True,
                queue=root,
                request_id="a-running",
                sleep_after_ack=3.0,
            )
            args = argparse.Namespace(retries=3, retry_delay=0.0, retry_backoff=2.0, resume_timeout=10.0)

            started = time.monotonic()
            with mock.patch.dict(os.environ, {"REINS_CODEX_BIN": str(fake_codex)}):
                self.assertTrue(delivery_engine.process_one(root, args))
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertTrue(layout.request_path(root, "running", "a-running").exists())
            self.assertTrue(queue_store.delivery_lock_is_held(root, "a-running"))

            self._write_request(root, "b-same", tmp, target={"kind": "session", "session": "session-a"})
            self._write_request(root, "c-other", tmp, target={"kind": "session", "session": "session-b"})
            self.assertEqual(delivery_engine.select_pending(root, time.time()).stem, "c-other")

            delivery_engine.recover_running(root)
            self.assertTrue(layout.request_path(root, "running", "a-running").exists())
            for delivery in delivery_engine._BACKGROUND_RESUMES.values():
                delivery.deadline = time.monotonic() - 1.0
            delivery_engine.reap_background_resumes()
            self.assertFalse(delivery_engine._BACKGROUND_RESUMES)
            delivery_engine.recover_running(root)
            self.assertTrue(layout.request_path(root, "done", "a-running").exists())

    def test_delivery_rechecks_ack_after_lock_before_spawning_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "ack-race", tmp, target={"kind": "session", "session": "race-session"})
            request = dispatch.load_request(layout.request_path(root, "pending", "ack-race"))
            args = argparse.Namespace(resume_timeout=10.0)
            original_acquire = queue_store.acquire_owner_lock

            def acquire_then_ack(
                queue_root: Path,
                lock_id: str,
                *,
                blocking: bool,
            ) -> tuple[object, Path] | None:
                lock = original_acquire(queue_root, lock_id, blocking=blocking)
                if lock_id == layout.delivery_lock_id("ack-race"):
                    queue_store.write_request(layout.ack_path(root, "ack-race"), {"id": "ack-race", "marked_at": time.time()})
                return lock

            with (
                mock.patch.object(queue_store, "acquire_owner_lock", side_effect=acquire_then_ack),
                mock.patch.object(cli.subprocess, "Popen") as spawn,
            ):
                result, acked, live = delivery_engine.run_resume_until_exit_or_ack(root, "ack-race", request, args)

            spawn.assert_not_called()
            self.assertTrue(acked)
            self.assertFalse(live)
            self.assertEqual(result.returncode, 0)

    def test_headless_resume_runs_with_the_submitting_session_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            seen = Path(tmp) / "seen.json"
            fake = Path(tmp) / "fake-codex"
            fake.write_text(
                f"#!{sys.executable}\nimport json, os, sys\nsys.stdin.read()\n"
                f"names = ('MODEL_ENDPOINT', 'CLAUDECODE', 'STY', 'REINS_HOME')\n"
                f"open({str(seen)!r}, 'w').write(json.dumps({{n: os.environ.get(n) for n in names}}))\n",
                encoding="utf-8")
            executable_python_fixture(fake)
            self._write_request(root, "env-check", tmp, target={"kind": "session", "session": "env-session"})
            request = dispatch.load_request(layout.request_path(root, "pending", "env-check"))
            # What the submitting shell had, including markers that must not reach the resumed CLI.
            resume_environment.capture(root, "env-check", {
                "PATH": os.environ.get("PATH", ""), "MODEL_ENDPOINT": "https://models.example/v1",
                "CLAUDECODE": "1", "STY": "parent-screen", "REINS_HOME": str(Path(tmp) / "elsewhere")})
            assert_private_file(self, resume_environment.path(root, "env-check"))
            coordinator_home = os.environ.get("REINS_HOME")
            with mock.patch.dict(os.environ, {"REINS_CODEX_BIN": str(fake), "REINS_DESKTOP_APP_SERVER": "0"}):
                result, _, _ = delivery_engine.run_resume_until_exit_or_ack(
                    root, "env-check", request, argparse.Namespace(resume_timeout=30.0))
            self.assertEqual(result.returncode, 0)
            environment = json.loads(seen.read_text(encoding="utf-8"))
            self.assertEqual(environment["MODEL_ENDPOINT"], "https://models.example/v1")
            self.assertIsNone(environment["CLAUDECODE"])
            self.assertIsNone(environment["STY"])
            self.assertEqual(environment["REINS_HOME"], coordinator_home, "the coordinator's queue home wins")

    def test_submission_keeps_the_session_environment_for_its_callback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            args = self._run_args(tmp, root, [sys.executable, "-c", "pass"])
            with mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"), \
                    mock.patch.dict(os.environ, {"MODEL_ENDPOINT": "https://models.example/v1", "CLAUDECODE": "1"}), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(dispatch.submit_managed_run(args), 0)
            task_id = next(layout.managed_tasks_root(root).glob("*/task.json")).parent.name
            kept = resume_environment.load(root, ids.callback_id(task_id))
            self.assertEqual(kept["MODEL_ENDPOINT"], "https://models.example/v1")
            self.assertNotIn("CLAUDECODE", kept)
            assert_private_file(self, resume_environment.path(root, ids.callback_id(task_id)))

    def test_session_environment_copies_are_removed_once_the_callback_is_settled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            queue_store.ensure_daemon_dirs(root)
            for callback_id in ("acked-c1", "done-c1", "failed-c1", "pending-c1"):
                resume_environment.capture(root, callback_id, {"MODEL_ENDPOINT": "x"})
            queue_store.write_request(layout.ack_path(root, "acked-c1"), {"id": "acked-c1"})
            self._write_request(root, "done-c1", tmp)
            queue_store.move_request(layout.request_path(root, "pending", "done-c1"), root / "done")
            resume_environment.prune(root)
            left = sorted(p.stem for p in (root / resume_environment.DIRECTORY_NAME).glob("*.json"))
            self.assertEqual(left, ["failed-c1", "pending-c1"], "failed callbacks keep theirs for reins retry")
            self.assertEqual(resume_environment.merged({"REINS_HOME": "/coordinator", "PATH": "/svc"},
                                                       {"REINS_HOME": "/session", "PATH": "/session/bin"}),
                             {"REINS_HOME": "/coordinator", "PATH": "/session/bin"})

    def test_canceled_live_delivery_retains_target_lease_after_memory_loss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "held-canceled", tmp, target={"kind": "session", "session": "held-session"})
            queue_store.ensure_daemon_dirs(root)
            queue_store.move_request(layout.request_path(root, "pending", "held-canceled"), root / "canceled")
            delivery_lock = queue_store.acquire_owner_lock(root, layout.delivery_lock_id("held-canceled"), blocking=False)
            self.assertIsNotNone(delivery_lock)
            try:
                self._write_request(root, "same-target", tmp, target={"kind": "session", "session": "held-session"})
                delivery_engine._BACKGROUND_RESUMES.clear()
                self.assertIsNone(delivery_engine.select_pending(root, time.time()))
            finally:
                queue_store.release_owner_lock(delivery_lock, remove=False)
            self.assertEqual(delivery_engine.select_pending(root, time.time()).stem, "same-target")

    def test_global_target_lease_blocks_same_session_across_queue_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_a = Path(tmp) / "queue-a"
            root_b = Path(tmp) / "queue-b"
            target = {"kind": "session", "session": "shared-session"}
            self._write_request(root_a, "owner", tmp, target=target)
            owner = dispatch.load_request(layout.request_path(root_a, "pending", "owner"))
            self._write_request(root_b, "a-same", tmp, target=target)
            self._write_request(
                root_b,
                "b-other",
                tmp,
                target={"kind": "session", "session": "other-session"},
            )

            with mock.patch.dict(
                os.environ,
                {layout.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "global-target-locks")},
            ):
                lease = delivery_engine.acquire_target_lock(owner, blocking=False)
                self.assertIsNotNone(lease)
                try:
                    selected = delivery_engine.select_pending(root_b, time.time())
                    self.assertIsNotNone(selected)
                    self.assertEqual(selected.stem, "b-other")
                finally:
                    queue_store.release_owner_lock(lease, remove=False)

                self.assertEqual(delivery_engine.select_pending(root_b, time.time()).stem, "a-same")

    def test_cross_queue_lease_race_requeues_without_consuming_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_a = Path(tmp) / "queue-a"
            root_b = Path(tmp) / "queue-b"
            target = {"kind": "session", "session": "race-session"}
            self._write_request(root_a, "owner", tmp, target=target)
            owner = dispatch.load_request(layout.request_path(root_a, "pending", "owner"))
            self._write_request(root_b, "contender", tmp, target=target)
            args = argparse.Namespace(retries=3, retry_delay=0.0, retry_backoff=2.0, resume_timeout=10.0)

            with mock.patch.dict(
                os.environ,
                {layout.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "global-target-locks")},
            ):
                lease = delivery_engine.acquire_target_lock(owner, blocking=False)
                self.assertIsNotNone(lease)
                try:
                    with (
                        mock.patch.object(delivery_engine, "target_has_live_resume", return_value=False),
                        mock.patch.object(cli.subprocess, "Popen") as spawn,
                    ):
                        self.assertTrue(delivery_engine.process_one(root_b, args))
                    spawn.assert_not_called()
                finally:
                    queue_store.release_owner_lock(lease, remove=False)

            pending = layout.request_path(root_b, "pending", "contender")
            self.assertTrue(pending.exists())
            request = json.loads(pending.read_text(encoding="utf-8"))
            self.assertEqual(request["attempts"], 0)
            self.assertIn("target lease held", request["last_deferred_reason"])

    def test_stale_selection_rechecks_retained_target_lease_before_worker_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_a = Path(tmp) / "queue-a"
            root_b = Path(tmp) / "queue-b"
            target = {"kind": "session", "session": "stale-selection-session"}
            failed = reins_records.callback_record("desktop-unknown", tmp, target=target, prompt="wake up", created_at=1.0, retain_target_lease=True)
            queue_store.ensure_daemon_dirs(root_a)
            queue_store.write_request(layout.request_path(root_a, "failed", "desktop-unknown"), failed)
            self._write_request(root_b, "contender", tmp, target=target)
            args = argparse.Namespace(retries=3, retry_delay=0.0, retry_backoff=2.0, resume_timeout=10.0)

            with mock.patch.dict(
                os.environ,
                {layout.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "global-target-locks")},
            ):
                delivery_engine.retain_target_lease(root_a, failed)
                with (
                    mock.patch.object(delivery_engine, "target_has_live_resume", return_value=False),
                    mock.patch.object(cli.subprocess, "Popen") as spawn,
                ):
                    self.assertTrue(delivery_engine.process_one(root_b, args))
                spawn.assert_not_called()

            pending = layout.request_path(root_b, "pending", "contender")
            self.assertTrue(pending.exists())
            request = json.loads(pending.read_text(encoding="utf-8"))
            self.assertEqual(request["attempts"], 0)
            self.assertIn("target lease held", request["last_deferred_reason"])

    def test_delivery_worker_holds_global_target_lease_across_queues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_a = Path(tmp) / "queue-a"
            root_b = Path(tmp) / "queue-b"
            target = {"kind": "session", "session": "worker-session"}
            self._write_request(root_a, "owner", tmp, target=target)
            fake_codex = self._fake_codex(
                Path(tmp),
                ack=True,
                queue=root_a,
                request_id="owner",
                sleep_after_ack=30.0,
            )
            args = argparse.Namespace(retries=3, retry_delay=0.0, retry_backoff=2.0, resume_timeout=10.0)

            with mock.patch.dict(
                os.environ,
                {
                    "REINS_CODEX_BIN": str(fake_codex),
                    layout.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "global-target-locks"),
                },
            ):
                self.assertTrue(delivery_engine.process_one(root_a, args))
                self._write_request(root_b, "same-session", tmp, target=target)
                contender = dispatch.load_request(layout.request_path(root_b, "pending", "same-session"))
                self.assertTrue(delivery_engine.target_lock_is_held(contender))
                self.assertIsNone(delivery_engine.select_pending(root_b, time.time()))

                for delivery in delivery_engine._BACKGROUND_RESUMES.values():
                    delivery.deadline = time.monotonic() - 1.0
                delivery_engine.reap_background_resumes()
                self.assertFalse(delivery_engine.target_lock_is_held(contender))
                self.assertEqual(delivery_engine.select_pending(root_b, time.time()).stem, "same-session")

    def test_recover_running_tolerates_concurrent_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "cancel-race", tmp)
            queue_store.ensure_daemon_dirs(root)
            queue_store.move_request(layout.request_path(root, "pending", "cancel-race"), root / "running")
            running = layout.request_path(root, "running", "cancel-race")
            data = json.loads(running.read_text(encoding="utf-8"))

            def cancel_during_move(source: Path, destination: Path) -> Path:
                canceled = dict(data)
                canceled.update({"canceled_at": time.time(), "canceled_from": "running"})
                queue_store.write_request(layout.request_path(root, "canceled", "cancel-race"), canceled)
                source.unlink()
                raise FileNotFoundError(source)

            with mock.patch.object(queue_store, "move_request", side_effect=cancel_during_move):
                delivery_engine.recover_running(root)

            self.assertTrue(layout.request_path(root, "canceled", "cancel-race").exists())

    @unittest.skipUnless(os.name == "posix", "POSIX SIGKILL/session inheritance; Windows has native handle tests")
    def test_real_daemon_sigkill_does_not_redeliver_live_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            launch_log = Path(tmp) / "resume-launches.log"
            self._write_request(root, "restart-lease", tmp, target={"kind": "session", "session": "restart-session"})
            fake_codex = self._fake_codex(
                Path(tmp),
                ack=False,
                queue=root,
                request_id="restart-lease",
                sleep_after_ack=30.0,
                launch_log=launch_log,
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(cli.__file__).parents[1])
            env["REINS_CODEX_BIN"] = str(fake_codex)
            daemon_command = [
                sys.executable,
                "-m",
                "harness_reins",
                "daemon",
                "--queue-dir",
                str(root),
                "--interval",
                "0.05",
                "--resume-timeout",
                "60",
            ]
            daemon = subprocess.Popen(
                daemon_command,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            child_pid = None
            try:
                deadline = time.time() + 10.0
                while time.time() < deadline:
                    if layout.request_path(root, "running", "restart-lease").exists() and launch_log.exists():
                        child_pid = int(launch_log.read_text(encoding="utf-8").splitlines()[0])
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(child_pid)

                os.kill(daemon.pid, signal.SIGKILL)
                daemon.wait(timeout=5)
                replacement = subprocess.run(
                    [*daemon_command, "--once"],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=5,
                )
                self.assertEqual(replacement.returncode, 0)
                self.assertEqual(len(launch_log.read_text(encoding="utf-8").splitlines()), 1)
                self.assertTrue(layout.request_path(root, "running", "restart-lease").exists())
                self.assertTrue(queue_store.delivery_lock_is_held(root, "restart-lease"))

                os.kill(child_pid, signal.SIGTERM)
                deadline = time.time() + 5.0
                while time.time() < deadline and queue_store.delivery_lock_is_held(root, "restart-lease"):
                    time.sleep(0.05)
                delivery_engine.recover_running(root)
                self.assertTrue(layout.request_path(root, "pending", "restart-lease").exists())
            finally:
                if daemon.poll() is None:
                    daemon.kill()
                    daemon.wait(timeout=5)
                if child_pid is not None:
                    try:
                        os.kill(child_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass

    @unittest.skipUnless(os.name == "posix", "POSIX descendant PID/signals; Windows has Job/handle tests")
    def test_delivery_lock_is_not_inherited_by_codex_descendants(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            descendant_pid_path = Path(tmp) / "descendant.pid"
            self._write_request(root, "no-fd-leak", tmp, target={"kind": "session", "session": "fd-session"})
            fake_codex = self._fake_codex(
                Path(tmp),
                ack=False,
                queue=root,
                request_id="no-fd-leak",
                descendant_pid_path=descendant_pid_path,
            )
            args = argparse.Namespace(retries=3, retry_delay=0.0, retry_backoff=2.0, resume_timeout=10.0)
            descendant_pid = None
            try:
                with mock.patch.dict(os.environ, {"REINS_CODEX_BIN": str(fake_codex)}):
                    self.assertTrue(delivery_engine.process_one(root, args))
                descendant_pid = int(descendant_pid_path.read_text(encoding="utf-8"))
                os.kill(descendant_pid, 0)
                self.assertFalse(queue_store.delivery_lock_is_held(root, "no-fd-leak"))
                self.assertTrue(layout.request_path(root, "pending", "no-fd-leak").exists())
            finally:
                if descendant_pid is not None:
                    try:
                        os.kill(descendant_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass

    def test_setup_installs_skill_then_systemd(self) -> None:
        args = argparse.Namespace(
            skill_path="/tmp/skills",
            name="harness-reins",
            queue_dir="/tmp/queue",
            interval=2.0,
            retries=3,
            retry_delay=30.0,
            retry_backoff=2.0,
            restart_sec=5.0,
            exec_start=None,
            codex_bin=None,
            path="/bin",
            force=True,
            enable=True,
            now=True,
        )

        with (
            mock.patch.object(sys, "platform", "linux"),
            mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"),
            mock.patch.object(skill_install, "install_skill", return_value=0) as install_skill,
            mock.patch.object(reminder_policy, "ensure_callback_hook_file") as ensure_callback_hook_file,
            mock.patch.object(services, "report_claude_agent_readiness") as report_claude_agent_readiness,
            mock.patch.object(services, "install_systemd", return_value=0) as install_systemd,
            mock.patch.dict(os.environ, {"REINS_HOME": self._temporary_directory()}),
        ):
            self.assertEqual(services.setup(args), 0)

        skill_args = install_skill.call_args.args[0]
        self.assertEqual(skill_args.path, "/tmp/skills")
        self.assertTrue(skill_args.force)
        ensure_callback_hook_file.assert_called_once_with()
        report_claude_agent_readiness.assert_called_once_with(args)

        systemd_args = install_systemd.call_args.args[0]
        self.assertEqual(systemd_args.queue_dir, "/tmp/queue")
        self.assertEqual(systemd_args.path, "/bin")
        self.assertTrue(systemd_args.enable)
        self.assertTrue(systemd_args.now)

    def test_claude_readiness_check_preserves_config_without_parent_markers(self) -> None:
        args = argparse.Namespace(claude_bin=None)
        completed = subprocess.CompletedProcess(["claude", "auth", "status", "--json"], 0)
        output = io.StringIO()
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        with (
            mock.patch.object(cli.shutil, "which", return_value="/opt/claude/bin/claude"),
            mock.patch.object(shell, "process_command", side_effect=lambda command, environment=None: command),
            mock.patch.object(cli.subprocess, "run", return_value=completed) as auth_status,
            mock.patch.dict(
                os.environ,
                {
                    "ANTHROPIC_API_KEY": "private-value",
                    "CLAUDE_CONFIG_DIR": "/tmp/claude-config",
                    "CODEX_THREAD_ID": "parent-codex",
                    "CLAUDE_CODE_SESSION_ID": "parent-claude",
                    "CLAUDECODE": "1",
                    "HOME": home.name, "USERPROFILE": home.name,
                },
                clear=True,
            ),
            mock.patch("sys.stdout", output),
        ):
            services.report_claude_agent_readiness(args)

        call = auth_status.call_args
        self.assertEqual(call.args[0], ["/opt/claude/bin/claude", "auth", "status", "--json"])
        child_env = call.kwargs["env"]
        self.assertEqual(child_env["ANTHROPIC_API_KEY"], "private-value")
        self.assertEqual(child_env["CLAUDE_CONFIG_DIR"], "/tmp/claude-config")
        self.assertNotIn("CODEX_THREAD_ID", child_env)
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", child_env)
        self.assertNotIn("CLAUDECODE", child_env)
        self.assertNotIn("private-value", output.getvalue())
        self.assertIn("configuration values were not printed", output.getvalue())

    def test_claude_readiness_check_warns_without_blocking_when_cli_is_missing(self) -> None:
        errors = io.StringIO()
        with (
            mock.patch.object(cli.shutil, "which", return_value=None),
            mock.patch.object(cli.subprocess, "run") as auth_status,
            mock.patch("sys.stderr", errors),
        ):
            services.report_claude_agent_readiness(argparse.Namespace(claude_bin=None))

        auth_status.assert_not_called()
        self.assertIn("setup will continue", errors.getvalue())
        self.assertIn("reins agent claude", errors.getvalue())

    def test_state_home_is_independent_of_the_codex_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"REINS_HOME": str(Path(tmp) / "reins"), "CODEX_HOME": str(Path(tmp) / "codex")}):
                self.assertEqual(layout.daemon_state_dir(), Path(tmp) / "reins")
                self.assertEqual(layout.queue_dir(), Path(tmp) / "reins" / "queue")
                self.assertEqual(layout.callback_hook_path(), Path(tmp) / "reins" / "callback-hook.md")
            environment = {name: value for name, value in os.environ.items() if name != "REINS_HOME"}
            environment.update(HOME=tmp, USERPROFILE=tmp)
            with mock.patch.dict(os.environ, environment, clear=True):
                self.assertEqual(layout.daemon_state_dir(), Path(tmp) / ".reins")

    def test_callback_hook_file_is_created_once_and_preserves_user_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "reins-home"
            with mock.patch.dict(os.environ, {"REINS_HOME": str(home)}):
                path = reminder_policy.ensure_callback_hook_file()
                self.assertEqual(path, home / "callback-hook.md")
                self.assertEqual(path.read_text(encoding="utf-8"), "")

                path.write_text("Always summarize the final metrics.\n", encoding="utf-8")
                self.assertEqual(reminder_policy.ensure_callback_hook_file(), path)
                self.assertEqual(path.read_text(encoding="utf-8"), "Always summarize the final metrics.\n")

    def test_callback_hook_is_reloaded_for_each_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "callback-hook.md"
            path.write_text("First instruction.\n", encoding="utf-8")
            first = reminder_policy.attach_callback_hook("wake up", path)
            self.assertEqual(first, "wake up\n\n[reins-user-hook]\nFirst instruction.")

            path.write_text("Second instruction.\n", encoding="utf-8")
            second = reminder_policy.attach_callback_hook("wake up", path)
            self.assertNotIn("First instruction.", second)
            self.assertIn("Second instruction.", second)

            path.write_text("\n", encoding="utf-8")
            self.assertEqual(reminder_policy.attach_callback_hook("wake up", path), "wake up")

    def test_setup_refuses_before_installing_when_screen_is_missing(self) -> None:
        args = argparse.Namespace()
        with (
            mock.patch.object(managed_tasks, "screen_binary", return_value=None),
            mock.patch.object(skill_install, "install_skill") as install_skill,
            mock.patch.object(services, "install_systemd") as install_systemd,
        ):
            self.assertEqual(services.setup(args), 2)
        install_skill.assert_not_called()
        install_systemd.assert_not_called()

    def test_install_systemd_persists_only_proxy_values_in_private_environment_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / ".env"
            source.write_text(
                "HTTP_PROXY=http://proxy.example:8080\nSECRET_TOKEN=must-not-be-copied\nno_proxy=localhost,127.0.0.1\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(
                name="harness-reins",
                queue_dir="/tmp/queue",
                interval=2.0,
                retries=3,
                retry_delay=30.0,
                retry_backoff=2.0,
                resume_timeout=3600.0,
                restart_sec=5.0,
                exec_start=None,
                codex_bin=None,
                path="/bin",
                proxy_env_file=str(source),
                inherit_proxy=False,
                force=True,
                enable=False,
                now=False,
                print=False,
            )
            home = Path(tmp) / "reins-home"
            with (
                mock.patch.dict(os.environ, {"REINS_HOME": str(home)}, clear=False),
                mock.patch.object(services, "systemd_user_dir", return_value=Path(tmp) / "systemd"),
                mock.patch.object(cli.shutil, "which", return_value=None),
            ):
                self.assertEqual(services.install_systemd(args), 0)

            proxy_file = home / "service-proxy.env"
            self.assertEqual(tower_environment.parse_proxy_environment_file(proxy_file), {
                "HTTP_PROXY": "http://proxy.example:8080",
                "no_proxy": "localhost,127.0.0.1",
            })
            assert_private_file(self, proxy_file)
            service = (Path(tmp) / "systemd" / "harness-reins.service").read_text(encoding="utf-8")
            self.assertIn(f"EnvironmentFile=-{proxy_file}", service)
            self.assertIn("ExecReload=/bin/kill -HUP $MAINPID", service)
            self.assertNotIn("proxy.example", service)
            self.assertNotIn("SECRET_TOKEN", service)

    def test_install_systemd_defers_old_daemon_restart_while_delivery_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "inflight", tmp)
            queue_store.move_request(root / "pending" / "inflight.json", root / "running")
            args = argparse.Namespace(
                name="harness-reins",
                queue_dir=str(root),
                interval=2.0,
                retries=3,
                retry_delay=30.0,
                retry_backoff=2.0,
                resume_timeout=3600.0,
                restart_sec=5.0,
                exec_start=None,
                codex_bin=None,
                path="/bin",
                proxy_env_file=None,
                inherit_proxy=False,
                force=True,
                enable=True,
                now=True,
                print=False,
            )
            with (
                mock.patch.dict(os.environ, {"REINS_HOME": str(Path(tmp) / "reins-home")}, clear=False),
                mock.patch.object(services, "systemd_user_dir", return_value=Path(tmp) / "systemd"),
                mock.patch.object(cli.shutil, "which", side_effect=lambda command: "/usr/bin/systemctl" if command == "systemctl" else None),
                mock.patch.object(services, "run_systemctl", return_value=0) as run_systemctl,
            ):
                self.assertEqual(services.install_systemd(args), 0)

            self.assertEqual(run_systemctl.call_args_list, [mock.call(["daemon-reload"]), mock.call(["enable", "harness-reins.service"])])

    def test_install_systemd_reloads_supported_daemon_while_delivery_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            tower, "daemon_process_identity", return_value={"start_ticks": 123, "boot_id": "fixture-boot"}
        ):
            root = Path(tmp) / "queue"
            self._write_request(root, "inflight", tmp)
            queue_store.move_request(root / "pending" / "inflight.json", root / "running")
            home = Path(tmp) / "reins-home"
            runtime = home / "daemon-runtime.json"
            runtime.parent.mkdir(parents=True)
            runtime.write_text(
                json.dumps({"pid": os.getpid(), "reload_protocol": tower.RELOAD_PROTOCOL_VERSION,
                            "process_identity": tower.daemon_process_identity(os.getpid()),
                            "queue_dir": str(root.resolve())}) + "\n",
                encoding="utf-8",
            )
            args = argparse.Namespace(
                name="harness-reins",
                queue_dir=str(root),
                interval=2.0,
                retries=3,
                retry_delay=30.0,
                retry_backoff=2.0,
                resume_timeout=3600.0,
                restart_sec=5.0,
                exec_start=None,
                codex_bin=None,
                path="/bin",
                proxy_env_file=None,
                inherit_proxy=False,
                force=True,
                enable=True,
                now=True,
                print=False,
            )
            with (
                mock.patch.dict(os.environ, {"REINS_HOME": str(home)}, clear=False),
                mock.patch.object(services, "systemd_user_dir", return_value=Path(tmp) / "systemd"),
                mock.patch.object(cli.shutil, "which", side_effect=lambda command: "/usr/bin/systemctl" if command == "systemctl" else None),
                mock.patch.object(services, "run_systemctl", return_value=0) as run_systemctl,
            ):
                self.assertEqual(services.install_systemd(args), 0)

            self.assertEqual(
                run_systemctl.call_args_list,
                [
                    mock.call(["daemon-reload"]),
                    mock.call(["enable", "harness-reins.service"]),
                    mock.call(["reload", "harness-reins.service"]),
                ],
            )

    def test_supervisor_config_does_not_embed_proxy_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "reins-home"
            proxy = home / "service-proxy.env"
            proxy.parent.mkdir(parents=True)
            proxy.write_text("HTTPS_PROXY=https://user:secret@proxy.example:8443\n", encoding="utf-8")
            args = argparse.Namespace(
                name="harness-reins",
                queue_dir="/tmp/queue",
                interval=2.0,
                retries=3,
                retry_delay=30.0,
                retry_backoff=2.0,
                resume_timeout=3600.0,
                exec_start="/usr/local/bin/harness-reins",
                codex_bin="/usr/local/bin/codex",
                path="/bin",
            )
            with mock.patch.dict(os.environ, {"REINS_HOME": str(home)}, clear=False):
                text = services.supervisor_config_text(args)

            self.assertIn(f"{tower_environment.PROXY_ENV_FILE_ENV}=", text)
            self.assertNotIn("secret@proxy.example", text)

    def test_proxy_parser_rejects_values_unsafe_for_service_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / ".env"
            source.write_text("HTTPS_PROXY=https://user:bad password@proxy.example\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "unsafe for a systemd environment file"):
                tower_environment.parse_proxy_environment_file(source)

    def test_clear_proxy_removes_persisted_proxy_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "reins-home"
            proxy = home / "service-proxy.env"
            proxy.parent.mkdir(parents=True)
            proxy.write_text("HTTP_PROXY=http://proxy.example:8080\n", encoding="utf-8")
            args = argparse.Namespace(proxy_env_file=None, inherit_proxy=False, clear_proxy=True)

            with mock.patch.dict(os.environ, {"REINS_HOME": str(home)}, clear=False):
                self.assertIsNone(tower_environment.configure_proxy_environment(args))

            self.assertFalse(proxy.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX SIGHUP and execv reload protocol")
    def test_daemon_hup_waits_for_live_delivery_workers_before_reexec(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                queue_dir=str(Path(tmp) / "queue"),
                interval=2.0,
                retries=3,
                retry_delay=30.0,
                retry_backoff=2.0,
                resume_timeout=3600.0,
                once=True,
                max_items=None,
            )
            process = mock.Mock()
            process.poll.return_value = None
            delivery = delivery_engine.BackgroundResume(process=process, target_key="session:test", deadline=time.monotonic() + 60, request_id="inflight")

            def signal_immediately(signum: int, handler: object) -> object:
                if signum == signal.SIGHUP and callable(handler):
                    handler(signal.SIGHUP, None)
                return signal.SIG_DFL

            with (
                mock.patch.dict(os.environ, {"CODEX_HOME": str(Path(tmp) / "codex-home")}, clear=False),
                mock.patch.dict(delivery_engine._BACKGROUND_RESUMES, {123: delivery}, clear=True),
                mock.patch.object(cli.signal, "signal", side_effect=signal_immediately),
                mock.patch.object(cli.os, "execv") as execv,
            ):
                self.assertEqual(tower.daemon(args), 0)

            execv.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "POSIX SIGHUP and execv reload protocol")
    def test_daemon_hup_reexecs_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                queue_dir=str(Path(tmp) / "queue"),
                interval=2.0,
                retries=3,
                retry_delay=30.0,
                retry_backoff=2.0,
                resume_timeout=3600.0,
                once=False,
                max_items=None,
            )
            expected = tower.daemon_reexec_command(args)

            def signal_immediately(signum: int, handler: object) -> object:
                if signum == signal.SIGHUP and callable(handler):
                    handler(signal.SIGHUP, None)
                return signal.SIG_DFL

            with (
                mock.patch.dict(os.environ, {"CODEX_HOME": str(Path(tmp) / "codex-home")}, clear=False),
                mock.patch.object(cli.signal, "signal", side_effect=signal_immediately),
                mock.patch.object(cli.os, "execv", side_effect=RuntimeError("reexec")) as execv,
            ):
                with self.assertRaisesRegex(RuntimeError, "reexec"):
                    tower.daemon(args)

            execv.assert_called_once_with(sys.executable, expected)

    def test_install_systemd_starts_supervisor_in_container(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                name="harness-reins",
                queue_dir="/tmp/queue",
                interval=2.0,
                retries=3,
                retry_delay=30.0,
                retry_backoff=2.0,
                restart_sec=5.0,
                exec_start=None,
                codex_bin=None,
                path="/bin",
                force=True,
                enable=True,
                now=True,
                print=False,
            )

            with (
                mock.patch.object(services, "systemd_user_dir", return_value=Path(tmp)),
                mock.patch.object(cli.shutil, "which", side_effect=lambda command: "/usr/bin/supervisorctl" if command == "supervisorctl" else None),
                mock.patch.object(services, "running_in_container", return_value=True),
                mock.patch.object(services, "install_supervisor", return_value=0) as install_supervisor,
            ):
                self.assertEqual(services.install_systemd(args), 0)

            self.assertTrue((Path(tmp) / "harness-reins.service").exists())
            install_supervisor.assert_called_once()

    def test_install_systemd_starts_standalone_without_supervisor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                name="harness-reins",
                queue_dir="/tmp/queue",
                interval=2.0,
                retries=3,
                retry_delay=30.0,
                retry_backoff=2.0,
                restart_sec=5.0,
                exec_start=None,
                codex_bin=None,
                path="/bin",
                force=True,
                enable=True,
                now=True,
                print=False,
            )

            with (
                mock.patch.object(services, "systemd_user_dir", return_value=Path(tmp)),
                mock.patch.object(cli.shutil, "which", return_value=None),
                mock.patch.object(services, "running_in_container", return_value=True),
                mock.patch.object(services, "start_standalone_daemon", return_value=0) as start_standalone,
            ):
                self.assertEqual(services.install_systemd(args), 0)

            self.assertTrue((Path(tmp) / "harness-reins.service").exists())
            start_standalone.assert_called_once()

    def test_install_systemd_falls_back_when_user_bus_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                name="harness-reins",
                queue_dir="/tmp/queue",
                interval=2.0,
                retries=3,
                retry_delay=30.0,
                retry_backoff=2.0,
                restart_sec=5.0,
                exec_start=None,
                codex_bin=None,
                path="/bin",
                force=True,
                enable=True,
                now=True,
                print=False,
            )

            with (
                mock.patch.object(services, "systemd_user_dir", return_value=Path(tmp)),
                mock.patch.object(cli.shutil, "which", side_effect=lambda command: "/usr/bin/systemctl" if command == "systemctl" else None),
                mock.patch.object(services, "run_systemctl", return_value=1),
                mock.patch.object(services, "start_standalone_daemon", return_value=0) as start_standalone,
            ):
                self.assertEqual(services.install_systemd(args), 0)

            start_standalone.assert_called_once()

    def test_supervisor_config_uses_autorestart(self) -> None:
        args = argparse.Namespace(
            name="harness-reins",
            queue_dir="/tmp/queue",
            interval=2.0,
            retries=3,
            retry_delay=30.0,
            retry_backoff=2.0,
            exec_start="/usr/local/bin/harness-reins",
            codex_bin="/usr/local/bin/codex",
            path="/bin",
        )

        text = services.supervisor_config_text(args)

        self.assertIn("[program:harness-reins]", text)
        self.assertIn("autostart=true", text)
        self.assertIn("autorestart=true", text)
        self.assertIn("--queue-dir " + shlex.quote(str(Path("/tmp/queue"))), text)

    def test_cancel_moves_pending_to_canceled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "cancel-me", tmp)

            self.assertTrue(recovery.cancel_one(root, "cancel-me", "not needed"))

            self.assertFalse((root / "pending" / "cancel-me.json").exists())
            canceled = root / "canceled" / "cancel-me.json"
            self.assertTrue(canceled.exists())
            request = json.loads(canceled.read_text(encoding="utf-8"))
            self.assertEqual(request["id"], "cancel-me")
            self.assertEqual(request["canceled_from"], "pending")
            self.assertEqual(request["cancel_message"], "not needed")

    def test_cancel_all_moves_pending_and_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            self._write_request(root, "pending-one", tmp)
            (root / "running").mkdir(parents=True)
            (root / "running" / "running-one.json").write_text(
                json.dumps(
                    reins_records.callback_record("running-one", tmp, target={"kind": "session", "session": "fixture-session"}, prompt="wake up", created_at=1.0)
                )
                + "\n",
                encoding="utf-8",
            )

            self.assertEqual(recovery.cancel_all(root), 2)

            self.assertFalse((root / "pending" / "pending-one.json").exists())
            self.assertFalse((root / "running" / "running-one.json").exists())
            self.assertTrue((root / "canceled" / "pending-one.json").exists())
            self.assertTrue((root / "canceled" / "running-one.json").exists())

    def test_status_lists_pending_task_and_bound_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            (root / "pending").mkdir(parents=True)
            request = reins_records.callback_record("pending-id", tmp, target={"kind": "session", "session": "thread-1"}, prompt="[reins]\nTask: train model\n", created_at=1.0)
            (root / "pending" / "pending-id.json").write_text(json.dumps(request), encoding="utf-8")
            args = argparse.Namespace(queue_dir=str(root), state=None, limit=5, quiet_empty=False, shell_hook=True)

            with mock.patch("builtins.print") as output:
                self.assertEqual(callback_commands.status(args), 0)

            rendered = "\n".join(str(call.args[0]) for call in output.call_args_list)
            self.assertIn("train model", rendered)
            self.assertIn("session thread-1", rendered)
            self.assertIn("pending-id", rendered)

    def test_status_is_quiet_when_queue_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(queue_dir=tmp, state=None, limit=5, quiet_empty=True, shell_hook=True)
            with mock.patch("builtins.print") as output:
                self.assertEqual(callback_commands.status(args), 0)
            output.assert_not_called()

    def test_install_shell_hook_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rc_file = Path(tmp) / ".bashrc"
            rc_file.write_text("export EXISTING=1\n", encoding="utf-8")
            args = argparse.Namespace(rc_file=str(rc_file), command="/opt/bin/harness-reins")

            self.assertEqual(skill_install.install_shell_hook(args), 0)
            self.assertEqual(skill_install.install_shell_hook(args), 0)

            rendered = rc_file.read_text(encoding="utf-8")
            self.assertIn("export EXISTING=1", rendered)
            self.assertEqual(rendered.count(skill_install.SHELL_HOOK_BEGIN), 1)
            self.assertEqual(rendered.count(skill_install.SHELL_HOOK_END), 1)
            self.assertIn("timeout 2s /opt/bin/harness-reins status", rendered)

    def test_completed_goal_is_terminal_and_suppresses_reminders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            start_args = argparse.Namespace(
                queue_dir=str(root), id="terminal-goal", session="thread-1", last=False,
                cwd=tmp, task="finish report", plan_file=str(self._goal_plan(tmp, status="completed")),
                idle_seconds=1.0,
            )
            self.assertEqual(goal_commands.goal_start(start_args), 0)
            self.assertEqual(
                goal_commands.goal_check(argparse.Namespace(queue_dir=str(root), id="terminal-goal")),
                0,
            )
            plan_digest = goals.load_goal(root, "terminal-goal")["last_plan_check_sha256"]
            ack_args = argparse.Namespace(
                queue_dir=str(root), id="terminal-goal", state="completed", message="done",
                condition=None, email_to=None, email_after=goals.DEFAULT_BLOCKED_EMAIL_SECONDS,
                plan_sha256=plan_digest,
            )
            self.assertEqual(goal_commands.goal_ack(ack_args), 0)

            goals.update_goal_from_callback(root, {"goal_id": "terminal-goal", "created_at": time.time()})
            self.assertEqual(
                dispatch.enqueue_existing_request(
                    root,
                    reins_records.callback_record("terminal-callback", tmp, target={"kind": "session", "session": "thread-1"}, prompt="Task: stale callback", created_at=time.time(), target_source="--session", goal_id="terminal-goal"),
                    "Task: stale callback",
                ),
                1,
            )

            goal = goals.load_goal(root, "terminal-goal")
            self.assertEqual(goal["state"], "completed")
            self.assertFalse((root / "pending" / "terminal-callback.json").exists())
            self.assertFalse(goals.process_goal_reminders(root))
            self.assertEqual(
                goal_commands.goal_resume(argparse.Namespace(queue_dir=str(root), id="terminal-goal")),
                1,
            )

    def test_goal_completion_follows_latest_mutable_yaml_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            plan_path = Path(tmp) / "release-goal.yaml"

            def write_plan(revision: int, goal: str, second_status: str, third_status: str | None = None) -> None:
                third = ""
                amendments = ""
                if third_status is not None:
                    third = (
                        "  - id: publish\n"
                        "    title: Publish the revised release\n"
                        f"    status: {third_status}\n"
                    )
                    amendments = textwrap.dedent(
                        """\
                        amendments:
                          - revision: 2
                            reason: User added a publication step
                        """
                    )
                plan_path.write_text(
                    textwrap.dedent(
                        f"""\
                        version: 1
                        revision: {revision}
                        goal: {goal}
                        path:
                          - id: implement
                            title: Implement the feature
                            status: completed
                          - id: verify
                            title: Verify the feature
                            status: {second_status}
                        """
                    )
                    + third
                    + amendments,
                    encoding="utf-8",
                )

            write_plan(1, "Release version 0.6.2", "pending")
            start_args = argparse.Namespace(
                queue_dir=str(root), id="mutable-goal", session="thread-1", last=False,
                cwd=tmp, task="release", plan_file=str(plan_path), idle_seconds=1.0,
            )
            self.assertEqual(goal_commands.goal_start(start_args), 0)
            self.assertEqual(goal_commands.goal_check(argparse.Namespace(queue_dir=str(root), id="mutable-goal")), 0)
            first_digest = goals.load_goal(root, "mutable-goal")["last_plan_check_sha256"]
            incomplete_ack = argparse.Namespace(
                queue_dir=str(root), id="mutable-goal", state="completed", message=None,
                condition=None, email_to=None, email_after=goals.DEFAULT_BLOCKED_EMAIL_SECONDS,
                plan_sha256=first_digest,
            )
            self.assertEqual(goal_commands.goal_ack(incomplete_ack), 1)

            write_plan(2, "Release and publish version 0.6.2", "completed", "pending")
            self.assertEqual(goal_commands.goal_ack(incomplete_ack), 1)
            output = io.StringIO()
            with mock.patch("sys.stdout", output):
                self.assertEqual(goal_commands.goal_check(argparse.Namespace(queue_dir=str(root), id="mutable-goal")), 0)
            self.assertIn("Plan revision: 2", output.getvalue())
            self.assertIn("Current item: publish [pending]", output.getvalue())
            self.assertIn("Release and publish version 0.6.2", output.getvalue())

            write_plan(3, "Release and publish version 0.6.2", "completed", "completed")
            self.assertEqual(goal_commands.goal_ack(incomplete_ack), 1)
            self.assertEqual(goal_commands.goal_check(argparse.Namespace(queue_dir=str(root), id="mutable-goal")), 0)
            final_digest = goals.load_goal(root, "mutable-goal")["last_plan_check_sha256"]
            incomplete_ack.plan_sha256 = final_digest
            self.assertEqual(goal_commands.goal_ack(incomplete_ack), 0)
            goal = goals.load_goal(root, "mutable-goal")
            self.assertEqual(goal["state"], "completed")
            self.assertEqual(goal["completion_plan_revision"], 3)

    def test_goal_start_rejects_missing_or_nonsequential_yaml_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            base = dict(
                queue_dir=str(root), id="invalid-goal", session="thread-1", last=False,
                cwd=tmp, task="invalid", idle_seconds=1.0,
            )
            self.assertEqual(goal_commands.goal_start(argparse.Namespace(**base, plan_file=None)), 2)

            plan_path = Path(tmp) / "invalid.yaml"
            plan_path.write_text(
                textwrap.dedent(
                    """\
                    version: 1
                    revision: 1
                    goal: Invalid skipped path
                    path:
                      - id: first
                        title: First item
                        status: pending
                      - id: second
                        title: Second item
                        status: completed
                    """
                ),
                encoding="utf-8",
            )
            self.assertEqual(goal_commands.goal_start(argparse.Namespace(**base, plan_file=str(plan_path))), 2)

    def test_goal_set_plan_migrates_legacy_goal_and_clears_old_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            queue_store.ensure_daemon_dirs(root)
            goals.write_goal(
                root,
                {
                    "version": 1,
                    "id": "legacy-goal",
                    "task": "legacy task",
                    "cwd": tmp,
                    "target": {"kind": "session", "session": "thread-1"},
                    "state": "active",
                    "created_at": time.time(),
                    "last_external_callback_at": time.time(),
                    "last_plan_check_sha256": "obsolete",
                },
            )
            plan_path = self._goal_plan(tmp, status="completed")
            args = argparse.Namespace(
                queue_dir=str(root), id="legacy-goal", plan_file=str(plan_path)
            )
            self.assertEqual(goal_commands.goal_set_plan(args), 0)
            goal = goals.load_goal(root, "legacy-goal")
            self.assertEqual(goal["plan_file"], str(plan_path.resolve()))
            self.assertNotIn("last_plan_check_sha256", goal)

            no_check_ack = argparse.Namespace(
                queue_dir=str(root), id="legacy-goal", state="completed", message=None,
                condition=None, email_to=None, email_after=goals.DEFAULT_BLOCKED_EMAIL_SECONDS,
                plan_sha256=goal["plan_sha256_when_attached"],
            )
            self.assertEqual(goal_commands.goal_ack(no_check_ack), 1)
            self.assertEqual(goal_commands.goal_check(argparse.Namespace(queue_dir=str(root), id="legacy-goal")), 0)
            no_check_ack.plan_sha256 = goals.load_goal(root, "legacy-goal")["last_plan_check_sha256"]
            self.assertEqual(goal_commands.goal_ack(no_check_ack), 0)

    def test_goal_reminder_is_deduplicated_and_external_callback_resets_idle_timer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            start_args = argparse.Namespace(
                queue_dir=str(root), id="reminder-goal", session="thread-1", last=False,
                cwd=tmp, task="train model", plan_file=str(self._goal_plan(tmp)), idle_seconds=1.0,
            )
            self.assertEqual(goal_commands.goal_start(start_args), 0)
            # Drive the reminder clock explicitly: with a 1s idle window, a slow
            # fsync between two real-time calls would otherwise age the reminder.
            clock = argparse.Namespace(now=time.time())
            patcher = mock.patch.object(goals, "time", argparse.Namespace(time=lambda: clock.now))
            patcher.start()
            self.addCleanup(patcher.stop)
            goal = goals.load_goal(root, "reminder-goal")
            goal["last_external_callback_at"] = clock.now - 2.0
            goals.write_goal(root, goal)

            self.assertTrue(goals.process_goal_reminders(root))
            clock.now += 0.5  # Still inside the reminder's idle window.
            self.assertFalse(goals.process_goal_reminders(root))
            pending = list((root / "pending").glob("*.json"))
            self.assertEqual(len(pending), 1)
            reminder = dispatch.load_request(pending[0])
            self.assertTrue(reminder["goal_reminder"])
            self.assertIn(reins_runtime.control_command("goal", "ack", "--queue-dir", str(root), "--id", "reminder-goal"), reminder["prompt"])
            self.assertIn(reins_runtime.control_command("goal", "check", "--queue-dir", str(root), "--id", "reminder-goal"), reminder["prompt"])
            self.assertIn("--condition", reminder["prompt"])
            self.assertIn("Current item: finish [pending]", reminder["prompt"])
            self.assertIn("Remaining path:", reminder["prompt"])

            queue_store.move_request(pending[0], root / "done")
            goal = goals.load_goal(root, "reminder-goal")
            goal["last_reminder_at"] = clock.now - 2.0
            goals.write_goal(root, goal)
            self.assertTrue(goals.process_goal_reminders(root))
            self.assertEqual(len(list((root / "pending").glob("*.json"))), 1)

            for path in (root / "pending").glob("*.json"):
                queue_store.move_request(path, root / "done")
            callback_at = clock.now
            goals.update_goal_from_callback(root, {"goal_id": "reminder-goal", "created_at": callback_at})
            goal = goals.load_goal(root, "reminder-goal")
            self.assertNotIn("last_reminder_at", goal)
            self.assertEqual(float(goal["last_external_callback_at"]), callback_at)
            clock.now += 0.5  # The external callback restarted the idle timer.
            self.assertFalse(goals.process_goal_reminders(root))
            clock.now += 1.0
            self.assertTrue(goals.process_goal_reminders(root))
            self.assertEqual(len(list((root / "pending").glob("*.json"))), 1)

    def test_goal_reminder_reconciles_callback_persisted_before_goal_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            start_args = argparse.Namespace(
                queue_dir=str(root), id="recovery-goal", session="thread-1", last=False,
                cwd=tmp, task="run recovery", plan_file=str(self._goal_plan(tmp)), idle_seconds=1.0,
            )
            self.assertEqual(goal_commands.goal_start(start_args), 0)
            goal = goals.load_goal(root, "recovery-goal")
            goal["last_external_callback_at"] = time.time() - 2.0
            goals.write_goal(root, goal)
            callback_time = time.time()
            queue_store.write_request(
                root / "pending" / "crash-window.json",
                reins_records.callback_record("crash-window", tmp, target={"kind": "session", "session": "thread-1"}, prompt="Task: callback persisted before goal timestamp", created_at=callback_time, goal_id="recovery-goal"),
            )

            self.assertFalse(goals.process_goal_reminders(root))
            goal = goals.load_goal(root, "recovery-goal")
            self.assertEqual(float(goal["last_external_callback_at"]), callback_time)
            self.assertNotIn("last_reminder_at", goal)

    def test_blocked_goal_email_is_configured_and_sent_once_after_delay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"REINS_BLOCKED_EMAIL_TO": "owner@example.test"}, clear=False
        ):
            root = Path(tmp) / "queue"
            start_args = argparse.Namespace(
                queue_dir=str(root), id="blocked-goal", session="thread-1", last=False,
                cwd=tmp, task="wait for access", plan_file=str(self._goal_plan(tmp)), idle_seconds=1.0,
            )
            self.assertEqual(goal_commands.goal_start(start_args), 0)
            bad_ack_args = argparse.Namespace(
                queue_dir=str(root), id="blocked-goal", state="blocked_conditions", message=None,
                condition="need credentials", email_to="other@example.test", email_after=1.0,
            )
            self.assertEqual(goal_commands.goal_ack(bad_ack_args), 2)
            ack_args = argparse.Namespace(
                queue_dir=str(root), id="blocked-goal", state="blocked_conditions", message=None,
                condition="need credentials", email_to="owner@example.test", email_after=1.0,
            )
            self.assertEqual(goal_commands.goal_ack(ack_args), 0)
            goal = goals.load_goal(root, "blocked-goal")
            goal["state_changed_at"] = time.time() - 2.0
            goals.write_goal(root, goal)

            accepted = subprocess.CompletedProcess(["sendmail"], 0)
            with mock.patch.object(cli.subprocess, "run", return_value=accepted) as sendmail:
                self.assertTrue(goals.process_blocked_goal_email(root))
                self.assertFalse(goals.process_blocked_goal_email(root))

            self.assertEqual(sendmail.call_count, 1)
            self.assertEqual(goals.load_goal(root, "blocked-goal")["blocked_email_result"], "accepted")

    def test_blocked_goal_email_requires_one_recipient_and_stops_after_three_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {"REINS_BLOCKED_EMAIL_TO": "owner@example.test"}, clear=False
        ):
            root = Path(tmp) / "queue"
            start_args = argparse.Namespace(
                queue_dir=str(root), id="retry-goal", session="thread-1", last=False,
                cwd=tmp, task="wait for service", plan_file=str(self._goal_plan(tmp)), idle_seconds=1.0,
            )
            self.assertEqual(goal_commands.goal_start(start_args), 0)
            multi_recipient_args = argparse.Namespace(
                queue_dir=str(root), id="retry-goal", state="blocked_conditions", message=None,
                condition="need service", email_to="owner@example.test,other@example.test", email_after=1.0,
            )
            self.assertEqual(goal_commands.goal_ack(multi_recipient_args), 2)
            ack_args = argparse.Namespace(
                queue_dir=str(root), id="retry-goal", state="blocked_conditions", message=None,
                condition="need service", email_to="owner@example.test", email_after=1.0,
            )
            self.assertEqual(goal_commands.goal_ack(ack_args), 0)
            goal = goals.load_goal(root, "retry-goal")
            goal["state_changed_at"] = time.time() - 2.0
            goals.write_goal(root, goal)

            failed = subprocess.CompletedProcess(["sendmail"], 1)
            with mock.patch.object(cli.subprocess, "run", return_value=failed) as sendmail:
                for attempt in range(3):
                    self.assertTrue(goals.process_blocked_goal_email(root))
                    if attempt < 2:
                        goal = goals.load_goal(root, "retry-goal")
                        goal["blocked_email_next_attempt_at"] = time.time() - 1.0
                        goals.write_goal(root, goal)
                self.assertFalse(goals.process_blocked_goal_email(root))

            goal = goals.load_goal(root, "retry-goal")
            self.assertEqual(sendmail.call_count, 3)
            self.assertIn("blocked_email_exhausted_at", goal)

    @unittest.skipUnless(os.name == "posix", "Desktop transport uses POSIX peer credentials")
    def test_desktop_app_server_delivery_preserves_turn_completion_and_unknown_submit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            def start_server(*, reply_to_turn_start: bool) -> tuple[Path, threading.Thread, list[dict[str, object]], list[BaseException]]:
                socket_path = Path(tmp) / ("confirmed.sock" if reply_to_turn_start else "unknown.sock")
                listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                listener.bind(str(socket_path))
                listener.listen(1)
                messages: list[dict[str, object]] = []
                errors: list[BaseException] = []

                def read_exact(connection: socket.socket, size: int) -> bytes:
                    chunks = bytearray()
                    while len(chunks) < size:
                        chunk = connection.recv(size - len(chunks))
                        if not chunk:
                            raise AssertionError("fake App Server closed unexpectedly")
                        chunks.extend(chunk)
                    return bytes(chunks)

                def read_frame(connection: socket.socket) -> dict[str, object]:
                    first, second = read_exact(connection, 2)
                    self.assertEqual(first & 0x0F, 0x1)
                    length = second & 0x7F
                    if length == 126:
                        length = int.from_bytes(read_exact(connection, 2), "big")
                    self.assertTrue(second & 0x80)
                    mask = read_exact(connection, 4)
                    payload = read_exact(connection, length)
                    decoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
                    return json.loads(decoded.decode("utf-8"))

                def send_json(connection: socket.socket, message: dict[str, object]) -> None:
                    payload = json.dumps(message).encode("utf-8")
                    self.assertLess(len(payload), 126)
                    connection.sendall(bytes([0x81, len(payload)]) + payload)

                def serve() -> None:
                    try:
                        connection, _ = listener.accept()
                        with connection:
                            request = bytearray()
                            while not request.endswith(b"\r\n\r\n"):
                                request.extend(read_exact(connection, 1))
                            key = next(
                                line.split(b":", 1)[1].strip()
                                for line in bytes(request).split(b"\r\n")
                                if line.lower().startswith(b"sec-websocket-key:")
                            )
                            accepted = base64.b64encode(
                                hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
                            )
                            connection.sendall(
                                b"HTTP/1.1 101 Switching Protocols\r\n"
                                b"Connection: Upgrade\r\n"
                                b"Upgrade: websocket\r\n"
                                b"Sec-WebSocket-Accept: " + accepted + b"\r\n\r\n"
                            )
                            for expected_method in ("initialize", "initialized", "thread/resume", "turn/start"):
                                message = read_frame(connection)
                                self.assertEqual(message["method"], expected_method)
                                messages.append(message)
                                if "id" not in message:
                                    continue
                                if expected_method == "turn/start" and not reply_to_turn_start:
                                    return
                                if expected_method == "initialize":
                                    send_json(connection, {"method": "thread/status/changed", "params": {}})
                                result: dict[str, object] = {"turn": {"id": "desktop-turn"}} if expected_method == "turn/start" else {}
                                send_json(connection, {"id": message["id"], "result": result})
                            send_json(
                                connection,
                                {
                                    "method": "turn/completed",
                                    "params": {"threadId": "desktop-thread", "turn": {"id": "desktop-turn"}},
                                },
                            )
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        listener.close()

                thread = threading.Thread(target=serve)
                thread.start()
                return socket_path, thread, messages, errors

            request = {
                "target": {"kind": "session", "session": "desktop-thread"},
                "queue_dir": str(Path(tmp) / "queue"),
                "sandbox_mode": "workspace-write",
                "approval_policy": "on-request",
                "approvals_reviewer": "auto_review",
            }
            payload = {"request": request, "prompt": "Desktop callback test", "cwd": tmp, "timeout": 2.0}
            socket_path, server_thread, messages, errors = start_server(reply_to_turn_start=True)
            with mock.patch.dict(
                os.environ,
                {
                    app_server.DESKTOP_APP_SERVER_ENV: "1",
                    app_server.APP_SERVER_SOCKET_ENV: str(socket_path),
                    app_server.ALLOW_APP_SERVER_SOCKET_OVERRIDE_ENV: "1",
                },
                clear=False,
            ):
                delivery = delivery_engine.start_desktop_app_server_turn(payload)
            self.assertIsNotNone(delivery)
            assert delivery is not None
            self.assertTrue(delivery.wait_for_completion(1.0))
            delivery.close()
            server_thread.join(timeout=5)

            self.assertFalse(server_thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual([message["method"] for message in messages], ["initialize", "initialized", "thread/resume", "turn/start"])
            self.assertTrue(all("jsonrpc" not in message for message in messages))
            self.assertEqual(messages[0]["params"]["capabilities"], {"experimentalApi": True})
            self.assertEqual(
                messages[-1]["params"]["sandboxPolicy"],
                {"type": "workspaceWrite", "writableRoots": [str(Path(tmp) / "queue")]},
            )

            socket_path, server_thread, messages, errors = start_server(reply_to_turn_start=False)
            with mock.patch.dict(
                os.environ,
                {
                    app_server.DESKTOP_APP_SERVER_ENV: "1",
                    app_server.APP_SERVER_SOCKET_ENV: str(socket_path),
                    app_server.ALLOW_APP_SERVER_SOCKET_OVERRIDE_ENV: "1",
                },
                clear=False,
            ):
                unknown_delivery = delivery_engine.start_desktop_app_server_turn(payload)
            self.assertIsNotNone(unknown_delivery)
            assert unknown_delivery is not None
            self.assertFalse(unknown_delivery.wait_for_completion(0.01))
            unknown_delivery.close()
            server_thread.join(timeout=5)
            self.assertFalse(server_thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(messages[-1]["method"], "turn/start")

    def test_desktop_delivery_worker_releases_lease_immediately_after_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ack_marker = Path(tmp) / "acks" / "callback.json"

            class CompletingDelivery:
                def __init__(self) -> None:
                    self.waits = 0
                    self.closed = False

                def wait_for_completion(self, _timeout: float) -> bool:
                    self.waits += 1
                    if self.waits == 1:
                        ack_marker.parent.mkdir(parents=True)
                        ack_marker.write_text("{}\n", encoding="utf-8")
                        return False
                    return True

                def close(self) -> None:
                    self.closed = True

            delivery = CompletingDelivery()
            payload = {
                "command": ["unused"],
                "prompt": "wake up",
                "cwd": tmp,
                "request": {},
                "timeout": 1.0,
                "ack_path": str(ack_marker),
                "canceled_path": str(Path(tmp) / "canceled.json"),
            }
            read_fd, lock_fd = os.pipe()
            output = io.StringIO()
            try:
                with (
                    mock.patch.dict(os.environ, {"CODEX_LONG_TASK_DELIVERY_LOCK_FDS": json.dumps([lock_fd])}, clear=False),
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                    mock.patch.object(sys, "stdout", output),
                    mock.patch.object(delivery_engine, "start_desktop_app_server_turn", return_value=delivery),
                    mock.patch.object(cli.subprocess, "Popen") as popen,
                ):
                    self.assertEqual(delivery_engine.delivery_worker_main(), 0)
            finally:
                os.close(read_fd)

            result = json.loads(output.getvalue())
            self.assertEqual(result["returncode"], 0)
            self.assertEqual(result["skipped"], "acknowledged")
            self.assertEqual(delivery.waits, 1)
            self.assertTrue(delivery.closed)
            popen.assert_not_called()

    def test_codex_cli_resume_into_a_live_thread_waits_instead_of_failing(self) -> None:
        # The original `codex exec` still runs, so `codex exec resume` exits 1 before any turn.
        cases = (("Error: thread/resume failed: thread t1 already has an active writer (code -32600)", 123),
                 ("Error: thread/resume failed: no rollout found for thread id t1", 1))
        for message, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                payload = {
                    "command": [sys.executable, "-c", f"import sys; sys.stdin.read(); sys.stderr.write({message!r}); sys.exit(1)"],
                    "prompt": "wake up",
                    "cwd": tmp,
                    "request": {"agent": "codex", "target": {"kind": "session", "session": "t1"}},
                    "timeout": 30.0,
                    "ack_path": str(Path(tmp) / "acks" / "callback.json"),
                    "canceled_path": str(Path(tmp) / "canceled.json"),
                }
                read_fd, lock_fd = os.pipe()
                output = io.StringIO()
                # Popen needs a real descriptor; close it before Windows removes the directory.
                with open(Path(tmp) / "worker.err", "w+", encoding="utf-8") as errors:
                    try:
                        with (
                            mock.patch.dict(os.environ, {"CODEX_LONG_TASK_DELIVERY_LOCK_FDS": json.dumps([lock_fd])}, clear=False),
                            mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                            mock.patch.object(sys, "stdout", output),
                            mock.patch.object(sys, "stderr", errors),
                            mock.patch.object(delivery_engine, "start_desktop_app_server_turn", return_value=None),
                        ):
                            delivery_engine.delivery_worker_main()
                    finally:
                        os.close(read_fd)
                    self.assertEqual(json.loads(output.getvalue())["returncode"], expected)
                    errors.seek(0)
                    self.assertIn(message, errors.read())  # Codex's stderr still reaches the daemon log.

    def test_desktop_app_server_explicit_turn_rejection_allows_cli_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            class RejectedConnection:
                def __init__(self) -> None:
                    self.methods: list[str] = []
                    self.closed = False

                def connect(self) -> None:
                    pass

                def request(self, method: str, _params: dict[str, object]) -> dict[str, object]:
                    self.methods.append(method)
                    if method == "turn/start":
                        raise app_server.AppServerRpcError("App Server turn/start error: rejected")
                    return {}

                def notify(self, method: str, _params: dict[str, object]) -> None:
                    self.methods.append(method)

                def close(self) -> None:
                    self.closed = True

            connection = RejectedConnection()
            payload = {
                "request": {
                    "target": {"kind": "session", "session": "desktop-thread"},
                    "queue_dir": str(Path(tmp) / "queue"),
                    "sandbox_mode": "workspace-write",
                },
                "prompt": "Desktop callback test",
                "cwd": tmp,
                "timeout": 1.0,
            }
            with (
                mock.patch.dict(os.environ, {app_server.DESKTOP_APP_SERVER_ENV: "1"}, clear=False),
                mock.patch.object(app_server, "desktop_app_server_socket", return_value=Path(tmp) / "fake.sock"),
                mock.patch.object(app_server, "AppServerConnection", return_value=connection),
            ):
                self.assertIsNone(delivery_engine.start_desktop_app_server_turn(payload))

            self.assertEqual(connection.methods, ["initialize", "initialized", "thread/resume", "turn/start"])
            self.assertTrue(connection.closed)

    def test_desktop_app_server_handles_masked_frames_and_rejects_invalid_protocol(self) -> None:
        deadline = time.monotonic() + 1.0
        for frame in (
            bytes([0xC1, 0x00]),
            bytes([0x09, 0x00]),
        ):
            connection = app_server.AppServerConnection(Path("/tmp/not-used.sock"), 1.0)
            connection.buffer = frame
            with self.assertRaises(app_server.AppServerProtocolError):
                connection._receive_frame(deadline)

        payload = b'{"id":1,"result":{}}'
        mask = b"mask"
        encoded = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        connection = app_server.AppServerConnection(Path("/tmp/not-used.sock"), 1.0)
        connection.buffer = bytes([0x81, 0x80 | len(payload)]) + mask + encoded
        self.assertEqual(connection._receive_frame(deadline), (True, 0x1, payload))

        connection = app_server.AppServerConnection(Path("/tmp/not-used.sock"), 1.0)
        with (
            mock.patch.object(connection, "_send_frame"),
            mock.patch.object(connection, "_receive_json", return_value={"id": 1, "result": {}}),
        ):
            self.assertEqual(connection.request("initialize", {}), {})

        connection = app_server.AppServerConnection(Path("/tmp/not-used.sock"), 1.0)
        invalid_message = json.dumps(
            {
                "params": {"threadId": "desktop-thread", "turn": {"id": "turn-1"}},
            }
        ).encode("utf-8")
        with mock.patch.object(connection, "_receive_message", return_value=(0x1, invalid_message)):
            with self.assertRaises(app_server.AppServerProtocolError):
                connection._receive_json(deadline)

        request = {"target": {"kind": "session", "session": "desktop-thread"}}
        with (
            mock.patch.dict(
                os.environ,
                {
                    app_server.DESKTOP_APP_SERVER_ENV: "1",
                    app_server.APP_SERVER_SOCKET_ENV: "/tmp/override.sock",
                    app_server.ALLOW_APP_SERVER_SOCKET_OVERRIDE_ENV: "0",
                },
                clear=False,
            ),
            mock.patch.object(layout, "codex_home", return_value=Path("/tmp/codex-home")),
        ):
            expected = None if os.name == "nt" else Path("/tmp/codex-home/app-server-control/app-server-control.sock")
            self.assertEqual(app_server.desktop_app_server_socket(request), expected)

    def test_desktop_delivery_worker_uses_cli_after_explicit_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hook = Path(tmp) / "callback-hook.md"
            hook.write_text("Use the custom callback policy.\n", encoding="utf-8")
            payload = {
                "command": [sys.executable, "-c", "pass"],
                "prompt": "wake up",
                "callback_hook_path": str(hook),
                "cwd": tmp,
                "request": {},
                "timeout": 1.0,
                "ack_path": str(Path(tmp) / "acks" / "callback.json"),
                "canceled_path": str(Path(tmp) / "canceled.json"),
            }
            process = mock.Mock(returncode=0)
            process.communicate.return_value = ("", "")
            read_fd, lock_fd = os.pipe()
            output = io.StringIO()
            try:
                with (
                    mock.patch.dict(os.environ, {"CODEX_LONG_TASK_DELIVERY_LOCK_FDS": json.dumps([lock_fd])}, clear=False),
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                    mock.patch.object(sys, "stdout", output),
                    mock.patch.object(delivery_engine, "start_desktop_app_server_turn", return_value=None),
                    mock.patch.object(cli.subprocess, "Popen", return_value=process) as popen,
                ):
                    self.assertEqual(delivery_engine.delivery_worker_main(), 0)
            finally:
                os.close(read_fd)

            self.assertEqual(json.loads(output.getvalue())["returncode"], 0)
            popen.assert_called_once()
            process.communicate.assert_called_once_with(
                input=(
                    "wake up\n\n[reins-user-hook]\n"
                    "Use the custom callback policy."
                ),
                timeout=1.0,
            )

    def test_desktop_delivery_worker_suppresses_cli_on_unknown_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            class IncompleteDelivery:
                def __init__(self) -> None:
                    self.closed = False

                def wait_for_completion(self, timeout: float) -> bool:
                    time.sleep(timeout)
                    return False

                def close(self) -> None:
                    self.closed = True

            delivery = IncompleteDelivery()
            queue_root = Path(tmp) / "queue"
            request = reins_records.callback_record("desktop-timeout", tmp, target={"kind": "session", "session": "desktop-thread"}, prompt="wake up", created_at=1.0)
            payload = {
                "command": ["unused"],
                "prompt": "wake up",
                "cwd": tmp,
                "request": request,
                "queue_dir": str(queue_root),
                "timeout": 1.0,
                "ack_path": str(Path(tmp) / "acks" / "callback.json"),
                "canceled_path": str(Path(tmp) / "canceled.json"),
            }
            read_fd, lock_fd = os.pipe()
            output = io.StringIO()
            try:
                with (
                    mock.patch.dict(
                        os.environ,
                        {
                            "CODEX_LONG_TASK_DELIVERY_LOCK_FDS": json.dumps([lock_fd]),
                            layout.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks"),
                        },
                        clear=False,
                    ),
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                    mock.patch.object(sys, "stdout", output),
                    mock.patch.object(delivery_engine, "start_desktop_app_server_turn", return_value=delivery),
                    mock.patch.object(cli.subprocess, "Popen") as popen,
                ):
                    self.assertEqual(delivery_engine.delivery_worker_main(), 0)
            finally:
                os.close(read_fd)

            result = json.loads(output.getvalue())
            self.assertEqual(result["returncode"], 125)
            self.assertTrue(result["manual_recovery_required"])
            self.assertTrue(delivery.closed)
            popen.assert_not_called()
            with mock.patch.dict(os.environ, {layout.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks")}, clear=False):
                self.assertTrue(delivery_engine.retained_target_lease_is_held(request))

    def test_desktop_delivery_worker_clears_timeout_marker_after_late_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            class IncompleteDelivery:
                def wait_for_completion(self, timeout: float) -> bool:
                    time.sleep(timeout)
                    return False

                def close(self) -> None:
                    pass

            queue_root = Path(tmp) / "queue"
            request = reins_records.callback_record("desktop-late-ack", tmp, target={"kind": "session", "session": "desktop-thread"}, prompt="wake up", created_at=1.0)
            ack = Path(tmp) / "acks" / "callback.json"
            payload = {
                "command": ["unused"],
                "prompt": "wake up",
                "cwd": tmp,
                "request": request,
                "queue_dir": str(queue_root),
                "timeout": 1.0,
                "ack_path": str(ack),
                "canceled_path": str(Path(tmp) / "canceled.json"),
            }
            original_retain = delivery_engine.retain_target_lease

            def retain_then_ack(root: Path, callback: dict[str, object]) -> None:
                original_retain(root, callback)
                queue_store.write_request(ack, {"id": callback["id"], "marked_at": time.time()})

            read_fd, lock_fd = os.pipe()
            output = io.StringIO()
            try:
                with (
                    mock.patch.dict(
                        os.environ,
                        {
                            "CODEX_LONG_TASK_DELIVERY_LOCK_FDS": json.dumps([lock_fd]),
                            layout.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks"),
                        },
                        clear=False,
                    ),
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))),
                    mock.patch.object(sys, "stdout", output),
                    mock.patch.object(delivery_engine, "start_desktop_app_server_turn", return_value=IncompleteDelivery()),
                    mock.patch.object(delivery_engine, "retain_target_lease", side_effect=retain_then_ack),
                ):
                    self.assertEqual(delivery_engine.delivery_worker_main(), 0)
            finally:
                os.close(read_fd)

            self.assertEqual(json.loads(output.getvalue())["returncode"], 0)
            with mock.patch.dict(os.environ, {layout.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks")}, clear=False):
                self.assertFalse(delivery_engine.retained_target_lease_is_held(request))

    def test_manual_desktop_recovery_retains_target_lease_until_canceled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            queue_store.ensure_daemon_dirs(root)
            request = reins_records.callback_record("desktop-unknown", tmp, target={"kind": "session", "session": "desktop-thread"}, prompt="wake up", created_at=1.0, retain_target_lease=True)
            queue_store.write_request(layout.request_path(root, "failed", "desktop-unknown"), request)
            with mock.patch.dict(os.environ, {layout.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks")}, clear=False):
                delivery_engine.retain_target_lease(root, request)
                self.assertTrue(delivery_engine.target_has_live_resume(root, request))
                self.assertTrue(recovery.cancel_one(root, "desktop-unknown", "manual recovery resolved"))
                self.assertFalse(delivery_engine.target_has_live_resume(root, request))

    def test_manual_desktop_recovery_lease_blocks_same_session_across_queue_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root_a = Path(tmp) / "queue-a"
            root_b = Path(tmp) / "queue-b"
            target = {"kind": "session", "session": "desktop-thread"}
            failed = reins_records.callback_record("desktop-unknown", tmp, target=target, prompt="wake up", created_at=1.0, retain_target_lease=True)
            queue_store.ensure_daemon_dirs(root_a)
            queue_store.write_request(layout.request_path(root_a, "failed", "desktop-unknown"), failed)
            self._write_request(root_b, "same-session", tmp, target=target)
            with mock.patch.dict(os.environ, {layout.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks")}, clear=False):
                delivery_engine.retain_target_lease(root_a, failed)
                self.assertIsNone(delivery_engine.select_pending(root_b, time.time()))
                self.assertTrue(recovery.cancel_one(root_a, "desktop-unknown", "manual recovery resolved"))
                self.assertEqual(delivery_engine.select_pending(root_b, time.time()).stem, "same-session")

    def test_cancel_releases_timeout_marker_while_parent_still_marks_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "queue"
            target = {"kind": "session", "session": "timeout-thread"}
            self._write_request(root, "timeout-cancel", tmp, target=target)
            queue_store.ensure_daemon_dirs(root)
            queue_store.move_request(layout.request_path(root, "pending", "timeout-cancel"), root / "running")
            request = dispatch.load_request(layout.request_path(root, "running", "timeout-cancel"))

            with mock.patch.dict(os.environ, {layout.TARGET_LOCK_DIR_ENV: str(Path(tmp) / "target-locks")}, clear=False):
                delivery_engine.retain_target_lease(root, request)
                self.assertTrue(recovery.cancel_one(root, "timeout-cancel", "stop after timeout"))
                self.assertFalse(delivery_engine.retained_target_lease_is_held(request))

    def _write_request(
        self,
        root: Path,
        request_id: str,
        cwd: str,
        *,
        target: dict[str, str] | None = None,
    ) -> None:
        (root / "pending").mkdir(parents=True, exist_ok=True)
        request = reins_records.callback_record(request_id, cwd, target=target or {"kind": "session", "session": "fixture-session"}, prompt="wake up", created_at=1.0)
        (root / "pending" / f"{request_id}.json").write_text(
            json.dumps(request) + "\n",
            encoding="utf-8",
        )

    def _temporary_directory(self) -> str:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return temporary.name

    def _run_args(self, cwd: str, root: Path, command: list[str]) -> argparse.Namespace:
        return argparse.Namespace(
            cwd=cwd,
            task="test long task",
            command=None,
            exit_code=None,
            message=None,
            session="test-thread",
            last=False,
            via_daemon=False,
            queue_dir=str(root),
            approvals_reviewer="auto_review",
            approval_policy="on-request",
            sandbox_mode="workspace-write",
            dry_run=False,
            strict=False,
            wrapped_command=command,
        )

    def _agent_args(
        self,
        cwd: str,
        root: Path,
        worker: str,
        prompt: str,
    ) -> argparse.Namespace:
        args = self._run_args(cwd, root, [])
        args.agent = "codex"
        args.agent_worker = worker
        args.agent_prompt = ["--", prompt]
        args.permission_mode = "auto"
        return args

    def _fake_child_agent(self, directory: Path) -> Path:
        script = directory / "fake-claude"
        script.write_text(
            textwrap.dedent(
                f"""\
                #!{sys.executable}
                import json
                import os
                import sys

                from pathlib import Path

                payload = {{
                    "prompt": sys.stdin.read(),
                    "sentinel": os.environ.get("REINS_TEST_SENTINEL"),
                    "api_key_present": bool(os.environ.get("ANTHROPIC_API_KEY")),
                    "base_url": os.environ.get("ANTHROPIC_BASE_URL"),
                    "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
                    "codex_thread_id": os.environ.get("CODEX_THREAD_ID"),
                    "claude_session_id": os.environ.get("CLAUDE_CODE_SESSION_ID"),
                    "claudecode": os.environ.get("CLAUDECODE"),
                }}
                if "-o" in sys.argv:
                    Path(sys.argv[sys.argv.index("-o") + 1]).write_text(json.dumps(payload), encoding="utf-8")
                else:
                    json.dump(payload, sys.stdout)
                """
            ),
            encoding="utf-8",
        )
        return executable_python_fixture(script)

    def _fake_codex(
        self,
        directory: Path,
        *,
        ack: bool,
        queue: Path,
        request_id: str,
        sleep_after_ack: float = 0.0,
        launch_log: Path | None = None,
        descendant_pid_path: Path | None = None,
    ) -> Path:
        script = directory / ("fake-codex-ack" if ack else "fake-codex")
        src_path = Path(cli.__file__).parents[1]
        ack_code = (
            f"os.environ['PYTHONPATH'] = {str(src_path)!r} + os.pathsep + os.environ.get('PYTHONPATH', ''); "
            "subprocess.run([sys.executable, '-m', 'harness_reins', 'ack', "
            f"'--queue-dir', {str(queue)!r}, '--id', {request_id!r}], check=True)"
            if ack
            else "pass"
        )
        script.write_text(
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import os
                import pathlib
                import subprocess
                import sys
                import time

                launch_log = {str(launch_log) if launch_log is not None else None!r}
                if launch_log is not None:
                    with open(launch_log, "a", encoding="utf-8") as handle:
                        handle.write(str(os.getpid()) + "\\n")
                        handle.flush()
                sys.stdin.read()
                {ack_code}
                descendant_pid_path = {str(descendant_pid_path) if descendant_pid_path is not None else None!r}
                if descendant_pid_path is not None:
                    descendant = subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(30)"],
                        start_new_session=True,
                    )
                    pathlib.Path(descendant_pid_path).write_text(str(descendant.pid), encoding="utf-8")
                time.sleep({sleep_after_ack!r})
                raise SystemExit(0)
                """
            ),
            encoding="utf-8",
        )
        return executable_python_fixture(script)


if __name__ == "__main__":
    unittest.main()
