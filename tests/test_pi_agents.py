"""Independent Pi child-worker contracts; execution belongs to the parent.

The process fixtures call no model APIs and touch only temporary queues. They
exercise submission and the real private worker, including its durable result,
captured environment, text artifacts and callback routing.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from harness_reins import cli
from harness_reins.commands import work as work_commands
from harness_reins.tower import main as tower
from harness_reins.engine import dispatch
from harness_reins.engine import tasks as managed_tasks
from harness_reins.engine import delivery as delivery_engine
from harness_reins.store import records as queue_store
from harness_reins.store import layout
from harness_reins.install import diagnostics
from harness_reins.harness.pi import channel as pi_channel
from harness_reins.harness import AGENTS, CHILD_AGENT_NAMES, ChildOptions, get_agent, get_child_agent
from harness_reins.runtime import worker_command
from harness_reins.core import ids
from test_cli import assert_private_file, executable_python_fixture, patch_fixture_commands
import reins_records


PI_THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
SESSION_MARKERS = (
    "CODEX_THREAD_ID", "CLAUDE_CODE_SESSION_ID", "CLAUDECODE",
    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "PI_SESSION_ID", "PI_SESSION_FILE", "PI_CODING_AGENT",
)


class PiAdapterContractTests(unittest.TestCase):
    def options(self, *, model=None, effort=None, system_prompt_path=None):
        return ChildOptions(
            cwd="/work with spaces", result_path=Path("/results/report with spaces.md"),
            sandbox_mode="workspace-write", permission_mode="auto", model=model,
            reasoning_effort=effort, system_prompt_path=system_prompt_path,
        )

    def test_pi_is_registered_as_both_a_child_worker_and_a_callback_parent(self):
        self.assertEqual(set(AGENTS.names), {"codex", "claude", "pi", "dsh"})
        self.assertEqual(set(CHILD_AGENT_NAMES), {"codex", "claude", "pi", "dsh"})
        self.assertIs(get_agent("pi"), get_child_agent("pi"))
        for environment, expected in (
            ({"PI_SESSION_FILE": "/pi/session.jsonl"}, "pi"),
            ({"PI_SESSION_ID": "pi-parent"}, "pi"),
            ({"PI_CODING_AGENT": "true"}, "pi"),
            ({"PI_SESSION_ID": "pi-parent", "PI_SESSION_FILE": "/pi/session.jsonl"}, "pi"),
            ({"PI_SESSION_ID": "pi-parent", "CODEX_THREAD_ID": "codex-parent"}, "pi"),
            ({"PI_SESSION_FILE": "/pi/session.jsonl", "CLAUDE_CODE_SESSION_ID": "claude-parent",
              "CLAUDECODE": "1"}, "pi"),
            ({"PI_PROVIDER": "fixture", "PI_MODEL": "fixture-model", "PI_REASONING_LEVEL": "high"}, "codex"),
        ):
            with self.subTest(environment=environment):
                self.assertEqual(AGENTS.detect(environment), expected)
                with mock.patch.dict(os.environ, environment, clear=True):
                    self.assertEqual(dispatch.resolve_agent(), expected)
        self.assertEqual(dispatch.resolve_agent(argparse.Namespace(agent="pi")), "pi")

    def test_pi_resume_command_resumes_the_bound_session_file_in_print_mode(self):
        session = ("/home/user/.pi/agent/sessions/--data-proj--/"
                   "2026-10-03T14-37-07-412Z_01a10232-83d4-700f-a8a1-691db3724158.jsonl")
        self.assertEqual(
            get_agent("pi").resume_command({"target": {"kind": "session", "session": session}}, {}),
            ["pi", "--print", "--session", session],
        )
        with mock.patch.dict(os.environ, {"REINS_PI_BIN": "/opt/my Pi/bin/pi"}, clear=False):
            self.assertEqual(
                delivery_engine.resume_command({"agent": "pi", "target": {"kind": "session", "session": session}}),
                ["/opt/my Pi/bin/pi", "--print", "--session", session],
            )
        self.assertNotIn("--no-session", get_agent("pi").resume_command(
            {"target": {"kind": "session", "session": session}}, {}))
        for target in (None, {}, {"kind": "session", "session": ""}, {"kind": "last"}, {"kind": "future-unknown"}):
            with self.subTest(target=target), self.assertRaises(ValueError):
                get_agent("pi").resume_command({"target": target}, {})

    def test_make_request_binds_the_pi_session_file_and_acknowledgement_names_pi(self):
        session = os.path.abspath("/home/user/.pi/agent/sessions/--data-proj--/2026-10-03T14-37-07-412Z_1.jsonl")
        args = argparse.Namespace(
            cwd="/tmp", session=None, last=False, agent=None, task="long task",
            command=None, exit_code=None, message=None,
            approvals_reviewer="auto_review", approval_policy="on-request", sandbox_mode="workspace-write",
        )
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        with mock.patch.dict(os.environ, {"PI_SESSION_FILE": session, "PI_SESSION_ID": "1",
                                          "HOME": home.name, "USERPROFILE": home.name}, clear=True):
            request = dispatch.make_request(args, "wake up")
            self.assertEqual(dispatch.resolve_agent(args), "pi")
            prompt = dispatch.build_prompt(args)
        self.assertEqual(request["agent"], "pi")
        # Bound by its resolved path: on macOS /home is reached through /System/Volumes/Data.
        self.assertEqual(request["target"], {"kind": "session", "session": pi_channel.canonical_session(session)})
        self.assertEqual(request["origin"]["binding"], "PI_SESSION_FILE")
        self.assertIn("called back into PI Agent", prompt)
        acknowledgement = dispatch.build_acknowledgement_text("reins ack --queue-dir /q --id abc", agent="pi")
        self.assertIn("original PI Agent process through its native message API", acknowledgement)
        self.assertIn("--queue-dir /q --id abc", acknowledgement)

    def test_pi_command_is_pure_fresh_print_text_mode_with_no_positional_prompt(self):
        environment = {"REINS_PI_BIN": "/opt/my Pi/bin/pi", "PI_PROVIDER": "fixture"}
        before = dict(environment)
        command = get_child_agent("pi").child_command(
            self.options(model="provider/model with spaces", effort="high"), environment
        )
        self.assertEqual(command, [
            "/opt/my Pi/bin/pi", "--print", "--mode", "text", "--no-session",
            "--model", "provider/model with spaces", "--thinking", "high",
        ])
        self.assertEqual(environment, before)
        self.assertEqual(get_child_agent("pi").child_command(self.options(), {}),
                         ["pi", "--print", "--mode", "text", "--no-session"])
        self.assertEqual(get_child_agent("pi").child_result_mode, "stdout")

    def test_every_supported_pi_thinking_level_maps_to_native_thinking_flag(self):
        for effort in PI_THINKING_LEVELS:
            with self.subTest(effort=effort):
                command = get_child_agent("pi").child_command(self.options(effort=effort), {})
                self.assertEqual(command, ["pi", "--print", "--mode", "text", "--no-session",
                                           "--thinking", effort])

    def test_pi_adapter_passes_the_system_prompt_as_a_separate_file_argument(self):
        snapshot = Path("/managed task/agent-system-prompt.md")
        command = get_child_agent("pi").child_command(self.options(system_prompt_path=snapshot), {})
        self.assertEqual(command, ["pi", "--print", "--mode", "text", "--no-session",
                                   "--system-prompt", str(snapshot)])
        self.assertNotIn("--append-system-prompt", command)

    def test_fresh_children_strip_all_session_markers_and_keep_pi_config_and_credentials(self):
        environment = {name: "inherited-parent-value" for name in SESSION_MARKERS}
        preserved = {
            "PI_CODING_AGENT_DIR": "/fixture/pi profile", "PI_PROVIDER": "custom-provider",
            "PI_MODEL": "custom-model", "PI_REASONING_LEVEL": "high",
            "OPENAI_API_KEY": "fixture-openai-key", "ANTHROPIC_API_KEY": "fixture-anthropic-key",
            "PATH": "/fixture/bin", "CUSTOM_WORKLOAD_SETTING": "preserve-me",
        }
        environment.update(preserved)
        before = dict(environment)
        self.assertEqual(managed_tasks.child_agent_environment(environment), preserved)
        self.assertEqual(environment, before)


class PiAgentContractTests(unittest.TestCase):
    def setUp(self):
        # Submission uses the screen contract in these portable fixtures. The
        # worker still uses native Windows storage/process support on Windows.
        if os.name == "nt":
            platform = mock.patch.object(sys, "platform", "linux")
            platform.start()
            self.addCleanup(platform.stop)
        patch_fixture_commands(self)
        temporary = tempfile.TemporaryDirectory(prefix="reins Pi 中文 ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.queue = self.directory / "queue with spaces"
        self.templates = self.directory / "templates"
        self.templates.mkdir()
        self.codex_home = self.directory / "codex-profile"
        self.claude_home = self.directory / "claude-profile"
        for home in (self.codex_home, self.claude_home):
            skill = home / "skills" / "harness-reins" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("Fixture Reins skill", encoding="utf-8")
        environment = mock.patch.dict(os.environ, {
            "REINS_TEMPLATE_DIR": str(self.templates), "CODEX_HOME": str(self.codex_home), "REINS_HOME": str(self.codex_home / "reins-home"),
            "CLAUDE_CONFIG_DIR": str(self.claude_home),
            layout.TARGET_LOCK_DIR_ENV: str(self.directory / "target-locks"),
            "PYTHONIOENCODING": "utf-8",
            "PI_CODING_AGENT_DIR": str(self.directory / "pi-profile"),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def tearDown(self):
        for delivery in list(delivery_engine._BACKGROUND_RESUMES.values()):
            delivery_engine.stop_resume_process(delivery.process)
        delivery_engine._BACKGROUND_RESUMES.clear()

    def invoke(self, options=(), *, parent="codex", prompt="Inspect the public Pi child contract."):
        argv = ["reins", "agent", "pi", "--cwd", str(self.directory),
                "--queue-dir", str(self.queue), "--backend", "screen", "--callback-mode", "cli",
                "--agent", parent, "--session", f"parent-{parent}", "--task", "Pi contract fixture",
                *options, "--", prompt]
        output = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(managed_tasks, "screen_binary", return_value="/fixture/screen"), \
                mock.patch.object(diagnostics, "emit_if_needed"), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            result = cli.main()
        return result, output.getvalue()

    def task(self):
        paths = list(layout.managed_tasks_root(self.queue).glob("*/task.json"))
        self.assertEqual(len(paths), 1)
        return paths[0], managed_tasks.load_managed_task(paths[0])

    def assert_rejected(self, options=(), **kwargs):
        try:
            result, _ = self.invoke(options, **kwargs)
        except SystemExit as exc:
            self.assertNotEqual(exc.code, 0)
        else:
            self.assertNotEqual(result, 0)
        self.assertFalse(layout.managed_tasks_root(self.queue).exists())

    def write_template(self, body, name="review.yaml"):
        path = self.templates / name
        path.write_text(body, encoding="utf-8")
        return path

    def fake_pi(self):
        script = self.directory / "fake Pi child.py"
        script.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json
            import os
            from pathlib import Path
            import sys

            # Record process admission before reading any optional prompt file.
            with Path(os.environ['REINS_TEST_PI_EXECUTIONS']).open('a', encoding='utf-8') as stream:
                stream.write('executed\\n')
            names = {SESSION_MARKERS + ('PI_CODING_AGENT_DIR', 'PI_PROVIDER', 'PI_MODEL', 'PI_REASONING_LEVEL', 'OPENAI_API_KEY', 'CUSTOM_WORKLOAD_SETTING')!r}
            payload = {{'argv': sys.argv[1:], 'prompt': sys.stdin.read(), 'cwd': os.getcwd(),
                       'environment': {{name: os.environ.get(name) for name in names}}}}
            if '--system-prompt' in sys.argv:
                system_path = Path(sys.argv[sys.argv.index('--system-prompt') + 1])
                payload['system_prompt_path'] = str(system_path)
                payload['system_prompt'] = system_path.read_text(encoding='utf-8')
            else:
                payload['system_prompt_path'] = None
                payload['system_prompt'] = None
            Path(os.environ['REINS_TEST_PI_CAPTURE']).write_text(json.dumps(payload), encoding='utf-8')
            sys.stderr.write('fixture Pi diagnostic\\n')
            sys.stdout.write(os.environ['REINS_TEST_PI_REPORT'])
            raise SystemExit(int(os.environ['REINS_TEST_PI_EXIT']))
            """), encoding="utf-8")
        return executable_python_fixture(script)

    def fixture_environment(self, executable, capture, *, report, exit_code):
        return {
            "REINS_PI_BIN": str(executable), "REINS_TEST_PI_CAPTURE": str(capture),
            "REINS_TEST_PI_EXECUTIONS": str(capture.with_suffix(".executions")),
            "REINS_TEST_PI_REPORT": report, "REINS_TEST_PI_EXIT": str(exit_code),
            "CODEX_THREAD_ID": "submitter-codex", "CLAUDE_CODE_SESSION_ID": "submitter-claude",
            "CLAUDECODE": "1", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "submitter-desktop",
            "PI_SESSION_ID": "submitter-pi", "PI_SESSION_FILE": "/fixture/parent-session.jsonl",
            "PI_CODING_AGENT": "true",
            "PI_CODING_AGENT_DIR": str(self.directory / "Pi config with spaces"),
            "PI_PROVIDER": "custom-provider", "PI_MODEL": "custom-model", "PI_REASONING_LEVEL": "high",
            "OPENAI_API_KEY": "fixture-preserved-secret", "CUSTOM_WORKLOAD_SETTING": "submission-value",
        }

    def run_worker(self, path, task):
        task["state"] = "launching"
        managed_tasks.write_managed_task(self.queue, task)
        command = worker_command("_screen-worker", "--task-file", str(path), f"--token={task['token']}")
        # This is the log redirection normally supplied by the execution owner;
        # no actual screen session or coordinator is needed to run admission.
        environment = dict(os.environ, STY="fixture-screen", PYTHONIOENCODING="utf-8",
                           REINS_PI_BIN="daemon-must-not-replace-frozen-executable",
                           PI_CODING_AGENT_DIR="daemon-must-not-replace-submitter-config",
                           CUSTOM_WORKLOAD_SETTING="daemon-must-not-leak")
        with Path(task["log_path"]).open("wb") as log:
            return subprocess.run(command, cwd=self.directory, env=environment,
                                  stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                  check=False, timeout=15).returncode

    def assert_system_prompt_worker_rejected(self, path, task, capture):
        code = self.run_worker(path, task)
        self.assertNotEqual(code, 0, Path(task["log_path"]).read_text(encoding="utf-8"))
        self.assertFalse(capture.exists())
        self.assertFalse(capture.with_suffix(".executions").exists(), "Pi must not start before snapshot validation")
        completed = managed_tasks.load_managed_task(path)
        self.assertEqual((completed["state"], completed["outcome"]), ("interrupted", "unknown"))
        reason = completed["recovery_reason"]
        self.assertIn("system prompt", reason.lower().replace("_", " ").replace("-", " "))
        callbacks = list((self.queue / "pending").glob("*.json"))
        self.assertEqual(len(callbacks), 1)
        callback = dispatch.load_request(callbacks[0])
        self.assertEqual(callback["agent"], task["agent"])
        self.assertEqual(callback["target"], task["target"])
        self.assertEqual(callback["agent_worker"], "pi")
        self.assertEqual(callback["outcome"], "unknown")
        self.assertEqual(callback["recovery_reason"], reason)

    def test_public_submission_and_real_worker_preserve_text_environment_and_parent_routes(self):
        executable = self.fake_pi()
        for parent, exit_code, report in (
            ("codex", 0, "Pi result: 中文 ✓\nsecond line without a final newline"),
            ("claude", 7, "Partial Pi result\nfailed after useful evidence\n"),
        ):
            with self.subTest(parent=parent, exit_code=exit_code):
                self.queue = self.directory / f"worker-queue-{parent}"
                capture = self.directory / f"capture-{parent}.json"
                prompt = "Inspect --model literally in this prompt.\nPreserve 中文 and report evidence."
                environment = self.fixture_environment(executable, capture, report=report, exit_code=exit_code)
                with mock.patch.dict(os.environ, environment), mock.patch.object(cli.subprocess, "run") as process:
                    self.assertEqual(self.invoke(["--model", "fixture/provider-model", "--reasoning-effort", "high"],
                                                 parent=parent, prompt=prompt)[0], 0)
                    process.assert_not_called()
                path, task = self.task()
                self.assertEqual(task["state"], "submitted")
                self.assertEqual(task["agent_worker"], "pi")
                self.assertIsNone(task.get("agent_system_prompt_path"))
                self.assertIsNone(task.get("agent_system_prompt_source"))
                self.assertNotIn("--system-prompt", task["wrapped_command"])
                self.assertEqual(task["agent"], parent)
                self.assertEqual(task["target"], {"kind": "session", "session": f"parent-{parent}"})
                self.assertEqual(Path(task["agent_prompt_path"]).read_bytes(), reins_records.agent_prompt(prompt, task).encode("utf-8"))
                self.assertNotIn(prompt, path.read_text(encoding="utf-8"))
                assert_private_file(self, Path(task["agent_prompt_path"]))
                assert_private_file(self, Path(task["environment_path"]))
                self.assertEqual(self.run_worker(path, task), exit_code,
                                 Path(task["log_path"]).read_text(encoding="utf-8"))
                received = json.loads(capture.read_text(encoding="utf-8"))
                self.assertEqual(received["prompt"], reins_records.agent_prompt(prompt, task))
                self.assertIsNone(received["system_prompt"])
                self.assertEqual(received["cwd"], str(self.directory))
                self.assertEqual(received["argv"], ["--print", "--mode", "text", "--session-dir", task["node"]["session_dir"],
                                                    "--model", "fixture/provider-model", "--thinking", "high"])
                for marker in SESSION_MARKERS:
                    self.assertIsNone(received["environment"][marker], marker)
                for name in ("PI_CODING_AGENT_DIR", "PI_PROVIDER", "PI_MODEL", "PI_REASONING_LEVEL",
                             "OPENAI_API_KEY", "CUSTOM_WORKLOAD_SETTING"):
                    self.assertEqual(received["environment"][name], environment[name], name)
                self.assertEqual(capture.with_suffix(".executions").read_text(encoding="utf-8"), "executed\n")
                completed = managed_tasks.load_managed_task(path)
                self.assertEqual((completed["state"], completed["outcome"], completed["exit_code"]),
                                 ("completed", "completed", exit_code))
                result_path = Path(completed["agent_result_path"])
                self.assertEqual(result_path.read_bytes(), report.encode("utf-8"))
                assert_private_file(self, result_path)
                self.assertFalse(Path(task["environment_path"]).exists())
                durable = json.loads(layout.managed_result_path(self.queue, task["id"]).read_text(encoding="utf-8"))
                self.assertEqual(durable["observed"]["exit_code"], exit_code)
                log = Path(task["log_path"]).read_text(encoding="utf-8")
                self.assertIn(report, log)
                self.assertIn("fixture Pi diagnostic", log)
                self.assertNotIn("fixture-preserved-secret", log)
                pending = list((self.queue / "pending").glob("*.json"))
                self.assertEqual(len(pending), 1)
                callback = dispatch.load_request(pending[0])
                self.assertEqual(callback["agent"], parent)
                self.assertEqual(callback["agent_worker"], "pi")
                self.assertEqual(callback["target"], task["target"])
                self.assertEqual(callback["exit_code"], exit_code)
                self.assertEqual(callback["agent_result_path"], str(result_path))
                details = Path(callback["prompt_details_path"]).read_text(encoding="utf-8")
                self.assertIn("Child agent: PI Agent", details)
                self.assertIn(str(result_path), details)
                self.assertIn(task["log_path"], details)
                self.assertNotIn("fixture-preserved-secret", details)

    def test_empty_failed_stdout_is_saved_as_an_empty_report_without_losing_exit_status(self):
        executable = self.fake_pi()
        capture = self.directory / "empty-failed.json"
        environment = self.fixture_environment(executable, capture, report="", exit_code=1)
        with mock.patch.dict(os.environ, environment):
            self.assertEqual(self.invoke()[0], 0)
        path, task = self.task()
        self.assertEqual(self.run_worker(path, task), 1)
        self.assertEqual(Path(task["agent_result_path"]).read_bytes(), b"")
        self.assertEqual(managed_tasks.load_managed_task(path)["exit_code"], 1)
        callback = dispatch.load_request(self.queue / "pending" / f"{ids.callback_id(task['id'])}.json")
        self.assertEqual(callback["exit_code"], 1)
        self.assertEqual(callback["agent"], "codex")

    def test_all_pi_cli_thinking_choices_are_supported_including_off_and_max(self):
        for effort in PI_THINKING_LEVELS:
            with self.subTest(effort=effort):
                self.queue = self.directory / f"effort-{effort}"
                self.assertEqual(self.invoke(["--reasoning-effort", effort])[0], 0)
                _, task = self.task()
                self.assertEqual(task["child_reasoning_effort"], effort)
                command = task["wrapped_command"]
                self.assertEqual(command[command.index("--thinking") + 1], effort)

    def test_invalid_cli_options_fail_before_creating_any_task_directory(self):
        for options, prompt in (
            (["--reasoning-effort", "ultra"], "requirements"),
            (["--reasoning-effort", "unknown"], "requirements"),
            (["--reasoning-effort", ""], "requirements"),
            (["--model", " "], "requirements"),
            ([], " \n "),
        ):
            with self.subTest(options=options, prompt=prompt):
                self.assert_rejected(options, prompt=prompt)

    def test_pi_is_a_valid_public_callback_parent(self):
        session = str(self.directory / "pi-session.jsonl")
        for arguments in (
            ["run", "--", sys.executable, "-c", "pass"],
            ["done", "--exit-code", "0"],
        ):
            # Common options precede the remainder prompt or command.
            separator = arguments.index("--") if "--" in arguments else len(arguments)
            argv = ["reins", *arguments[:separator], "--queue-dir", str(self.queue),
                    "--agent", "pi", "--session", session, "--dry-run", *arguments[separator:]]
            with self.subTest(arguments=arguments), mock.patch.object(sys, "argv", argv), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as stderr:
                cli.main()
            # Bound by its canonical path (resolved; lower case on Windows).
            self.assertIn(pi_channel.canonical_session(session), stderr.getvalue())
            self.assertFalse(layout.managed_tasks_root(self.queue).exists())

    def test_doctor_for_a_pi_callback_parent_keeps_cli_resume_and_a_valid_repair_command(self):
        session = str(self.directory / "pi-session.jsonl")
        args = argparse.Namespace(
            mode="run", operation="run", backend="screen", agent="pi", session=session, last=False,
            queue_dir=str(self.queue), dry_run=False, task="fixture task",
            wrapped_command=["/bin/true"], callback_mode="cli", _health_backend="screen",
        )
        with mock.patch.object(diagnostics, "queue_writable", return_value=True), \
                mock.patch.object(diagnostics, "coordinator_issue", return_value={
                    "code": "daemon_not_running", "action": "Start the coordinator for this queue."}), \
                mock.patch.object(diagnostics, "runtime_issues", return_value=[]), \
                mock.patch.object(diagnostics, "delivery_issues", return_value=[]), \
                mock.patch.object(diagnostics.ScreenBackend, "available", return_value=True), \
                mock.patch.object(diagnostics.routes, "inspect_route",
                                  return_value={"status": "ready", "mode": "cli", "transport": "cli_resume"}), \
                mock.patch.object(diagnostics.shutil, "which", side_effect=lambda name: "/fixture/bin/" + name):
            report = diagnostics.inspect(args)
        self.assertEqual(report["callback"]["transport"], "cli_resume")
        repair = report["repair_command"]
        self.assertIsInstance(repair, list)
        self.assertEqual(repair[repair.index("--skill-target") + 1], "pi")
        self.assertEqual(repair[repair.index("--pi-bin") + 1], "/fixture/bin/pi")
        recheck = report["recheck_command"]
        self.assertEqual(recheck[recheck.index("--agent") + 1], "pi")
        self.assertEqual(recheck[recheck.index("--session") + 1], pi_channel.canonical_session(session))

    def fake_pi_callback(self):
        script = self.directory / "fake Pi callback.py"
        src_path = Path(cli.__file__).parents[1]
        script.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json
            import os
            import subprocess
            import sys
            from pathlib import Path

            payload = {{'argv': sys.argv[1:], 'cwd': os.getcwd(), 'prompt': sys.stdin.read()}}
            Path(os.environ['REINS_TEST_PI_CALLBACK_CAPTURE']).write_text(
                json.dumps(payload), encoding='utf-8')
            os.environ['PYTHONPATH'] = {str(src_path)!r} + os.pathsep + os.environ.get('PYTHONPATH', '')
            subprocess.run([sys.executable, '-m', 'harness_reins', 'ack',
                            '--queue-dir', os.environ['REINS_TEST_PI_CALLBACK_QUEUE'],
                            '--id', os.environ['REINS_TEST_PI_CALLBACK_ID']], check=True)
            raise SystemExit(0)
            """), encoding="utf-8")
        return executable_python_fixture(script)

    def test_dead_managed_pi_callback_resumes_the_bound_file_with_prompt_on_stdin(self):
        root = self.directory / "pi-callback-queue"
        session = self.directory / "pi parent session.jsonl"
        session.write_text('{"type":"session","version":3,"id":"1",'
                           '"timestamp":"2026-01-01T00:00:00.000Z","cwd":"/tmp"}\n', encoding="utf-8")
        capture = self.directory / "pi-callback-capture.json"
        fake_pi = self.fake_pi_callback()
        argv = ["reins", "done", "--exit-code", "0", "--cwd", str(self.directory),
                "--queue-dir", str(root), "--backend", "screen", "--callback-mode", "cli",
                "--agent", "pi", "--session", str(session), "--task", "Pi callback fixture",
                "--message", "Inspect the finished work."]
        output = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(diagnostics, "emit_if_needed"), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            self.assertEqual(cli.main(), 0)
        pending = list((root / "pending").glob("*.json"))
        self.assertEqual(len(pending), 1)
        request = dispatch.load_request(pending[0])
        self.assertEqual(request["agent"], "pi")
        bound = pi_channel.canonical_session(str(session))
        self.assertEqual(request["target"], {"kind": "session", "session": bound})
        self.assertIn("Bound session: " + bound,
                      Path(str(request["prompt_details_path"])).read_text(encoding="utf-8"))

        # setUp models Linux for screen selection; offline recovery must record and
        # check this host's real process identity.
        native = mock.patch.object(sys, "platform", "win32") if os.name == "nt" else contextlib.nullcontext()
        with native:
            channel = pi_channel.channel_dir(str(session), Path(request["pi_channel_root"]))
            pi_channel.ensure_private_directory(channel)
            owner = {"version": 1, "session_file": bound, "session_id": "1",
                     "channel_dir": str(channel), "profile_dir": request["pi_profile_dir"],
                     "owner_nonce": "fixture", "owner_pid": 99999999, "managed": True,
                     "state": "closed", "owner_identity": tower.daemon_process_identity(os.getpid())}
            queue_store.write_request(channel / "owner.json", owner)
            environment = {
                "REINS_PI_BIN": str(fake_pi),
                "REINS_TEST_PI_CALLBACK_CAPTURE": str(capture),
                "REINS_TEST_PI_CALLBACK_QUEUE": str(root),
                "REINS_TEST_PI_CALLBACK_ID": str(request["id"]),
            }
            args = argparse.Namespace(retries=3, retry_delay=0.0, retry_backoff=2.0, resume_timeout=15.0)
            delivery_output = io.StringIO()
            with mock.patch.dict(os.environ, environment), \
                    contextlib.redirect_stdout(delivery_output), contextlib.redirect_stderr(delivery_output):
                self.assertTrue(delivery_engine.process_one(root, args))

            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                delivery_engine.reap_background_resumes()
                if not delivery_engine._BACKGROUND_RESUMES:
                    break
                time.sleep(0.05)
            self.assertFalse(delivery_engine._BACKGROUND_RESUMES, "the delivery worker did not finish")
            delivery_engine.recover_running(root)
        records = {state: dispatch.load_request(path) for state in ("pending", "running", "failed")
                   for path in (root / state).glob(f"{request['id']}.json")}
        self.assertTrue((root / "done" / f"{request['id']}.json").exists(), json.dumps({
            "output": delivery_output.getvalue()[-2000:], "captured": capture.exists(),
            "states": {state: {key: record.get(key) for key in ("last_error", "last_deferred_reason", "attempts")}
                       for state, record in records.items()}}, indent=1))
        received = json.loads(capture.read_text(encoding="utf-8"))
        # Windows paths compare case-insensitively (the worker may see Lib, the test lib).
        self.assertEqual([os.path.normcase(value) for value in received["argv"]],
                         [os.path.normcase(value) for value in
                          ["--print", "--session", bound, "--extension", str(pi_channel.extension_path())]])
        self.assertNotIn("--no-session", received["argv"])
        self.assertEqual(received["cwd"], str(self.directory))
        self.assertIn("[reins]", received["prompt"])
        self.assertIn("Session: " + bound + " (only)", received["prompt"])
        self.assertIn("Details: " + str(request["prompt_details_path"]) + " (read before acting)", received["prompt"])
        self.assertIn("Inspect the finished work.",
                      Path(str(request["prompt_details_path"])).read_text(encoding="utf-8"))

    def test_custom_pi_defaults_and_cli_overrides_do_not_leak_other_workers_settings(self):
        self.write_template("version: 3\nprompt: Review the published contract.\n"
                            "codex:\n  model: codex-only\n  reasoning_effort: ultra\n"
                            "claude:\n  model: claude-only\n"
                            "pi:\n  model: custom-provider/custom-model\n  reasoning_effort: max\n")
        for options, model, effort in (
            (["--template", "review"], "custom-provider/custom-model", "max"),
            (["--template", "review", "--model", "override/model", "--reasoning-effort", "off"],
             "override/model", "off"),
        ):
            with self.subTest(options=options):
                self.queue = self.directory / ("profile-default" if effort == "max" else "profile-override")
                self.assertEqual(self.invoke(options)[0], 0)
                _, task = self.task()
                self.assertEqual(task["child_model"], model)
                self.assertEqual(task["child_reasoning_effort"], effort)
                command = task["wrapped_command"]
                self.assertEqual(command[command.index("--model") + 1], model)
                self.assertEqual(command[command.index("--thinking") + 1], effort)
                self.assertNotIn("codex-only", command)
                self.assertNotIn("claude-only", command)

    def test_invalid_pi_template_settings_fail_closed_before_task_persistence(self):
        for settings in (
            "  reasoning_effort: ultra\n", "  reasoning_effort: impossible\n",
            "  reasoning_effort: ''\n", "  model: ' '\n", "  thinking: high\n",
        ):
            with self.subTest(settings=settings):
                path = self.write_template("version: 1\nprompt: Review.\npi:\n" + settings)
                self.assert_rejected(["--template", "review"])
                self.assert_rejected(["--template-file", str(path)])

    def test_custom_template_command_prompt_and_handoff_are_frozen_at_submission(self):
        template = self.write_template("version: 8\nprompt: ORIGINAL PI PROMPT\n"
                                       "pi:\n  model: original-provider/original-model\n  reasoning_effort: high\n"
                                       "handoff: ORIGINAL HANDOFF requires parent inspection.\n")
        executable = self.fake_pi()
        capture = self.directory / "frozen-profile.json"
        environment = self.fixture_environment(executable, capture, report="Frozen Pi report\n", exit_code=0)
        with mock.patch.dict(os.environ, environment):
            self.assertEqual(self.invoke(["--template-file", str(template)], parent="claude")[0], 0)
        path, task = self.task()
        submitted_prompt = Path(task["agent_prompt_path"]).read_text(encoding="utf-8")
        submitted_command = list(task["wrapped_command"])
        template.write_text("version: 9\nprompt: REPLACEMENT PROMPT\n"
                            "pi:\n  model: replacement/model\n  reasoning_effort: off\n"
                            "handoff: REPLACEMENT HANDOFF\n", encoding="utf-8")
        self.assertEqual(self.run_worker(path, task), 0)
        received = json.loads(capture.read_text(encoding="utf-8"))
        self.assertEqual(received["prompt"], submitted_prompt)
        self.assertEqual(received["argv"], ["--print", "--mode", "text", "--session-dir", task["node"]["session_dir"],
                                            "--model", "original-provider/original-model", "--thinking", "high"])
        self.assertEqual(managed_tasks.load_managed_task(path)["wrapped_command"], submitted_command)
        callback = dispatch.load_request(self.queue / "pending" / f"{ids.callback_id(task['id'])}.json")
        self.assertEqual(callback["agent_template"], "review")
        self.assertEqual(callback["agent_template_version"], 8)
        self.assertEqual(callback["agent_template_source"], str(template.resolve()))
        self.assertEqual(callback["child_model"], "original-provider/original-model")
        self.assertEqual(callback["child_reasoning_effort"], "high")
        self.assertEqual(callback["agent_template_handoff"], "ORIGINAL HANDOFF requires parent inspection.")
        self.assertEqual(callback["agent"], "claude")
        self.assertEqual(callback["target"], {"kind": "session", "session": "parent-claude"})
        details = Path(callback["prompt_details_path"]).read_text(encoding="utf-8")
        self.assertIn("ORIGINAL HANDOFF", details)
        self.assertNotIn("REPLACEMENT", details)

    def test_builtin_test_template_preserves_test_authoring_contract_without_codex_defaults(self):
        self.assertEqual(self.invoke(["--template", "test"])[0], 0)
        _, task = self.task()
        self.assertIsNone(task.get("child_model"))
        self.assertIsNone(task.get("child_reasoning_effort"))
        self.assertNotIn("--model", task["wrapped_command"])
        self.assertNotIn("--thinking", task["wrapped_command"])
        self.assertIn("Do not run tests", Path(task["agent_prompt_path"]).read_text(encoding="utf-8"))
        task.update(state="completed", outcome="completed", exit_code=7)
        handoff = managed_tasks.managed_task_prompt(task, outcome="completed")
        self.assertIn("NOT a test execution result", handoff)
        self.assertIn("execute the tests", handoff)
        self.assertIn("exit code: 7", handoff)

    def test_dry_run_resolves_pi_profile_without_a_queue_or_process(self):
        self.write_template("version: 2\nprompt: PI DRY RUN PROMPT\n"
                            "pi:\n  model: dry-provider/dry-model\n  reasoning_effort: xhigh\n"
                            "handoff: PI DRY RUN HANDOFF\n")
        with mock.patch.object(cli.subprocess, "run") as process:
            result, output = self.invoke(["--template", "review", "--dry-run"])
        self.assertEqual(result, 0)
        for expected in ("PI Agent", "dry-provider/dry-model", "xhigh", "PI DRY RUN PROMPT",
                         "PI DRY RUN HANDOFF", "Inspect the public Pi child contract."):
            self.assertIn(expected, output)
        process.assert_not_called()
        self.assertFalse(self.queue.exists())

    def test_cli_system_prompt_uses_the_submitting_directory_and_a_private_literal_snapshot(self):
        submitter = self.directory / "submitting shell"
        child = self.directory / "different child cwd"
        submitter.mkdir()
        child.mkdir()
        source = submitter / "Pi role 中文.md"
        system_text = "Use 中文 instructions.\nKeep $PI_MODEL, ${HOME}, $(echo literal), and {{role}} literal.\n"
        # A valid UTF-8 BOM is removed as Pi does; all remaining text is literal.
        source.write_bytes(b"\xef\xbb\xbf" + system_text.encode("utf-8"))
        user_prompt = "This is the user task, separate from the Pi system instructions."
        original_cwd = Path.cwd()
        try:
            os.chdir(submitter)
            with mock.patch.object(cli.subprocess, "run") as process:
                self.assertEqual(self.invoke(["--cwd", str(child), "--system-prompt-file", source.name],
                                             prompt=user_prompt)[0], 0)
                process.assert_not_called()
        finally:
            os.chdir(original_cwd)
        path, task = self.task()
        snapshot = Path(task["agent_system_prompt_path"])
        self.assertEqual(task["agent_system_prompt_source"], str(source.resolve()))
        self.assertEqual(snapshot, path.parent / "agent-system-prompt.md")
        self.assertNotEqual(snapshot, source)
        self.assertEqual(snapshot.read_bytes(), system_text.encode("utf-8"))
        assert_private_file(self, snapshot)
        command = task["wrapped_command"]
        self.assertEqual(command[command.index("--system-prompt") + 1], str(snapshot))
        self.assertNotIn(str(source), command)
        self.assertNotIn(system_text, path.read_text(encoding="utf-8"))
        self.assertEqual(Path(task["agent_prompt_path"]).read_text(encoding="utf-8"), reins_records.agent_prompt(user_prompt, task))
        self.assertEqual(task["cwd"], str(child.resolve()))

    def test_yaml_system_prompt_is_relative_to_the_template_and_cli_file_overrides_it(self):
        prompts = self.templates / "prompts"
        prompts.mkdir()
        default = prompts / "Pi role.md"
        default.write_text("System instructions supplied by the YAML profile.\n", encoding="utf-8")
        template = self.write_template("version: 4\nprompt: YAML user-task instructions.\n"
                                       "pi:\n  system_prompt_file: prompts/Pi role.md\n")
        submitter = self.directory / "submission directory"
        child = self.directory / "child directory"
        submitter.mkdir()
        child.mkdir()
        override = submitter / "CLI override.md"
        override.write_text("System instructions from the CLI override.\n", encoding="utf-8")
        # A decoy with the same relative name catches resolution against the
        # submission shell rather than the YAML source directory.
        (submitter / "prompts").mkdir()
        (submitter / "prompts" / default.name).write_text("Wrong relative base.\n", encoding="utf-8")
        for extra, expected in (([], default), (["--system-prompt-file", override.name], override)):
            with self.subTest(source=expected):
                self.queue = self.directory / ("yaml-system-default" if expected == default else "cli-system-override")
                original_cwd = Path.cwd()
                try:
                    os.chdir(submitter)
                    self.assertEqual(self.invoke(["--cwd", str(child), "--template-file", str(template), *extra])[0], 0)
                finally:
                    os.chdir(original_cwd)
                _, task = self.task()
                snapshot = Path(task["agent_system_prompt_path"])
                self.assertEqual(task["agent_system_prompt_source"], str(expected.resolve()))
                self.assertEqual(snapshot.read_bytes(), expected.read_bytes())
                self.assertEqual(task["agent_template_source"], str(template.resolve()))
                self.assertIn("YAML user-task instructions.",
                              Path(task["agent_prompt_path"]).read_text(encoding="utf-8"))

    def test_invalid_system_prompt_files_fail_before_any_task_persistence(self):
        invalid = {"empty.md": b"", "whitespace.md": b" \n\t", "invalid-utf8.md": b"\xff\xfe",
                   "bom-only.md": b"\xef\xbb\xbf", "bom-whitespace.md": b"\xef\xbb\xbf \n"}
        candidates = [self.directory / "missing.md", self.templates]
        for name, content in invalid.items():
            path = self.directory / name
            path.write_bytes(content)
            candidates.append(path)
        for source in candidates:
            for selector in ("cli", "yaml"):
                with self.subTest(source=source.name, selector=selector):
                    if selector == "cli":
                        options = ["--system-prompt-file", str(source)]
                    else:
                        template = self.write_template("version: 1\nprompt: Valid user-task profile.\n"
                                                       "pi:\n  system_prompt_file: " + json.dumps(str(source)) + "\n")
                        options = ["--template-file", str(template)]
                    self.assert_rejected(options)
        self.assert_rejected(["--system-prompt-file", str(self.directory / "missing.md"), "--dry-run"])

    def test_worker_reads_the_saved_system_prompt_after_source_rewrite_or_deletion(self):
        executable = self.fake_pi()
        original_system = "ORIGINAL PI SYSTEM INSTRUCTIONS\nKeep ${PI_MODEL} literal. 中文\n"
        user_prompt = "USER TASK: inspect artifacts using the saved instructions."
        for change in ("rewrite", "delete"):
            with self.subTest(change=change):
                self.queue = self.directory / f"frozen-system-{change}"
                source = self.directory / f"source-system-{change}.md"
                source.write_text(original_system, encoding="utf-8")
                capture = self.directory / f"system-capture-{change}.json"
                environment = self.fixture_environment(executable, capture, report="Saved-system report\n", exit_code=0)
                with mock.patch.dict(os.environ, environment):
                    self.assertEqual(self.invoke(["--system-prompt-file", str(source)],
                                                 parent="claude", prompt=user_prompt)[0], 0)
                path, task = self.task()
                if change == "rewrite":
                    source.write_text("REPLACEMENT SYSTEM INSTRUCTIONS\n", encoding="utf-8")
                else:
                    source.unlink()
                self.assertEqual(self.run_worker(path, task), 0,
                                 Path(task["log_path"]).read_text(encoding="utf-8"))
                received = json.loads(capture.read_text(encoding="utf-8"))
                self.assertEqual(received["system_prompt"], original_system)
                self.assertEqual(received["system_prompt_path"], task["agent_system_prompt_path"])
                self.assertEqual(received["prompt"], reins_records.agent_prompt(user_prompt, task))
                snapshot = Path(task["agent_system_prompt_path"])
                self.assertEqual(snapshot.read_text(encoding="utf-8"), original_system)
                assert_private_file(self, snapshot)
                callback = dispatch.load_request(self.queue / "pending" / f"{ids.callback_id(task['id'])}.json")
                self.assertEqual(callback["agent"], "claude")
                self.assertEqual(callback["target"], {"kind": "session", "session": "parent-claude"})
                self.assertEqual(callback["exit_code"], 0)

    def test_worker_refuses_missing_or_blank_private_system_prompt_before_starting_pi(self):
        executable = self.fake_pi()
        source = self.directory / "valid-original-system.md"
        source.write_text("Valid original system instructions.\n", encoding="utf-8")
        for damage in ("missing", "blank"):
            with self.subTest(damage=damage):
                self.queue = self.directory / f"damaged-system-{damage}"
                capture = self.directory / f"damaged-system-capture-{damage}.json"
                environment = self.fixture_environment(executable, capture, report="Must never execute.\n", exit_code=0)
                with mock.patch.dict(os.environ, environment):
                    self.assertEqual(self.invoke(["--system-prompt-file", str(source)])[0], 0)
                path, task = self.task()
                snapshot = Path(task["agent_system_prompt_path"])
                if damage == "missing":
                    snapshot.unlink()
                else:
                    snapshot.write_text(" \n\t", encoding="utf-8")
                self.assert_system_prompt_worker_rejected(path, task, capture)

    def test_worker_refuses_a_system_prompt_record_pointing_outside_its_task_directory(self):
        executable = self.fake_pi()
        source = self.directory / "outside-managed-task-system.md"
        source.write_text("Readable source is not the managed task snapshot.\n", encoding="utf-8")
        capture = self.directory / "wrong-system-path-capture.json"
        environment = self.fixture_environment(executable, capture, report="Must never execute.\n", exit_code=0)
        with mock.patch.dict(os.environ, environment):
            self.assertEqual(self.invoke(["--system-prompt-file", str(source)], parent="claude")[0], 0)
        path, task = self.task()
        task["agent_system_prompt_path"] = str(source)
        self.assert_system_prompt_worker_rejected(path, task, capture)

    def test_system_prompt_dry_run_reports_the_resolved_source_without_writes_or_execution(self):
        source = self.templates / "dry-run-system.md"
        source.write_text("Use this system prompt only after durable submission.\n", encoding="utf-8")
        template = self.write_template("version: 2\nprompt: Dry-run task profile.\n"
                                       "pi:\n  system_prompt_file: dry-run-system.md\n")
        for options in (["--system-prompt-file", str(source)], ["--template-file", str(template)]):
            with self.subTest(options=options), mock.patch.object(cli.subprocess, "run") as process:
                result, output = self.invoke([*options, "--dry-run"])
            self.assertEqual(result, 0)
            self.assertIn(str(source.resolve()), output)
            process.assert_not_called()
            self.assertFalse(self.queue.exists())
            self.assertFalse(list(self.directory.rglob("agent-system-prompt.md")))

    def test_system_prompt_file_cli_option_is_available_only_for_pi_children(self):
        source = self.directory / "valid-system.md"
        source.write_text("Valid system instructions.\n", encoding="utf-8")
        for worker in ("codex", "claude"):
            argv = ["reins", "agent", worker, "--queue-dir", str(self.queue), "--agent", "codex",
                    "--session", "parent-codex", "--system-prompt-file", str(source), "--", "User task."]
            with self.subTest(worker=worker), mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(work_commands, "agent", return_value=0) as submit, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as rejected:
                cli.main()
            self.assertNotEqual(rejected.exception.code, 0)
            submit.assert_not_called()
            self.assertFalse(layout.managed_tasks_root(self.queue).exists())

    def test_doctor_checks_pi_child_executable_separately_from_callback_agent(self):
        environment = {"REINS_CODEX_BIN": "fixture-codex",
                       "REINS_CLAUDE_BIN": "fixture-claude",
                       "REINS_PI_BIN": "fixture-pi"}
        for parent, available in (("codex", True), ("claude", False)):
            with self.subTest(parent=parent, pi_available=available):
                output = io.StringIO()
                argv = ["reins", "doctor", "--operation", "agent", "--agent-worker", "pi",
                        "--agent", parent, "--session", f"parent-{parent}", "--backend", "screen",
                        "--callback-mode", "cli", "--queue-dir", str(self.queue)]

                def which(name):
                    return None if name == "fixture-pi" and not available else "/fixture/bin/" + name

                with mock.patch.dict(os.environ, environment), mock.patch.object(sys, "argv", argv), \
                        mock.patch.object(diagnostics, "queue_writable", return_value=True), \
                        mock.patch.object(diagnostics, "coordinator_issue", return_value=None), \
                        mock.patch.object(diagnostics, "runtime_issues", return_value=[]), \
                        mock.patch.object(diagnostics, "delivery_issues", return_value=[]), \
                        mock.patch.object(diagnostics.ScreenBackend, "available", return_value=True), \
                        mock.patch.object(diagnostics.routes, "inspect_route", return_value={"status": "ready"}), \
                        mock.patch.object(diagnostics.shutil, "which", side_effect=which) as probe, \
                        contextlib.redirect_stdout(output):
                    result = cli.main()
                report = json.loads(output.getvalue())
                codes = {issue["code"] for issue in report["issues"]}
                self.assertEqual(result, 0 if available else 1)
                self.assertEqual("child_agent_unavailable" in codes, not available)
                self.assertNotIn("callback_agent_unavailable", codes)
                self.assertNotIn("child_agent_unspecified", codes)
                self.assertNotIn("diagnostics_failed", codes)
                probe.assert_any_call("fixture-pi")
                recheck = report["recheck_command"]
                self.assertEqual(recheck[recheck.index("--agent-worker") + 1], "pi")
                self.assertEqual(recheck[recheck.index("--agent") + 1], parent)
                self.assertEqual(recheck[recheck.index("--session") + 1], f"parent-{parent}")
                repair = report["repair_command"]
                self.assertEqual(repair[repair.index("--skill-target") + 1], parent)
                self.assertFalse(self.queue.exists())


if __name__ == "__main__":
    unittest.main()
