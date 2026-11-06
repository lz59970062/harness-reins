"""Installation of the macOS per-user callback coordinator.

Business commands are owned by separate jobs in owners/macos.py. Updating a
coordinator never boots out a live process or interrupts its callback delivery.
"""
from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import subprocess
import sys

from ..owners import OwnerState
from ..owners import macos
from . import service_sandbox


def agent_directory() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def program_arguments(args: argparse.Namespace) -> list[str]:
    from ..tower import main as tower
    from ..store import layout

    command = tower.daemon_command(args)
    command[0] = str(Path(command[0]).expanduser().absolute())
    # Pin even the default queue: launchd does not inherit the submitting shell.
    if "--queue-dir" in command:
        command[command.index("--queue-dir") + 1] = str(layout.queue_dir(args).resolve())
    else:
        command.extend(["--queue-dir", str(layout.queue_dir(args).resolve())])
    return command


def running_command(pid: int) -> str | None:
    """The coordinator's command line as ps shows it: arguments joined by spaces, unquoted."""
    try:
        result = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                                check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    text = result.stdout.strip()
    return text or None


def runs_command(running: str, expected: list[str]) -> bool:
    return running in (" ".join(expected), " ".join(os.path.realpath(part) if os.path.isabs(part) else part
                                                    for part in expected))


def service_text(args: argparse.Namespace) -> str:
    from ..tower import environment as tower_environment
    from ..harness.codex import app_server
    from ..store import records as queue_store
    from ..store import layout

    label = macos.validate_label(args.name)
    command = program_arguments(args)
    environment = tower_environment.daemon_environment(args, include_proxy_values=False)
    environment.update(CODEX_HOME=str(layout.codex_home().resolve()),
                       CLAUDE_CONFIG_DIR=str(layout.claude_home().resolve()))
    for name in (layout.TARGET_LOCK_DIR_ENV, app_server.APP_SERVER_SOCKET_ENV, app_server.ALLOW_APP_SERVER_SOCKET_OVERRIDE_ENV):
        if name in os.environ:
            environment[name] = os.environ[name]
    log = str(layout.daemon_state_dir().resolve() / "daemon.log")
    return macos.plist_text({
        "Label": label, "ProgramArguments": command, "WorkingDirectory": str(Path.home()),
        "EnvironmentVariables": environment, "RunAtLoad": True, "KeepAlive": True,
        "ThrottleInterval": max(1, math.ceil(args.restart_sec)), "Umask": 0o077,
        "StandardInPath": "/dev/null", "StandardOutPath": log, "StandardErrorPath": log,
    })


def install(args: argparse.Namespace) -> int:
    from ..tower import environment as tower_environment
    from ..tower import main as tower
    from ..harness.codex import app_server
    from ..store import records as queue_store
    from ..store import layout

    try:
        text = service_text(args)
        if args.print:
            print(text, end="")
            return 0
        if sys.platform != "darwin":
            raise ValueError("launchd coordinator requires macOS")
        target = agent_directory() / f"{args.name}.plist"
        if target.exists() and not args.force:
            print(f"LaunchAgent already exists at {target}. Re-run with --force to overwrite.", file=sys.stderr)
            return 1
        tower_environment.configure_proxy_environment(args)
        state_dir = layout.daemon_state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(state_dir, 0o700)
        queue_store.write_private_text(target, text)
        print(f"Installed launchd LaunchAgent to {target}")
        if not (args.enable or args.now):
            return 0
        service = f"{macos.gui_domain()}/{args.name}"
        result = macos.launchctl(["enable", service])
        if result.returncode != 0:
            print("reins: could not enable the LaunchAgent; log in to the macOS GUI session and retry", file=sys.stderr)
            return 1
        if not args.now:
            return 0

        state, pid, registered = macos.job_status(args.name)
        if state == OwnerState.ALIVE and pid is not None:
            # A reload re-executes the coordinator's own command, so after an upgrade or new
            # flags it would keep the old ones; restart it instead while nothing is delivering.
            running = running_command(pid)
            expected = service_sandbox.launched_command(program_arguments(args))
            if running is not None and expected and not runs_command(running, expected):
                if not any((layout.queue_dir(args) / "running").glob("*.json")):
                    print("Restarting the coordinator: it runs an older command than the LaunchAgent now gives.")
                    result = macos.launchctl(["bootout", service])
                    if result.returncode != 0:
                        print("reins: could not stop the running coordinator; it was left as it was", file=sys.stderr)
                        return 1
                    result = macos.launchctl(["bootstrap", macos.gui_domain(), str(target)])
                    if result.returncode != 0:
                        print("reins: LaunchAgent bootstrap failed; inspect launchctl status and daemon.log before "
                              "retrying", file=sys.stderr)
                        return 1
                    print(f"Started {service}; log: {state_dir / 'daemon.log'}")
                    return 0
                print("reins: the coordinator runs an older command, and callback delivery is running, so it is "
                      "only reloaded and keeps its old flags; rerun `reins setup --now` when `reins status` shows "
                      "nothing running", file=sys.stderr)
        if state == OwnerState.ALIVE:
            if pid is not None and tower.send_standalone_reload(pid, expected_queue=layout.queue_dir(args)):
                print("Requested coordinator reload after callback deliveries drain. LaunchAgent settings apply at next load.")
                return 0
            print("reins: running LaunchAgent could not be verified for safe reload; it was left untouched", file=sys.stderr)
            return 1
        if state == OwnerState.UNKNOWN:
            print("reins: LaunchAgent state is unknown; refusing to replace or start another coordinator", file=sys.stderr)
            return 1

        queue = layout.queue_dir(args)
        admission = queue_store.acquire_owner_lock(queue, "daemon-singleton", blocking=False)
        if admission is None:
            runtime = tower.read_daemon_runtime()
            pid = runtime.get("pid") if runtime else None
            if type(pid) is int and tower.send_standalone_reload(pid, expected_queue=queue):
                print("Reloading the existing coordinator. LaunchAgent activation is deferred until that coordinator exits.")
                return 0
            print("reins: another coordinator owns the queue; LaunchAgent activation was deferred", file=sys.stderr)
            return 1
        queue_store.release_owner_lock(admission, remove=False)
        if registered:
            result = macos.launchctl(["bootout", service])
            if result.returncode != 0:
                print("reins: could not unload the inactive LaunchAgent; activation deferred", file=sys.stderr)
                return 1
        result = macos.launchctl(["bootstrap", macos.gui_domain(), str(target)])
        if result.returncode != 0:
            print("reins: LaunchAgent bootstrap failed; inspect launchctl status and daemon.log before retrying", file=sys.stderr)
            return 1
        print(f"Started {service}; log: {state_dir / 'daemon.log'}")
        return 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"reins: could not configure launchd coordinator ({type(error).__name__}); inspect its saved configuration", file=sys.stderr)
        return 2
