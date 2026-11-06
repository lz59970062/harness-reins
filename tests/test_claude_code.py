from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from harness_reins.harness.claude import session as claude_session
from harness_reins import cli
from harness_reins.commands import callbacks as callback_commands
from harness_reins.engine import dispatch
from harness_reins.engine import delivery as delivery_engine
from harness_reins.engine import messages as messages_engine
from harness_reins import runtime as reins_runtime
from harness_reins.store import records as queue_store
from harness_reins.store import layout
from harness_reins.system import shell

SRC = str(Path(__file__).resolve().parents[1] / "src")
SESSION = "3f0c9a52-claude-session"
WINDOW_SESSION = "11111111-2222-4333-8444-555555555555"
OTHER_WINDOW_SESSION = "66666666-7777-4888-8999-000000000000"


def make_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def no_which(_name: str) -> None:
    return None


class ExecutableDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.environment = {"HOME": str(self.home), "PATH": "/nonexistent"}

    def bundle(self, version: str) -> Path:
        return make_executable(
            self.home / "Library/Application Support/Claude/claude-code" / version / "build"
            / "claude.app/Contents/MacOS/claude"
        )

    def test_nothing_installed(self) -> None:
        self.assertIsNone(claude_session.discover_executable(self.environment, which=no_which))
        resolved = claude_session.with_resolved_executable(self.environment, which=no_which)
        self.assertNotIn(claude_session.CLAUDE_BIN_ENV, resolved)

    def test_path_lookup_is_preferred_to_known_locations(self) -> None:
        make_executable(self.home / ".local/bin/claude")
        found = claude_session.discover_executable(self.environment, which=lambda name: "/usr/bin/claude")
        self.assertEqual(found, claude_session.Executable("/usr/bin/claude", "path"))

    def test_standalone_install_is_found_without_path(self) -> None:
        standalone = make_executable(self.home / ".local/bin/claude")
        self.bundle("2.1.0")
        found = claude_session.discover_executable(self.environment, which=no_which)
        self.assertEqual(found, claude_session.Executable(str(standalone), "standalone"))

    def test_session_execpath_then_newest_desktop_bundle(self) -> None:
        old = self.bundle("2.1.0")
        new = self.bundle("2.2.0")
        os.utime(old, (1, 1))
        found = claude_session.discover_executable(self.environment, which=no_which)
        self.assertEqual(found, claude_session.Executable(str(new), "desktop-bundle"))
        session_binary = make_executable(self.home / "custom/claude")
        environment = dict(self.environment, CLAUDE_CODE_EXECPATH=str(session_binary))
        found = claude_session.discover_executable(environment, which=no_which)
        self.assertEqual(found, claude_session.Executable(str(session_binary), "session"))

    def test_explicit_path_is_honored_even_when_missing(self) -> None:
        environment = dict(self.environment, **{claude_session.CLAUDE_BIN_ENV: "/opt/claude"})
        found = claude_session.discover_executable(environment, which=no_which)
        self.assertEqual(found, claude_session.Executable("/opt/claude", "configured"))

    def test_explicit_name_is_honored_but_the_bare_default_is_rediscovered(self) -> None:
        standalone = make_executable(self.home / ".local/bin/claude")
        named = dict(self.environment, **{claude_session.CLAUDE_BIN_ENV: "claude-nightly"})
        self.assertEqual(claude_session.discover_executable(named, which=no_which),
                         claude_session.Executable("claude-nightly", "configured"))
        default = dict(self.environment, **{claude_session.CLAUDE_BIN_ENV: "claude"})
        self.assertEqual(claude_session.discover_executable(default, which=no_which),
                         claude_session.Executable(str(standalone), "standalone"))

    def test_vanished_desktop_bundle_is_rediscovered(self) -> None:
        current = self.bundle("2.2.0")
        stale = str(current).replace("2.2.0", "2.1.0")
        environment = dict(self.environment, **{claude_session.CLAUDE_BIN_ENV: stale})
        found = claude_session.discover_executable(environment, which=no_which)
        self.assertEqual(found, claude_session.Executable(str(current), "desktop-bundle"))

    def test_newest_nvm_install_is_found_without_path(self) -> None:
        old = make_executable(self.home / ".nvm/versions/node/v20.1.0/bin/claude")
        new = make_executable(self.home / ".nvm/versions/node/v22.3.0/bin/claude")
        os.utime(old, (1, 1))
        found = claude_session.discover_executable(self.environment, which=no_which)
        self.assertEqual(found, claude_session.Executable(str(new), "standalone"))

    def test_linux_desktop_remote_cli_is_found_and_rediscovered(self) -> None:
        # The desktop app's SSH sessions install a versioned CLI under ~/.claude/remote.
        remote = self.home / ".claude/remote/ccd-cli"
        old = make_executable(remote / "2.1.293-aaaa")
        new = make_executable(remote / "2.1.295-bbbb")
        os.utime(old, (1, 1))
        found = claude_session.discover_executable(self.environment, which=no_which)
        self.assertEqual(found, claude_session.Executable(str(new), "desktop-remote"))
        self.assertFalse(claude_session.is_desktop_bundle(new))  # It shares ~/.claude sign-in.
        session = dict(self.environment, CLAUDE_CODE_EXECPATH=str(new))
        self.assertEqual(claude_session.discover_executable(session, which=no_which),
                         claude_session.Executable(str(new), "desktop-remote"))
        stale = dict(self.environment, **{claude_session.CLAUDE_BIN_ENV: str(remote / "2.1.200-gone")})
        self.assertEqual(claude_session.discover_executable(stale, which=no_which),
                         claude_session.Executable(str(new), "desktop-remote"))
        standalone = make_executable(self.home / ".local/bin/claude")
        self.assertEqual(claude_session.discover_executable(self.environment, which=no_which),
                         claude_session.Executable(str(standalone), "standalone"))

    def test_resume_command_uses_discovered_binary(self) -> None:
        request = {"agent": "claude", "target": {"kind": "session", "session": SESSION}, "cwd": "/tmp", "prompt": "x"}
        with mock.patch.object(cli.claude_session, "discover_executable",
                               return_value=claude_session.Executable("/found/claude", "standalone")):
            self.assertEqual(delivery_engine.resume_command(request)[0], "/found/claude")


class SessionDirectoryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "claude-config"
        self.project = self.root / "project"
        self.project.mkdir()
        self.environment = {"HOME": str(self.root), "CLAUDE_CONFIG_DIR": str(self.config)}

    def write_session(self, *records: dict[str, object]) -> Path:
        path = self.config / "projects" / "-slug-of-project" / f"{SESSION}.jsonl"
        path.parent.mkdir(parents=True)
        lines = ["not json"] + [json.dumps(record) for record in records]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_first_recorded_cwd_wins_over_later_cd(self) -> None:
        later = self.project / "subdir"
        later.mkdir()
        self.write_session({"type": "queue-operation"}, {"cwd": str(self.project)}, {"cwd": str(later)})
        self.assertEqual(claude_session.session_project_cwd(SESSION, self.environment), str(self.project))

    def test_missing_unsafe_or_deleted_sessions_have_no_cwd(self) -> None:
        self.assertIsNone(claude_session.session_project_cwd(SESSION, self.environment))
        self.assertIsNone(claude_session.session_project_cwd("../escape", self.environment))
        self.write_session({"cwd": str(self.root / "deleted")})
        self.assertIsNone(claude_session.session_project_cwd(SESSION, self.environment))

    def test_daemon_resumes_claude_where_the_session_started(self) -> None:
        self.write_session({"cwd": str(self.project)})
        claude = {"agent": "claude", "target": {"kind": "session", "session": SESSION}, "cwd": "/task/cwd"}
        codex = dict(claude, agent="codex")
        unrecorded = dict(claude, target={"kind": "session", "session": "session-without-a-transcript"})
        with mock.patch.dict(os.environ, self.environment):
            self.assertEqual(delivery_engine.resume_cwd(claude), str(self.project))
            self.assertEqual(delivery_engine.resume_cwd(codex), "/task/cwd")
            self.assertEqual(delivery_engine.resume_cwd(unrecorded), "/task/cwd")


class LiveWaiterTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="reins live ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "queue"
        self.environment = {
            "CODEX_HOME": str(self.directory / "codex-home"), "REINS_HOME": str(self.directory / "codex-home" / "reins-home"),
            layout.TARGET_LOCK_DIR_ENV: str(self.directory / "target-locks"),
        }
        patcher = mock.patch.dict(os.environ, self.environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        queue_store.ensure_daemon_dirs(self.root)

    def enqueue(self, ident: str, *, session: str = SESSION, agent: str = "claude", **extra: object) -> Path:
        args = argparse.Namespace(
            agent=agent, session=session, last=False, task="train model", cwd=str(self.directory),
            command="python train.py", exit_code=0, message=None, queue_dir=str(self.root), _callback_id=ident,
        )
        prompt = dispatch.build_prompt(args, duration=12.0)
        request = dispatch.make_request(args, prompt)
        request.update(extra)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dispatch.enqueue_existing_request(self.root, request, prompt), 0)
        return layout.request_path(self.root, "pending", ident)

    def run_wait(self, **overrides: object) -> tuple[int, str, str]:
        values = dict(queue_dir=str(self.root), session=SESSION, task=None, timeout=None, poll_interval=0.05)
        values.update(overrides)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = claude_session.wait(argparse.Namespace(**values))
        return status, stdout.getvalue(), stderr.getvalue()

    def ack(self, ident: str) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            callback_commands.ack(argparse.Namespace(queue_dir=str(self.root), id=ident, message=None))

    def test_watcher_registration_is_visible_only_for_claude_and_stale_files_are_cleaned(self) -> None:
        claude = {"agent": "claude", "target": {"kind": "session", "session": SESSION}}
        codex = dict(claude, agent="codex")
        self.assertFalse(claude_session.live_watcher_is_held(claude))
        watcher = claude_session.register_live_watcher(SESSION)
        self.assertTrue(claude_session.live_watcher_is_held(claude))
        self.assertFalse(claude_session.live_watcher_is_held(codex))
        claude_session.release_live_watcher(watcher)
        self.assertFalse(claude_session.live_watcher_is_held(claude))
        stale = claude_session.live_watcher_dir(SESSION) / "999-dead.lock"
        stale.write_text("", encoding="utf-8")
        self.assertFalse(claude_session.live_watcher_is_held(claude))
        self.assertFalse(stale.exists())

    def test_watcher_never_renames_or_deletes_an_open_lock_on_windows(self) -> None:
        # Models Windows sharing rules on POSIX: a file opened with share mode 0
        # can be neither renamed nor deleted (WinError 32). On Windows the other
        # tests here exercise the real lock.
        if os.name == "nt":
            self.skipTest("native Windows runs the real lock")
        held: set[Path] = set()

        def acquire(path: Path, *, blocking: bool) -> tuple[object, Path] | None:
            path = Path(path)
            if path in held:
                return None
            path.touch()
            held.add(path)
            return io.StringIO(), path

        def release(lock: tuple[object, Path] | None, *, remove: bool) -> None:
            if lock is not None:
                held.discard(lock[1])

        def refuse_open(path: object) -> None:
            if Path(path) in held:
                raise PermissionError(13, "[WinError 32] being used by another process", str(path))

        real_replace, real_unlink = os.replace, Path.unlink

        def replace(source: object, destination: object) -> None:
            refuse_open(source)
            real_replace(source, destination)

        def unlink(path: Path, missing_ok: bool = False) -> None:
            refuse_open(path)
            real_unlink(path, missing_ok=missing_ok)

        windows_os = types.SimpleNamespace(**vars(os))
        windows_os.name, windows_os.replace = "nt", replace
        for patcher in (
            mock.patch.object(claude_session, "os", windows_os),
            mock.patch.object(queue_store, "acquire_path_lock", acquire),
            mock.patch.object(queue_store, "release_owner_lock", release),
            mock.patch.object(Path, "unlink", unlink),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

        claude = {"agent": "claude", "target": {"kind": "session", "session": SESSION}}
        directory = claude_session.live_watcher_dir(SESSION)
        watcher = claude_session.register_live_watcher(SESSION)
        self.assertEqual([path.name for path in directory.iterdir()], [watcher[1].name])
        self.assertTrue(claude_session.live_watcher_is_held(claude))
        claude_session.release_live_watcher(watcher)
        self.assertFalse(claude_session.live_watcher_is_held(claude))
        self.assertEqual(list(directory.iterdir()), [])

        self.enqueue("cb-1")
        status, output, _ = self.run_wait()
        self.assertEqual(status, 0)
        self.assertIn("[reins] cb-1", output)
        self.assertEqual(list(directory.iterdir()), [])

    def test_daemon_leaves_only_claude_callbacks_to_a_live_waiter(self) -> None:
        claude = self.enqueue("cb-claude")
        codex = self.enqueue("cb-codex", agent="codex")
        watcher = claude_session.register_live_watcher(SESSION)
        try:
            self.assertEqual(delivery_engine.select_pending(self.root, time.time()), codex)
            codex.unlink()
            self.assertIsNone(delivery_engine.select_pending(self.root, time.time()))
        finally:
            claude_session.release_live_watcher(watcher)
        self.assertEqual(delivery_engine.select_pending(self.root, time.time()), claude)

    def test_wait_refuses_outside_claude_code_and_never_claims_other_agents(self) -> None:
        self.enqueue("cb-codex", agent="codex")
        with mock.patch.dict(os.environ, {"CODEX_THREAD_ID": SESSION}):
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
            status, output, errors = self.run_wait(session=None)
        self.assertEqual((status, output), (2, ""))
        self.assertIn("only for Claude Code", errors)
        self.assertEqual(self.run_wait(timeout=0.2)[0], 3)
        self.assertTrue(layout.request_path(self.root, "pending", "cb-codex").exists())

    def test_wait_prints_callback_defers_daemon_and_ack_finishes_it(self) -> None:
        self.enqueue("cb-other", session="another-session")
        self.enqueue("cb-1")
        status, output, _ = self.run_wait()
        self.assertEqual(status, 0)
        self.assertIn("[reins] cb-1", output)
        self.assertIn("ack", output)
        self.assertNotIn("cb-other", output)

        pending = layout.request_path(self.root, "pending", "cb-1")
        request = json.loads(pending.read_text(encoding="utf-8"))
        self.assertGreater(request["next_attempt_at"], time.time() + 1000)
        self.assertEqual(delivery_engine.select_pending(self.root, time.time()).stem, "cb-other")
        self.assertFalse(any((self.root / "running").glob("*.json")))

        self.ack("cb-1")
        self.assertTrue(layout.request_path(self.root, "done", "cb-1").exists())
        self.assertFalse(pending.exists())

    def test_unacknowledged_callback_comes_back_until_acked(self) -> None:
        self.enqueue("cb-1")
        self.assertEqual(self.run_wait()[0], 0)
        status, output, _ = self.run_wait()
        self.assertEqual(status, 0)
        self.assertIn("cb-1", output)
        self.ack("cb-1")
        self.assertEqual(self.run_wait(timeout=0.2)[0], 3)

    def test_wait_filters_by_task_and_reports_acknowledged_task(self) -> None:
        layout.managed_task_dir(self.root, "abcd1234").mkdir(parents=True)
        layout.managed_task_path(self.root, "abcd1234").write_text("{}", encoding="utf-8")
        self.enqueue("cb-unrelated", managed_task_id="ffff0000")
        self.enqueue("cb-task", managed_task_id="abcd1234")
        status, output, _ = self.run_wait(task="abcd1234")
        self.assertEqual(status, 0)
        self.assertIn("cb-task", output)
        self.assertNotIn("cb-unrelated", output)

        self.ack("cb-task")
        status, output, errors = self.run_wait(task="abcd1234")
        self.assertEqual((status, output), (0, ""))
        self.assertIn("already acknowledged", errors)
        self.assertEqual(self.run_wait(task="00000000")[0], 2)

    def test_wait_recovers_a_callback_whose_headless_retries_failed(self) -> None:
        queue_store.move_request(self.enqueue("cb-failed"), self.root / "failed")
        status, output, _ = self.run_wait()
        self.assertEqual(status, 0)
        self.assertIn("cb-failed", output)
        self.assertTrue(layout.request_path(self.root, "pending", "cb-failed").exists())

    def open_window(self, session: str = WINDOW_SESSION, **fields: object) -> Path:
        config = self.directory / "claude"
        sessions = config / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        path = sessions / f"{os.getpid()}.json"
        path.write_text(json.dumps({"pid": os.getpid(), "sessionId": session, "entrypoint": "cli", **fields}),
                        encoding="utf-8")
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return path

    def run_hook(self, session: str = WINDOW_SESSION, *, grace: float = 0.0, **environment: str) -> tuple[int, str]:
        stderr = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"session_id": session}))), \
                mock.patch.object(claude_session, "WINDOW_RECORD_GRACE_SECONDS", grace), \
                mock.patch.dict(os.environ, environment), contextlib.redirect_stderr(stderr):
            status = claude_session.hook(argparse.Namespace(queue_dir=str(self.root), poll_interval=0.01))
        return status, stderr.getvalue()

    def test_an_open_window_keeps_the_daemon_from_forking_it(self) -> None:
        pending = self.enqueue("cb-open", session=WINDOW_SESSION)
        record = self.open_window()
        self.assertIsNone(delivery_engine.select_pending(self.root, time.time()))
        record.unlink()
        self.assertEqual(delivery_engine.select_pending(self.root, time.time()), pending)

    def test_arrival_tells_an_open_window_from_a_closed_session(self) -> None:
        record = {"harness": "claude", "session": WINDOW_SESSION}
        self.assertEqual(messages_engine.arrival(record), "resume")
        window = self.open_window()
        self.assertEqual(messages_engine.arrival(record), "window")
        watcher = claude_session.register_live_watcher(WINDOW_SESSION, prefix=claude_session.HOOK_WATCHER_PREFIX)
        self.addCleanup(claude_session.release_live_watcher, watcher)
        self.assertEqual(messages_engine.arrival(record), "live")
        window.unlink()

    def test_only_a_live_window_on_this_session_counts_as_open(self) -> None:
        self.open_window(entrypoint="sdk-cli")  # A headless run, ours or anyone's, ends with its turn.
        self.assertIsNone(claude_session.open_window(WINDOW_SESSION, os.environ))
        self.open_window(session=OTHER_WINDOW_SESSION)
        self.assertIsNone(claude_session.open_window(WINDOW_SESSION, os.environ))
        record = self.open_window()
        self.assertEqual(claude_session.open_window(WINDOW_SESSION, os.environ), os.getpid())
        record.write_text(json.dumps({"pid": 2 ** 22 + 1, "sessionId": WINDOW_SESSION}), encoding="utf-8")
        self.assertIsNone(claude_session.open_window(WINDOW_SESSION, os.environ))

    @unittest.skipUnless(sys.platform.startswith("linux"), "start ticks come from /proc")
    def test_a_reused_pid_is_not_an_open_window(self) -> None:
        self.open_window(procStart="1")
        self.assertIsNone(claude_session.open_window(WINDOW_SESSION, os.environ))

    def test_a_session_never_gets_two_headless_resumes_at_once(self) -> None:
        first = self.enqueue("cb-first", session=WINDOW_SESSION)
        self.enqueue("cb-second", session=WINDOW_SESSION)
        self.assertEqual(delivery_engine.select_pending(self.root, time.time()), first)
        lock = delivery_engine.acquire_target_lock(
            {"agent": "claude", "target": {"kind": "session", "session": WINDOW_SESSION}}, blocking=False)
        try:
            self.assertIsNone(delivery_engine.select_pending(self.root, time.time()))
            self.assertIsNone(delivery_engine.acquire_target_lock(
                {"agent": "claude", "target": {"kind": "session", "session": WINDOW_SESSION}}, blocking=False))
        finally:
            queue_store.release_owner_lock(lock, remove=False)
        self.assertEqual(delivery_engine.select_pending(self.root, time.time()), first)

    def test_the_hook_wakes_the_window_and_does_not_repeat_itself(self) -> None:
        self.open_window()
        self.enqueue("cb-hook", session=WINDOW_SESSION)
        status, output = self.run_hook()
        self.assertEqual(status, 2)
        self.assertIn("[reins] cb-hook", output)
        self.assertTrue(layout.request_path(self.root, "pending", "cb-hook").exists())
        # A turn that did not ACK ends, Stop starts the hook, and it must not wake the model again.
        with mock.patch.object(claude_session, "open_window", return_value=None):
            self.assertEqual(self.run_hook(), (0, ""))
        # Left unacknowledged for the reminder interval, it is raised once more.
        request = json.loads(layout.request_path(self.root, "pending", "cb-hook").read_text(encoding="utf-8"))
        request["live_delivered_at"] = time.time() - claude_session.LIVE_REMIND_SECONDS - 1
        queue_store.write_request(layout.request_path(self.root, "pending", "cb-hook"), request)
        with mock.patch.object(claude_session, "open_window", return_value=None):
            self.assertEqual(self.run_hook()[0], 2)
        with mock.patch.object(claude_session, "open_window", return_value=None):
            self.assertEqual(self.run_hook(), (0, ""))

    def test_the_hook_leaves_at_once_where_it_has_no_window(self) -> None:
        self.enqueue("cb-hook", session=WINDOW_SESSION)
        # A headless run does not exit while the hook lives, so a long grace must not matter.
        for environment in ({claude_session.ENTRYPOINT_ENV: claude_session.HEADLESS_ENTRYPOINT},
                            {claude_session.RESUMED_TARGET_ENV: f"session:{WINDOW_SESSION}"}):
            self.assertEqual(self.run_hook(grace=60.0, **environment), (0, ""))
        self.assertEqual(self.run_hook("not a session id", grace=60.0), (0, ""))
        self.assertEqual(self.run_hook(grace=0.0), (2, mock.ANY))

    def test_the_hook_exits_when_the_window_closes_and_stops_holding_the_daemon(self) -> None:
        self.assertEqual(self.run_hook(), (0, ""))
        self.assertFalse(claude_session.hook_is_held(WINDOW_SESSION))

    def test_one_hook_serves_a_session_and_reins_wait_steps_aside(self) -> None:
        self.enqueue("cb-hook", session=WINDOW_SESSION)
        watcher = claude_session.register_live_watcher(WINDOW_SESSION, prefix=claude_session.HOOK_WATCHER_PREFIX)
        try:
            self.assertEqual(self.run_hook(grace=60.0), (0, ""))
            status, output, errors = self.run_wait(session=WINDOW_SESSION)
        finally:
            claude_session.release_live_watcher(watcher)
        self.assertEqual((status, output), (0, ""))
        self.assertIn("no waiter is needed", errors)

    def test_wait_skips_callbacks_the_daemon_is_delivering(self) -> None:
        self.enqueue("cb-busy")
        target_lock = delivery_engine.acquire_target_lock({"target": {"kind": "session", "session": SESSION}}, blocking=False)
        try:
            self.assertEqual(self.run_wait(timeout=0.2)[0], 3)
        finally:
            queue_store.release_owner_lock(target_lock, remove=False)
        self.assertEqual(self.run_wait()[0], 0)

    def test_submission_holds_callbacks_for_the_waiter_it_asks_for(self) -> None:
        # A task that fails at once queues its callback before the session has
        # started `reins wait`; the daemon must not resume the session headless.
        claude_session.expect_live_waiter(SESSION, 60)
        claude = self.enqueue("cb-instant")
        codex = self.enqueue("cb-codex", agent="codex")
        self.assertEqual(delivery_engine.select_pending(self.root, time.time()), codex)
        codex.unlink()
        self.assertIsNone(delivery_engine.select_pending(self.root, time.time()))
        status, output, _ = self.run_wait()
        self.assertEqual(status, 0)
        self.assertIn("[reins] cb-instant", output)
        self.ack("cb-instant")
        self.assertFalse(claude.exists())

    def test_expired_reservation_lets_the_daemon_resume_a_closed_session(self) -> None:
        directory = claude_session.live_watcher_dir(SESSION)
        claude_session.expect_live_waiter(SESSION, 60)
        reservation, = directory.glob("*" + claude_session.EXPECTED_WAITER_SUFFIX)
        reservation.write_text(repr(time.time() - 1), encoding="utf-8")
        claude = self.enqueue("cb-late")
        self.assertEqual(delivery_engine.select_pending(self.root, time.time()), claude)
        self.assertFalse(reservation.exists())
        claude_session.expect_live_waiter(SESSION, 0)
        self.assertFalse(any(directory.glob("*" + claude_session.EXPECTED_WAITER_SUFFIX)))

    def test_live_wait_grace_comes_from_the_environment(self) -> None:
        grace = claude_session.live_wait_grace
        self.assertEqual(grace({}), claude_session.DEFAULT_LIVE_WAIT_GRACE_SECONDS)
        self.assertEqual(grace({claude_session.LIVE_WAIT_GRACE_ENV: "15"}), 15.0)
        self.assertEqual(grace({claude_session.LIVE_WAIT_GRACE_ENV: "0"}), 0.0)
        self.assertEqual(grace({claude_session.LIVE_WAIT_GRACE_ENV: "-3"}), 0.0)
        self.assertEqual(grace({claude_session.LIVE_WAIT_GRACE_ENV: "soon"}),
                         claude_session.DEFAULT_LIVE_WAIT_GRACE_SECONDS)

    def test_wait_inside_the_headless_resume_reaches_its_own_session(self) -> None:
        # The resume's worker holds the target lock until the resumed process
        # exits; a waiter started by that process must not wait for itself.
        self.enqueue("cb-next")
        target = {"agent": "claude", "target": {"kind": "session", "session": SESSION}}
        target_lock = delivery_engine.acquire_target_lock(target, blocking=False)
        try:
            with mock.patch.dict(os.environ, {claude_session.RESUMED_TARGET_ENV: "session:another-session"}):
                self.assertEqual(self.run_wait(timeout=0.2)[0], 3)
            key = delivery_engine.callback_target_key(target)
            with mock.patch.dict(os.environ, {claude_session.RESUMED_TARGET_ENV: key}):
                status, output, _ = self.run_wait(timeout=5)
        finally:
            queue_store.release_owner_lock(target_lock, remove=False)
        self.assertEqual(status, 0)
        self.assertIn("[reins] cb-next", output)

    def test_wait_says_when_a_headless_resume_has_its_callback(self) -> None:
        layout.managed_task_dir(self.root, "abcd1234").mkdir(parents=True)
        layout.managed_task_path(self.root, "abcd1234").write_text("{}", encoding="utf-8")
        running = queue_store.move_request(self.enqueue("cb-headless", managed_task_id="abcd1234"), self.root / "running")
        delivery_lock = queue_store.acquire_owner_lock(self.root, layout.delivery_lock_id("cb-headless"), blocking=False)
        try:
            with mock.patch.object(claude_session, "HEADLESS_NOTICE_SECONDS", 0.1):
                status, output, _ = self.run_wait(task="abcd1234", timeout=30)
            self.assertEqual(status, claude_session.HEADLESS_EXIT_CODE)
            self.assertIn("[reins] cb-headless went to a headless resume", output)
            self.assertIn(f"claude -p --resume {SESSION}", output)
            self.assertNotIn("ACK after inspection", output)

            # The resume ACKed it but is still running: the callback stays under
            # running/, and a waiter for that task must not wait for it forever.
            self.ack("cb-headless")
            self.assertTrue(running.exists())
            status, output, errors = self.run_wait(task="abcd1234", timeout=30)
            self.assertEqual((status, output), (0, ""))
            self.assertIn("already acknowledged", errors)
        finally:
            queue_store.release_owner_lock(delivery_lock, remove=False)

    def test_daemon_does_not_redeliver_a_callback_live_delivered_after_selection(self) -> None:
        self.enqueue("cb-raced")
        select_pending = delivery_engine.select_pending

        def waiter_delivers_after_selection(root: Path, now: float) -> Path | None:
            selected = select_pending(root, now)
            self.assertIsNotNone(selected)  # No waiter yet: the daemon picks it.
            self.assertEqual(self.run_wait()[0], 0)  # `reins wait` delivers it and exits.
            return selected

        args = argparse.Namespace(resume_timeout=1, retries=3, retry_delay=0, retry_backoff=1)
        with mock.patch.object(delivery_engine, "select_pending", side_effect=waiter_delivers_after_selection), \
                mock.patch.object(delivery_engine, "run_resume_until_exit_or_ack") as resume:
            self.assertTrue(delivery_engine.process_one(self.root, args))
        resume.assert_not_called()
        request = dispatch.load_request(layout.request_path(self.root, "pending", "cb-raced"))
        self.assertIn("live_delivered_at", request)
        self.assertGreater(float(request["next_attempt_at"]), time.time())
        self.assertEqual(request.get("attempts", 0), 0)

    def test_background_waiter_process_wakes_when_callback_arrives(self) -> None:
        environment = dict(os.environ, PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", ""),
                           CLAUDE_CODE_SESSION_ID=SESSION, CLAUDECODE="1")
        # Generous real-time bounds for a loaded host; they only end a hung waiter.
        process = subprocess.Popen(
            [sys.executable, "-m", "harness_reins", "wait", "--queue-dir", str(self.root),
             "--poll-interval", "0.05", "--timeout", "300"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment,
        )
        request = {"agent": "claude", "target": {"kind": "session", "session": SESSION}}
        try:
            deadline = time.monotonic() + 120
            while not claude_session.live_watcher_is_held(request):
                if process.poll() is not None:
                    self.fail("waiter exited early: " + process.communicate()[1])
                self.assertLess(time.monotonic(), deadline, "waiter never registered")
                time.sleep(0.05)
            # Hold the callback's delivery lock so the waiter cannot claim it yet:
            # the daemon must skip it because the waiter is live, not because
            # the waiter happened to be mid-claim or already done.
            delivery_lock = queue_store.acquire_owner_lock(self.root, layout.delivery_lock_id("cb-async"), blocking=False)
            try:
                self.enqueue("cb-async")
                self.assertIsNone(delivery_engine.select_pending(self.root, time.time()))
                self.assertIsNone(process.poll())
            finally:
                queue_store.release_owner_lock(delivery_lock, remove=False)
            stdout, stderr = process.communicate(timeout=120)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.assertEqual(process.returncode, 0, stderr)
        self.assertIn("[reins] cb-async", stdout)
        self.assertFalse(claude_session.live_watcher_is_held(request))


class ControlCommandTests(unittest.TestCase):
    def test_ack_command_is_runnable_without_atc_on_path(self) -> None:
        with mock.patch.object(reins_runtime, "console_script_path", return_value=None):
            command = reins_runtime.control_command("ack", "--id", "x")
        # PowerShell's call operator on Windows, a POSIX shell line elsewhere.
        self.assertEqual(command, shell.format_command(
            [sys.executable, str(Path(cli.__file__).with_name("_entry.py")), "ack", "--id", "x"]))

if __name__ == "__main__":
    unittest.main()
