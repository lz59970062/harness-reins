from __future__ import annotations

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

from harness_reins import template_registration
from harness_reins.install import skills as skill_install

HOLD = """
import fcntl, json, sys, time
handle = open(sys.argv[1], "a+")
fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
handle.seek(0); handle.truncate(0)
handle.write(json.dumps({"pid": int(sys.argv[3]), "host": sys.argv[4], "since": 0, "command": "reins setup"}))
handle.flush()
print("held", flush=True)
time.sleep(float(sys.argv[2]))
"""


@unittest.skipIf(os.name == "nt", "POSIX flock")
class RegistrationLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "skills"
        self.root.mkdir()
        patcher = mock.patch.dict(os.environ, {"REINS_TEMPLATE_DIR": str(Path(self.tmp.name) / "templates")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.lock_file = self.root / ".reins-registration.lock"

    def hold(self, seconds: float, *, pid: int | None = None, host: str | None = None) -> subprocess.Popen:
        import socket

        holder = subprocess.Popen(
            [sys.executable, "-c", HOLD, str(self.lock_file), str(seconds),
             str(pid if pid is not None else 4242), host if host is not None else socket.gethostname()],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        return holder

    def test_a_stale_empty_lock_file_is_not_a_holder(self) -> None:
        self.lock_file.write_bytes(b"")
        old = time.time() - 8 * 86400
        os.utime(self.lock_file, (old, old))
        with template_registration._lock([self.root]):
            holder = json.loads(self.lock_file.read_text())
        self.assertEqual(holder["pid"], os.getpid())

    def test_waits_for_a_holder_that_finishes(self) -> None:
        holder = self.hold(0.5)
        err = io.StringIO()
        with mock.patch.object(template_registration, "LOCK_WAIT_SECONDS", 10.0), contextlib.redirect_stderr(err):
            with template_registration._lock([self.root]):
                pass
        self.assertIsNotNone(holder.poll())
        self.assertIn("waiting up to 10 s", err.getvalue())

    def test_names_a_live_holder_after_the_wait(self) -> None:
        holder = self.hold(30)
        with mock.patch.object(template_registration, "LOCK_WAIT_SECONDS", 0.3), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(template_registration.RegistrationBusy) as caught:
                with template_registration._lock([self.root]):
                    self.fail("acquired a held lock")
        message = str(caught.exception)
        self.assertIn(str(self.lock_file), message)
        self.assertIn("pid 4242", message)  # The pid the holder wrote, not its own.
        self.assertIn("(reins setup)", message)
        self.assertIn(f"fuser -v {self.lock_file}", message)

    def test_reports_whether_the_recorded_holder_still_runs(self) -> None:
        alive = self.hold(30, pid=os.getpid())
        with mock.patch.object(template_registration, "LOCK_WAIT_SECONDS", 0.2), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(template_registration.RegistrationBusy) as caught:
                with template_registration._lock([self.root]):
                    pass
        self.assertIn("still running", str(caught.exception))
        alive.kill()
        alive.wait()
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        self.hold(30, pid=gone.pid)
        with mock.patch.object(template_registration, "LOCK_WAIT_SECONDS", 0.2), \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(template_registration.RegistrationBusy) as caught:
                with template_registration._lock([self.root]):
                    pass
        self.assertIn("no longer running", str(caught.exception))

    def test_no_lock_wait_fails_at_once(self) -> None:
        self.hold(30)
        err = io.StringIO()
        started = time.monotonic()
        with template_registration.no_lock_wait(), contextlib.redirect_stderr(err):
            with self.assertRaises(template_registration.RegistrationBusy):
                with template_registration._lock([self.root]):
                    pass
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertNotIn("waiting", err.getvalue())

    def test_the_fuser_hint_quotes_the_path(self) -> None:
        message = str(template_registration._busy(Path("/a dir/.reins-registration.lock")))
        self.assertIn("fuser -v '/a dir/.reins-registration.lock'", message)

    def test_an_unreadable_holder_record_is_said_so(self) -> None:
        self.assertEqual(template_registration._describe_holder(self.root / "missing.lock"),
                         "the lock file does not say which process holds it")
        other = self.root / "other.lock"
        other.write_text(json.dumps({"pid": 4242, "host": "elsewhere"}))
        self.assertEqual(template_registration._describe_holder(other), "pid 4242 on elsewhere")
        other.write_text(json.dumps({"pid": 0, "host": "elsewhere"}))  # kill(0, 0) would probe our own group.
        self.assertEqual(template_registration._describe_holder(other),
                         "the lock file does not say which process holds it")


class SkillInstallBusyTests(unittest.TestCase):
    def test_setup_prints_an_error_instead_of_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "skills" / "harness-reins"
            busy = template_registration.RegistrationBusy("another skill installation holds /x/.lock (pid 1)")

            @contextlib.contextmanager
            def held(_target):
                raise busy
                yield

            err = io.StringIO()
            with mock.patch.object(template_registration, "preserve_installed_index", held), \
                    contextlib.redirect_stderr(err):
                status = skill_install.install_skill_tree(target, include_codex_plugin=False, force=True)
        self.assertEqual(status, 1)
        self.assertIn(f"reins: error: could not install the skill at {target}: another skill installation", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_setup_stops_waiting_after_one_busy_lock(self) -> None:
        waits: list = []

        def install(target, **options):
            self.assertTrue(options.pop("raise_busy"))
            waits.append(template_registration._lock_wait.get())
            raise template_registration.RegistrationBusy(f"busy at {target}")

        args = mock.Mock(path=None, target="all", force=True, keep_existing=False, claude_hook=None,
                         refresh_installed=False)
        err = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(skill_install, "install_skill_tree", install), \
                mock.patch.object(skill_install.layout, "codex_home", lambda: Path(tmp) / "codex"), \
                mock.patch.object(skill_install.layout, "claude_home", lambda: Path(tmp) / "claude"), \
                mock.patch.object(skill_install.layout, "dsh_home", lambda: Path(tmp) / "dsh"), \
                mock.patch.object(skill_install.pi_channel, "profile_dir", lambda: Path(tmp) / "pi"), \
                contextlib.redirect_stderr(err):
            self.assertEqual(skill_install.install_skill(args), 1)
        self.assertEqual(waits, [None, 0.0, 0.0, 0.0])  # Only the first target waited.
        self.assertEqual(err.getvalue().count("reins: error: could not install the skill"), 4)


if __name__ == "__main__":
    unittest.main()
