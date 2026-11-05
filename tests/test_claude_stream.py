from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from harness_reins.engine import delivery as delivery_engine
from harness_reins.engine import dispatch
from harness_reins.harness.claude import session as claude_session
from harness_reins.harness.claude import stream as claude_stream
from harness_reins.store import layout
from harness_reins.store import records as queue_store

SESSION = "3f0c9a52-claude-session"
OTHER_SESSION = "77aa01b2-claude-session"

# Writes each message it is given to a log, then does what the scenario in argv says.
FAKE_CLAUDE = textwrap.dedent("""\
    import json, sys, time
    log, scenario = sys.argv[1:3]
    def emit(**event):
        print(json.dumps(event), flush=True)
    for line in sys.stdin:
        with open(log, "a", encoding="utf-8") as stream:
            stream.write(json.loads(line)["message"]["content"] + chr(10))
        if scenario == "silent":
            continue
        emit(type="system", subtype="init")
        if scenario == "background":
            emit(type="system", subtype="background_tasks_changed", tasks=[{"task_id": "b1"}])
            emit(type="result", subtype="success", result="started")
            time.sleep(1.0)
            with open(log, "a", encoding="utf-8") as stream:
                stream.write("background finished" + chr(10))
            emit(type="system", subtype="background_tasks_changed", tasks=[])
        emit(type="result", subtype="success", result="done")
    with open(log, "a", encoding="utf-8") as stream:
        stream.write("stdin closed" + chr(10))
    """)


class StreamCase(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "queue"
        patcher = mock.patch.dict(os.environ, {
            "REINS_HOME": str(self.directory / "reins-home"), "CLAUDE_CONFIG_DIR": str(self.directory / "claude"),
            layout.TARGET_LOCK_DIR_ENV: str(self.directory / "target-locks"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        queue_store.ensure_daemon_dirs(self.root)
        self.script = self.directory / "fake_claude.py"
        self.script.write_text(FAKE_CLAUDE, encoding="utf-8")
        self.log = self.directory / "fake.log"
        for name, value in (("POLL_SECONDS", 0.02), ("QUIET_SECONDS", 0.3)):
            speed = mock.patch.object(claude_stream, name, value)
            speed.start()
            self.addCleanup(speed.stop)

    def enqueue(self, ident: str, *, session: str = SESSION, **fields: object) -> Path:
        args = argparse.Namespace(
            agent="claude", session=session, last=False, task="train model", cwd=str(self.directory),
            command="python train.py", exit_code=0, message=None, queue_dir=str(self.root), _callback_id=ident,
        )
        prompt = dispatch.build_prompt(args, duration=12.0)
        request = dispatch.make_request(args, prompt)
        request.update(fields)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dispatch.enqueue_existing_request(self.root, request, prompt), 0)
        return layout.request_path(self.root, "pending", ident)

    def start(self, scenario: str = "normal") -> subprocess.Popen[str]:
        process = subprocess.Popen([sys.executable, str(self.script), str(self.log), scenario],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8")
        self.addCleanup(self.finish, process)
        return process

    @staticmethod
    def finish(process: subprocess.Popen[str]) -> None:
        process.kill()
        process.wait()
        process.stdout.close()
        with contextlib.suppress(OSError):
            process.stdin.close()

    def supervise(self, process: subprocess.Popen[str], timeout: float | None = 30.0) -> int | None:
        with mock.patch.dict(os.environ, {claude_session.RESUMED_TARGET_ENV: f"session:{SESSION}"}), \
                contextlib.redirect_stderr(io.StringIO()):
            return claude_stream.supervise(process, "first callback", self.root, SESSION, timeout)

    def logged(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


class SuperviseTests(StreamCase):
    def test_a_callback_due_while_it_runs_goes_into_the_same_process(self) -> None:
        self.enqueue("cb-later")
        self.assertEqual(self.supervise(self.start()), 0)
        lines = self.logged()
        self.assertEqual(lines[0], "first callback")
        self.assertIn("cb-later", "\n".join(lines[1:]))
        self.assertEqual(lines[-1], "stdin closed")

    def test_it_leaves_another_sessions_callback_alone(self) -> None:
        other = self.enqueue("cb-other", session=OTHER_SESSION)
        self.assertEqual(self.supervise(self.start()), 0)
        self.assertNotIn("cb-other", "\n".join(self.logged()))
        self.assertNotIn("live_delivered_at", json.loads(other.read_text(encoding="utf-8")))

    def test_a_manual_callback_waits_to_be_asked_for(self) -> None:
        self.enqueue("cb-manual", callback_mode="manual")
        self.assertEqual(self.supervise(self.start()), 0)
        self.assertNotIn("cb-manual", "\n".join(self.logged()))

    def test_stdin_stays_open_until_background_work_is_finished(self) -> None:
        self.assertEqual(self.supervise(self.start("background")), 0)
        lines = self.logged()
        self.assertLess(lines.index("background finished"), lines.index("stdin closed"))

    def test_an_unacknowledged_callback_is_due_as_soon_as_the_resume_ends(self) -> None:
        path = self.enqueue("cb-later")
        self.assertEqual(self.supervise(self.start()), 0)
        request = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("next_attempt_at", request)
        self.assertNotIn("live_delivered_at", request)
        self.assertEqual(delivery_engine.select_pending(self.root, time.time()), path)

    def test_an_acknowledged_callback_stays_finished(self) -> None:
        path = self.enqueue("cb-later")
        real_claim = claude_session.claim_for_resume

        def claim_and_ack(root: Path, session: str) -> list[tuple[str, str]]:
            claimed = real_claim(root, session)
            if claimed:
                layout.ack_path(root, "cb-later").write_text("{}", encoding="utf-8")
            return claimed

        with mock.patch.object(claude_session, "claim_for_resume", side_effect=claim_and_ack):
            self.assertEqual(self.supervise(self.start()), 0)
        self.assertIn("live_delivered_at", json.loads(path.read_text(encoding="utf-8")))

    def test_a_cli_that_refuses_the_flags_is_reported_for_the_plain_prompt(self) -> None:
        process = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(1)"], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, text=True)
        self.addCleanup(self.finish, process)
        self.assertIsNone(self.supervise(process))

    def test_a_cli_that_says_nothing_still_gets_its_stdin_closed(self) -> None:
        with mock.patch.object(claude_stream, "FIRST_EVENT_SECONDS", 0.2):
            self.assertEqual(self.supervise(self.start("silent")), 0)
        self.assertEqual(self.logged(), ["first callback", "stdin closed"])

    def test_a_limit_is_only_applied_when_one_is_set(self) -> None:
        process = self.start("background")
        with self.assertRaises(subprocess.TimeoutExpired):
            self.supervise(process, timeout=0.2)


@unittest.skipIf(os.name == "nt", "the fixture is a shebang script")
class WorkerTests(StreamCase):
    def test_the_worker_resumes_through_the_stream_and_finishes_both_callbacks(self) -> None:
        claude = self.directory / "claude-bin"
        # The first callback ACKs at once; the one that arrives later is ACKed when it comes in.
        claude.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json, pathlib, re, sys, time
            acks = pathlib.Path({str(self.root)!r}) / "acks"
            log = pathlib.Path({str(self.log)!r})
            log.write_text(" ".join(sys.argv[1:]) + chr(10))
            for line in sys.stdin:
                text = json.loads(line)["message"]["content"]
                for ident in re.findall(r"cb-[a-z]+", text)[:1]:
                    with log.open("a") as stream:
                        stream.write("got " + ident + chr(10))
                    (acks / (ident + ".json")).write_text("{{}}")
                print(json.dumps({{"type": "system", "subtype": "init"}}), flush=True)
                print(json.dumps({{"type": "result", "subtype": "success"}}), flush=True)
            """), encoding="utf-8")
        claude.chmod(0o755)
        self.enqueue("cb-first")
        self.enqueue("cb-second")
        args = argparse.Namespace(resume_timeout=0, retries=3, retry_delay=0, retry_backoff=1)
        environment = {"REINS_CLAUDE_BIN": str(claude), "PYTHONPATH": str(Path(delivery_engine.__file__).parents[2])}
        with mock.patch.dict(os.environ, environment), contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(delivery_engine, "resume_cwd", return_value=str(self.directory)):
            self.assertTrue(delivery_engine.process_one(self.root, args))
        deadline = time.monotonic() + 30
        while "got cb-second" not in self.log.read_text(encoding="utf-8") and time.monotonic() < deadline:
            time.sleep(0.05)
        lines = self.log.read_text(encoding="utf-8").splitlines()
        self.assertIn("--input-format stream-json", lines[0])
        self.assertEqual(lines[1:], ["got cb-first", "got cb-second"])


class ParallelDeliveryTests(StreamCase):
    def setUp(self) -> None:
        super().setUp()
        self.started: list[str] = []
        self.release = threading.Event()
        self.addCleanup(self.release.set)

        def hold(root: Path, args: argparse.Namespace, path: Path) -> bool:
            self.started.append(path.stem)
            self.release.wait(10)
            queue_store.move_request(path, root / "done")
            return True

        patcher = mock.patch.object(delivery_engine, "process_selected", side_effect=hold)
        patcher.start()
        self.addCleanup(patcher.stop)

    def wait_for(self, *names: str) -> None:
        deadline = time.monotonic() + 5
        while not set(names) <= set(self.started) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertLessEqual(set(names), set(self.started))

    def drain(self) -> None:
        self.release.set()
        deadline = time.monotonic() + 5
        while delivery_engine.deliveries_active() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(delivery_engine.deliveries_active())

    def test_a_long_resume_holds_its_own_session_and_no_other(self) -> None:
        self.enqueue("cb-a1")
        self.enqueue("cb-a2")
        self.enqueue("cb-b1", session=OTHER_SESSION)
        args = argparse.Namespace()
        self.assertTrue(delivery_engine.start_one(self.root, args))
        self.assertTrue(delivery_engine.start_one(self.root, args))
        self.wait_for("cb-a1", "cb-b1")
        self.assertFalse(delivery_engine.start_one(self.root, args))
        self.assertNotIn("cb-a2", self.started)
        self.drain()
        self.assertTrue(delivery_engine.start_one(self.root, args))
        self.wait_for("cb-a2")
        self.drain()

    def test_recovery_leaves_a_callback_alone_while_its_thread_owns_it(self) -> None:
        path = self.enqueue("cb-a1")
        running = queue_store.move_request(path, self.root / "running")
        with mock.patch.dict(delivery_engine._ACTIVE_DELIVERIES, {"cb-a1": f"session:{SESSION}"}):
            delivery_engine.recover_running(self.root)
            self.assertTrue(running.exists())
        delivery_engine.recover_running(self.root)
        self.assertTrue(path.exists())


class ResumeLimitTests(unittest.TestCase):
    def test_no_limit_unless_one_is_set(self) -> None:
        self.assertEqual(delivery_engine.DEFAULT_RESUME_TIMEOUT, 0.0)
        self.assertIsNone(delivery_engine.resume_limit(0))
        self.assertIsNone(delivery_engine.resume_limit(None))
        self.assertEqual(delivery_engine.resume_limit(90), 90.0)
        self.assertEqual(delivery_engine.resume_limit(0.1), 1.0)


if __name__ == "__main__":
    unittest.main()
