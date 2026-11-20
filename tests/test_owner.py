"""reins owner: which session started a Reins task, by its name in the Reins address book."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness_reins.commands import owner as owner_command
from harness_reins.store import layout, link

SESSION = "c52a489f-2db0-432f-9b52-75c1b26d358e"


class OwnerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.queue = Path(self.temporary.name) / "queue"
        self.queue.mkdir()
        self.book = [{"name": "gsmerge-claude", "harness": "claude", "session": SESSION}]
        patcher = mock.patch.object(link, "sessions", lambda queue: list(self.book))
        patcher.start()
        self.addCleanup(patcher.stop)

    def task(self, task_id: str, **fields) -> Path:
        directory = layout.managed_task_dir(self.queue, task_id)
        directory.mkdir(parents=True)
        log = directory / "attempt-1.log"
        log.write_text("output\n")
        record = {"id": task_id, "task": f"job {task_id}", "state": "running", "command": "python serve.py",
                  "cwd": "/work", "queue_dir": str(self.queue), "log_path": str(log), "agent": "claude",
                  "origin": {"harness": "claude", "session": SESSION, "cwd": "/work"},
                  "target": {"kind": "session", "session": SESSION}, "started_at": 1000.0}
        record.update(fields)
        (directory / "task.json").write_text(json.dumps(record))
        return directory

    def run_owner(self, **options):
        args = argparse.Namespace(task_id=None, json=False, queue_dir=str(self.queue))
        args.__dict__.update(options)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = owner_command.owner(args)
        return code, out.getvalue(), err.getvalue()

    def session_of(self, task_id: str):
        code, out, _ = self.run_owner(task_id=task_id, json=True)
        self.assertEqual(code, 0)
        return json.loads(out)["tasks"][0]["session"]


class LookupTests(OwnerCase):
    def test_a_task_names_the_session_that_started_it(self):
        self.task("29037999")
        self.assertEqual(self.session_of("29037999"),
                         {"harness": "claude", "id": SESSION, "name": "gsmerge-claude", "cwd": "/work"})

    def test_the_name_is_read_when_asked_so_a_rename_shows(self):
        self.task("29037999")
        self.book[0]["name"] = "renamed"
        self.assertEqual(self.session_of("29037999")["name"], "renamed")

    def test_an_unnamed_session_has_no_name(self):
        self.task("29037999")
        self.book.clear()
        self.assertIsNone(self.session_of("29037999")["name"])

    def test_without_an_origin_the_callback_target_is_the_session(self):
        self.task("29037999", origin=None)
        self.assertEqual(self.session_of("29037999")["id"], SESSION)

    def test_a_task_with_no_session_says_so(self):
        self.task("29037999", origin=None, target={"kind": "none"})
        self.assertIsNone(self.session_of("29037999"))

    def test_a_prefix_or_callback_id_finds_the_task(self):
        self.task("29037999")
        self.assertEqual(self.session_of("2903")["id"], SESSION)
        self.assertEqual(self.session_of("29037999-c1")["id"], SESSION)

    def test_tasks_that_have_not_ended_are_listed_longest_quiet_first(self):
        fresh = self.task("aaaaaaaa")
        stale = self.task("bbbbbbbb", state="submitted")
        self.task("cccccccc", state="completed")
        os.utime(stale / "attempt-1.log", (100, 100))
        os.utime(fresh / "attempt-1.log", (200, 200))
        code, out, _ = self.run_owner(json=True)
        self.assertEqual([task["id"] for task in json.loads(out)["tasks"]], ["bbbbbbbb", "aaaaaaaa"])


class CommandTests(OwnerCase):
    def test_one_task_shows_who_started_it_and_how_to_reach_it(self):
        self.task("29037999")
        code, out, _ = self.run_owner(task_id="29037999")
        self.assertEqual(code, 0)
        for text in ("Task 29037999  running for", "Started by Claude Code session c52a489f  @gsmerge-claude",
                     "reins log 29037999", "reins send gsmerge-claude", "reins tui 29037999"):
            self.assertIn(text, out)

    def test_an_unnamed_owner_gets_no_send_hint(self):
        self.task("29037999")
        self.book.clear()
        _, out, _ = self.run_owner(task_id="29037999")
        self.assertIn("(no name)", out)
        self.assertNotIn("reins send", out)

    def test_json_has_the_schema(self):
        self.task("29037999")
        _, out, _ = self.run_owner(task_id="29037999", json=True)
        self.assertEqual(json.loads(out)["schema"], "reins.owner/1")

    def test_a_missing_task_fails_with_a_message(self):
        code, _, err = self.run_owner(task_id="ffffffff")
        self.assertEqual(code, 1)
        self.assertIn("no task ffffffff", err)

    def test_no_task_id_prints_the_table(self):
        self.task("29037999")
        code, out, _ = self.run_owner()
        self.assertEqual(code, 0)
        self.assertIn("STARTED BY", out)
        self.assertIn("@gsmerge-claude (claude)", out)


class AgeTests(unittest.TestCase):
    def test_ages_read_naturally(self):
        self.assertEqual(owner_command.age(0, 30), "30 s")
        self.assertEqual(owner_command.age(0, 47 * 60), "47 min")
        self.assertEqual(owner_command.age(0, 3 * 3600 + 12 * 60), "3 h 12 min")
        self.assertEqual(owner_command.age(0, 3 * 86400), "3 days")
        self.assertEqual(owner_command.age(None, 0), "unknown")


if __name__ == "__main__":
    unittest.main()
