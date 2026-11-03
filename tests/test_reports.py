"""Child agent reports: validated claims, kept apart from what Reins observed."""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import reins_records
from harness_reins.commands import work as work_commands
from harness_reins.core import ids, results
from harness_reins.engine import dispatch
from harness_reins.engine import tasks as managed_tasks
from harness_reins.engine import worker as task_worker
from harness_reins.store import layout
from test_cli import executable_python_fixture, patch_fixture_commands

CHILD = """\
#!{python}
import json, os, sys
from pathlib import Path
prompt = sys.stdin.read()
report_file = os.environ.get("REINS_REPORT_FILE")
Path(sys.argv[sys.argv.index("-o") + 1]).write_text(
    json.dumps({{"prompt": prompt, "report_file": report_file}}), encoding="utf-8")
REPORT = {report!r}
if REPORT is not None and report_file:
    Path(report_file).write_text(REPORT, encoding="utf-8")
"""


class ChildReportTests(unittest.TestCase):
    def setUp(self) -> None:
        # Models screen selection as CliTests does; Windows IO and process
        # adapters still run natively because os.name remains unchanged.
        if os.name == "nt":
            platform = mock.patch.object(sys, "platform", "linux")
            platform.start()
            self.addCleanup(platform.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "queue"
        patch_fixture_commands(self)

    def child(self, report: str | None) -> Path:
        script = self.directory / "fake-codex"
        script.write_text(textwrap.dedent(CHILD.format(python=sys.executable, report=report)), encoding="utf-8")
        return executable_python_fixture(script)

    def submit(self, child: Path, *, no_report: bool = False, worker: str = "codex") -> tuple[Path, dict[str, object]]:
        args = argparse.Namespace(
            cwd=str(self.directory), task="write the guide", command=None, exit_code=None, message=None,
            session="parent-thread", via_daemon=False, queue_dir=str(self.root), approvals_reviewer="auto_review",
            approval_policy="on-request", sandbox_mode="workspace-write", dry_run=False, strict=False,
            wrapped_command=[], agent="codex", agent_worker=worker, agent_prompt=["--", "Write the guide."],
            permission_mode="auto", no_report=no_report)
        with mock.patch.object(managed_tasks, "screen_binary", return_value="/usr/bin/screen"), \
                mock.patch.dict(os.environ, {"REINS_CODEX_BIN": str(child), "REINS_DSH_BIN": str(child)}), mock.patch("sys.stderr"):
            self.assertEqual(work_commands.agent(args), 0)
        path = next(layout.managed_tasks_root(self.root).glob("*/task.json"))
        task = managed_tasks.load_managed_task(path)
        task["state"] = "launching"
        managed_tasks.write_managed_task(self.root, task)
        return path, task

    def run_worker(self, path: Path, task: dict[str, object]) -> dict[str, object]:
        with mock.patch.dict(os.environ, {"STY": "screen-session"}, clear=True):
            self.assertEqual(task_worker.run_screen_worker(argparse.Namespace(task_file=str(path), token=task["token"])), 0)
        return json.loads(layout.managed_result_path(self.root, str(task["id"])).read_text(encoding="utf-8"))

    def callback(self, task: dict[str, object]) -> dict[str, object]:
        return dispatch.load_request(layout.request_path(self.root, "pending", ids.callback_id(str(task["id"]))))

    def test_report_is_validated_into_the_result_and_shown_as_a_claim(self):
        report = json.dumps({"status": "completed", "summary": "Wrote the migration guide.",
                             "artifacts": [{"path": "docs/guide.md", "description": "the guide"}]})
        path, task = self.submit(self.child(report))
        result = self.run_worker(path, task)
        self.assertEqual(result["schema"], results.RESULT_SCHEMA)
        self.assertEqual(result["observed"]["state"], "exited")
        self.assertEqual(result["observed"]["exit_code"], 0)
        self.assertEqual(result["report"]["status"], "completed")
        self.assertEqual(result["report"]["artifacts"], [{"path": "docs/guide.md", "description": "the guide"}])
        self.assertEqual(result["report_source"], "report-file")
        self.assertIsNone(result["report_error"])
        self.assertEqual(result["worker"]["harness"], "codex")
        compact = self.callback(task)["prompt_compact"]
        self.assertIn("Report: completed (claimed, unverified) · 1 artifact\n", compact)
        self.assertRegex(compact, r"Files: .+ \(attempt-1\.log, agent-result\.txt, result\.json\)")

    def test_a_missing_report_never_fails_the_task(self):
        path, task = self.submit(self.child(None))
        result = self.run_worker(path, task)
        self.assertEqual(managed_tasks.load_managed_task(path)["state"], "completed")
        self.assertEqual(result["observed"]["exit_code"], 0)
        self.assertIsNone(result["report"])
        self.assertEqual(result["report_error"], "no report was written")
        self.assertIn("Report: none: no report was written", self.callback(task)["prompt_compact"])

    def test_dsh_children_are_not_asked_for_a_report_they_cannot_write(self):
        # The headless profile confines writes to the workspace with no approval channel.
        _, task = self.submit(self.child('{"status": "completed"}'), worker="dsh")
        self.assertNotIn("report_path", task)
        self.assertNotIn("REINS_REPORT_FILE", Path(str(task["agent_prompt_path"])).read_text(encoding="utf-8"))

    def test_no_report_sends_the_prompt_alone(self):
        path, task = self.submit(self.child('{"status": "completed"}'), no_report=True)
        self.assertNotIn("report_path", task)
        self.assertEqual(Path(str(task["agent_prompt_path"])).read_text(encoding="utf-8"), "Write the guide.")
        result = self.run_worker(path, task)
        seen = json.loads(Path(str(task["agent_result_path"])).read_text(encoding="utf-8"))
        self.assertIsNone(seen["report_file"])
        self.assertIsNone(result["report"])
        self.assertIsNone(result["report_error"])
        self.assertNotIn("Report:", self.callback(task)["prompt_compact"])

    def test_a_report_left_by_an_earlier_attempt_is_not_read(self):
        path, task = self.submit(self.child(None))
        Path(str(task["report_path"])).write_text('{"status": "completed"}', encoding="utf-8")
        result = self.run_worker(path, task)
        self.assertIsNone(result["report"])
        self.assertEqual(result["report_error"], "no report was written")

    def test_the_child_learns_where_to_write_its_report(self):
        path, task = self.submit(self.child(None))
        self.run_worker(path, task)
        seen = json.loads(Path(str(task["agent_result_path"])).read_text(encoding="utf-8"))
        self.assertEqual(seen["report_file"], task["report_path"])
        self.assertEqual(seen["prompt"], reins_records.agent_prompt("Write the guide.", task))
        self.assertIn(str(task["report_path"]), seen["prompt"])


class ReportParsingTests(unittest.TestCase):
    def test_reports_are_bounded_and_problems_are_named(self):
        report, problem = results.parse_report(json.dumps({
            "status": "partial", "summary": "x" * (results.SUMMARY_LIMIT + 5),
            "artifacts": [{"description": "no path"}, {"path": "a.py"}] + [{"path": f"f{i}"} for i in range(60)],
            "questions": ["why?"], "next_steps": "not a list"}))
        self.assertEqual(report["status"], "partial")
        self.assertEqual(len(report["summary"]), results.SUMMARY_LIMIT)
        self.assertEqual(len(report["artifacts"]), results.ITEM_LIMIT - 1)
        self.assertEqual(report["questions"], ["why?"])
        self.assertEqual(report["next_steps"], [])
        for expected in ("summary truncated", "artifacts truncated", "without a path", "next_steps is not a list"):
            self.assertIn(expected, problem)

    def test_unusable_reports_are_refused_with_a_reason(self):
        for text, reason in ((None, "no report"), ("not json", "not valid JSON"), ("[]", "not a JSON object"),
                             ('{"status": "done"}', "status must be one of")):
            with self.subTest(text=text):
                report, problem = results.parse_report(text)
                self.assertIsNone(report)
                self.assertIn(reason, problem)

    def test_timestamps_round_trip_at_millisecond_precision(self):
        stamp = results.timestamp(1760400000.1234)
        self.assertEqual(stamp, "2025-10-14T00:00:00.123Z")
        self.assertAlmostEqual(results.seconds(stamp), 1760400000.123, places=3)
        self.assertIsNone(results.timestamp(None))
        self.assertIsNone(results.seconds("yesterday"))


if __name__ == "__main__":
    unittest.main()


class UnfinishedResultTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "queue"
        layout.managed_task_dir(self.root, "1a2b3c4d").mkdir(parents=True)
        self.task = {"id": "1a2b3c4d", "execution_backend": "systemd-user", "started_at": 100.0}

    def result(self) -> dict[str, object]:
        return json.loads(layout.managed_result_path(self.root, "1a2b3c4d").read_text(encoding="utf-8"))

    def test_interruptions_are_recorded_with_their_reason(self):
        for reason, state in (("interrupted_by_host_reboot", "interrupted"),
                              ("execution_launch_outcome_unknown", "unknown")):
            with self.subTest(reason=reason):
                layout.managed_result_path(self.root, "1a2b3c4d").unlink(missing_ok=True)
                managed_tasks.record_unfinished_result(self.root, self.task, state, reason, 200.0)
                result = results.validate(self.result(), "1a2b3c4d")
                self.assertEqual(result["observed"]["state"], state)
                self.assertEqual(result["observed"]["recovery_reason"], reason)
                self.assertIsNone(result["observed"]["exit_code"])
                with self.assertRaisesRegex(ValueError, "observed exit"):
                    results.observed_exit(result, "1a2b3c4d")

    def test_an_observed_exit_is_never_overwritten(self):
        exited = reins_records.result_record(self.task, exit_code=3, ended_at=150.0)
        layout.managed_result_path(self.root, "1a2b3c4d").write_text(json.dumps(exited), encoding="utf-8")
        managed_tasks.record_unfinished_result(self.root, self.task, "interrupted", "late_reboot", 200.0)
        self.assertEqual(results.observed_exit(self.result(), "1a2b3c4d"), 3)

    def test_unfinished_states_are_closed(self):
        with self.assertRaises(ValueError):
            results.ended(self.task, state="exited", recovery_reason="x", at=1.0)
