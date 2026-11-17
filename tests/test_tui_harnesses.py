"""Codex, Pi and DeepSeek Harness sessions in the Reins panel, and the Codex callback route that keeps one writer."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness_reins.commands.tui import launch, live
from harness_reins.core import capabilities, sessions
from harness_reins.engine import native_queue
from harness_reins.harness.codex import app_server, threads
from harness_reins.harness.pi import channel as pi_channel
from harness_reins.harness.pi import sessions as pi_sessions

from test_tui_live import FakeZellij, LiveCase, bare, write

THREAD = "01a1e91a-0a90-7eb1-97c5-ca946a877e7d"
NEIGHBOUR = "01a1e91a-1111-7222-8333-944455556666"  # Same timestamp digits: started within the same minute.


def thread(ident, status="notLoaded", **fields):
    return {"id": ident, "name": None, "preview": "fix the build", "cwd": "/w", "createdAt": 1_900_000_000,
            "status": {"type": status, **({"activeFlags": fields.pop("flags")} if "flags" in fields else {})},
            **fields}


class FakeConnection:
    """The daemon's JSON-RPC, as app_server.AppServerConnection is used."""

    def __init__(self, threads=(), loaded=(), forked=None):
        self.threads = list(threads)
        self.loaded = list(loaded)
        self.forked = forked
        self.requests: list[tuple] = []
        self.closed = 0

    def __call__(self, path, timeout):
        return self

    def connect(self):
        pass

    def close(self):
        self.closed += 1

    def notify(self, method, params):
        pass

    def request(self, method, params):
        self.requests.append((method, params))
        if method == "initialize":
            return {}
        if method == "thread/list":
            return {"data": self.threads, "nextCursor": None}
        if method == "thread/loaded/list":
            return {"data": self.loaded, "nextCursor": None}
        if method == "thread/read":
            found = next((item for item in self.threads if item["id"] == params["threadId"]), None)
            if found is None:
                raise app_server.AppServerRpcError("no such thread")
            return {"thread": found}
        if method == "thread/fork":
            return {"thread": {"id": self.forked}}
        raise AssertionError(method)


class CodexThreadTests(unittest.TestCase):
    def test_states_come_from_the_daemon_and_other_writers(self) -> None:
        connection = FakeConnection(threads=[
            thread("a" * 8 + THREAD[8:]), thread("b" * 8 + THREAD[8:], "idle", name="named"),
            thread("c" * 8 + THREAD[8:], "active", flags=[]), thread("d" * 8 + THREAD[8:], "active", flags=["waitingOnApproval"]),
            thread("e" * 8 + THREAD[8:]), thread("f" * 8 + THREAD[8:], "idle")])
        others = {"e" * 8 + THREAD[8:]: (41, False), "f" * 8 + THREAD[8:]: (42, True)}
        views = {view.session[0]: view for view in threads.session_views({}, connect=connection, others=others)}
        self.assertEqual((views["a"].state, views["a"].name), (sessions.CLOSED, "fix the build"))
        self.assertEqual((views["b"].state, views["b"].activity, views["b"].name, views["b"].host),
                         (sessions.RUNNING, sessions.IDLE, "named", threads.HOST_DAEMON))
        self.assertEqual(views["c"].activity, sessions.WORKING)
        self.assertEqual(views["d"].activity, sessions.BLOCKED)
        # A Codex window off the daemon holds the thread: opening it here would make a second writer.
        self.assertEqual((views["e"].state, views["e"].pid), (sessions.FOREIGN, 41))
        self.assertEqual(views["f"].state, sessions.HEADLESS)
        # The fast list: the state database, not a scan of every rollout.
        self.assertTrue(dict(connection.requests)["thread/list"]["useStateDbOnly"])
        self.assertEqual(connection.closed, 1)

    def test_handles_tell_threads_of_the_same_minute_apart(self) -> None:
        self.assertNotEqual(threads.handle(THREAD), threads.handle(NEIGHBOUR))
        self.assertEqual(threads.handle(THREAD), "6a877e7d")

    def test_commands_go_through_the_daemon(self) -> None:
        environment = {"CODEX_HOME": "/c", "REINS_CODEX_BIN": "/bin/codex"}
        self.assertEqual(threads.resume_command(THREAD, environment),
                         ["/bin/codex", "resume", "--remote", "unix:///c/app-server-control/app-server-control.sock",
                          THREAD])
        self.assertEqual(threads.new_command("/w", environment)[1:3],
                         ["--remote", "unix:///c/app-server-control/app-server-control.sock"])
        self.assertEqual(threads.new_command("/w", environment)[-2:], ["-C", "/w"])

    def test_no_daemon_means_no_rows(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(threads.session_views({"CODEX_HOME": home}), [])
            self.assertFalse(threads.loaded_on_daemon(THREAD, {"CODEX_HOME": home}))

    @unittest.skipUnless(hasattr(os, "getuid") and os.name == "posix", "symlinks and uids")
    def test_writers_read_open_rollouts_and_skip_the_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            binary = proc / "bin" / "codex"
            binary.parent.mkdir()
            binary.write_text("")
            rollout = proc / f"rollout-2026-11-17T18-42-40-{THREAD}.jsonl"
            rollout.write_text("")
            other = proc / f"rollout-2026-11-17T18-42-40-{NEIGHBOUR}.jsonl"
            other.write_text("")

            def process(pid, argv, path):
                entry = proc / str(pid)
                (entry / "fd").mkdir(parents=True)
                (entry / "exe").symlink_to(binary)
                (entry / "cmdline").write_bytes(b"\0".join(word.encode() for word in argv) + b"\0")
                (entry / "fd" / "7").symlink_to(path)

            process(10, ["codex", "resume", THREAD], rollout)
            process(11, ["codex", "app-server", "--listen", "unix://"], other)  # The daemon.
            process(12, ["codex", "exec", "resume", "--all", NEIGHBOUR, "-"], other)
            (proc / "self").mkdir()
            self.assertEqual(threads.writers(proc), {THREAD: (10, False), NEIGHBOUR: (12, True)})


def write_pi_session(directory: Path, ident: str, cwd: str, *lines: dict) -> Path:
    path = directory / f"2026-11-17T10-00-00-000Z_{ident}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {"type": "session", "version": 3, "id": ident, "timestamp": "2026-11-17T10:00:00.000Z", "cwd": cwd}
    path.write_text("".join(json.dumps(line) + "\n" for line in (header, *lines)), encoding="utf-8")
    return path


def user(text):
    return {"type": "message", "id": "m", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


class PiSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.profile = Path(tmp.name) / "agent"
        self.environment = {channel_env: str(self.profile) for channel_env in (pi_channel.PROFILE_ENV,)}
        pi_sessions._cache.clear()

    def test_titles_are_the_name_else_the_first_user_text(self) -> None:
        sessions_dir = self.profile / "sessions" / "--w--"
        first = write_pi_session(sessions_dir, THREAD, "/w", user("fix  the\nbuild"), user("later"))
        second = write_pi_session(sessions_dir, NEIGHBOUR, "/w", user("hi"),
                                  {"type": "session_info", "id": "i", "name": "Refactor auth"})
        (sessions_dir / "notes.jsonl").write_text('{"type": "other"}\n', encoding="utf-8")
        views = {view.session: view for view in pi_sessions.session_views(self.environment)}
        self.assertEqual(set(views), {str(first.resolve()), str(second.resolve())})
        self.assertEqual(views[str(first.resolve())].name, "fix the build")
        self.assertEqual(views[str(second.resolve())].name, "Refactor auth")
        self.assertEqual((views[str(first.resolve())].state, views[str(first.resolve())].cwd), (sessions.CLOSED, "/w"))
        self.assertEqual(views[str(first.resolve())].handle, "6a877e7d")
        # Only what was appended is read again, and a later rename shows.
        with first.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "session_info", "id": "j", "name": "Build fix"}) + "\n")
        self.assertEqual(pi_sessions.session_view(str(first), self.environment).name, "Build fix")

    def test_a_live_extension_owner_means_open_elsewhere(self) -> None:
        path = write_pi_session(self.profile / "sessions" / "--w--", THREAD, "/w")
        session = pi_channel.canonical_session(str(path))
        owner = pi_channel.channel_dir(session, self.profile / "harness-reins" / "channels") / "owner.json"
        write(owner, {"version": 1, "owner_pid": os.getpid(), "state": "active"})
        view = pi_sessions.session_view(session, self.environment)
        self.assertEqual((view.state, view.pid), (sessions.FOREIGN, os.getpid()))
        write(owner, {"version": 1, "owner_pid": os.getpid(), "state": "closed"})
        self.assertEqual(pi_sessions.session_view(session, self.environment).state, sessions.CLOSED)

    def test_a_new_managed_session_file_is_named_by_its_header_id(self) -> None:
        with tempfile.TemporaryDirectory() as cwd, mock.patch.dict(os.environ, self.environment):
            session = pi_channel.create_session(cwd)
            header = pi_channel.session_header(session)
        self.assertEqual(pi_sessions.handle(session), header["id"].replace("-", "")[-8:])
        self.assertEqual(header["cwd"], str(Path(cwd).resolve()))
        self.assertTrue(session.startswith(str((self.profile / "sessions" / "reins-managed").resolve())))


class HarnessBackendTests(LiveCase):
    def setUp(self) -> None:
        super().setUp()
        self.codex = FakeConnection()
        self.others: dict = {}
        self.pi_profile = self.home / "pi"
        pi_sessions._cache.clear()
        self.backend = live.LiveBackend(
            queue_root=self.root, zellij=self.zellij, now=lambda: self.now, codex_connect=self.codex,
            codex_writers=lambda: dict(self.others),
            environment={"PATH": "/bin", "REINS_CODEX_BIN": "/bin/codex", "CODEX_HOME": str(self.home / "codex"),
                         pi_channel.PROFILE_ENV: str(self.pi_profile)})

    def rows(self):
        return {row.session: row for row in self.backend.sessions()}

    def test_codex_threads_are_listed_and_open_through_the_daemon(self) -> None:
        self.codex.threads = [thread(THREAD, "idle", name="build fix")]
        self.codex.loaded = [THREAD]
        row = self.rows()[THREAD]
        self.assertEqual((row.agent, row.state, row.title), ("codex", sessions.RUNNING, "build fix"))
        result = self.backend.open_session(THREAD)
        self.assertTrue(result.ok, result.message)
        [(argv, name, _where, _close)] = self.zellij.opened
        self.assertEqual(bare(argv)[:3], ["/bin/codex", "resume", "--remote"])
        self.assertEqual(bare(argv)[-1], THREAD)
        self.assertEqual(name, "codex 6a877e7d build fix")
        # With its pane open the thread shows as here, and Enter jumps to the pane.
        self.zellij.pane_list = [{"id": 9, "is_plugin": False, "title": name}]
        self.assertEqual(self.rows()[THREAD].state, sessions.ATTACHED)
        self.backend.open_session(THREAD)
        self.assertEqual(self.zellij.focused, ["terminal_9"])

    def test_a_codex_thread_open_off_the_daemon_is_refused(self) -> None:
        self.codex.threads = [thread(THREAD)]
        self.others = {THREAD: (77, False)}
        self.rows()
        result = self.backend.open_session(THREAD)
        self.assertFalse(result.ok)
        self.assertIn("pid 77", result.message)
        self.assertEqual(self.zellij.opened, [])

    def test_codex_fork_uses_the_daemon_and_opens_the_copy(self) -> None:
        self.codex.threads = [thread(THREAD, "idle", name="build fix")]
        self.codex.forked = NEIGHBOUR
        self.rows()
        result = self.backend.fork_session(THREAD)
        self.assertTrue(result.ok, result.message)
        self.assertIn(("thread/fork", {"threadId": THREAD}), self.codex.requests)
        [(argv, name, _where, _close)] = self.zellij.opened
        self.assertEqual((bare(argv)[-1], name), (NEIGHBOUR, "codex 55556666 build fix (fork)"))

    def test_a_new_codex_window_takes_its_threads_name_once_listed(self) -> None:
        workdir = self.home / "w"
        workdir.mkdir()
        result = self.backend.new_session("codex", str(workdir))
        self.assertTrue(result.ok, result.message)
        [(argv, name, _where, _close)] = self.zellij.opened
        self.assertEqual((bare(argv)[-2:], name), (["-C", str(workdir)], "codex new w"))
        self.zellij.pane_list = [{"id": 9, "is_plugin": False, "title": name}]
        self.codex.threads = [thread(THREAD, "active", flags=[], cwd=str(workdir), createdAt=self.now + 3,
                                     preview="hello")]
        self.assertEqual(self.rows()[THREAD].state, sessions.RUNNING)  # Seen; named on this same pass.
        self.assertIn(("terminal_9", "codex 6a877e7d hello"), self.zellij.renamed)

    def test_stop_is_only_for_claude(self) -> None:
        self.codex.threads = [thread(THREAD, "idle")]
        self.rows()
        self.assertIn("unloads an idle thread", self.backend.stop_session(THREAD).message)

    def test_pi_sessions_open_under_reins_pi_and_new_ones_get_a_file(self) -> None:
        workdir = self.home / "w"
        workdir.mkdir()
        path = write_pi_session(self.pi_profile / "sessions" / "--w--", THREAD, str(workdir), user("plot it"))
        session = pi_channel.canonical_session(str(path))
        row = self.rows()[session]
        self.assertEqual((row.agent, row.title, row.state), ("pi", "plot it", sessions.CLOSED))
        result = self.backend.open_session(session)
        self.assertTrue(result.ok, result.message)
        [(argv, name, _where, _close)] = self.zellij.opened
        self.assertEqual(bare(argv)[-5:], ["pi", "--cwd", str(workdir), "--session", session])
        self.assertEqual(name, "pi 6a877e7d plot it")
        # Its Pi process is the pane's: with the pane open, the live owner counts as here.
        owner = pi_channel.channel_dir(session, self.pi_profile / "harness-reins" / "channels") / "owner.json"
        write(owner, {"version": 1, "owner_pid": os.getpid(), "state": "active"})
        self.zellij.pane_list = [{"id": 9, "is_plugin": False, "title": name}]
        self.assertEqual(self.rows()[session].state, sessions.ATTACHED)
        self.zellij.pane_list = []
        self.assertEqual(self.rows()[session].state, sessions.FOREIGN)
        self.assertIn("another terminal", self.backend.open_session(session).message)
        with mock.patch.dict(os.environ, {pi_channel.PROFILE_ENV: str(self.pi_profile)}):
            result = self.backend.new_session("pi", str(workdir))
        self.assertTrue(result.ok, result.message)
        new = bare(self.zellij.opened[-1][0])
        self.assertTrue(Path(new[-1]).is_file())
        self.assertEqual(self.zellij.opened[-1][1], "pi %s new in w" % pi_sessions.handle(new[-1]))

    def test_dsh_sessions_are_listed_from_the_address_book_but_not_opened(self) -> None:
        write(self.root.parent / "link" / "sessions" / "desk.json",
              {"schema": "reins.session/v1", "name": "desk", "harness": "dsh", "session": "session-abc",
               "cwd": "/w", "registered_at": "2026-11-17T10:00:00Z", "route": {}})
        with mock.patch.object(live.link, "sessions", return_value=[
                {"name": "desk", "harness": "dsh", "session": "session-abc", "cwd": "/w",
                 "registered_at": "2026-11-17T10:00:00Z"}]):
            row = self.rows()["session-abc"]
            self.assertEqual((row.agent, row.address, row.state), ("dsh", "desk", sessions.CLOSED))
            self.assertIn("no terminal window", self.backend.open_session("session-abc").message)
        self.assertIn("no terminal window", self.backend.new_session("dsh", str(self.home)).message)

    def test_closed_codex_and_pi_sessions_come_from_their_tasks(self) -> None:
        self.task("t1", THREAD, agent="codex")
        row = self.rows()[THREAD]
        self.assertEqual((row.agent, row.state), ("codex", sessions.CLOSED))


class ReopenableTests(unittest.TestCase):
    def test_what_closing_the_screen_would_lose(self) -> None:
        env = f"env -u {live.USER_CACHE_ENV} -u {live.LAUNCH_ENV} XDG_CACHE_HOME=/c "
        self.assertTrue(launch.reopenable(env + "/x/claude attach 08e8651e"))
        self.assertTrue(launch.reopenable(env + f"/x/codex resume --remote unix:///s {THREAD}"))
        self.assertTrue(launch.reopenable(env + "/p/python /p/_entry.py log 1234abcd"))
        self.assertTrue(launch.reopenable("/p/python /p/_entry.py _tui-panel"))
        self.assertFalse(launch.reopenable(env + "/p/python /p/_entry.py pi --cwd /w --session /s.jsonl"))
        self.assertFalse(launch.reopenable(env + "/bin/bash"))
        # A shell command that merely mentions the words is still the user's (K3, PR 11).
        self.assertFalse(launch.reopenable("/bin/bash -c 'tail -f log | claude attach'"))
        self.assertFalse(launch.reopenable("vim attach notes"))


class CodexRouteTests(unittest.TestCase):
    def payload(self, **request):
        base = {"id": "t1-c1", "agent": "codex", "callback_mode": "cli", "requires": [capabilities.CLI_RESUME],
                "target": {"kind": "session", "session": THREAD}}
        return {"request": {**base, **request}, "timeout": 10}

    def test_a_thread_loaded_on_the_daemon_gets_the_queue(self) -> None:
        with mock.patch.object(threads, "loaded_on_daemon", return_value=True):
            hosted = native_queue.hosted_request(self.payload())
        self.assertEqual(hosted["callback_mode"], "codex-queue")
        self.assertIn(capabilities.CODEX_QUEUE, hosted["requires"])
        native_queue.validate_route(hosted)

    def test_everything_else_keeps_its_route(self) -> None:
        with mock.patch.object(threads, "loaded_on_daemon", return_value=False):
            self.assertIsNone(native_queue.hosted_request(self.payload()))
        with mock.patch.object(threads, "loaded_on_daemon", return_value=True):
            self.assertIsNone(native_queue.hosted_request(self.payload(agent="claude")))
            self.assertIsNone(native_queue.hosted_request(self.payload(callback_mode="codex-queue")))
            self.assertIsNone(native_queue.hosted_request(self.payload(target={"kind": "cwd"})))
        with mock.patch.object(threads, "loaded_on_daemon", side_effect=app_server.AppServerProtocolError("down")):
            self.assertIsNone(native_queue.hosted_request(self.payload()))


if __name__ == "__main__":
    unittest.main()
