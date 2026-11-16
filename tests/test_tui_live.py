from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from harness_reins.commands.tui import launch, live
from harness_reins.core import sessions
from harness_reins.core.sessions import SessionView
from harness_reins.engine import claude_background

SESSION = "08e8651e-ea49-48d0-b286-088a680b9c82"
OTHER = "4b585398-c6bf-4d5c-8b80-addaeb80e7f3"
CLAUDE = "/opt/claude/bin/claude"


class FakeZellij:
    def __init__(self, panes=()) -> None:
        self.pane_list = list(panes)
        self.opened: list[tuple] = []
        self.focused: list[str] = []

    def panes(self):
        return list(self.pane_list)

    def new_pane(self, argv, *, name, direction="right", close_on_exit=True, cwd=None, replace=None):
        self.opened.append((list(argv), name, f"in place of {replace}" if replace else direction, close_on_exit))
        return "terminal_9"

    def focus(self, pane):
        self.focused.append(pane)


def write(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


class LiveCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.root = self.home / "queue"
        for state in ("pending", "running", "done", "failed", "canceled", "acks", "goals"):
            (self.root / state).mkdir(parents=True)
        self.now = 2_000_000_000.0
        self.zellij = FakeZellij()
        self.views: list[SessionView] = []
        patcher = mock.patch.object(live.claude_agents, "session_views", side_effect=lambda *a, **k: list(self.views))
        patcher.start()
        self.addCleanup(patcher.stop)
        found = mock.patch.object(claude_background, "_executable", return_value=CLAUDE)
        found.start()
        self.addCleanup(found.stop)
        names = mock.patch.object(live.skill_install, "agent_command_names", return_value=["reins"])
        names.start()
        self.addCleanup(names.stop)
        self.backend = live.LiveBackend(queue_root=self.root, environment={"PATH": "/bin"}, zellij=self.zellij,
                                        now=lambda: self.now)

    def task(self, task_id: str, session: str, state: str = "running", **fields) -> None:
        write(self.root.parent / "tasks" / task_id / "task.json",
              {"id": task_id, "task": f"job {task_id}", "state": state, "agent": "claude", "cwd": "/w",
               "submitted_at": self.now - 60, "target": {"kind": "session", "session": session}, **fields})


class ReadTests(LiveCase):
    def test_sessions_count_tasks_callbacks_and_goals(self) -> None:
        self.views = [SessionView(agent="claude", session=SESSION, state=sessions.RUNNING, handle=SESSION[:8],
                                  name="motion plots", pid=5, activity=sessions.WORKING)]
        self.zellij.pane_list = [{"id": 3, "is_plugin": False, "terminal_command": f"{CLAUDE} attach {SESSION[:8]}"}]
        self.task("t1", SESSION)
        self.task("t2", SESSION, state="completed")
        self.task("t3", OTHER, state="completed")
        write(self.root / "pending" / "t2-c1.json", {"target": {"kind": "session", "session": SESSION}})
        write(self.root / "goals" / "g1.json", {"id": "g1", "task": "ship it", "state": "active",
                                                "target": {"session": SESSION}, "plan_completed_items": 1,
                                                "plan_total_items": 3})
        rows = {row.session: row for row in self.backend.sessions()}
        live_row = rows[SESSION]
        self.assertEqual((live_row.state, live_row.title, live_row.tasks_running, live_row.callbacks_pending, live_row.goal),
                         (sessions.ATTACHED, "motion plots", 1, 1, "ship it"))
        # A closed session Reins worked with lately is listed, so the user can find it again.
        self.assertEqual((rows[OTHER].state, rows[OTHER].cwd), (sessions.CLOSED, "/w"))

    def test_old_tasks_are_left_out(self) -> None:
        self.task("old", OTHER, submitted_at=self.now - live.RECENT_SECONDS - 10)
        self.assertEqual(self.backend.tasks(), [])
        self.assertEqual(self.backend.sessions(), [])

    def test_tasks_carry_their_callback_state(self) -> None:
        self.task("t1", SESSION, state="completed")
        write(self.root / "done" / "t1-c1.json", {})
        write(self.root / "acks" / "t1-c1.json", {})
        self.task("t2", SESSION)
        [second, first] = self.backend.tasks() if self.backend.tasks()[0].task == "t2" else reversed(self.backend.tasks())
        self.assertEqual((first.callback, first.callback_state), ("t1-c1", "acked"))
        self.assertIsNone(second.callback)

    def test_goals_and_messages(self) -> None:
        write(self.root / "goals" / "g1.json", {"id": "g1", "task": "t", "state": "blocked_conditions",
                                                "target": {"session": SESSION}})
        write(self.root.parent / "link" / "messages" / "m-1.json",
              {"id": "m-1", "from": {"name": "desk"}, "to": {"kind": "session_name", "name": "lab"},
               "subject": "hello", "created_at": "2026-11-05T20:03:07.323Z", "local": {}})
        [goal] = self.backend.goals()
        self.assertEqual((goal.goal, goal.state, goal.progress), ("g1", "blocked_conditions", None))
        [message] = self.backend.messages()
        self.assertEqual((message.sender, message.recipient, message.subject, message.read), ("desk", "lab", "hello", False))
        self.assertGreater(message.sent_at, 0)


class ActionTests(LiveCase):
    def test_open_jumps_to_an_open_pane(self) -> None:
        self.views = [SessionView(agent="claude", session=SESSION, state=sessions.RUNNING, handle=SESSION[:8], pid=5)]
        self.zellij.pane_list = [{"id": 3, "is_plugin": False, "terminal_command": f"{CLAUDE} attach {SESSION[:8]}"}]
        result = self.backend.open_session(SESSION)
        self.assertTrue(result.ok)
        self.assertEqual((self.zellij.focused, self.zellij.opened), (["terminal_3"], []))

    def test_open_attaches_a_running_or_stopped_session(self) -> None:
        for state in (sessions.RUNNING, sessions.STOPPED):
            self.zellij.opened.clear()
            self.views = [SessionView(agent="claude", session=SESSION, state=state, handle=SESSION[:8], name="plots")]
            self.assertTrue(self.backend.open_session(SESSION).ok)
            [(argv, name, direction, close)] = self.zellij.opened
            self.assertEqual((argv, name, close), ([CLAUDE, "attach", SESSION[:8]], f"claude {SESSION[:8]} plots", True))

    def test_open_resumes_a_closed_session_first(self) -> None:
        closed = SessionView(agent="claude", session=SESSION, state=sessions.CLOSED, cwd="/w")
        with mock.patch.object(live.claude_agents, "session_view", return_value=closed), \
                mock.patch.object(claude_background, "resume", return_value=SESSION[:8]) as resume:
            self.assertTrue(self.backend.open_session(SESSION).ok)
        self.assertEqual(resume.call_args.args[:2], (SESSION, "/w"))
        self.assertFalse(resume.call_args.kwargs["stopped"])
        self.assertIn("reins ack", resume.call_args.kwargs["settings"])
        self.assertEqual(self.zellij.opened[0][0], [CLAUDE, "attach", SESSION[:8]])

    def test_open_refuses_a_window_or_headless_turn(self) -> None:
        self.views = [SessionView(agent="claude", session=SESSION, state=sessions.FOREIGN, host=sessions.HOST_DESKTOP, pid=7),
                      SessionView(agent="claude", session=OTHER, state=sessions.HEADLESS, pid=8)]
        desktop = self.backend.open_session(SESSION)
        self.assertFalse(desktop.ok)
        self.assertIn("desktop app (pid 7)", desktop.message)
        self.assertIn("headless", self.backend.open_session(OTHER).message)
        self.assertEqual(self.zellij.opened, [])

    def test_open_reports_a_copy_and_an_unknown_session(self) -> None:
        closed = SessionView(agent="claude", session=SESSION, state=sessions.CLOSED, cwd="/w")
        with mock.patch.object(live.claude_agents, "session_view", return_value=closed), \
                mock.patch.object(claude_background, "resume", side_effect=claude_background.StartedCopy("x")):
            self.assertIn("opened somewhere else", self.backend.open_session(SESSION).message)
        with mock.patch.object(live.claude_agents, "session_view", return_value=None):
            self.assertIn("no Claude session", self.backend.open_session(SESSION).message)

    def test_new_session_starts_in_the_background_and_attaches(self) -> None:
        with mock.patch.object(claude_background, "start", return_value="abcdef12") as start:
            self.assertTrue(self.backend.new_session("claude", str(self.home)).ok)
        self.assertEqual(start.call_args.args[0], str(self.home))
        self.assertEqual(self.zellij.opened[0][0], [CLAUDE, "attach", "abcdef12"])
        self.assertFalse(self.backend.new_session("claude", str(self.home / "missing")).ok)
        self.assertFalse(self.backend.new_session("codex", str(self.home)).ok)

    def test_stop_only_a_running_background_session(self) -> None:
        self.views = [SessionView(agent="claude", session=SESSION, state=sessions.RUNNING, handle=SESSION[:8], pid=5),
                      SessionView(agent="claude", session=OTHER, state=sessions.FOREIGN, pid=6)]
        with mock.patch.object(claude_background, "stop") as stop:
            self.assertTrue(self.backend.stop_session(SESSION).ok)
            self.assertFalse(self.backend.stop_session(OTHER).ok)
        stop.assert_called_once()
        self.assertEqual(stop.call_args.args[0], SESSION[:8])

    def test_terminal_opens_a_shell_in_the_directory(self) -> None:
        self.backend.environment["SHELL"] = "/bin/zsh"
        self.assertTrue(self.backend.open_terminal(str(self.home), "right").ok)
        argv, name, direction, close = self.zellij.opened[-1]
        self.assertEqual((argv, name, direction, close), (["/bin/zsh"], f"shell {self.home.name}", "right", True))
        self.assertTrue(self.backend.open_terminal("/no/such/dir").ok)  # Falls back to the home directory.

    def test_naming_registers_the_session_and_its_address_shows(self) -> None:
        self.views = [SessionView(agent="claude", session=SESSION, state=sessions.RUNNING, handle=SESSION[:8], cwd="/w")]
        ran = []

        def run(argv, **kwargs):
            ran.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="named lab\n", stderr="")

        self.backend.run = run
        self.assertTrue(self.backend.name_session(SESSION, "lab").ok)
        # A queue other than the default one is named, so the command works on the panel's queue.
        self.assertEqual(ran[-1][-11:], ["session", "register", "lab", "--agent", "claude", "--session", SESSION,
                                         "--cwd", "/w", "--queue-dir", str(self.root)])
        with mock.patch.object(live.link, "sessions", return_value=[{"name": "lab", "session": SESSION}]):
            [row] = self.backend.sessions()
        self.assertEqual(row.address, "lab")

    def test_log_send_and_cancel(self) -> None:
        self.assertTrue(self.backend.show_log("t1").ok)
        argv, name, direction, close = self.zellij.opened[-1]
        self.assertEqual((argv[-2:], name, direction, close), (["log", "t1"], "log t1", "right", False))
        ran = []

        def run(argv, **kwargs):
            ran.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="reins: sent m-1\n", stderr="")

        self.backend.run = run
        self.assertIn("no Reins name", self.backend.send_message(SESSION, "hi").message)
        with mock.patch.object(live.link, "sessions", return_value=[{"name": "lab", "session": SESSION}]):
            result = self.backend.send_message(SESSION, "hi")
        self.assertEqual((result.ok, result.message), (True, "reins: sent m-1"))
        self.assertEqual(ran[-1][-6:], ["send", "lab", "--message", "hi", "--queue-dir", str(self.root)])
        self.assertTrue(self.backend.cancel_callback("t1-c1").ok)
        self.assertEqual(ran[-1][-5:], ["cancel", "--id", "t1-c1", "--queue-dir", str(self.root)])


class PlacementTests(unittest.TestCase):
    def test_the_first_pane_replaces_the_hint_and_later_ones_stack_below(self) -> None:
        zellij = FakeZellij([{"id": 0, "title": "Reins"}, {"id": 1, "title": live.HINT_TITLE}])
        live.place(zellij, ["claude", "attach", "aaaaaaaa"], name="claude aaaaaaaa", close_on_exit=True)
        self.assertEqual(zellij.opened[-1][2], "in place of terminal_1")
        zellij.pane_list = [{"id": 0, "title": "Reins"}, {"id": 2, "title": "claude aaaaaaaa x"},
                            {"id": 3, "title": "log t1"}, {"id": 4, "title": "claude bbbbbbbb", "exited": True}]
        live.place(zellij, ["claude", "attach", "cccccccc"], name="claude cccccccc", close_on_exit=True)
        # The newest open session or log pane is split downwards; the panel is never focused for it.
        self.assertEqual((zellij.focused, zellij.opened[-1][2]), (["terminal_3"], "down"))

    def test_beside_splits_the_newest_pane_to_the_right_and_shells_count(self) -> None:
        zellij = FakeZellij([{"id": 0, "title": "Reins"}, {"id": 2, "title": "claude aaaaaaaa"},
                             {"id": 6, "title": "shell project"}])
        live.place(zellij, ["claude", "attach", "bbbbbbbb"], name="claude bbbbbbbb", close_on_exit=True,
                   direction="right")
        self.assertEqual((zellij.focused, zellij.opened[-1][2]), (["terminal_6"], "right"))

    def test_a_pane_that_renamed_itself_still_counts(self) -> None:
        zellij = FakeZellij([{"id": 0, "title": "Reins"}, {"id": 5, "title": "user@host: ~/project"}])
        live.place(zellij, ["reins", "log", "t1"], name="log t1", close_on_exit=False)
        self.assertEqual((zellij.focused, zellij.opened[-1][2]), (["terminal_5"], "down"))

    def test_panes_in_another_tab_or_floating_are_not_split(self) -> None:
        zellij = FakeZellij([{"id": 0, "title": "Reins", "tab_id": 0}, {"id": 2, "title": "claude aaaaaaaa", "tab_id": 0},
                             {"id": 7, "title": "elsewhere", "tab_id": 1}, {"id": 8, "title": "float", "tab_id": 0,
                                                                           "is_floating": True}])
        live.place(zellij, ["reins", "log", "t1"], name="log t1", close_on_exit=False)
        self.assertEqual(zellij.focused, ["terminal_2"])

    def test_with_nothing_on_the_right_it_opens_to_the_right(self) -> None:
        zellij = FakeZellij([{"id": 0, "title": "Reins"}])
        live.place(zellij, ["reins", "log", "t1"], name="log t1", close_on_exit=False)
        self.assertEqual((zellij.focused, zellij.opened[-1][2]), ([], "right"))


class HealthTests(unittest.TestCase):
    def test_doctor_issues_in_plain_words_without_agent_only_ones(self) -> None:
        report = {"issues": [{"code": "callback_delivery_failed", "count": 4}, {"code": "session_unbound"},
                             {"code": "daemon_not_running"}, {"code": "something_new"}]}
        self.assertEqual(live.health_lines(report), [
            "4 callbacks failed to deliver (Tasks tab; c cancels one)",
            "the Reins daemon is not running: reins setup --enable --now",
            "something new: reins doctor"])

    def test_the_backend_adds_a_missing_hook_and_survives_a_failing_check(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        backend = live.LiveBackend(queue_root=Path(tmp.name) / "queue", environment={})
        with mock.patch("harness_reins.install.diagnostics.inspect", side_effect=RuntimeError("boom")), \
                mock.patch.object(live.claude_session, "hook_installed", return_value=False):
            lines = backend.health()
        self.assertIn("the check itself failed (RuntimeError): reins doctor", lines)
        self.assertTrue(any("hook is not installed" in line for line in lines))
        with mock.patch("harness_reins.install.diagnostics.inspect", return_value={"status": "ready", "issues": []}), \
                mock.patch.object(live.claude_session, "hook_installed", return_value=True):
            self.assertEqual(backend.health(), [])


class AttachedHandleTests(unittest.TestCase):
    def test_only_live_claude_attach_panes_count(self) -> None:
        panes = [
            {"id": 1, "is_plugin": False, "terminal_command": "/x/claude attach 08e8651e"},
            {"id": 2, "is_plugin": False, "terminal_command": "/x/claude attach deadbeef", "exited": True},
            {"id": 3, "is_plugin": True, "terminal_command": "claude attach cafebabe"},
            {"id": 4, "is_plugin": False, "terminal_command": "reins log t1"},
            {"id": 5, "is_plugin": False, "terminal_command": "vim attach notes"},
            # Zellij stops reporting the command; the name Reins gave the pane still says.
            {"id": 6, "is_plugin": False, "title": live.pane_name("abcdef12", "motion plots"), "terminal_command": None},
        ]
        self.assertEqual(live.attached_handles(panes), {"08e8651e": "terminal_1", "abcdef12": "terminal_6"})


class LaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "queue"

    def test_layout_runs_the_panel_and_quotes_its_arguments(self) -> None:
        directory = self.root.parent / "zellij"
        launch.write_config(directory, ["/opt/reins dir/reins", "_tui-panel", "--queue-dir", 'a "q"'])
        layout_text = (directory / "layouts" / "reins.kdl").read_text()
        self.assertIn('command="/opt/reins dir/reins"', layout_text)
        self.assertIn('args "_tui-panel" "--queue-dir" "a \\"q\\""', layout_text)
        self.assertIn(f'pane name="Reins" size="{launch.PANEL_WIDTH}"', layout_text)
        self.assertIn(f'pane name="{live.HINT_TITLE}"', layout_text)
        config = (directory / "config.kdl").read_text()
        self.assertIn('default_layout "reins"', config)
        # Zellij's modal Ctrl keys are gone; a few Alt keys act at once.
        self.assertIn("keybinds clear-defaults=true", config)
        for key in ("Alt x", "Alt d", "Alt q", "Alt Left"):
            self.assertIn(f'"{key}"', config)
        self.assertIn("session_serialization false", config)

    def test_non_ascii_paths_stay_literal_in_kdl(self) -> None:
        # KDL has no \\uXXXX escape; Zellij rejected the whole layout before this was fixed.
        directory = self.root.parent / "zellij"
        launch.write_config(directory, ["/home/李雷/reins", "_tui-panel"])
        self.assertIn('command="/home/李雷/reins"', (directory / "layouts" / "reins.kdl").read_text(encoding="utf-8"))

    def test_a_changed_layout_is_reported(self) -> None:
        directory = self.root.parent / "zellij"
        self.assertFalse(launch.write_config(directory, ["/a/reins", "_tui-panel"]))  # First write.
        self.assertFalse(launch.write_config(directory, ["/a/reins", "_tui-panel"]))
        self.assertTrue(launch.write_config(directory, ["/b/reins", "_tui-panel"]))

    def test_people_only(self) -> None:
        self.assertEqual(launch.inside_agent({"CLAUDECODE": "1"}), "claude")
        self.assertIsNone(launch.inside_agent({"PATH": "/bin"}))

    def test_resolve_by_session_task_callback_or_prefix(self) -> None:
        write(self.root.parent / "tasks" / "cf408bf5" / "task.json", {"id": "cf408bf5", "target": {"session": SESSION}})
        write(self.root / "done" / "aa11bb22-c1.json", {"target": {"session": OTHER}})
        with mock.patch.object(launch.claude_agents, "session_views", return_value=[]):
            self.assertEqual(launch.resolve(SESSION, self.root, {}), SESSION)
            self.assertEqual(launch.resolve("cf408bf5", self.root, {}), SESSION)
            self.assertEqual(launch.resolve("aa11bb22-c1", self.root, {}), OTHER)
            self.assertEqual(launch.resolve("cf40", self.root, {}), SESSION)
            with self.assertRaises(ValueError):
                launch.resolve("zz", self.root, {})
        view = SessionView(agent="claude", session=OTHER, state=sessions.RUNNING, handle=OTHER[:8])
        with mock.patch.object(launch.claude_agents, "session_views", return_value=[view]):
            self.assertEqual(launch.resolve(OTHER[:8], self.root, {}), OTHER)

    def test_zellij_that_does_not_answer_is_a_failure_not_a_crash(self) -> None:
        late = subprocess.TimeoutExpired(["zellij"], 10.0)
        with mock.patch.object(launch.subprocess, "run", side_effect=late):
            result = launch._zellij("zellij", self.root, "action", "list-panes")
            self.assertEqual(result.returncode, 124)
            self.assertIsNone(launch._panes("zellij", self.root))
            self.assertIsNone(launch.session_state("zellij", self.root))
        zellij = live.Zellij("zellij", run=mock.Mock(side_effect=late))
        self.assertEqual(zellij.panes(), [])
        with self.assertRaisesRegex(RuntimeError, "did not answer"):
            zellij.focus("terminal_1")

    def test_wait_ready_keeps_waiting_through_a_slow_first_start(self) -> None:
        panel = [{"id": 0, "title": "Reins", "is_plugin": False}]
        answers = [None, None, None, panel, panel, panel]
        clock = iter(float(second) for second in range(100))
        with mock.patch.object(launch, "_panes", side_effect=lambda *_: answers.pop(0) if answers else panel), \
                mock.patch.object(launch.time, "monotonic", side_effect=lambda: next(clock)), \
                mock.patch.object(launch.time, "sleep"), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(launch._wait_ready("zellij", self.root), launch.READY)
        self.assertIn("first start can take a minute", stderr.getvalue())

    def test_wait_ready_tells_silence_from_a_missing_panel(self) -> None:
        for answer, expected in ((None, launch.SILENT), ([{"id": 1, "title": "other"}], launch.NO_PANEL)):
            clock = iter(float(second) for second in range(0, 1000, 5))
            with mock.patch.object(launch, "_panes", return_value=answer), \
                    mock.patch.object(launch.time, "monotonic", side_effect=lambda: next(clock)), \
                    mock.patch.object(launch.time, "sleep"), mock.patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(launch._wait_ready("zellij", self.root), expected)

    @unittest.skipUnless(sys.platform.startswith("linux"), "an unreaped child counts as gone only through /proc")
    def test_a_silent_zellij_server_is_ended_by_process_id(self) -> None:
        sleeper = subprocess.Popen(["sleep", "60"])
        self.addCleanup(sleeper.kill)
        self.assertEqual(launch._end({sleeper.pid}, grace=1.0), set())
        self.assertIsNotNone(sleeper.poll())

    def test_only_the_server_this_launch_started_is_found(self) -> None:
        proc = Path(self.root) / "proc"
        marker = f"{launch.LAUNCH_ENV}=1-2".encode()
        for pid, argv, environ in ((10, b"/opt/z/zellij\0--server\0/tmp/zellij-0/c/ab\0", b"A=1\0" + marker + b"\0"),
                                   (11, b"/opt/z/zellij\0--server\0/tmp/zellij-0/c/cd\0", b"A=1\0"),
                                   (12, b"/opt/z/zellij\0action\0list-panes\0", marker + b"\0"),
                                   (13, b"", b"")):
            (proc / str(pid)).mkdir(parents=True)
            (proc / str(pid) / "cmdline").write_bytes(argv)
            (proc / str(pid) / "environ").write_bytes(environ)
        (proc / "self").mkdir()
        self.assertEqual(launch.zellij_servers("1-2", proc), {10})
        self.assertEqual(launch.zellij_servers("9-9", proc), set())

    def test_zellij_lookup_order(self) -> None:
        tool = self.root.parent / "zellij-bin"
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
        self.assertEqual(launch.zellij_executable({launch.ZELLIJ_ENV: str(tool)}), str(tool))
        self.assertIsNone(launch.zellij_executable({launch.ZELLIJ_ENV: str(tool) + "-missing"}))
        self.assertIsNone(launch.zellij_executable({"PATH": str(self.root)}))


if __name__ == "__main__":
    unittest.main()
