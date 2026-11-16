from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness_reins.core import sessions
from harness_reins.core.sessions import SessionView
from harness_reins.engine import claude_background as background
from harness_reins.engine import delivery as delivery_engine
from harness_reins.harness.claude import session as claude_session

SESSION = "08e8651e-ea49-48d0-b286-088a680b9c82"
CLAUDE = "/opt/claude/bin/claude"


def completed(argv, stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


class FakeClaude:
    """Answers Claude commands the way 2.1.325 does, and records them."""

    def __init__(self, *, short: str = SESSION[:8], untrusted: bool = False, help_text: str = "  --bg, --background") -> None:
        self.calls: list[list[str]] = []
        self.short = short
        self.untrusted = untrusted
        self.help_text = help_text

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        self.calls.append(argv)
        command = argv[argv.index(CLAUDE) + 1:] if CLAUDE in argv else argv
        if command[:1] == ["--help"]:
            return completed(argv, self.help_text)
        if command[:1] == ["--bg"]:
            if self.untrusted:
                return completed(argv, "", 1, "Workspace not trusted. Run `claude` in /x once and accept the trust prompt, then retry.")
            return completed(argv, f"backgrounded · {self.short} (idle — send a prompt to start)\n  claude agents  list sessions")
        if command[:1] in (["stop"], ["rm"]):
            return completed(argv, f"{command[0]}ped {command[1]}")
        if command[:2] == ["agents", "--json"]:
            return completed(argv, "[]")
        return completed(argv, "", 2, f"unexpected {command}")

    def claude_calls(self) -> list[list[str]]:
        return [call[call.index(CLAUDE) + 1:] for call in self.calls if CLAUDE in call]


class BackgroundCase(unittest.TestCase):
    def setUp(self) -> None:
        background._SUPPORT.clear()
        found = claude_session.Executable(CLAUDE, "configured")
        patcher = mock.patch.object(claude_session, "discover_executable", return_value=found)
        patcher.start()
        self.addCleanup(patcher.stop)
        stat = mock.patch.object(background, "_modified", return_value=1.0)
        stat.start()
        self.addCleanup(stat.stop)


class RulesAndEnvironmentTests(BackgroundCase):
    def test_allow_rules_cover_only_bookkeeping_commands(self) -> None:
        rules = background.allow_rules(["/home/u/.reins/bin/reins", "reins"])
        self.assertIn("Bash(/home/u/.reins/bin/reins ack *)", rules)
        self.assertIn("Bash(reins goal check *)", rules)
        self.assertFalse(any(" run " in rule or "cancel" in rule for rule in rules))
        self.assertEqual(json.loads(background.settings_argument(["reins"]))["permissions"]["allow"],
                         background.allow_rules(["reins"]))

    def test_launch_environment_drops_the_parent_session(self) -> None:
        clean = background.launch_environment({
            "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": SESSION, "CLAUDE_CODE_CHILD_SESSION": "1",
            "CLAUDE_CONFIG_DIR": "/cfg", claude_session.RESUMED_TARGET_ENV: "session:x", "PATH": "/bin"})
        self.assertEqual({key: clean[key] for key in ("CLAUDE_CONFIG_DIR", "PATH")}, {"CLAUDE_CONFIG_DIR": "/cfg", "PATH": "/bin"})
        self.assertFalse([key for key in clean if key.startswith("CLAUDE") and key != "CLAUDE_CONFIG_DIR"])
        self.assertNotIn(claude_session.RESUMED_TARGET_ENV, clean)
        # How the user's Claude signs in and which provider it uses stay.
        kept = background.launch_environment({"CLAUDE_CODE_OAUTH_TOKEN": "t", "CLAUDE_CODE_USE_BEDROCK": "1",
                                              "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1", "ANTHROPIC_API_KEY": "k",
                                              "HTTPS_PROXY": "http://p", "CLAUDE_CODE_ENTRYPOINT": "cli"})
        kept = {key for key in kept if key not in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")}
        self.assertEqual(sorted(kept), ["ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
                                        "CLAUDE_CODE_USE_BEDROCK", "HTTPS_PROXY"])

    def test_scope_on_linux_with_a_user_manager(self) -> None:
        with mock.patch.object(background.sys, "platform", "linux"), \
                mock.patch.object(background.shutil, "which", return_value="/usr/bin/systemd-run"):
            argv = background.scoped(["claude", "--bg"], {"XDG_RUNTIME_DIR": "/run/user/1", "PATH": "/usr/bin"})
            self.assertEqual(argv[:3], ["/usr/bin/systemd-run", "--user", "--scope"])
            self.assertEqual(argv[-2:], ["claude", "--bg"])
            self.assertEqual(background.scoped(["claude"], {"PATH": "/usr/bin"}), ["claude"])
        with mock.patch.object(background.sys, "platform", "darwin"):
            self.assertEqual(background.scoped(["claude"], {"XDG_RUNTIME_DIR": "/r"}), ["claude"])

    def test_support_is_read_from_help(self) -> None:
        self.assertTrue(background.supports_background({}, run=FakeClaude()))
        background._SUPPORT.clear()
        self.assertFalse(background.supports_background({}, run=FakeClaude(help_text="  -p, --print")))


class ResumeTests(BackgroundCase):
    def test_a_closed_session_gets_settings_and_permission_mode(self) -> None:
        fake = FakeClaude()
        with mock.patch.object(background, "scoped", side_effect=lambda argv, env: argv):
            short = background.resume(SESSION, "/w", {}, stopped=False, settings='{"x":1}', permission_mode="auto",
                                      extra=["--add-dir", "/q"], run=fake)
        self.assertEqual(short, SESSION[:8])
        self.assertEqual(fake.claude_calls(), [["--bg", "--resume", SESSION, "--settings", '{"x":1}',
                                                "--permission-mode", "auto", "--add-dir", "/q"]])

    def test_a_stopped_background_session_gets_no_flags(self) -> None:
        # New flags would make Claude start a copy under another id.
        fake = FakeClaude()
        background.resume(SESSION, "/w", {}, stopped=True, settings='{"x":1}', permission_mode="auto",
                          extra=["--add-dir", "/q"], run=fake)
        self.assertEqual(fake.claude_calls(), [["--bg", "--resume", SESSION]])

    def test_a_copy_is_removed_and_reported(self) -> None:
        fake = FakeClaude(short="f87b2607")
        with self.assertRaises(background.StartedCopy):
            background.resume(SESSION, "/w", {}, stopped=False, run=fake)
        self.assertEqual(fake.claude_calls()[1:], [["stop", "f87b2607"], ["rm", "f87b2607"]])

    def test_an_untrusted_directory_says_so(self) -> None:
        with self.assertRaises(background.Untrusted) as caught:
            background.resume(SESSION, "/w", {}, stopped=False, run=FakeClaude(untrusted=True))
        self.assertIn("/w", str(caught.exception))

    def test_a_failure_names_the_last_line(self) -> None:
        def failing(argv, **kwargs):
            return completed(argv, "starting\n", 1, "Error: something broke")
        with self.assertRaises(background.BackgroundError) as caught:
            background.stop("08e8651e", {}, run=failing)
        self.assertIn("something broke", str(caught.exception))

    def test_an_unreadable_success_warns_about_a_possible_orphan(self) -> None:
        def odd(argv, **kwargs):
            return completed(argv, "Started session in background\n")
        with mock.patch("sys.stderr", io.StringIO()) as err, self.assertRaises(background.BackgroundError):
            background.start("/w", {}, run=odd)
        self.assertIn("claude agents", err.getvalue())

    def test_start_returns_the_short_id(self) -> None:
        fake = FakeClaude(short="abcdef12")
        self.assertEqual(background.start("/w", {}, run=fake), "abcdef12")
        self.assertEqual(fake.claude_calls(), [["--bg"]])

    def test_commands_run_without_the_parent_session(self) -> None:
        fake = mock.Mock(wraps=FakeClaude())
        background.stop("08e8651e", {"CLAUDECODE": "1", "PATH": "/bin"}, run=fake)
        self.assertNotIn("CLAUDECODE", fake.call_args.kwargs["env"])


class ResumeForCallbackTests(BackgroundCase):
    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        hook = mock.patch.object(claude_session, "hook_installed", return_value=True)
        hook.start()
        self.addCleanup(hook.stop)

    def resume(self, fake, view=None, **kwargs):
        with mock.patch.object(background.claude_agents, "session_view", return_value=view), \
                mock.patch.object(background, "scoped", side_effect=lambda argv, env: argv), \
                mock.patch("sys.stderr", io.StringIO()):
            return background.resume_for_callback(self.root, SESSION, "/w", {}, reins_commands=["reins"],
                                                   permission_mode="auto", run=fake, **kwargs)

    def test_a_closed_session_comes_up_and_waits_for_the_hook(self) -> None:
        fake = FakeClaude()
        closed = SessionView(agent="claude", session=SESSION, state=sessions.CLOSED)
        self.assertIsNone(self.resume(fake, closed, now=1000.0))
        resumed = [call for call in fake.claude_calls() if call[:1] == ["--bg"]]
        self.assertEqual(resumed[0][:3], ["--bg", "--resume", SESSION])
        self.assertIn("--settings", resumed[0])
        # A second try soon after means the first did not come up: go headless.
        self.assertIn("did not come up", self.resume(FakeClaude(), closed, now=1010.0))
        self.assertIsNone(self.resume(FakeClaude(), closed, now=1000.0 + background.RETRY_AFTER_SECONDS + 1))

    def test_a_session_with_a_process_is_left_to_its_hook(self) -> None:
        fake = FakeClaude()
        window = SessionView(agent="claude", session=SESSION, state=sessions.FOREIGN, pid=42)
        self.assertIsNone(self.resume(fake, window))
        # Also right after Reins brought it up: it is up, so its hook delivers.
        self.assertIsNone(self.resume(fake, SessionView(agent="claude", session=SESSION, state=sessions.CLOSED), now=5.0))
        running = SessionView(agent="claude", session=SESSION, state=sessions.RUNNING, pid=7)
        fake = FakeClaude()
        self.assertIsNone(self.resume(fake, running, now=6.0))
        self.assertFalse([call for call in fake.claude_calls() if call[:1] == ["--bg"]])

    def test_a_stopped_session_is_woken_without_flags(self) -> None:
        fake = FakeClaude()
        stopped = SessionView(agent="claude", session=SESSION, state=sessions.STOPPED)
        self.assertIsNone(self.resume(fake, stopped))
        self.assertIn(["--bg", "--resume", SESSION], fake.claude_calls())

    def test_reasons_to_resume_headless(self) -> None:
        self.assertIn("trusted", self.resume(FakeClaude(untrusted=True)))
        background._SUPPORT.clear()
        self.assertIn("no --bg", self.resume(FakeClaude(help_text="-p")))
        self.assertIn("=0", background.resume_for_callback(self.root, SESSION, "/w", {background.DISABLE_ENV: "0"},
                                                           reins_commands=[], permission_mode=None, run=FakeClaude()))
        with mock.patch.object(claude_session, "hook_installed", return_value=False):
            self.assertIn("hook", self.resume(FakeClaude()))

    def test_a_bug_falls_back_instead_of_raising(self) -> None:
        with mock.patch.object(background, "supports_background", side_effect=RuntimeError("boom")):
            self.assertIn("boom", self.resume(FakeClaude()))


class WorkerTests(unittest.TestCase):
    def run_worker(self, why_not):
        with tempfile.TemporaryDirectory() as tmp:
            request = {"id": "cb-1", "agent": "claude", "target": {"kind": "session", "session": SESSION}}
            payload = {
                "command": [sys.executable, "-c", "pass"], "prompt": "wake up", "cwd": tmp, "request": request,
                "timeout": 5.0, "queue_dir": tmp, "ack_path": str(Path(tmp) / "acks" / "cb-1.json"),
                "canceled_path": str(Path(tmp) / "canceled.json"),
            }
            read_fd, lock_fd = os.pipe()
            output = io.StringIO()
            process = mock.Mock(returncode=0)
            process.communicate.return_value = ("", "")
            try:
                with mock.patch.dict(os.environ, {"CODEX_LONG_TASK_DELIVERY_LOCK_FDS": json.dumps([lock_fd])}), \
                        mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                        mock.patch.object(sys, "stdout", output), \
                        mock.patch("sys.stderr", io.StringIO()), \
                        mock.patch.object(delivery_engine, "start_desktop_app_server_turn", return_value=None), \
                        mock.patch.object(delivery_engine, "waits_for_open_window", return_value=False), \
                        mock.patch.object(delivery_engine, "resume_in_background", return_value=why_not) as tried, \
                        mock.patch.object(delivery_engine.claude_stream, "supervise", return_value=0), \
                        mock.patch.object(delivery_engine.subprocess, "Popen", return_value=process) as popen:
                    self.assertEqual(delivery_engine.delivery_worker_main(), 0)
            finally:
                os.close(read_fd)
            return json.loads(output.getvalue()), tried, popen

    def test_a_session_up_in_the_background_waits_for_its_hook(self) -> None:
        result, tried, popen = self.run_worker(None)
        self.assertEqual((result["returncode"], result["delivery_state"]), (123, "waiting_for_window"))
        tried.assert_called_once()
        popen.assert_not_called()

    def test_otherwise_the_headless_resume_runs(self) -> None:
        result, tried, popen = self.run_worker("this Claude Code CLI has no --bg")
        self.assertEqual(result["returncode"], 0)
        popen.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class SetupPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "settings.json"

    def test_adds_once_keeps_other_rules_and_removes_only_its_own(self) -> None:
        from harness_reins.install import skills

        self.path.write_text(json.dumps({"permissions": {"allow": ["Bash(npm test *)"], "deny": ["Bash(rm *)"]},
                                         "model": "opus"}))
        commands = ["/home/u/.reins/bin/reins", "reins"]
        added = skills.configure_claude_permissions(commands, self.path)
        self.assertEqual(added, background.allow_rules(commands))
        self.assertEqual(skills.configure_claude_permissions(commands, self.path), [])  # Idempotent.
        settings = json.loads(self.path.read_text())
        self.assertEqual(settings["permissions"]["allow"][0], "Bash(npm test *)")
        self.assertEqual((settings["permissions"]["deny"], settings["model"]), (["Bash(rm *)"], "opus"))
        removed = skills.configure_claude_permissions(None, self.path)
        self.assertEqual(sorted(removed), sorted(added))
        self.assertEqual(json.loads(self.path.read_text())["permissions"],
                         {"allow": ["Bash(npm test *)"], "deny": ["Bash(rm *)"]})

    def test_an_empty_allow_list_is_dropped_on_removal(self) -> None:
        from harness_reins.install import skills

        skills.configure_claude_permissions(["reins"], self.path)
        skills.configure_claude_permissions(None, self.path)
        self.assertEqual(json.loads(self.path.read_text()), {})

    def test_a_malformed_allow_list_is_left_alone(self) -> None:
        from harness_reins.install import skills

        self.path.write_text(json.dumps({"permissions": {"allow": "Bash(*)"}}))
        with self.assertRaises(ValueError):
            skills.configure_claude_permissions(["reins"], self.path)
        self.assertEqual(json.loads(self.path.read_text()), {"permissions": {"allow": "Bash(*)"}})

    def test_only_reins_bookkeeping_rules_count_as_its_own(self) -> None:
        from harness_reins.install import skills

        self.assertTrue(skills._is_reins_rule("Bash(/x/bin/reins goal check *)"))
        self.assertFalse(skills._is_reins_rule("Bash(git ack *)"))
        self.assertFalse(skills._is_reins_rule("Bash(reins run *)"))
        self.assertFalse(skills._is_reins_rule("Bash(foreins ack *)"))
        self.assertFalse(skills._is_reins_rule("Bash(sudo reins ack *)"))
        self.assertTrue(skills._is_reins_rule("Bash(reins ack *)"))
