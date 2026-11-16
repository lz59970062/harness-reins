from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from harness_reins.core import sessions
from harness_reins.core.sessions import SessionView
from harness_reins.harness.claude import agents
from harness_reins.harness.claude.adapter import CLAUDE_BIN_ENV

S1 = "11111111-aaaa-4aaa-8aaa-111111111111"
S2 = "22222222-bbbb-4bbb-8bbb-222222222222"
S3 = "33333333-cccc-4ccc-8ccc-333333333333"


def background(session: str, *, pid: int | None = 4242, state: str | None = "working", started: int = 1_000) -> dict:
    entry = {"kind": "background", "id": session[:8], "sessionId": session, "name": "bg work",
             "cwd": "/work", "startedAt": started, "pid": pid, "status": "busy" if pid else None, "state": state}
    return entry


def window(session: str, *, pid: int = 5151, status: str = "idle", started: int = 2_000) -> dict:
    return {"kind": "interactive", "sessionId": session, "name": "a window", "cwd": "/work",
            "startedAt": started, "pid": pid, "status": status}


class SessionViewTests(unittest.TestCase):
    def test_rejects_unknown_state_and_activity(self) -> None:
        with self.assertRaises(ValueError):
            SessionView(agent="claude", session=S1, state="open")
        with self.assertRaises(ValueError):
            SessionView(agent="claude", session=S1, state=sessions.RUNNING, activity="busy")

    def test_only_reins_side_sessions_may_be_taken_over(self) -> None:
        hosted = {state for state in sessions.STATES
                  if SessionView(agent="claude", session=S1, state=state).reins_hosted}
        self.assertEqual(hosted, {sessions.ATTACHED, sessions.RUNNING, sessions.STOPPED})
        with_process = {state for state in sessions.STATES
                        if SessionView(agent="claude", session=S1, state=state).has_process}
        self.assertEqual(with_process, {sessions.ATTACHED, sessions.RUNNING, sessions.FOREIGN, sessions.HEADLESS})


class EntryViewTests(unittest.TestCase):
    def test_running_background_session(self) -> None:
        view = agents.entry_view(background(S1))
        self.assertEqual((view.state, view.host, view.activity, view.handle, view.pid),
                         (sessions.RUNNING, sessions.HOST_CLAUDE_BACKGROUND, sessions.WORKING, S1[:8], 4242))
        self.assertEqual(view.started_at, 1.0)
        self.assertEqual((view.name, view.cwd), ("bg work", "/work"))

    def test_attached_only_from_reins_panes(self) -> None:
        self.assertEqual(agents.entry_view(background(S1), attached={S1}).state, sessions.ATTACHED)
        self.assertEqual(agents.entry_view(background(S1), attached={S2}).state, sessions.RUNNING)

    def test_exited_background_session_is_stopped(self) -> None:
        view = agents.entry_view(background(S1, pid=None, state="done"))
        self.assertEqual((view.state, view.activity, view.handle), (sessions.STOPPED, None, S1[:8]))
        # A stopped session is never attached, even if a stale pane still names it.
        self.assertEqual(agents.entry_view(background(S1, pid=None), attached={S1}).state, sessions.STOPPED)

    def test_background_activity(self) -> None:
        for raw, activity in (("working", sessions.WORKING), ("done", sessions.IDLE),
                              ("blocked", sessions.BLOCKED), ("later", None), (None, None)):
            with self.subTest(raw=raw):
                self.assertEqual(agents.entry_view(background(S1, state=raw)).activity, activity)

    def test_window_is_foreign(self) -> None:
        desktop = agents.entry_view(window(S2), record={"entrypoint": "claude-desktop"})
        self.assertEqual((desktop.state, desktop.host, desktop.activity, desktop.handle),
                         (sessions.FOREIGN, sessions.HOST_DESKTOP, sessions.IDLE, None))
        terminal = agents.entry_view(window(S2, status="busy"), record={"entrypoint": "cli"})
        self.assertEqual((terminal.host, terminal.activity), (sessions.HOST_TERMINAL, sessions.WORKING))
        self.assertEqual(agents.entry_view(window(S2), attached={S2}).state, sessions.FOREIGN)

    def test_unknown_kind_is_skipped_unless_it_has_a_process(self) -> None:
        self.assertIsNone(agents.entry_view({"kind": "cloud", "sessionId": S1}))
        # A process Reins cannot classify is left alone rather than reported closed.
        self.assertEqual(agents.entry_view({"kind": "cloud", "sessionId": S1, "pid": 77}).state, sessions.FOREIGN)

    def test_missing_short_id_falls_back_to_the_session_prefix(self) -> None:
        entry = background(S1)
        del entry["id"]
        self.assertEqual(agents.entry_view(entry).handle, S1[:8])


class RecordViewTests(unittest.TestCase):
    def test_kinds(self) -> None:
        headless = agents.record_view({"sessionId": S1, "pid": 7, "entrypoint": "sdk-cli", "kind": "interactive"})
        self.assertEqual((headless.state, headless.host), (sessions.HEADLESS, sessions.HOST_HEADLESS))
        bg = agents.record_view({"sessionId": S1, "pid": 7, "entrypoint": "cli", "kind": "bg"}, attached={S1})
        self.assertEqual((bg.state, bg.handle), (sessions.ATTACHED, S1[:8]))
        desktop = agents.record_view({"sessionId": S1, "pid": 7, "entrypoint": "claude-desktop",
                                      "kind": "interactive", "status": "busy"})
        self.assertEqual((desktop.state, desktop.host, desktop.activity),
                         (sessions.FOREIGN, sessions.HOST_DESKTOP, sessions.WORKING))

    def test_background_record_activity_from_state_or_status(self) -> None:
        for fields, activity in (({"state": "blocked"}, sessions.BLOCKED), ({"status": "busy"}, sessions.WORKING),
                                 ({}, None)):
            with self.subTest(fields=fields):
                view = agents.record_view({"sessionId": S1, "pid": 7, "kind": "bg", **fields})
                self.assertEqual(view.activity, activity)


class BuildViewsTests(unittest.TestCase):
    def test_listed_sessions_newest_first(self) -> None:
        views = agents.build_views([background(S1, started=1_000), window(S2, started=3_000)], [])
        self.assertEqual([view.session for view in views], [S2, S1])

    def test_headless_resume_comes_from_records(self) -> None:
        records = [{"sessionId": S3, "pid": 9, "entrypoint": "sdk-cli", "kind": "interactive"}]
        views = agents.build_views([window(S2)], records)
        self.assertEqual({view.session: view.state for view in views},
                         {S2: sessions.FOREIGN, S3: sessions.HEADLESS})

    def test_headless_resume_of_a_stopped_background_session(self) -> None:
        records = [{"sessionId": S1, "pid": 9, "entrypoint": "sdk-cli", "kind": "interactive"}]
        views = agents.build_views([background(S1, pid=None)], records)
        self.assertEqual([view.state for view in views], [sessions.HEADLESS])

    def test_a_window_wins_over_a_headless_resume(self) -> None:
        records = [{"sessionId": S2, "pid": 5151, "entrypoint": "claude-desktop", "kind": "interactive"},
                   {"sessionId": S2, "pid": 9, "entrypoint": "sdk-cli", "kind": "interactive"}]
        views = agents.build_views([window(S2)], records)
        self.assertEqual([(view.state, view.host) for view in views], [(sessions.FOREIGN, sessions.HOST_DESKTOP)])

    def test_a_window_that_opened_after_the_list_still_counts(self) -> None:
        # The list was taken before the window started; reporting it closed would let it be resumed beside the window.
        records = [{"sessionId": S3, "pid": 9, "entrypoint": "cli", "kind": "interactive"}]
        self.assertEqual([(view.session, view.state) for view in agents.build_views([], records)],
                         [(S3, sessions.FOREIGN)])

    def test_a_window_record_outranks_a_listed_background_session(self) -> None:
        records = [{"sessionId": S1, "pid": 9, "entrypoint": "claude-desktop", "kind": "interactive"}]
        self.assertEqual([view.state for view in agents.build_views([background(S1)], records)], [sessions.FOREIGN])

    def test_duplicate_listing_resolves_the_same_in_any_order(self) -> None:
        listed = [background(S1), window(S1)]
        for entries in (listed, list(reversed(listed))):
            self.assertEqual([view.state for view in agents.build_views(entries, [])], [sessions.FOREIGN])

    def test_records_alone_resolve_the_same_in_any_order(self) -> None:
        records = [{"sessionId": S2, "pid": 8, "entrypoint": "sdk-cli", "kind": "interactive"},
                   {"sessionId": S2, "pid": 9, "entrypoint": "claude-desktop", "kind": "interactive"}]
        for ordered in (records, list(reversed(records))):
            self.assertEqual([view.state for view in agents.build_views(None, ordered)], [sessions.FOREIGN])

    def test_a_live_background_record_beats_a_stopped_listing(self) -> None:
        records = [{"sessionId": S1, "pid": 9, "entrypoint": "cli", "kind": "bg"}]
        self.assertEqual([view.state for view in agents.build_views([background(S1, pid=None)], records)],
                         [sessions.RUNNING])

    def test_records_alone_when_claude_cannot_list(self) -> None:
        records = [{"sessionId": S1, "pid": 7, "entrypoint": "cli", "kind": "bg", "startedAt": 5_000},
                   {"sessionId": S2, "pid": 8, "entrypoint": "claude-desktop", "kind": "interactive"}]
        views = agents.build_views(None, records)
        self.assertEqual([(view.session, view.state) for view in views],
                         [(S1, sessions.RUNNING), (S2, sessions.FOREIGN)])


class ListAgentsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        binary = Path(self.tmp.name) / "claude"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        self.environment = {CLAUDE_BIN_ENV: str(binary), "HOME": self.tmp.name, "PATH": ""}
        self.binary = str(binary)

    def answer(self, stdout: str = "[]", returncode: int = 0, error: Exception | None = None):
        calls = []

        def run(command, **options):
            calls.append((command, options))
            if error is not None:
                raise error
            return subprocess.CompletedProcess(command, returncode, stdout, "")

        return run, calls

    def test_runs_the_discovered_cli(self) -> None:
        run, calls = self.answer(json.dumps([window(S1), {"kind": "background"}, "junk"]))
        entries = agents.list_agents(self.environment, run=run)
        self.assertEqual([entry["sessionId"] for entry in entries], [S1])
        command, options = calls[0]
        self.assertEqual(command, [self.binary, "agents", "--json", "--all"])
        self.assertEqual(options["timeout"], agents.LIST_TIMEOUT_SECONDS)
        self.assertIs(options["stdin"], subprocess.DEVNULL)

    def test_failures_mean_unknown(self) -> None:
        for run, _ in (self.answer(returncode=1), self.answer("not json"), self.answer('{"a": 1}'),
                       self.answer(error=OSError("gone")),
                       self.answer(error=subprocess.TimeoutExpired("claude", 10))):
            self.assertIsNone(agents.list_agents(self.environment, run=run))

    def test_no_cli_means_unknown(self) -> None:
        run, calls = self.answer()
        self.assertIsNone(agents.list_agents(self.environment, run=run, discover=lambda environment: None))
        self.assertEqual(calls, [])

    def test_undecodable_output_means_unknown_not_an_exception(self) -> None:
        def run(command, **options):
            self.assertEqual((options["encoding"], options["errors"]), ("utf-8", "replace"))
            return subprocess.CompletedProcess(command, 0, b"[\xff]".decode("utf-8", "replace"), "")

        self.assertIsNone(agents.list_agents(self.environment, run=run))


class LiveProcessRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.records = Path(self.tmp.name) / "claude" / "sessions"
        self.records.mkdir(parents=True)
        self.environment = {"CLAUDE_CONFIG_DIR": str(self.records.parent), "HOME": self.tmp.name}

    def write(self, name: str, **record: object) -> None:
        (self.records / f"{name}.json").write_text(json.dumps(record), encoding="utf-8")

    def live(self) -> set[str]:
        return {str(record["sessionId"]) for record in agents.claude_session.live_process_records(self.environment)}

    def test_live_windows_and_headless_resumes_but_not_dead_processes(self) -> None:
        self.write("window", pid=os.getpid(), sessionId=S1, entrypoint="cli")
        self.write("headless", pid=os.getpid(), sessionId=S2, entrypoint="sdk-cli")
        self.write("dead", pid=2 ** 22 + 1, sessionId=S3, entrypoint="cli")
        (self.records / "broken.json").write_text("{", encoding="utf-8")
        self.write("no-pid", sessionId="44444444-dddd-4ddd-8ddd-444444444444")
        self.assertEqual(self.live(), {S1, S2})
        # The window helpers still leave headless resumes out.
        self.assertEqual({record["sessionId"] for record in agents.claude_session.open_windows(self.environment)}, {S1})

    @unittest.skipUnless(sys.platform.startswith("linux"), "process start times come from /proc")
    def test_a_reused_pid_is_not_the_recorded_process(self) -> None:
        self.write("reused", pid=os.getpid(), sessionId=S1, entrypoint="cli", procStart="1")
        self.assertEqual(self.live(), set())


class SessionViewLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Path(self.tmp.name) / "claude"
        self.environment = {"CLAUDE_CONFIG_DIR": str(self.config), "HOME": self.tmp.name}

    def test_listed_closed_or_unknown(self) -> None:
        listed = [SessionView(agent="claude", session=S1, state=sessions.RUNNING)]
        self.assertEqual(agents.session_view(S1, self.environment, views=listed).state, sessions.RUNNING)
        self.assertIsNone(agents.session_view(S2, self.environment, views=listed))
        project = self.config / "projects" / "-work"
        project.mkdir(parents=True)
        (project / f"{S2}.jsonl").write_text(json.dumps({"type": "user", "cwd": "/work", "sessionId": S2}) + "\n")
        closed = agents.session_view(S2, self.environment, views=listed)
        self.assertEqual((closed.state, closed.host, closed.pid), (sessions.CLOSED, None, None))

    def test_reads_claude_when_no_views_are_given(self) -> None:
        run = lambda command, **options: subprocess.CompletedProcess(command, 0, json.dumps([background(S3)]), "")
        binary = Path(self.tmp.name) / "claude-bin"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        environment = dict(self.environment, **{CLAUDE_BIN_ENV: str(binary)})
        self.assertEqual(agents.session_view(S3, environment, run=run).state, sessions.RUNNING)


if __name__ == "__main__":
    unittest.main()
