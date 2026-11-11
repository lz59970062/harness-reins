from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from harness_reins import cli
from harness_reins.commands import logs


class _Out(io.StringIO):
    """A text stdout with a binary buffer, like the real one."""

    def __init__(self) -> None:
        super().__init__()
        self.buffer = io.BytesIO()


class TaskLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = Path(self.tmp.name) / "queue"
        self.tasks = Path(self.tmp.name) / "tasks"

    def task(self, task_id: str, state: str = "completed", output: bytes | None = b"", **extra: object) -> Path:
        directory = self.tasks / task_id
        directory.mkdir(parents=True)
        log_path = directory / "attempt-1.log"
        if output is not None:
            log_path.write_bytes(output)
        record = {"id": task_id, "state": state, "log_path": str(log_path), **extra}
        (directory / "task.json").write_text(json.dumps(record), encoding="utf-8")
        return directory

    def run_log(self, task_id: str, *, lines: int = 10, follow: bool = False,
                whole: bool = False) -> tuple[int, bytes, str]:
        args = argparse.Namespace(task_id=task_id, lines=lines, all=whole, no_follow=not follow,
                                  queue_dir=str(self.queue))
        out, err = _Out(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = logs.log(args)
        return code, out.buffer.getvalue(), err.getvalue()

    def test_finds_exact_prefix_and_callback_ids(self) -> None:
        self.task("ab12cd34")
        self.task("ab99ff00")
        self.assertEqual(logs.find_task(self.tasks, "ab12cd34")[0], self.tasks / "ab12cd34")
        self.assertEqual(logs.find_task(self.tasks, "ab12")[0], self.tasks / "ab12cd34")
        self.assertEqual(logs.find_task(self.tasks, "ab12cd34-c1")[0], self.tasks / "ab12cd34")
        self.assertEqual(logs.find_task(self.tasks, "ab12cd34-a2")[0], self.tasks / "ab12cd34")
        directory, problem = logs.find_task(self.tasks, "ab")
        self.assertIsNone(directory)
        self.assertIn("ab12cd34, ab99ff00", problem)
        self.assertIn("no task", logs.find_task(self.tasks, "ff")[1])
        self.assertIn("not a task id", logs.find_task(self.tasks, "../ab12cd34")[1])
        self.assertIn("not a task id", logs.find_task(self.tasks, "ab*")[1])

    def test_tail_offset_counts_lines_like_tail(self) -> None:
        path = Path(self.tmp.name) / "out.log"
        for content, lines, expected in (
            (b"a\nb\nc\n", 2, b"b\nc\n"),
            (b"a\nb\nc", 2, b"b\nc"),
            (b"a\nb\nc\n", 10, b"a\nb\nc\n"),
            (b"a\nb\nc\n", 0, b""),
            (b"", 3, b""),
            (b"\n\n\n", 2, b"\n\n"),
        ):
            path.write_bytes(content)
            with self.subTest(content=content, lines=lines):
                self.assertEqual(content[logs.tail_offset(path, lines):], expected)

    def test_tail_offset_crosses_read_blocks(self) -> None:
        path = Path(self.tmp.name) / "big.log"
        rows = [f"line {index:06d} ".ljust(100, "x").encode() + b"\n" for index in range(3000)]
        path.write_bytes(b"".join(rows))
        self.assertEqual(path.read_bytes()[logs.tail_offset(path, 1500):], b"".join(rows[-1500:]))

    def test_prints_the_last_lines_of_a_finished_task(self) -> None:
        self.task("ab12cd34", output="".join(f"{n}\n" for n in range(20)).encode())
        code, out, err = self.run_log("ab12", lines=3)
        self.assertEqual(code, 0)
        self.assertEqual(out, b"17\n18\n19\n")
        self.assertEqual(err, "")

    def test_all_prints_the_whole_log(self) -> None:
        output = "".join(f"{n}\n" for n in range(20)).encode()
        self.task("ab12cd34", output=output)
        self.assertEqual(self.run_log("ab12cd34", whole=True)[1], output)
        out = _Out()
        argv = ["reins", "log", "ab12cd34", "--all", "--no-follow", "--queue-dir", str(self.queue)]
        with mock.patch("sys.argv", argv), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(out.buffer.getvalue(), output)

    def test_follow_stops_when_the_task_ends(self) -> None:
        directory = self.task("ab12cd34", state="running", output=b"first\n")
        log_path = directory / "attempt-1.log"

        def finish() -> None:
            time.sleep(0.1)
            with log_path.open("ab") as handle:
                handle.write(b"second\n")
            time.sleep(0.1)
            with log_path.open("ab") as handle:
                handle.write(b"last\n")
            record = json.loads((directory / "task.json").read_text())
            record.update(state="completed", exit_code=3)
            (directory / "task.json").write_text(json.dumps(record))

        writer = threading.Thread(target=finish)
        with mock.patch.object(logs, "POLL_SECONDS", 0.02):
            writer.start()
            code, out, err = self.run_log("ab12cd34", follow=True)
        writer.join()
        self.assertEqual(code, 0)
        self.assertEqual(out, b"first\nsecond\nlast\n")
        self.assertIn("ended: completed, exit code 3", err)

    def test_follow_waits_for_the_log_to_appear(self) -> None:
        directory = self.task("ab12cd34", state="launching", output=None)

        def start() -> None:
            time.sleep(0.1)
            (directory / "attempt-1.log").write_bytes(b"hello\n")
            record = json.loads((directory / "task.json").read_text())
            record.update(state="completed", exit_code=0)
            (directory / "task.json").write_text(json.dumps(record))

        writer = threading.Thread(target=start)
        with mock.patch.object(logs, "POLL_SECONDS", 0.02):
            writer.start()
            code, out, _ = self.run_log("ab12cd34", follow=True)
        writer.join()
        self.assertEqual((code, out), (0, b"hello\n"))

    def test_a_finished_task_without_output_says_so(self) -> None:
        self.task("ab12cd34", state="launch_failed", output=None)
        code, out, err = self.run_log("ab12cd34", follow=True)
        self.assertEqual((code, out), (1, b""))
        self.assertIn("no output yet (launch_failed)", err)

    def test_unknown_task_is_an_error(self) -> None:
        self.task("ab12cd34")
        code, _, err = self.run_log("ffff")
        self.assertEqual(code, 2)
        self.assertIn("reins: error: no task ffff", err)

    def test_cli_runs_log_without_forwarding(self) -> None:
        self.assertIn("log", cli.LOCAL_MODES)
        self.task("ab12cd34", output=b"x\ny\n")
        out = _Out()
        argv = ["reins", "log", "ab12", "-n", "1", "--no-follow", "--queue-dir", str(self.queue)]
        with mock.patch("sys.argv", argv), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(out.buffer.getvalue(), b"y\n")


if __name__ == "__main__":
    unittest.main()
