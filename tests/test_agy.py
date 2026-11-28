"""Antigravity CLI (agy) integration contracts: binding, open-window holds and headless resume."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import yaml

from harness_reins.engine import delivery as delivery_engine
from harness_reins.engine import dispatch
from harness_reins.engine import messages as message_engine
from harness_reins.engine import resume_environment
from harness_reins.harness import AGENTS, ChildOptions, get_agent
from harness_reins.harness.agy import adapter as agy_adapter
from harness_reins.harness.agy import session as agy_session
from harness_reins.harness.claude import session as live_session
from harness_reins.install import skills as skill_install

CONVERSATION = "2b226add-59f9-42ee-bf33-285b515db906"
POSIX_ONLY = unittest.skipIf(os.name == "nt", "the presence lock is checked with flock")


@contextlib.contextmanager
def window_holding(conversation: str, app_data: Path):
    """What an open agy window does: hold an exclusive flock on the conversation's presence file."""
    import fcntl

    presence = app_data / "presence"
    presence.mkdir(parents=True, exist_ok=True)
    with open(presence / f"{conversation}.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


class AgyAdapterTests(unittest.TestCase):
    def test_registered_and_detected_from_the_conversation_variable(self):
        self.assertIn("agy", AGENTS.names)
        for environment in ({"ANTIGRAVITY_CONVERSATION_ID": CONVERSATION}, {"ANTIGRAVITY_AGENT": "1"},
                            {"ANTIGRAVITY_CONVERSATION_ID": CONVERSATION, "CLAUDECODE": "1",
                             "CLAUDE_CODE_SESSION_ID": "claude-parent"}):
            with self.subTest(environment=environment):
                self.assertEqual(AGENTS.detect(environment), "agy")
        # A DSH shell started from agy is still DSH.
        self.assertEqual(AGENTS.detect({"ANTIGRAVITY_AGENT": "1", "DSH_SESSION_ID": "session-x"}), "dsh")

    def test_session_values_and_the_csrf_token_never_reach_a_child_or_a_stored_resume(self):
        environment = {"ANTIGRAVITY_CONVERSATION_ID": CONVERSATION, "ANTIGRAVITY_AGENT": "1",
                       "ANTIGRAVITY_CSRF_TOKEN": "secret", "ANTIGRAVITY_LS_ADDRESS": "localhost:1",
                       "ANTIGRAVITY_APP_DATA_DIR": "/home/u/.gemini/antigravity-cli", "PATH": "/bin"}
        kept = resume_environment.session_environment(environment)
        self.assertEqual(kept, {"ANTIGRAVITY_APP_DATA_DIR": "/home/u/.gemini/antigravity-cli", "PATH": "/bin"})

    def test_resume_appends_a_headless_turn_with_the_prompt_on_stdin(self):
        request = {"agent": "agy", "target": {"kind": "session", "session": CONVERSATION},
                   "queue_dir": "/q", "link_dir": "/l"}
        command = get_agent("agy").resume_command(request, {"REINS_AGY_BIN": "/opt/agy"})
        self.assertEqual(command, ["/opt/agy", "--resume", CONVERSATION, "--add-dir", "/q", "--add-dir", "/l",
                                   "--input-format", "stream-json", "--output-format", "stream-json",
                                   "--print", ""])
        line = agy_adapter.resume_input("[reins] task-c1\nline two")
        self.assertTrue(line.endswith("\n"))
        self.assertEqual(json.loads(line), {"event": "user", "message": {"content": "[reins] task-c1\nline two"}})

    def test_a_headless_resume_may_run_the_reins_agent_commands_and_keeps_the_users_rules(self):
        with tempfile.TemporaryDirectory() as data:
            Path(data, "settings.json").write_text(json.dumps({"model": "x", "permissions": {
                "allow": ["command(git status)"], "deny": ["command(rm)"]}}), encoding="utf-8")
            environment = {"ANTIGRAVITY_APP_DATA_DIR": data}
            settings = json.loads(agy_session.resume_settings(["/opt/reins", "reins"], environment))
        self.assertEqual(set(settings), {"permissions"})  # Nothing else of the user's is overridden.
        permissions = settings["permissions"]
        self.assertEqual(permissions["deny"], ["command(rm)"])
        self.assertEqual(permissions["allow"][0], "command(git status)")
        for rule in ("command(/opt/reins ack)", "command(reins reply)", "command(reins goal ack)"):
            self.assertIn(rule, permissions["allow"])
        # Nothing that starts work is allowed headless.
        self.assertFalse([rule for rule in permissions["allow"] if " run)" in rule or " agent)" in rule])

    def test_the_resume_command_carries_those_settings_before_the_prompt(self):
        request = {"agent": "agy", "target": {"kind": "session", "session": CONVERSATION}, "queue_dir": "/q"}
        with tempfile.TemporaryDirectory() as data, \
                mock.patch.dict(os.environ, {"ANTIGRAVITY_APP_DATA_DIR": data, "REINS_AGY_BIN": "/opt/agy"}):
            command = delivery_engine.resume_command(request)
        self.assertEqual(command[-4], "--settings")
        self.assertIn("command(reins reply)", command[-3])
        self.assertEqual(command[-2:], ["--print", ""])

    def test_not_offered_as_a_child_agent(self):
        from harness_reins.harness import CHILD_AGENT_NAMES, get_child_agent
        self.assertNotIn("agy", CHILD_AGENT_NAMES)
        with self.assertRaisesRegex(ValueError, "unsupported child agent"):
            get_child_agent("agy")

    def test_children_are_refused_with_a_clear_message(self):
        options = ChildOptions(cwd="/w", result_path=Path("/r"), sandbox_mode="workspace-write", permission_mode="auto")
        with self.assertRaisesRegex(ValueError, "not supported yet"):
            get_agent("agy").child_command(options, {})

    def test_acknowledgement_explains_both_routes(self):
        text = dispatch.build_acknowledgement_text("reins ack x-c1", "agy")
        self.assertIn("reins wait", text)
        self.assertIn("agy --resume", text)

    def test_launcher_discovery(self):
        found = agy_session.discover_executable({"REINS_AGY_BIN": "/custom/agy"}, which=lambda name: None)
        self.assertEqual((found.path, found.source), ("/custom/agy", "configured"))
        found = agy_session.discover_executable({}, which=lambda name: "/usr/bin/agy")
        self.assertEqual((found.path, found.source), ("/usr/bin/agy", "path"))
        with tempfile.TemporaryDirectory() as home:
            binary = Path(home) / ".local" / "bin" / "agy"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o755)
            found = agy_session.discover_executable({"HOME": home}, which=lambda name: None)
            if os.name != "nt":
                self.assertEqual((found.path, found.source), (str(binary), "standalone"))


@POSIX_ONLY
class AgyWindowTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.app_data = Path(self.home.name) / "agy"
        self.root = Path(self.home.name) / "queue"
        for state in ("pending", "running", "failed", "done", "acks", "locks"):
            (self.root / state).mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.dict(os.environ, {
            "HOME": self.home.name, "REINS_HOME": str(Path(self.home.name) / ".reins"),
            "ANTIGRAVITY_APP_DATA_DIR": str(self.app_data), "ANTIGRAVITY_CONVERSATION_ID": CONVERSATION,
        }, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_request(self, callback_id="task-c1"):
        request = {"schema": "reins.callback/1", "id": callback_id, "callback_mode": "cli", "agent": "agy",
                   "target": {"kind": "session", "session": CONVERSATION}, "prompt": "[reins] callback",
                   "prompt_format": "full", "requires": [], "task": "train", "cwd": self.home.name}
        path = self.root / "pending" / f"{callback_id}.json"
        path.write_text(json.dumps(request), encoding="utf-8")
        return path

    def wait_args(self, **overrides):
        values = dict(queue_dir=str(self.root), task=None, session=None, agent=None, timeout=5, poll_interval=0.05)
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_presence_lock_says_whether_a_window_has_the_conversation_open(self):
        self.assertFalse(agy_session.window_open(CONVERSATION, os.environ))
        with window_holding(CONVERSATION, self.app_data):
            self.assertTrue(agy_session.window_open(CONVERSATION, os.environ))
        self.assertFalse(agy_session.window_open(CONVERSATION, os.environ))  # The file stays; the lock does not.
        self.assertFalse(agy_session.window_open("../../etc/passwd", os.environ))

    def test_the_daemon_never_resumes_beside_an_open_window(self):
        path = self.write_request()
        with window_holding(CONVERSATION, self.app_data):
            self.assertIsNone(delivery_engine.select_pending(self.root, 10**12))
        self.assertEqual(delivery_engine.select_pending(self.root, 10**12), path)

    def test_a_wait_in_the_window_claims_the_callback(self):
        self.write_request()
        output = io.StringIO()
        with window_holding(CONVERSATION, self.app_data):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(live_session.wait(self.wait_args()), 0)
        self.assertIn("[reins] callback", output.getvalue())

    def test_a_wait_leaves_when_its_window_closes(self):
        # The window has the conversation open when the wait starts, then closes.
        seen = iter([True])
        errors = io.StringIO()
        with mock.patch.object(live_session, "WAIT_WINDOW_CHECK_SECONDS", 0.05), \
                mock.patch.object(live_session, "_window_of", side_effect=lambda agent, session: next(seen, None)), \
                contextlib.redirect_stderr(errors):
            self.assertEqual(live_session.wait(self.wait_args()), 0)
        self.assertIn("Antigravity window", errors.getvalue())

    def test_inside_a_headless_resume_a_wait_returns_at_once_even_for_a_task(self):
        errors = io.StringIO()
        with mock.patch.object(live_session, "inside_resume_of", return_value=True), \
                mock.patch("harness_reins.store.layout.managed_task_path", return_value=Path(__file__)), \
                contextlib.redirect_stderr(errors):
            self.assertEqual(live_session.wait(self.wait_args(task="task")), 0)
        self.assertIn("headless resume ends with its turn", errors.getvalue())

    def test_a_message_resumes_a_closed_conversation_and_waits_for_an_open_one(self):
        record = {"harness": "agy", "session": CONVERSATION, "route": {}}
        self.assertEqual(message_engine._default_wake(record), "resume")
        self.assertEqual(message_engine.arrival(record), "resume")
        with window_holding(CONVERSATION, self.app_data):
            self.assertEqual(message_engine.arrival(record), "window")
        self.assertEqual(message_engine._callback_mode("agy", "never"), "manual")


class AgyInstallTests(unittest.TestCase):
    def test_install_puts_the_skill_in_the_global_customization_root(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}, clear=True):
                code = skill_install.install_skill(
                    argparse.Namespace(path=None, target="agy", force=False, keep_existing=False))
            self.assertEqual(code, 0)
            skill = Path(home) / ".gemini" / "config" / "skills" / "harness-reins" / "SKILL.md"
            text = skill.read_text(encoding="utf-8")
            self.assertIn("Antigravity", text)
            self.assertIn("WaitMsBeforeAsync", text)
            frontmatter = yaml.safe_load(text.split("---", 2)[1])
            self.assertEqual(frontmatter["name"], "harness-reins")
            self.assertLessEqual(len(frontmatter["description"]), 1024)
            self.assertTrue((skill.parent / "REFERENCE.md").is_file())


if __name__ == "__main__":
    unittest.main()


# The panel -------------------------------------------------------------------

from test_tui_live import FakeZellij, LiveCase, bare  # noqa: E402

from harness_reins.commands.tui import live  # noqa: E402
from harness_reins.core import sessions  # noqa: E402
from harness_reins.harness.agy import sessions as agy_sessions  # noqa: E402

OTHER = "55556666-7777-8888-9999-000011112222"
SUMMARY_SCHEMA = ("CREATE TABLE conversation_summaries (conversation_id text PRIMARY KEY, title text NOT NULL DEFAULT '', "
                  "workspace_uris text NOT NULL, last_modified_time datetime NOT NULL, nesting_depth integer NOT NULL "
                  "DEFAULT 0, parent_conversation_id text NOT NULL DEFAULT '')")


def add_conversation(app_data: Path, conversation: str, cwd: Path, title: str = "", *, depth: int = 0,
                     modified: str = "2026-11-27 16:20:39.449807923+00:00") -> None:
    import sqlite3

    (app_data / "conversations").mkdir(parents=True, exist_ok=True)
    (app_data / "conversations" / f"{conversation}.db").write_bytes(b"")
    connection = sqlite3.connect(app_data / agy_sessions.SUMMARIES_FILE)
    connection.execute(SUMMARY_SCHEMA.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS"))
    connection.execute("INSERT OR REPLACE INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?)",
                       (conversation, title, json.dumps([cwd.as_uri()]), modified, depth, "" if not depth else "p"))
    connection.commit()
    connection.close()


@POSIX_ONLY
class AgyPanelTests(LiveCase):
    def setUp(self) -> None:
        super().setUp()
        self.app_data = self.home / "agy"
        self.workdir = self.home / "w"
        self.workdir.mkdir()
        self.agy = self.home / "bin" / "agy"
        self.agy.parent.mkdir()
        self.agy.write_text("#!/bin/sh\n")
        self.agy.chmod(0o755)
        patcher = mock.patch.dict(os.environ, {"REINS_HOME": str(self.home / ".reins")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ran: list = []

        def run(argv, **kwargs):
            self.ran.append(list(argv))
            return __import__("subprocess").CompletedProcess(argv, 0, stdout="ok\n", stderr="")
        self.backend = live.LiveBackend(
            queue_root=self.root, zellij=self.zellij, now=lambda: self.now, codex_writers=dict, run=run,
            environment={"PATH": "/nowhere", "HOME": str(self.home), "REINS_AGY_BIN": str(self.agy),
                         "ANTIGRAVITY_APP_DATA_DIR": str(self.app_data)})
        self.backend.sleep = lambda seconds: setattr(self, "now", self.now + seconds)

    def rows(self):
        return {row.session: row for row in self.backend.sessions()}

    def test_top_level_conversations_are_listed_with_title_and_directory(self) -> None:
        add_conversation(self.app_data, CONVERSATION, self.workdir, "Fix the parser")
        add_conversation(self.app_data, OTHER, self.workdir, "a subagent", depth=1)
        rows = self.rows()
        self.assertNotIn(OTHER, rows)
        row = rows[CONVERSATION]
        self.assertEqual((row.agent, row.state, row.title, row.cwd), ("agy", sessions.CLOSED, "Fix the parser",
                                                                      str(self.workdir)))
        self.assertIn("agy", self.backend.new_agents())

    def test_a_closed_conversation_opens_in_a_pane_and_enter_then_finds_that_pane(self) -> None:
        add_conversation(self.app_data, CONVERSATION, self.workdir, "Fix the parser")
        self.rows()
        result = self.backend.open_session(CONVERSATION)
        self.assertTrue(result.ok, result.message)
        [(argv, name, _where, _close)] = self.zellij.opened
        self.assertEqual(bare(argv), [str(self.agy), "--resume", CONVERSATION])
        self.assertEqual(name, "agy 2b226add Fix the parser")
        self.zellij.pane_list = [{"id": 9, "is_plugin": False, "title": name}]
        with window_holding(CONVERSATION, self.app_data):  # The pane's agy holds the conversation open.
            self.assertEqual(self.rows()[CONVERSATION].state, sessions.ATTACHED)
            self.backend.open_session(CONVERSATION)
        self.assertEqual(self.zellij.focused, ["terminal_9"])
        self.assertEqual(len(self.zellij.opened), 1)

    def test_open_elsewhere_or_resumed_headless_is_never_opened_beside(self) -> None:
        add_conversation(self.app_data, CONVERSATION, self.workdir, "Fix the parser")
        with window_holding(CONVERSATION, self.app_data):
            self.assertEqual(self.rows()[CONVERSATION].state, sessions.FOREIGN)
            self.assertFalse(self.backend.open_session(CONVERSATION).ok)
            request = {"agent": "agy", "target": {"kind": "session", "session": CONVERSATION}}
            lock = delivery_engine.acquire_target_lock(request, blocking=False)
            self.addCleanup(__import__("harness_reins.store.records", fromlist=["x"]).release_owner_lock, lock,
                            remove=False)
            self.assertEqual(self.rows()[CONVERSATION].state, sessions.HEADLESS)
            result = self.backend.open_session(CONVERSATION)
        self.assertFalse(result.ok)
        self.assertIn("headless", result.message)
        self.assertEqual(self.zellij.opened, [])

    def test_a_conversation_agy_no_longer_has_is_not_opened(self) -> None:
        add_conversation(self.app_data, CONVERSATION, self.workdir, "Fix the parser")
        self.rows()
        (self.app_data / "conversations" / f"{CONVERSATION}.db").unlink()
        self.assertFalse(self.backend.open_session(CONVERSATION).ok)
        self.assertEqual(self.zellij.opened, [])

    def new_pane_creates(self, conversation: str):
        """What agy does as its pane starts: create the conversation and hold it open."""
        opened = self.zellij.new_pane
        holding = contextlib.ExitStack()
        self.addCleanup(holding.close)

        def new_pane(argv, **kwargs):
            add_conversation(self.app_data, conversation, self.workdir, modified="0001-01-01 00:00:00+00:00")
            holding.enter_context(window_holding(conversation, self.app_data))
            return opened(argv, **kwargs)
        self.zellij.new_pane = new_pane

    def test_a_new_window_takes_its_conversations_name_once_agy_creates_it(self) -> None:
        add_conversation(self.app_data, OTHER, self.workdir, "older")
        result = self.backend.new_session("agy", str(self.workdir), "ignored")
        self.assertTrue(result.ok, result.message)
        [(argv, name, _where, _close)] = self.zellij.opened
        self.assertEqual((bare(argv), name), ([str(self.agy)], "agy new w"))
        self.assertFalse(any("register" in argv for argv in self.ran))  # Named from its menu later, as for Codex.
        self.zellij.renamed.clear()  # The name it was opened with.
        self.zellij.pane_list = [{"id": 9, "is_plugin": False, "title": name}]
        self.rows()
        self.assertEqual(self.zellij.renamed, [])  # No conversation yet: agy creates it on the first prompt.
        # The first prompt: agy creates the conversation and its process holds it open.
        add_conversation(self.app_data, CONVERSATION, self.workdir, "Fix the parser")
        with window_holding(CONVERSATION, self.app_data):
            self.assertEqual(self.rows()[CONVERSATION].state, sessions.ATTACHED)  # On this same pass.
        self.assertEqual(self.zellij.renamed[0], ("terminal_9", "agy 2b226add"))

    def test_two_new_conversations_at_once_are_not_guessed_between(self) -> None:
        self.backend.new_session("agy", str(self.workdir))
        self.zellij.renamed.clear()
        self.zellij.pane_list = [{"id": 9, "is_plugin": False, "title": "agy new w"}]
        with contextlib.ExitStack() as holding:
            for conversation in (CONVERSATION, OTHER):
                add_conversation(self.app_data, conversation, self.workdir)
                holding.enter_context(window_holding(conversation, self.app_data))
            rows = self.rows()
        self.assertEqual(rows[CONVERSATION].state, sessions.FOREIGN)
        self.assertEqual(self.zellij.renamed, [])

    def test_a_collaborator_in_agy_starts_on_its_brief(self) -> None:
        add_conversation(self.app_data, OTHER, self.workdir, "plots")
        self.rows()
        self.new_pane_creates(CONVERSATION)
        result = self.backend.start_collaborator(OTHER, "agy", str(self.workdir), "review it")
        self.assertTrue(result.ok, result.message)
        [(argv, _name, _where, _close)] = self.zellij.opened
        self.assertEqual(bare(argv)[:2], [str(self.agy), "--prompt-interactive"])
        self.assertIn("Your first task: review it", bare(argv)[2])
        register = [argv[argv.index("register") + 1:] for argv in self.ran if "register" in argv]
        self.assertIn(CONVERSATION, register[-1])


class AgyDeliveryGuardTests(unittest.TestCase):
    def test_a_conversation_agy_does_not_have_is_never_resumed(self):
        # agy would start a new conversation for an unknown id and run the callback there.
        with tempfile.TemporaryDirectory() as tmp:
            payload = {"command": ["agy", "--resume", CONVERSATION], "prompt": "[reins] x-c1", "cwd": tmp,
                       "queue_dir": tmp, "timeout": 5.0, "ack_path": str(Path(tmp) / "ack.json"),
                       "canceled_path": str(Path(tmp) / "canceled.json"),
                       "environment": {"ANTIGRAVITY_APP_DATA_DIR": str(Path(tmp) / "agy"), "HOME": tmp},
                       "request": {"id": "x-c1", "agent": "agy", "callback_mode": "cli",
                                   "target": {"kind": "session", "session": CONVERSATION}}}
            output = io.StringIO()
            read_fd, lock_fd = os.pipe()
            self.addCleanup(os.close, read_fd)
            with mock.patch.dict(os.environ, {"REINS_HOME": str(Path(tmp) / ".reins"),
                                              "CODEX_LONG_TASK_DELIVERY_LOCK_FDS": json.dumps([lock_fd])}), \
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
                    mock.patch.object(sys, "stdout", output), contextlib.redirect_stderr(io.StringIO()), \
                    mock.patch.object(delivery_engine.subprocess, "Popen") as popen:
                delivery_engine.delivery_worker_main()
            popen.assert_not_called()
            result = json.loads(output.getvalue())
            self.assertEqual((result["returncode"], result["delivery_state"]), (126, "blocked"))


class CredentialTests(unittest.TestCase):
    """Antigravity's CSRF token never reaches a task, its stored environment or a resumed process."""

    def test_secrets_are_dropped_and_the_rest_kept(self):
        from harness_reins.harness import AGENTS
        kept = AGENTS.without_secrets({"ANTIGRAVITY_CSRF_TOKEN": "s", "ANTIGRAVITY_SIDECAR_UI_TOKEN": "s",
                                       "ANTIGRAVITY_CONVERSATION_ID": "c", "PATH": "/bin"})
        self.assertEqual(kept, {"ANTIGRAVITY_CONVERSATION_ID": "c", "PATH": "/bin"})

    def test_a_submitted_task_does_not_store_the_token(self):
        """The real `reins run` path, as in test_stop: no daemon or systemd is started."""
        from harness_reins.commands import work as work_commands
        from harness_reins.engine import tasks as managed_tasks
        from harness_reins.owners.linux import SystemdUserBackend
        from harness_reins.store import layout
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "queue"
            args = argparse.Namespace(
                backend="systemd", agent="agy", cwd=temporary, task="job", command=None, exit_code=None, message=None,
                session="5b1f0c2e-8d7a-4c3b-9e6f-1a2b3c4d5e6f", last=False, via_daemon=False, queue_dir=str(root),
                approvals_reviewer="auto_review", approval_policy="on-request", sandbox_mode="workspace-write",
                dry_run=False, strict=False, wrapped_command=[sys.executable, "-c", "pass"])
            with mock.patch.object(sys, "platform", "linux"), \
                    mock.patch.object(SystemdUserBackend, "available", return_value=True), \
                    mock.patch.object(SystemdUserBackend, "launch"), \
                    mock.patch.object(managed_tasks, "screen_binary", return_value=None), \
                    mock.patch.dict(os.environ, {"ANTIGRAVITY_CSRF_TOKEN": "token-must-not-leak", "KEEP_ME": "1"}), \
                    contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                work_commands.run(args)
            stored = next(layout.managed_tasks_root(root).glob("*/environment.json")).read_text(encoding="utf-8")
            self.assertNotIn("token-must-not-leak", stored)
            self.assertIn("KEEP_ME", stored)


class HeadlessRuleTests(unittest.TestCase):
    def test_no_rule_reaches_another_inbox_or_clears_alerts(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = json.loads(agy_session.resume_settings(["reins"], {"ANTIGRAVITY_APP_DATA_DIR": temporary}))
        allow = settings["permissions"]["allow"]
        self.assertIn("command(reins reply)", allow)
        self.assertIn("command(reins message show)", allow)
        self.assertFalse(any("inbox" in rule or rule == "command(reins message)" for rule in allow))
        self.assertFalse(any(rule.startswith(f"command(reins {word}") for rule in allow for word in ("run", "agent", "done")))
