"""Check that agent CLIs still start under the coordinator unit's hardening.

The systemd user unit sets NoNewPrivileges=yes. An agent CLI that is really a
launcher script re-executing through sudo (or another setuid helper) works in a
login shell but fails inside the service, so every headless callback resume
fails while the coordinator itself looks healthy. Probe each CLI the unit
records with ``--version`` in a transient unit that carries the same properties.
"""
from __future__ import annotations

import os
import shlex
from pathlib import Path
import shutil
import subprocess
import sys

from ..harness.claude.adapter import CLAUDE_BIN_ENV
from ..harness.dsh.adapter import DSH_BIN_ENV
from ..harness.pi.adapter import PI_BIN_ENV
from ..owners import linux as linux_platform
from ..store import install_record

HARDENING = ("NoNewPrivileges=yes",)
OPT_OUT_FLAG = "--allow-privilege-escalation"
OPT_OUT_COMMENT = f"# NoNewPrivileges omitted by reins setup {OPT_OUT_FLAG}"
ISSUE_CODE = "agent_cli_blocked_by_service_sandbox"
AGENT_BIN_ENVS = {"codex": "REINS_CODEX_BIN", "claude": CLAUDE_BIN_ENV, "pi": PI_BIN_ENV, "dsh": DSH_BIN_ENV}
PROBE_TIMEOUT = 20.0
STDERR_LIMIT = 300


def _run(properties: tuple[str, ...], path: str, command: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    argv = ["systemd-run", "--user", "--wait", "--pipe", "--quiet", "--collect"]
    for item in properties:
        argv.extend(["-p", item])
    argv.extend([f"--setenv=PATH={path}", "--", *command])
    try:
        return subprocess.run(
            argv, check=False, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            errors="replace", timeout=timeout, env=linux_platform.user_manager_environment(),
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _last_line(result: subprocess.CompletedProcess) -> str:
    for stream in (result.stderr, result.stdout):
        lines = [line.strip() for line in (stream or "").splitlines() if line.strip()]
        if lines:
            return lines[-1][:STDERR_LIMIT]
    return f"exit status {result.returncode}"


def issue(agent: str, binary: str, stderr: str, properties: tuple[str, ...] = HARDENING) -> dict[str, object]:
    hardening = ", ".join(properties)
    return {
        "code": ISSUE_CODE, "kind": "configuration", "component": "coordinator_service",
        "agent": agent, "binary": binary, "stderr": stderr,
        "action": (
            f"`{binary} --version` fails under the coordinator unit's {hardening} but runs without it, "
            f"so the service cannot start the {agent} CLI (headless callback resumes and child agents fail). "
            "This usually means the CLI is a launcher that re-executes through sudo or another setuid helper. "
            f"Prefer rerunning setup with --{agent}-bin pointing at a CLI that needs no privilege escalation. "
            f"If this launcher is required, ask the user before rerunning `reins setup --force --now {OPT_OUT_FLAG}`, "
            "which writes the unit without NoNewPrivileges=yes."
        ),
    }


def probe(binaries: dict[str, str], path: str, *, properties: tuple[str, ...] = HARDENING,
          timeout: float = PROBE_TIMEOUT) -> list[dict[str, object]]:
    """Return one issue per CLI that starts normally but not under ``properties``.

    Nothing is reported when systemd-run or the user manager is unavailable, a
    CLI is not installed, or a CLI fails even without the hardening: those are
    not evidence that the sandbox is the cause.
    """
    if sys.platform != "linux" or not properties or shutil.which("systemd-run") is None:
        return []
    true = shutil.which("true") or "/bin/true"
    preflight = _run(properties, path, [true], timeout)
    if preflight is None or preflight.returncode != 0:
        return []
    issues = []
    for agent, configured in binaries.items():
        executable = shutil.which(configured, path=path or None)
        if executable is None:
            continue
        executable = os.path.abspath(executable)
        sandboxed = _run(properties, path, [executable, "--version"], timeout)
        if sandboxed is None or sandboxed.returncode == 0:
            continue
        plain = _run((), path, [executable, "--version"], timeout)
        if plain is None or plain.returncode != 0:
            continue
        issues.append(issue(agent, executable, _last_line(sandboxed), properties))
    return issues


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return value


def dropin_overrides(path: Path) -> dict[str, str]:
    """Settings from ``<unit>.d/*.conf``, which systemd applies over the unit in filename order."""
    values: dict[str, str] = {}
    directory = path.with_name(path.name + ".d")
    try:
        files = sorted(directory.glob("*.conf"))
    except OSError:
        return values
    for item in files:
        try:
            lines = item.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            name, separator, value = line.strip().partition("=")
            if separator and name == "NoNewPrivileges":
                values[name] = value.strip().lower()
    return values


def read_unit(path: Path) -> dict[str, object] | None:
    """Parse the environment and effective hardening of a unit written by systemd_service_text.

    A drop-in such as ``<unit>.d/override.conf`` with ``NoNewPrivileges=no`` turns
    the hardening off for the running service, so it is not probed for.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    environment: dict[str, str] = {}
    exec_start: list[str] = []
    for line in lines:
        if line.startswith("Environment="):
            name, _, value = _unquote(line.removeprefix("Environment=")).partition("=")
            environment[name] = value
        elif line.startswith("ExecStart=") and not exec_start:
            try:
                exec_start = shlex.split(line.removeprefix("ExecStart="))
            except ValueError:
                exec_start = []
    hardening = tuple(item for item in HARDENING if item in lines)
    dropin = dropin_overrides(path).get("NoNewPrivileges")
    if dropin in ("no", "false", "0", "off"):
        hardening = tuple(item for item in hardening if not item.startswith("NoNewPrivileges="))
    elif dropin in ("yes", "true", "1", "on") and "NoNewPrivileges=yes" not in hardening:
        hardening += ("NoNewPrivileges=yes",)
    return {
        "environment": environment,
        "exec_start": exec_start,
        "hardening": hardening,
        "allows_privilege_escalation": OPT_OUT_COMMENT in lines,
        "dropin_disables_hardening": dropin in ("no", "false", "0", "off"),
    }


def launched_command(exec_start: list[str]) -> list[str]:
    """What a service's command line ends up running, through the launcher ``reins setup`` writes.

    A hot reload re-executes the running daemon's own command, so a service whose
    command changed (an upgrade into another installation, new daemon flags) needs
    a restart; comparing this with the running process tells which.
    """
    if not exec_start:
        return []
    record = install_record.read()
    try:
        through_launcher = Path(exec_start[0]).resolve() == install_record.launcher_path().resolve()
    except OSError:
        through_launcher = False
    if through_launcher and record is not None:
        return [*record["command"], *exec_start[1:]]
    return list(exec_start)


def same_command(expected: list[str], running: list[str]) -> bool:
    """Equal argument for argument, a path also when both resolve to the same file."""
    if len(expected) != len(running):
        return False
    for want, have in zip(expected, running):
        if want != have and not (os.path.isabs(want) and os.path.isabs(have)
                                 and os.path.realpath(want) == os.path.realpath(have)):
            return False
    return True


def recorded_binaries(environment: dict[str, str]) -> dict[str, str]:
    return {agent: environment[name] for agent, name in AGENT_BIN_ENVS.items() if environment.get(name)}


def report(issues: list[dict[str, object]]) -> None:
    for item in issues:
        print(
            f"reins: warning: {item['code']}: the {item['agent']} CLI {item['binary']} does not start under "
            f"the service's NoNewPrivileges=yes: {item['stderr']}",
            file=sys.stderr,
        )
        print(f"reins: action: {item['action']}", file=sys.stderr)
