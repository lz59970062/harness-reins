"""reins stop: the task's processes end, it never starts again, and its session is told not to restart it."""
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
import unittest
from pathlib import Path
from unittest import mock

from harness_reins.commands import stop as stop_command
from harness_reins.commands import work as work_commands
from harness_reins.engine import tasks as managed_tasks
from harness_reins.engine import worker as task_worker
from harness_reins.owners import OwnerState, SystemdUserBackend, host
from harness_reins.owners import linux
from harness_reins.store import layout

LINUX = sys.platform.startswith("linux")


def pending_text(root: Path) -> str:
    """Each queued callback's short prompt, as the session sees it first, then its details file."""
    texts = []
    for path in sorted((root / "pending").glob("*.json")):
        request = json.loads(path.read_text(encoding="utf-8"))
        texts += [request["prompt"], Path(request["prompt_details_path"]).read_text(encoding="utf-8")]
    return "\n".join(texts)


def short_prompt(root: Path) -> str:
    return "\n".join(json.loads(path.read_text(encoding="utf-8"))["prompt"]
                     for path in sorted((root / "pending").glob("*.json")))


class StopCase(unittest.TestCase):
    """A systemd task submitted through the real `reins run` path; no daemon or systemd is started."""

    def setUp(self) -> None:
        for patcher in (mock.patch.object(sys, "platform", "linux"),
                        mock.patch.object(SystemdUserBackend, "available", return_value=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "queue"

    def submit(self) -> tuple[Path, str]:
        args = argparse.Namespace(
            backend="systemd", agent="codex", cwd=str(self.directory), task="model server",
            command=None, exit_code=None, message=None, session="owner-session", last=False,
            via_daemon=False, queue_dir=str(self.root), approvals_reviewer="auto_review",
            approval_policy="on-request", sandbox_mode="workspace-write", dry_run=False,
            strict=False, wrapped_command=[sys.executable, "-c", "raise SystemExit(7)"],
        )
        with mock.patch.object(managed_tasks, "screen_binary", return_value=None), \
                mock.patch.object(SystemdUserBackend, "launch"), contextlib.redirect_stderr(io.StringIO()):
            work_commands.run(args)
        path = next(layout.managed_tasks_root(self.root).glob("*/task.json"))
        return path, path.parent.name

    def launch(self, path: Path) -> dict[str, object]:
        with mock.patch.object(SystemdUserBackend, "launch"), contextlib.redirect_stderr(io.StringIO()):
            managed_tasks.recover_managed_tasks(self.root)
        return managed_tasks.load_managed_task(path)

    def run_stop(self, task_id: str, **options):
        args = argparse.Namespace(task_id=task_id, message="free the GPU", yes=True, grace=1.0,
                                  queue_dir=str(self.root))
        args.__dict__.update(options)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = stop_command.stop(args)
        return code, out.getvalue(), err.getvalue()


class EngineTests(StopCase):
    def test_a_queued_task_never_starts_and_its_callback_says_the_user_stopped_it(self):
        path, task_id = self.submit()
        managed_tasks.write_stop_request(self.root, task_id, "free the GPU")
        with mock.patch.object(SystemdUserBackend, "launch") as launch:
            managed_tasks.recover_managed_tasks(self.root)
        launch.assert_not_called()
        task = managed_tasks.load_managed_task(path)
        self.assertEqual((task["state"], task["recovery_reason"]), ("interrupted", "stopped_by_user"))
        callback = pending_text(self.root)
        self.assertIn("stopped this task on purpose", callback)
        self.assertIn("free the GPU", callback)
        self.assertIn("Do not rerun, restart or replace it", callback)
        self.assertNotIn("recreate the task", callback)
        self.assertIn("Stopped: the user stopped this task on purpose", short_prompt(self.root))
        self.assertNotIn("before relaunching. Details", short_prompt(self.root))

    def test_a_vanished_owner_after_a_stop_is_recorded_as_stopped(self):
        path, task_id = self.submit()
        task = self.launch(path)
        task.update(state="running", boot_id=host.current_boot_id(), started_at=time.time())
        managed_tasks.write_managed_task(self.root, task)
        managed_tasks.write_stop_request(self.root, task_id, None)
        with mock.patch.object(SystemdUserBackend, "probe", return_value=OwnerState.ABSENT):
            managed_tasks.recover_managed_tasks(self.root)
        task = managed_tasks.load_managed_task(path)
        self.assertEqual(task["recovery_reason"], "stopped_by_user")
        self.assertIn("stopped this task on purpose", pending_text(self.root))

    def test_without_a_stop_the_callback_still_offers_to_recreate(self):
        path, _ = self.submit()
        task = self.launch(path)
        task.update(state="running", boot_id=host.current_boot_id(), started_at=time.time())
        managed_tasks.write_managed_task(self.root, task)
        with mock.patch.object(SystemdUserBackend, "probe", return_value=OwnerState.ABSENT):
            managed_tasks.recover_managed_tasks(self.root)
        self.assertIn("recreate the task", pending_text(self.root))

    def test_settling_waits_for_a_live_owner_and_a_recorded_exit(self):
        path, task_id = self.submit()
        task = self.launch(path)
        task.update(state="running", boot_id=host.current_boot_id(), started_at=time.time())
        managed_tasks.write_managed_task(self.root, task)
        managed_tasks.write_stop_request(self.root, task_id, None)
        with mock.patch.object(SystemdUserBackend, "probe", return_value=OwnerState.ALIVE):
            self.assertEqual(managed_tasks.settle_stopped_task(self.root, task_id), "running")
        layout.managed_result_path(self.root, task_id).write_text("{}")
        with mock.patch.object(SystemdUserBackend, "probe", return_value=OwnerState.ABSENT):
            self.assertEqual(managed_tasks.settle_stopped_task(self.root, task_id), "running")
        layout.managed_result_path(self.root, task_id).unlink()
        with mock.patch.object(SystemdUserBackend, "probe", return_value=OwnerState.ABSENT):
            self.assertEqual(managed_tasks.settle_stopped_task(self.root, task_id), "interrupted")

    def test_a_completed_stopped_task_still_says_the_user_stopped_it(self):
        path, task_id = self.submit()
        task = managed_tasks.load_managed_task(path)
        managed_tasks.write_stop_request(self.root, task_id, None)
        prompt = managed_tasks.managed_task_prompt(task, outcome="completed")
        self.assertIn("stopped this task on purpose", prompt)

    def run_worker(self, task_id: str, path: Path, *, stopped: bool):
        task = self.launch(path)
        environment = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(environment)))
        if stopped:
            managed_tasks.write_stop_request(self.root, task_id, None)
        with mock.patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as workload, \
                mock.patch.object(managed_tasks, "queue_managed_task_callback", return_value=True):
            code = task_worker.run_managed_worker_locked(self.root, task)
        return code, workload, dict(os.environ)

    def test_the_worker_never_starts_a_stopped_task(self):
        path, task_id = self.submit()
        code, workload, _ = self.run_worker(task_id, path, stopped=True)
        self.assertEqual(code, 0)
        workload.assert_not_called()
        self.assertEqual(managed_tasks.load_managed_task(path)["recovery_reason"], "stopped_by_user")

    def test_the_worker_marks_every_process_with_the_task_id(self):
        path, task_id = self.submit()
        _, workload, environment = self.run_worker(task_id, path, stopped=False)
        workload.assert_called_once()
        self.assertEqual(environment.get("REINS_TASK_ID"), task_id)


class MarkerTests(unittest.TestCase):
    def test_the_task_id_decides_and_old_tasks_fall_back_to_screen_or_unit(self):
        marked = stop_command.task_marker({"id": "abc", "screen_session": "reins-abc",
                                           "owner_invocation_id": "inv1"})
        self.assertTrue(marked(["REINS_TASK_ID=abc"]))
        self.assertTrue(marked(["STY=123.reins-abc"]))
        self.assertTrue(marked(["INVOCATION_ID=inv1"]))
        # A task started from inside this one carries its own id and is left alone.
        self.assertFalse(marked(["REINS_TASK_ID=other", "STY=123.reins-abc"]))
        self.assertFalse(marked(["STY=123.reins-abcd", "INVOCATION_ID=inv2", "PATH=/bin"]))
        self.assertFalse(marked([]))


@unittest.skipUnless(LINUX, "reads /proc")
class ProcessTests(unittest.TestCase):
    def test_detached_and_orphaned_processes_are_stopped_too(self):
        mark = f"REINS_TASK_ID=stop-test-{os.getpid()}-{time.time_ns()}"
        environment = dict(os.environ, REINS_TASK_ID=mark.split("=", 1)[1])
        script = ("setsid nohup sleep 301 >/dev/null 2>&1 &\n"
                  "( setsid sh -c 'sleep 302 & exit' & )\n"
                  "trap '' TERM; sleep 303 & wait\n")  # ignores SIGTERM: needs SIGKILL
        process = subprocess.Popen(["sh", "-c", script], env=environment, start_new_session=True)
        self.addCleanup(process.wait)
        marked = lambda lines: mark in lines  # noqa: E731
        deadline = time.monotonic() + 5
        while len(linux.marked_processes(marked)) < 5 and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertGreaterEqual(len(linux.marked_processes(marked)), 4)
        signalled, left = linux.stop_marked_processes(marked, grace=0.5)
        self.assertGreaterEqual(signalled, 4)
        self.assertEqual(left, [])

    def test_stop_never_signals_itself(self):
        mark = f"REINS_TASK_ID=self-{os.getpid()}"
        with mock.patch.dict(os.environ, {"REINS_TASK_ID": mark}):
            self.assertNotIn(os.getpid(), linux.marked_processes(lambda lines: True))


class CommandTests(StopCase):
    def test_a_queued_task_is_stopped_at_once(self):
        path, task_id = self.submit()
        code, out, _ = self.run_stop(task_id[:4])
        self.assertEqual(code, 0)
        self.assertIn(f"Stopped task {task_id}", out)
        self.assertIn("callback saying you stopped it on purpose", out)
        self.assertEqual(managed_tasks.load_managed_task(path)["recovery_reason"], "stopped_by_user")
        self.assertEqual(json.loads((path.parent / "stop.json").read_text())["message"], "free the GPU")

    def test_a_running_systemd_task_stops_its_unit(self):
        path, task_id = self.submit()
        task = self.launch(path)
        task.update(state="running", boot_id=host.current_boot_id(), started_at=time.time())
        managed_tasks.write_managed_task(self.root, task)
        with mock.patch.object(SystemdUserBackend, "stop", return_value=True) as unit, \
                mock.patch.object(SystemdUserBackend, "probe", return_value=OwnerState.ABSENT), \
                mock.patch.object(stop_command, "stop_marked_processes", return_value=(0, [])):
            code, out, _ = self.run_stop(task_id)
        unit.assert_called_once_with(task["execution_owner"])
        self.assertEqual(code, 0)
        self.assertIn("callback saying you stopped it on purpose", out)

    def test_a_unit_that_is_already_gone_counts_as_stopped(self):
        path, task_id = self.submit()
        task = self.launch(path)
        task.update(state="running", boot_id=host.current_boot_id(), started_at=time.time())
        managed_tasks.write_managed_task(self.root, task)
        with mock.patch.object(SystemdUserBackend, "stop", return_value=False), \
                mock.patch.object(SystemdUserBackend, "probe", return_value=OwnerState.ABSENT), \
                mock.patch.object(stop_command, "stop_marked_processes", return_value=(0, [])):
            code, _, err = self.run_stop(task_id)
        self.assertEqual(code, 0)
        self.assertNotIn("did not stop", err)

    def test_an_ended_systemd_task_does_not_touch_its_unit(self):
        path, task_id = self.submit()
        task = self.launch(path)
        task.update(state="completed", outcome="completed")
        managed_tasks.write_managed_task(self.root, task)
        with mock.patch.object(SystemdUserBackend, "stop") as unit, \
                mock.patch.object(stop_command, "stop_marked_processes", return_value=(0, [])):
            code, out, _ = self.run_stop(task_id)
        unit.assert_not_called()
        self.assertEqual(code, 0)
        self.assertIn("left nothing running", out)

    def stop_running(self, unit_stopped: bool, left: list[int]):
        path, task_id = self.submit()
        task = self.launch(path)
        task.update(state="running", boot_id=host.current_boot_id(), started_at=time.time())
        managed_tasks.write_managed_task(self.root, task)
        with mock.patch.object(SystemdUserBackend, "stop", return_value=unit_stopped), \
                mock.patch.object(SystemdUserBackend, "probe", return_value=OwnerState.ALIVE), \
                mock.patch.object(stop_command, "stop_marked_processes", return_value=(1, left)):
            code, out, err = self.run_stop(task_id)
        return task_id, code, out, err

    def test_a_unit_systemd_did_not_stop_asks_to_run_again(self):
        task_id, code, out, err = self.stop_running(unit_stopped=False, left=[])
        self.assertEqual(code, 2)
        self.assertIn("is incomplete", out)
        self.assertIn("systemd did not stop", err)
        self.assertIn(f"reins stop {task_id} again", err)

    def test_a_surviving_process_asks_to_run_again(self):
        task_id, code, out, err = self.stop_running(unit_stopped=True, left=[4242])
        self.assertEqual(code, 2)
        self.assertIn("is incomplete", out)
        self.assertNotIn("systemd did not stop", err)
        self.assertIn("still running: 4242", err)
        self.assertIn(f"reins stop {task_id} again", err)

    def test_the_confirmation_shows_the_task_without_owner_hints(self):
        _, _, out, _ = self.stop_running(unit_stopped=True, left=[])
        self.assertIn("Started by", out)
        self.assertNotIn("reins send", out)
        self.assertNotIn("reins tui", out)

    def test_an_ended_task_with_leftovers_or_nothing(self):
        path, task_id = self.submit()
        task = managed_tasks.load_managed_task(path)
        task.update(state="completed", outcome="completed", execution_backend="screen", screen_session="reins-x")
        managed_tasks.write_managed_task(self.root, task)
        with mock.patch.object(stop_command, "stop_marked_processes", return_value=(2, [])):
            code, out, _ = self.run_stop(task_id)
        self.assertEqual(code, 0)
        self.assertIn("already ended; stopped 2 process(es)", out)
        self.assertFalse((path.parent / "stop.json").exists())
        with mock.patch.object(stop_command, "stop_marked_processes", return_value=(0, [])):
            _, out, _ = self.run_stop(task_id)
        self.assertIn("left nothing running", out)

    def test_without_a_terminal_it_needs_yes(self):
        path, task_id = self.submit()
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            code, _, err = self.run_stop(task_id, yes=False)
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)
        self.assertEqual(managed_tasks.load_managed_task(path)["state"], "submitted")

    def test_other_platforms_and_backends_are_refused(self):
        path, task_id = self.submit()
        task = managed_tasks.load_managed_task(path)
        task["execution_backend"] = "launchd"
        managed_tasks.write_managed_task(self.root, task)
        code, _, err = self.run_stop(task_id)
        self.assertEqual(code, 1)
        self.assertIn("uses launchd", err)

    def test_a_missing_task_fails(self):
        code, _, err = self.run_stop("ffffffff")
        self.assertEqual(code, 1)
        self.assertIn("no task ffffffff", err)


if __name__ == "__main__":
    unittest.main()
