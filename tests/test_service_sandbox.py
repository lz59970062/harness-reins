"""Agent CLIs that cannot start under the systemd unit's NoNewPrivileges=yes."""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness_reins import cli
from harness_reins.install import diagnostics, service_sandbox, services

SUDO_ERROR = ("sudo: effective uid is not 0, is /usr/bin/sudo on a file system with the 'nosuid' "
              "option set or an NFS file system without root privileges?")

# Stands in for `systemd-run --user --wait --pipe`: logs its argv, then runs the
# command after `--` with FAKE_NNP=1 when the unit property is requested.
FAKE_SYSTEMD_RUN = """#!/bin/sh
echo "$*" >> "$FAKE_SYSTEMD_RUN_LOG"
[ -n "$FAKE_MANAGER_DOWN" ] && { echo "Failed to connect to bus: No medium found" >&2; exit 1; }
while [ "$#" -gt 0 ]; do
  case "$1" in
    -p) [ "$2" = "NoNewPrivileges=yes" ] && export FAKE_NNP=1; shift 2 ;;
    --setenv=PATH=*) export PATH="${1#--setenv=PATH=}"; shift ;;
    --) shift; break ;;
    *) shift ;;
  esac
done
exec "$@"
"""

# Like an administrator launcher that re-executes through sudo outside a namespace.
SUDO_LAUNCHER = f"""#!/bin/sh
if [ -n "$FAKE_NNP" ]; then echo "{SUDO_ERROR}" >&2; exit 1; fi
echo "2.1.0 (Claude Code)"
"""
PLAIN_CLI = "#!/bin/sh\necho 'codex-cli 0.50.0'\n"
BROKEN_CLI = "#!/bin/sh\necho 'node: not found' >&2\nexit 127\n"


@unittest.skipIf(os.name == "nt", "systemd units are Linux only")
class ServiceSandboxProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "systemd-run.log"
        self.write("systemd-run", FAKE_SYSTEMD_RUN)
        self.write("true", "#!/bin/sh\nexit 0\n")
        self.claude = self.write("claude", SUDO_LAUNCHER)
        self.codex = self.write("codex", PLAIN_CLI)
        self.path = f"{self.bin}:/usr/bin:/bin"
        environment = {"PATH": self.path, "FAKE_SYSTEMD_RUN_LOG": str(self.log),
                       "REINS_HOME": str(self.root / "reins-home"), "XDG_CONFIG_HOME": str(self.root / "config")}
        for patch in (mock.patch.dict(os.environ, environment), mock.patch.object(service_sandbox.sys, "platform", "linux")):
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop("FAKE_MANAGER_DOWN", None)

    def write(self, name, text):
        path = self.bin / name
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def calls(self):
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []

    def service_args(self, **updates):
        values = dict(name="harness-reins", queue_dir=None, interval=2.0, retries=3, retry_delay=30.0, retry_backoff=2.0,
                      resume_timeout=3600.0, restart_sec=5.0, exec_start="/fixture/reins", codex_bin=self.codex,
                      claude_bin=self.claude, pi_bin=str(self.bin / "pi"), dsh_bin=str(self.bin / "dsh"),
                      path=self.path, proxy_env_file=None,
                      inherit_proxy=False, clear_proxy=False, force=True, enable=False, now=False, print=False,
                      allow_privilege_escalation=False)
        values.update(updates)
        return argparse.Namespace(**values)

    def test_reports_cli_that_starts_only_without_no_new_privileges(self):
        issues = service_sandbox.probe({"claude": self.claude, "codex": self.codex, "pi": "pi"}, self.path)

        self.assertEqual(len(issues), 1)
        issue = issues[0]
        self.assertEqual(issue["code"], "agent_cli_blocked_by_service_sandbox")
        self.assertEqual(issue["kind"], "configuration")
        self.assertEqual((issue["agent"], issue["binary"]), ("claude", self.claude))
        self.assertEqual(issue["stderr"], SUDO_ERROR[:service_sandbox.STDERR_LIMIT])
        self.assertIn("--allow-privilege-escalation", issue["action"])
        self.assertIn("--claude-bin", issue["action"])
        self.assertIn("ask the user", issue["action"])
        sandboxed = [call for call in self.calls() if f"-- {self.claude} --version" in call and "NoNewPrivileges=yes" in call]
        self.assertEqual(len(sandboxed), 1)
        self.assertTrue(all("--user --wait --pipe" in call and f"--setenv=PATH={self.path}" in call for call in self.calls()))

    def test_cli_failing_without_hardening_is_not_blamed_on_the_sandbox(self):
        broken = self.write("broken", BROKEN_CLI)
        self.assertEqual(service_sandbox.probe({"claude": broken}, self.path), [])

    def test_unreachable_user_manager_or_missing_systemd_run_reports_nothing(self):
        with mock.patch.dict(os.environ, {"FAKE_MANAGER_DOWN": "1"}):
            self.assertEqual(service_sandbox.probe({"claude": self.claude}, self.path), [])
        self.assertEqual(len(self.calls()), 1)  # the preflight only
        (self.bin / "systemd-run").unlink()
        with mock.patch.dict(os.environ, {"PATH": str(self.bin)}):
            self.assertEqual(service_sandbox.probe({"claude": self.claude}, self.path), [])

    def test_unit_keeps_hardening_unless_explicitly_opted_out(self):
        hardened = services.systemd_service_text(self.service_args())
        self.assertIn("NoNewPrivileges=yes", hardened.splitlines())
        self.assertNotIn(service_sandbox.OPT_OUT_COMMENT, hardened)

        opted_out = services.systemd_service_text(self.service_args(allow_privilege_escalation=True))
        self.assertNotIn("NoNewPrivileges=yes", opted_out)
        self.assertIn(service_sandbox.OPT_OUT_COMMENT, opted_out.splitlines())

        unit = self.root / "harness-reins.service"
        unit.write_text(hardened, encoding="utf-8")
        parsed = service_sandbox.read_unit(unit)
        self.assertEqual(parsed["hardening"], ("NoNewPrivileges=yes",))
        self.assertFalse(parsed["allows_privilege_escalation"])
        self.assertEqual(service_sandbox.recorded_binaries(parsed["environment"]),
                         {"codex": self.codex, "claude": self.claude, "pi": str(self.bin / "pi"),
                          "dsh": str(self.bin / "dsh")})
        self.assertEqual(parsed["environment"]["PATH"], self.path)

    def test_a_dropin_that_turns_hardening_off_is_honored(self):
        # An administrator's or user's <unit>.d/*.conf overrides the unit, as systemd applies it.
        unit = self.root / "harness-reins.service"
        unit.write_text(services.systemd_service_text(self.service_args()), encoding="utf-8")
        dropins = self.root / "harness-reins.service.d"
        dropins.mkdir()
        (dropins / "10-allow.conf").write_text("[Service]\nNoNewPrivileges=no\n", encoding="utf-8")
        parsed = service_sandbox.read_unit(unit)
        self.assertEqual(parsed["hardening"], ())
        self.assertTrue(parsed["dropin_disables_hardening"])
        (dropins / "20-harden.conf").write_text("[Service]\nNoNewPrivileges=yes\n", encoding="utf-8")
        self.assertEqual(service_sandbox.read_unit(unit)["hardening"], ("NoNewPrivileges=yes",), "the last file wins")
        (dropins / "20-harden.conf").unlink()
        with contextlib.redirect_stderr(io.StringIO()) as errors, mock.patch.object(service_sandbox, "probe") as probe:
            services.check_agent_clis_under_hardening(self.service_args(), unit)
        probe.assert_not_called()
        self.assertIn("sets NoNewPrivileges=no", errors.getvalue())

    def running_daemon(self, *, no_new_privs: bool, environment: dict[str, str]) -> Path:
        proc = self.root / "proc"
        (proc / "4242").mkdir(parents=True)
        (proc / "4242" / "status").write_text(f"Name:\treins\nNoNewPrivs:\t{int(no_new_privs)}\n", encoding="utf-8")
        (proc / "4242" / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in environment.items()) + b"\0")
        return proc

    def test_a_reload_cannot_apply_new_hardening_or_environment(self):
        unit = self.root / "harness-reins.service"
        unit.write_text(services.systemd_service_text(self.service_args(allow_privilege_escalation=True)), encoding="utf-8")
        recorded = service_sandbox.read_unit(unit)["environment"]
        main_pid = mock.Mock(stdout=f"MainPID=4242\nFragmentPath={unit}\n")
        with mock.patch.object(services.sys, "platform", "linux"), \
                mock.patch.object(services.subprocess, "run", return_value=main_pid):
            # Started under the old, hardened unit: the flag survives every reload.
            proc = self.running_daemon(no_new_privs=True, environment=recorded)
            self.assertEqual(services.reload_cannot_apply("harness-reins.service", unit, proc=proc), ["NoNewPrivileges"])
            old_bin = dict(recorded, REINS_CODEX_BIN="/old/codex")
            (proc / "4242" / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in old_bin.items()))
            (proc / "4242" / "status").write_text("NoNewPrivs:\t0\n", encoding="utf-8")
            self.assertEqual(services.reload_cannot_apply("harness-reins.service", unit, proc=proc),
                             ["environment (REINS_CODEX_BIN)"])
            (proc / "4242" / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in recorded.items()))
            self.assertEqual(services.reload_cannot_apply("harness-reins.service", unit, proc=proc), [])
            (proc / "4242" / "status").write_text("NoNewPrivs:\t1\n", encoding="utf-8")
            other = mock.Mock(stdout="MainPID=4242\nFragmentPath=/elsewhere/harness-reins.service\n")
            with mock.patch.object(services.subprocess, "run", return_value=other):
                self.assertEqual(services.reload_cannot_apply("harness-reins.service", unit, proc=proc), [],
                                 "a service loaded from another unit file is not compared")

    def test_a_reload_cannot_apply_a_new_daemon_command(self):
        import json
        from harness_reins.store import install_record

        launcher = install_record.launcher_path()
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\n", encoding="utf-8")
        new = ["/opt/reins-0.13.1/bin/python3", "/opt/reins-0.13.1/harness_reins/_entry.py"]
        install_record.record_path().write_text(json.dumps({"schema": install_record.INSTALL_SCHEMA, "command": new}),
                                                encoding="utf-8")
        unit = self.root / "harness-reins.service"
        args = self.service_args(exec_start=str(launcher), resume_timeout=0.0)
        unit.write_text(services.systemd_service_text(args), encoding="utf-8")
        parsed = service_sandbox.read_unit(unit)
        main_pid = mock.Mock(stdout=f"MainPID=4242\nFragmentPath={unit}\n")
        proc = self.running_daemon(no_new_privs=True, environment=parsed["environment"])

        def running(command: list[str]) -> None:
            (proc / "4242" / "cmdline").write_bytes(b"\0".join(part.encode() for part in command) + b"\0")

        daemon = parsed["exec_start"][1:]
        with mock.patch.object(services.sys, "platform", "linux"), \
                mock.patch.object(services.subprocess, "run", return_value=main_pid):
            running([*new, *daemon])
            self.assertEqual(services.reload_cannot_apply("harness-reins.service", unit, proc=proc), [])
            # Upgraded into another installation: a reload would re-execute the old interpreter.
            running(["/opt/reins-0.12.0/bin/python3", "/opt/reins-0.12.0/harness_reins/_entry.py", *daemon])
            self.assertEqual(services.reload_cannot_apply("harness-reins.service", unit, proc=proc), ["daemon command"])
            # Same installation, new flags: a reload would keep the old --resume-timeout.
            old_flags = [part if part != "0.0" else "3600.0" for part in daemon]
            self.assertNotEqual(old_flags, daemon)
            running([*new, *old_flags])
            self.assertEqual(services.reload_cannot_apply("harness-reins.service", unit, proc=proc), ["daemon command"])

    def test_setup_restarts_when_a_reload_cannot_apply_the_unit_and_defers_during_delivery(self):
        args = self.service_args(now=True, enable=False)
        for running, expected in ((False, ["restart", "harness-reins.service"]), (True, None)):
            with self.subTest(delivering=running), \
                    mock.patch.object(services, "reload_cannot_apply", return_value=["NoNewPrivileges"]), \
                    mock.patch.object(services, "has_running_callbacks", return_value=running), \
                    mock.patch.object(services, "check_agent_clis_under_hardening"), \
                    mock.patch.object(services.tower_environment, "configure_proxy_environment"), \
                    mock.patch.object(services.shutil, "which", return_value="/usr/bin/systemctl"), \
                    mock.patch.object(services, "run_systemctl", return_value=0) as systemctl, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(services.install_systemd(args), 0)
            calls = [call.args[0] for call in systemctl.call_args_list]
            self.assertNotIn(["reload", "harness-reins.service"], calls)
            if expected:
                self.assertIn(expected, calls)
            else:
                self.assertNotIn(["restart", "harness-reins.service"], calls)
                self.assertIn("systemctl --user restart harness-reins.service", errors.getvalue())

    def test_setup_and_install_systemd_parse_the_opt_out(self):
        for argv, handler in ((["setup", "--allow-privilege-escalation"], "setup"),
                              (["install-systemd", "--allow-privilege-escalation"], "install_systemd"),
                              (["setup"], "setup")):
            with self.subTest(argv=argv), mock.patch.object(sys, "argv", ["reins", *argv]), \
                    mock.patch.object(services, handler, return_value=0) as called:
                self.assertEqual(cli.main(), 0)
            self.assertEqual(called.call_args.args[0].allow_privilege_escalation, "--allow-privilege-escalation" in argv)
        with mock.patch.object(sys, "argv", ["reins", "install-launchd", "--allow-privilege-escalation"]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main()

    def test_setup_forwards_the_opt_out_to_the_systemd_installer(self):
        args = argparse.Namespace(**{**vars(self.service_args(allow_privilege_escalation=True)),
                                     "service": "systemd", "skill_path": None, "callback_only": True})
        with mock.patch.object(services, "select_daemon_service", return_value="systemd"), \
                mock.patch("harness_reins.install.canonical.ensure", return_value={"version": "x", "build": "y"}), \
                mock.patch.object(services.skill_install, "install_skill", return_value=0), \
                mock.patch.object(services.reminder_policy, "ensure_callback_hook_file"), \
                mock.patch.object(services, "report_claude_agent_readiness"), \
                mock.patch.object(services, "install_systemd", return_value=0) as install, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(services.setup(args), 0)
        self.assertTrue(install.call_args.args[0].allow_privilege_escalation)

    def test_install_systemd_warns_with_code_binary_stderr_and_action(self):
        with mock.patch.object(services, "systemd_user_dir", return_value=self.root / "systemd"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            services.install_systemd(self.service_args())
        output = errors.getvalue()
        self.assertIn("agent_cli_blocked_by_service_sandbox", output)
        self.assertIn(self.claude, output)
        self.assertIn(SUDO_ERROR[:80], output)
        self.assertIn("--allow-privilege-escalation", output)
        self.assertIn("NoNewPrivileges=yes", (self.root / "systemd" / "harness-reins.service").read_text(encoding="utf-8"))

    def test_install_systemd_opt_out_writes_unhardened_unit_and_skips_probe(self):
        with mock.patch.object(services, "systemd_user_dir", return_value=self.root / "systemd"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            services.install_systemd(self.service_args(allow_privilege_escalation=True))
        unit = (self.root / "systemd" / "harness-reins.service").read_text(encoding="utf-8")
        self.assertNotIn("NoNewPrivileges=yes", unit)
        self.assertEqual(self.calls(), [])
        self.assertIn("omits NoNewPrivileges=yes", errors.getvalue())
        self.assertNotIn("agent_cli_blocked_by_service_sandbox", errors.getvalue())

    def doctor_args(self, mode="doctor"):
        return argparse.Namespace(mode=mode, operation="done", backend="auto", agent="codex", session="bound-session",
                                  queue_dir=str(self.root / "reins-home" / "queue"), callback_mode="auto", dry_run=False)

    def install_unit(self, **updates):
        unit = services.systemd_user_dir() / "harness-reins.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(services.systemd_service_text(self.service_args(**updates)), encoding="utf-8")

    def test_doctor_probes_the_installed_unit_binaries(self):
        self.install_unit()
        report = diagnostics.inspect(self.doctor_args())
        blocked = [issue for issue in report["issues"] if issue["code"] == "agent_cli_blocked_by_service_sandbox"]
        self.assertEqual([(issue["agent"], issue["binary"]) for issue in blocked], [("claude", self.claude)])
        self.assertEqual(blocked[0]["stderr"], SUDO_ERROR[:service_sandbox.STDERR_LIMIT])
        self.assertEqual(report["status"], "needs_configuration")
        self.assertEqual(report["checks"]["configuration"], "needs_attention")
        self.assertNotIn("--allow-privilege-escalation", report["repair_command"] or [])

    def test_blocked_cli_alone_gets_no_automatic_repair(self):
        self.install_unit()
        blocked = service_sandbox.issue("claude", self.claude, SUDO_ERROR)
        with mock.patch.object(diagnostics, "coordinator_issue", return_value=None), \
                mock.patch.object(diagnostics, "queue_writable", return_value=True), \
                mock.patch.object(diagnostics.service_sandbox, "probe", return_value=[blocked]), \
                mock.patch.object(diagnostics.shutil, "which", return_value="/fixture/codex"), \
                mock.patch.object(diagnostics.routes, "inspect_route", return_value={"status": "ready"}), \
                mock.patch.dict(os.environ, {"CODEX_HOME": str(self.root / "codex")}):
            skill = self.root / "codex" / "skills" / "harness-reins" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("fixture", encoding="utf-8")
            report = diagnostics.inspect(self.doctor_args())
        self.assertEqual([issue["code"] for issue in report["issues"]], ["agent_cli_blocked_by_service_sandbox"])
        self.assertIsNone(report["repair_command"])

    def test_automatic_checks_do_not_start_agent_clis(self):
        self.install_unit()
        report = diagnostics.inspect(self.doctor_args(mode="done"))
        self.assertNotIn("agent_cli_blocked_by_service_sandbox", {issue["code"] for issue in report["issues"]})
        self.assertEqual(self.calls(), [])

    def test_doctor_keeps_an_existing_opt_out_and_skips_the_probe(self):
        self.install_unit(allow_privilege_escalation=True)
        report = diagnostics.inspect(self.doctor_args())
        self.assertNotIn("agent_cli_blocked_by_service_sandbox", {issue["code"] for issue in report["issues"]})
        self.assertEqual(self.calls(), [])
        self.assertIn("--allow-privilege-escalation", report["repair_command"])

    def test_doctor_ignores_a_unit_serving_another_atc_home(self):
        self.install_unit()
        with mock.patch.dict(os.environ, {"REINS_HOME": str(self.root / "other-home")}):
            report = diagnostics.inspect(self.doctor_args())
        self.assertNotIn("agent_cli_blocked_by_service_sandbox", {issue["code"] for issue in report["issues"]})
        self.assertEqual(self.calls(), [])


if __name__ == "__main__":
    unittest.main()
