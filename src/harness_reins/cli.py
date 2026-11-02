from __future__ import annotations

import argparse
import base64
import contextlib
from email.message import EmailMessage
import hashlib
import importlib.resources as resources
import json
import os
import re
import secrets
import signal
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import yaml

try:
    import fcntl
except ImportError:  # pragma: no cover - the durable run lifecycle is POSIX-only
    fcntl = None

from . import __version__
from .core import envelope
from .engine import routes
from .harness.claude import session as claude_session
from .harness.pi import channel as pi_channel
from .install import diagnostics
from .system import files as private_files
from . import template_registration
from .harness import AGENTS, AGENT_NAMES, CHILD_AGENT_NAMES, ChildOptions, get_agent, get_child_agent
from .harness.base import (
    DEFAULT_APPROVALS_REVIEWER,
    DEFAULT_APPROVAL_POLICY,
    DEFAULT_SANDBOX_MODE,
    DEFAULT_CLAUDE_PERMISSION_MODE,
)
from .harness.codex.adapter import CODEX_THREAD_ID_ENV
from .harness.claude.adapter import (
    CLAUDE_THREAD_ID_ENV,
    CLAUDE_MARKER_ENV,
    CLAUDE_BIN_ENV,
    CLAUDE_PERMISSION_MODE_ENV,
)
from .harness.pi.adapter import PI_BIN_ENV, PI_SESSION_FILE_ENV
from .runtime import worker_command
from .owners import LaunchError, OwnerState, ScreenBackend, SystemdUserBackend, LaunchdBackend
from .owners import linux as linux_platform
from .owners import macos as macos_platform
from .system import posix as posix_platform
from .owners import windows as windows_platform
from .system import windows_io, windows_process
from .owners.windows import WindowsBackend
from .templates import TEMPLATES, REASONING_EFFORTS, load_template
from .store import layout
from .owners import host
from . import runtime as reins_runtime
from .store import records as queue_store
from .system import shell
from .engine import delivery as delivery_engine
from .engine import reminders as reminder_policy
from .harness.codex import app_server
from .engine import dispatch
from .engine import goals
from .engine import recovery
from .engine import tasks as managed_tasks
from .engine import worker as task_worker
from .install import services
from .install import skills as skill_install
from .tower import main as tower
from .commands import callbacks as callback_commands
from .commands import goals as goal_commands
from .commands import templates as template_commands
from .commands import work as work_commands


def add_common_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--callback-mode", choices=routes.MODES, default="auto", metavar="{" + ",".join(routes.OFFERED_MODES) + "}",
                        help="Callback route: auto uses codex-queue for Desktop; cli uses existing CLI/shared service; codex-queue sends through the native CLI queue; app-server sends native tool output to an explicit socket; manual saves for later receipt")
    parser.add_argument("--app-server-socket", help="Native Unix socket of the server owning the bound thread (requires --callback-mode app-server)")
    parser.add_argument("--backend", choices=("auto", "systemd", "launchd", "windows-task", "screen"),
                        default=os.environ.get("REINS_EXECUTION_BACKEND", "auto"),
                        help="Task owner: systemd on Linux, launchd on macOS, windows-task on Windows; screen is the POSIX fallback")
    parser.add_argument("--callback-format", choices=("compact", "full"), default="compact",
                        help="Callback envelope (default: compact, verbose evidence saved locally)")
    parser.add_argument(
        "--agent",
        choices=AGENT_NAMES,
        help="Agent to wake (default: auto-detect from $PI_SESSION_FILE/$PI_SESSION_ID, $CLAUDECODE/$CLAUDE_CODE_SESSION_ID or $CODEX_THREAD_ID)",
    )
    parser.add_argument(
        "--session",
        help=f"Agent session id to resume (default: ${PI_SESSION_FILE_ENV}, ${CLAUDE_THREAD_ID_ENV} or ${CODEX_THREAD_ID_ENV} from the launching agent thread)",
    )
    parser.add_argument("--cwd", default=os.getcwd(), help="Working directory for the task and resumed agent")
    parser.add_argument("--task", default="long task", help="Human-readable task name")
    parser.add_argument("--command", help="Original command text")
    parser.add_argument("--exit-code", type=int, help="Completed task exit code")
    parser.add_argument("--message", help="Extra callback message")
    parser.add_argument(
        "--via-daemon",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--queue-dir", help="Wakeup queue directory for daemon task submission and callback delivery")
    parser.add_argument("--goal-id", help="Explicit goal record to update when this callback is queued")
    parser.add_argument(
        "--approvals-reviewer",
        default=os.environ.get("REINS_APPROVALS_REVIEWER", DEFAULT_APPROVALS_REVIEWER),
        help="Codex-only: approvals_reviewer config value used when resuming (default: auto_review)",
    )
    parser.add_argument(
        "--approval-policy",
        default=os.environ.get("REINS_APPROVAL_POLICY", DEFAULT_APPROVAL_POLICY),
        help="Codex-only: approval_policy config value used when resuming (default: on-request)",
    )
    parser.add_argument(
        "--sandbox-mode",
        default=os.environ.get("REINS_SANDBOX_MODE", DEFAULT_SANDBOX_MODE),
        help="Codex-only: sandbox mode used by child agents and resumed callbacks (default: workspace-write)",
    )
    parser.add_argument(
        "--permission-mode",
        default=os.environ.get(CLAUDE_PERMISSION_MODE_ENV, DEFAULT_CLAUDE_PERMISSION_MODE),
        help="Claude Code-only: permission mode used by child agents and resumed callbacks (default: auto)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview the run submission or done callback without writing queue state",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Propagate callback failure. By default callback failure never changes task success or exit code.",
    )


def add_privilege_escalation_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--allow-privilege-escalation",
        action="store_true",
        help="systemd only: write the unit without NoNewPrivileges=yes, for an agent CLI launcher that "
             "must run sudo or another setuid helper (weakens the service hardening)",
    )


def add_proxy_environment_flags(parser: argparse.ArgumentParser) -> None:
    proxy_source = parser.add_mutually_exclusive_group()
    proxy_source.add_argument(
        "--proxy-env-file",
        help="Read only proxy variables from this .env-style file and persist them for the daemon service",
    )
    proxy_source.add_argument(
        "--inherit-proxy",
        action="store_true",
        help="Persist proxy variables from this command environment for the daemon service",
    )
    proxy_source.add_argument(
        "--clear-proxy",
        action="store_true",
        help="Remove the persisted proxy environment file before updating the daemon service",
    )


def invoke_with_diagnostics(args: argparse.Namespace, operation: Callable[[argparse.Namespace], int]) -> int:
    """Keep task admission/exit semantics; report configuration gaps afterward.

    Do not apply this wrapper to workers or acknowledgement/configuration paths:
    those must remain usable while an Agent repairs an incomplete installation.
    """
    try:
        return operation(args)
    finally:
        if not getattr(args, "dry_run", False):
            diagnostics.emit_if_needed(args)


# Modes that act on this installation itself, or that OS services start
# directly, never forward to the canonical installation.
LOCAL_MODES = frozenset({
    "setup", "migrate", "daemon", "desktop", "install-skill", "install-pi-extension", "install-shell-hook",
    "install-systemd", "install-launchd", "install-supervisor", "install-windows-task", "uninstall-windows-task",
})


def main() -> int:
    parser = argparse.ArgumentParser(prog="reins", description="Harness Reins: dispatch work to any agent and route the result back to the session that asked.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__} (build {reins_runtime.build_id()})")
    sub = parser.add_subparsers(dest="mode", required=True)

    doctor_parser = sub.add_parser("doctor", help="Check local readiness and print an Agent-owned repair plan as JSON")
    doctor_parser.add_argument("--queue-dir", help="Queue to inspect and configure")
    doctor_parser.add_argument("--backend", choices=("auto", "systemd", "launchd", "windows-task", "screen"),
                               default=os.environ.get("REINS_EXECUTION_BACKEND", "auto"))
    doctor_parser.add_argument("--operation", choices=("run", "agent", "done"), default="run",
                               help="Workflow whose prerequisites are checked (default: run)")
    doctor_parser.add_argument("--agent", choices=AGENT_NAMES, help="Callback Agent (default: detect current Agent)")
    doctor_parser.add_argument("--agent-worker", choices=CHILD_AGENT_NAMES, help="Child Agent for --operation agent")
    doctor_parser.add_argument("--callback-mode", choices=routes.MODES, default="auto")
    doctor_parser.add_argument("--app-server-socket", help="Explicit native App Server Unix socket")
    doctor_parser.add_argument("--session", help="Original Agent session to preserve during repair")

    prompt_policy_parser = sub.add_parser("prompt-policy", help="Show or set callback reminder intervals")
    prompt_policy_parser.add_argument("--system-every", type=reminder_policy.positive_prompt_interval, help="Show standard reminders every N distinct callbacks (default: 4)")
    prompt_policy_parser.add_argument("--user-every", type=reminder_policy.positive_prompt_interval, help="Show callback-hook reminders every N distinct callbacks (default: 3)")

    retry_parser = sub.add_parser("retry", help="Requeue a failed/manual callback without rerunning its workload")
    retry_parser.add_argument("--id", required=True)
    retry_parser.add_argument("--queue-dir")
    retry_parser.add_argument("--callback-mode", choices=routes.MODES, metavar="{" + ",".join(routes.OFFERED_MODES) + "}")
    retry_parser.add_argument("--app-server-socket", help="Explicit native App Server Unix socket")

    # Launchers generated by older versions still call `reins desktop launch`.
    desktop_parser = sub.add_parser("desktop", help="Retired: open Codex Desktop normally, no launcher is needed")
    desktop_parser.add_argument("retired_arguments", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    done_parser = sub.add_parser("done", help="Queue a callback after an externally managed task finishes")
    add_common_flags(done_parser)

    run_parser = sub.add_parser("run", help="Submit a command to an independent Linux, macOS or Windows task owner")
    add_common_flags(run_parser)
    run_parser.add_argument("wrapped_command", nargs=argparse.REMAINDER)

    agent_parser = sub.add_parser("agent", help="Run a durable fresh Codex, Claude Code or PI Agent child")
    agent_sub = agent_parser.add_subparsers(dest="agent_worker", required=True)
    for worker in CHILD_AGENT_NAMES:
        adapter = get_child_agent(worker)
        child_parser = agent_sub.add_parser(worker, help=f"Run a fresh {adapter.display_name} child agent")
        add_common_flags(child_parser)
        template_source = child_parser.add_mutually_exclusive_group()
        template_source.add_argument("--template", help="User template name in ~/.config/reins/templates, or built-in test")
        template_source.add_argument("--template-file", help="Custom YAML template file (relative to the submitting shell directory)")
        child_parser.add_argument("--model", dest="child_model", help="Child model only; overrides template default")
        if adapter.supports_system_prompt_file:
            child_parser.add_argument(
                "--system-prompt-file",
                help="UTF-8 system prompt file; overrides template default and freezes its text at submission",
            )
        child_parser.add_argument(
            "--reasoning-effort", dest="child_reasoning_effort",
            choices=adapter.reasoning_efforts or REASONING_EFFORTS,
            help="Child reasoning effort; PI Agent maps this to --thinking; model must support it",
        )
        child_parser.add_argument("--no-report", action="store_true",
                                  help="Do not ask the child for a structured report (REINS_REPORT_FILE)")
        child_parser.add_argument("--name", help="Short name for this node, for example research (required unless --task is given)")
        child_parser.add_argument("agent_prompt", nargs=argparse.REMAINDER)
    expert_child = agent_sub.add_parser("expert", help="Start a new chain with a configured expert agent")
    add_common_flags(expert_child)
    expert_child.add_argument("expert", help="Expert name from ~/.reins/experts.yaml")
    expert_child.add_argument("--experts", help="Use another experts file")
    expert_child.add_argument("--name", help="Short name for this node (required unless --task is given)")
    expert_child.add_argument("--no-report", action="store_true", help="Do not ask the child for a structured report")
    # nargs="*" after "--", not REMAINDER: REMAINDER after a positional would swallow --name and the other options.
    expert_child.add_argument("agent_prompt", nargs="*")
    for mode, help_text in (("continue", "Continue a finished node's agent at the end of its chain"),
                            ("fork", "Start a new chain from any finished node's agent")):
        lineage_parser = agent_sub.add_parser(mode, help=help_text)
        add_common_flags(lineage_parser)
        lineage_parser.add_argument("node", help="Task ID or full node name, for example research/review")
        lineage_parser.add_argument("--name", help="Short name for the new node; the full name becomes <node>/<name>")
        lineage_parser.add_argument("--model", dest="child_model", help="Use another model than the parent node")
        lineage_parser.add_argument("--no-report", action="store_true",
                                    help="Do not ask the child for a structured report (REINS_REPORT_FILE)")
        lineage_parser.add_argument("agent_prompt", nargs="*")

    screen_worker_parser = sub.add_parser("_screen-worker", help=argparse.SUPPRESS)
    screen_worker_parser.add_argument("--task-file", required=True)
    screen_worker_parser.add_argument("--token", required=True)

    task_worker_parser = sub.add_parser("_task-worker", help=argparse.SUPPRESS)
    task_worker_parser.add_argument("--task-file", required=True)
    task_worker_parser.add_argument("--attempt", type=int, required=True)
    sub.add_parser("_delivery-worker", help=argparse.SUPPRESS)
    windows_coordinator_parser = sub.add_parser("_windows-coordinator", help=argparse.SUPPRESS)
    windows_coordinator_parser.add_argument("--config-file", required=True)
    uninstall_windows_parser = sub.add_parser("uninstall-windows-task", help="Remove Windows coordinator login registration and drain deliveries")
    uninstall_windows_parser.add_argument("--name", default="harness-reins")

    daemon_parser = sub.add_parser("daemon", help="Process queued wakeup requests outside Codex tool sandboxes")
    daemon_parser.add_argument("--queue-dir", help="Wakeup queue directory")
    daemon_parser.add_argument("--interval", type=float, default=2.0, help="Polling interval in seconds")
    daemon_parser.add_argument("--once", action="store_true", help="Exit after the queue is empty")
    daemon_parser.add_argument("--max-items", type=int, help="Exit after processing this many queued requests")
    daemon_parser.add_argument("--retries", type=int, default=delivery_engine.DEFAULT_RETRIES, help="Retries after a callback is not acknowledged")
    daemon_parser.add_argument("--retry-delay", type=float, default=delivery_engine.DEFAULT_RETRY_DELAY, help="Initial retry delay in seconds")
    daemon_parser.add_argument("--retry-backoff", type=float, default=delivery_engine.DEFAULT_RETRY_BACKOFF, help="Retry delay multiplier")
    daemon_parser.add_argument("--resume-timeout", type=float, default=delivery_engine.DEFAULT_RESUME_TIMEOUT, help="Maximum seconds for one Codex resume before retrying")

    for service in ("systemd", "launchd", "windows-task"):
        service_parser = sub.add_parser(f"install-{service}", help=f"Install a user-level {service} coordinator")
        service_parser.add_argument("--name", default="harness-reins", help="Service name / LaunchAgent label")
        service_parser.add_argument("--queue-dir", help="Wakeup queue directory")
        service_parser.add_argument("--interval", type=float, default=2.0, help="Daemon polling interval in seconds")
        service_parser.add_argument("--retries", type=int, default=delivery_engine.DEFAULT_RETRIES)
        service_parser.add_argument("--retry-delay", type=float, default=delivery_engine.DEFAULT_RETRY_DELAY)
        service_parser.add_argument("--retry-backoff", type=float, default=delivery_engine.DEFAULT_RETRY_BACKOFF)
        service_parser.add_argument("--resume-timeout", type=float, default=delivery_engine.DEFAULT_RESUME_TIMEOUT)
        service_parser.add_argument("--restart-sec", type=float, default=5.0, help="Restart delay in seconds")
        service_parser.add_argument("--exec-start", help="Path to Reins executable")
        service_parser.add_argument("--codex-bin", help="Path to codex executable used by the daemon")
        service_parser.add_argument("--claude-bin", help="Path to claude executable used by the daemon")
        service_parser.add_argument("--pi-bin", help="Path to the PI Agent executable used by the daemon")
        service_parser.add_argument("--path", help="PATH environment for the daemon service")
        add_proxy_environment_flags(service_parser)
        service_parser.add_argument("--force", action="store_true", help="Overwrite an existing service file")
        service_parser.add_argument("--enable", action="store_true", help="Enable the service for this user")
        service_parser.add_argument("--now", action="store_true", help="Start the service or request a safe reload")
        service_parser.add_argument("--print", action="store_true", help="Print configuration without installing it")
        if service == "systemd":
            add_privilege_escalation_flag(service_parser)

    install_parser = sub.add_parser("install-skill", help="Install the bundled skill for Codex, Claude Code and/or Pi")
    install_parser.add_argument("--path", help="Skills directory to install into (overrides --target)")
    install_parser.add_argument(
        "--target",
        choices=skill_install.SKILL_TARGETS,
        default="both",
        help="Agent home to install into (default: both — ${CODEX_HOME:-~/.codex}/skills and ${CLAUDE_CONFIG_DIR:-~/.claude}/skills; "
             "pi — ${PI_CODING_AGENT_DIR:-~/.pi/agent}/skills; all — the three)",
    )
    install_parser.add_argument("--force", action="store_true", help="Overwrite an existing harness-reins skill")

    pi_install = sub.add_parser("install-pi-extension", help="Install Pi's native live callback extension")
    pi_install.add_argument("--profile", help="Pi profile directory (default: PI_CODING_AGENT_DIR or ~/.pi/agent)")
    pi_install.add_argument("--force", action="store_true", help="Replace an edited Reins extension after review")
    pi_launch = sub.add_parser("pi", help="Launch Pi with a session writer lease acquired before startup")
    pi_launch.add_argument("--cwd", default=os.getcwd(), help="Working directory for Pi")
    pi_launch.add_argument("--session", help="Absolute existing JSONL file (default: a new managed session)")
    pi_launch.add_argument("pi_args", nargs=argparse.REMAINDER, help="Pi options after --")

    template_parser = sub.add_parser("template", help="Register named child templates and advertise their skills")
    template_actions = template_parser.add_subparsers(dest="template_action", required=True)
    register_parser = template_actions.add_parser("register", help="Install a YAML template and discoverable task skill")
    register_parser.add_argument("name", help="Template name: lowercase letters, digits and hyphens")
    register_parser.add_argument("--file", help="Source YAML (default: existing named user template); relative paths use the current shell directory")
    register_parser.add_argument("--worker", choices=CHILD_AGENT_NAMES, help="Child worker (default: YAML worker)")
    register_parser.add_argument("--description", help="Skill discovery description (default: YAML description)")
    register_parser.add_argument("--target", choices=("codex", "claude", "both"), default="both", help="Parent Agent skill profiles to update")
    register_parser.add_argument("--force", action="store_true", help="Replace existing template files or edited owned entrypoints")
    register_parser.add_argument("--dry-run", action="store_true", help="Validate and show registration changes without writing files")
    register_parser.add_argument("--json", action="store_true", help="Print structured registration results")
    list_parser = template_actions.add_parser("list", help="List registered task capabilities")
    list_parser.add_argument("--json", action="store_true", help="Print structured registration metadata")
    unregister_parser = template_actions.add_parser("unregister", help="Remove discovery entrypoints; preserve template and prompt files")
    unregister_parser.add_argument("name")
    unregister_parser.add_argument("--dry-run", action="store_true")
    unregister_parser.add_argument("--json", action="store_true")

    expert_parser = sub.add_parser("expert", help="Manage the portable expert agents file")
    expert_actions = expert_parser.add_subparsers(dest="expert_action", required=True)
    expert_actions.add_parser("list", help="Configured experts")
    expert_show = expert_actions.add_parser("show", help="One expert's settings")
    expert_show.add_argument("name")
    expert_export = expert_actions.add_parser("export", help="Print a self-contained experts file")
    expert_export.add_argument("names", nargs="*", help="Only these experts")
    expert_import = expert_actions.add_parser("import", help="Add experts from a file (for example from another machine)")
    expert_import.add_argument("file")
    expert_import.add_argument("--replace", action="store_true", help="Replace all configured experts")
    for action in expert_actions.choices.values():
        action.add_argument("--experts", help="Experts file (default: ~/.reins/experts.yaml)")
    context_parser = sub.add_parser("context", help="List, inspect or archive chains of named agent nodes")
    context_actions = context_parser.add_subparsers(dest="context_action", required=True)
    context_list = context_actions.add_parser("list", help="Contexts and their nodes, as summaries")
    context_list.add_argument("--all", action="store_true", help="Include archived contexts")
    context_show = context_actions.add_parser("show", help="One node's chain: tasks, results and report summaries")
    context_show.add_argument("node", help="Task ID or full node name")
    context_archive = context_actions.add_parser("archive", help="Hide a node's context from the list")
    context_archive.add_argument("node", help="Task ID or full node name")
    for action in (context_list, context_show, context_archive):
        action.add_argument("--queue-dir", help="Queue directory (default: the Reins home queue)")
    for action in (context_list, context_show):
        action.add_argument("--json", action="store_true", help="Machine-readable summaries")
    migrate_parser = sub.add_parser("migrate", help="Move from ATC or Long Task Callback (LTC): shows the plan; --apply makes it")
    migrate_parser.add_argument("--apply", action="store_true",
                                help="Archive the old state and remove its service, skills and hooks")
    migrate_parser.add_argument("--wait", action="store_true",
                                help="Wait for the old daemons to finish their callbacks and tasks first")
    migrate_parser.add_argument("--only", choices=("atc", "ltc"), help="Migrate one generation and leave the other")
    setup_parser = sub.add_parser("setup", help="Install the bundled skill and user-level wakeup daemon")
    setup_parser.add_argument("--backend", choices=("auto", "systemd", "launchd", "windows-task", "screen"), default=os.environ.get("REINS_EXECUTION_BACKEND", "auto"))
    setup_parser.add_argument("--callback-only", action="store_true",
                              help="Configure callbacks for externally owned work without requiring a local task backend")
    setup_parser.add_argument("--service", choices=("auto", "systemd", "launchd", "windows-task", "supervisor", "standalone"), default="auto",
                              help="Coordinator hosting: systemd on Linux, launchd on macOS, Task Scheduler on Windows")
    setup_parser.add_argument("--skill-path", help="Skills directory to install into (overrides --skill-target)")
    setup_parser.add_argument("--with-pi-extension", action="store_true", help="Also install Pi's native live callback extension")
    setup_parser.add_argument(
        "--skill-target",
        choices=skill_install.SKILL_TARGETS,
        help="Agent home to install the skill into (default: both; all with --with-pi-extension)",
    )
    setup_parser.add_argument("--name", default="harness-reins", help="Service name / LaunchAgent label")
    setup_parser.add_argument("--queue-dir", help="Wakeup queue directory")
    setup_parser.add_argument("--interval", type=float, default=2.0, help="Daemon polling interval in seconds")
    setup_parser.add_argument("--retries", type=int, default=delivery_engine.DEFAULT_RETRIES, help="Retries after a callback is not acknowledged")
    setup_parser.add_argument("--retry-delay", type=float, default=delivery_engine.DEFAULT_RETRY_DELAY, help="Initial retry delay in seconds")
    setup_parser.add_argument("--retry-backoff", type=float, default=delivery_engine.DEFAULT_RETRY_BACKOFF, help="Retry delay multiplier")
    setup_parser.add_argument("--resume-timeout", type=float, default=delivery_engine.DEFAULT_RESUME_TIMEOUT, help="Maximum seconds for one Codex resume before retrying")
    setup_parser.add_argument("--restart-sec", type=float, default=5.0, help="Service restart delay in seconds")
    setup_parser.add_argument("--exec-start", help="Path to harness-reins executable")
    setup_parser.add_argument("--codex-bin", help="Path to codex executable used by the daemon")
    setup_parser.add_argument("--claude-bin", help="Path to claude executable used by the daemon")
    setup_parser.add_argument("--pi-bin", help="Path to the PI Agent executable used by the daemon")
    setup_parser.add_argument("--path", help="PATH environment for the daemon service")
    add_proxy_environment_flags(setup_parser)
    setup_parser.add_argument("--force", action="store_true", help="Overwrite an existing skill and service file")
    setup_parser.add_argument("--keep-skill", action="store_true",
                              help="Keep existing skill files during coordinator setup, even with --force; install if missing")
    # Accepted so existing install scripts keep working; setup generates no launcher.
    setup_parser.add_argument("--no-desktop-launcher", action="store_true", help=argparse.SUPPRESS)
    setup_parser.add_argument("--enable", action="store_true", help="Enable the service for this user")
    setup_parser.add_argument("--now", action="store_true", help="Start the service or request a safe reload")
    add_privilege_escalation_flag(setup_parser)

    wait_parser = sub.add_parser(
        "wait",
        help="Claude Code only: block until this session's callback is ready, then print it (run in the background)",
    )
    wait_parser.add_argument("--queue-dir", help="Wakeup queue directory")
    wait_parser.add_argument("--task", help="Only wait for this managed task's callback")
    wait_parser.add_argument("--session", help="Claude Code session id (default: $CLAUDE_CODE_SESSION_ID)")
    wait_parser.add_argument("--timeout", type=float, help="Give up after this many seconds (exit 3)")
    wait_parser.add_argument("--poll-interval", type=float, default=1.0, help=argparse.SUPPRESS)

    claude_parser = sub.add_parser(
        "claude",
        help="Start interactive Claude Code with the Reins channel (research preview): callbacks are pushed "
             "into the open session; pass Claude's own arguments after --",
    )
    claude_parser.add_argument("--queue-dir", help="Wakeup queue directory")
    claude_parser.add_argument("claude_args", nargs=argparse.REMAINDER, help="Arguments for claude")
    channel_parser = sub.add_parser("_claude-channel", help=argparse.SUPPRESS)
    channel_parser.add_argument("--queue-dir", help=argparse.SUPPRESS)
    channel_parser.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)
    channel_parser.add_argument("--poll-interval", type=float, default=1.0, help=argparse.SUPPRESS)

    session_parser = sub.add_parser("session", help="Name sessions on this machine so they can message each other")
    session_actions = session_parser.add_subparsers(dest="session_action", required=True)
    session_register = session_actions.add_parser("register", help="Give the current session a name")
    session_register.add_argument("name", help="Lowercase letters, digits, '-' or '_', up to 32 characters")
    session_register.add_argument("--replace", action="store_true", help="Take over the name, or rename this session")
    session_register.add_argument("--agent", choices=AGENT_NAMES, help="Harness of the session (default: auto-detect)")
    session_register.add_argument("--session", help="Session to name (default: the one this command runs in)")
    session_register.add_argument("--cwd", help="The session's working directory (default: here)")
    session_register.add_argument("--queue-dir", help="Wakeup queue directory")
    session_list = session_actions.add_parser("list", help="Named sessions, and how a message reaches each one now")
    session_list.add_argument("--json", action="store_true", help="Print JSON")
    session_list.add_argument("--queue-dir", help="Wakeup queue directory")
    session_unregister = session_actions.add_parser("unregister", help="Remove a name")
    session_unregister.add_argument("name")
    session_unregister.add_argument("--queue-dir", help="Wakeup queue directory")

    def add_message_flags(message_parser: argparse.ArgumentParser) -> None:
        message_parser.add_argument("--message", "-m", help="The message text")
        message_parser.add_argument("--message-file", help="Read the message text from this file, or - for stdin")
        message_parser.add_argument("--subject", help="One line; default: the text's first line")
        message_parser.add_argument("--ref", action="append", help="git:REPO:REF or path:PATH; repeatable")
        message_parser.add_argument("--wake", choices=("never", "resume"), default="never",
                                    help="never (default): nothing is started, a closed session reads it when it "
                                         "next opens. resume: a closed session may be resumed headless, which costs "
                                         "model usage and is limited per thread")
        message_parser.add_argument("--json", action="store_true", help="Print JSON")
        message_parser.add_argument("--agent", choices=AGENT_NAMES, help=argparse.SUPPRESS)
        message_parser.add_argument("--session", help=argparse.SUPPRESS)
        message_parser.add_argument("--queue-dir", help="Wakeup queue directory")

    send_parser = sub.add_parser("send", help="Send a message to a named session on this machine")
    send_parser.add_argument("name", help="The session's name; see reins session list")
    add_message_flags(send_parser)
    reply_parser = sub.add_parser("reply", help="Reply to a message; this also acknowledges it")
    reply_parser.add_argument("id", help="The message ID")
    add_message_flags(reply_parser)
    inbox_parser = sub.add_parser("inbox", help="Unread messages of this session")
    inbox_parser.add_argument("--name", help="Another session's inbox")
    inbox_parser.add_argument("--all", action="store_true", help="Include messages already acknowledged or expired")
    inbox_parser.add_argument("--sent", action="store_true", help="Messages this session sent, and what became of them")
    inbox_parser.add_argument("--thread", help="Every message of one thread")
    inbox_parser.add_argument("--json", action="store_true", help="Print JSON")
    inbox_parser.add_argument("--agent", choices=AGENT_NAMES, help=argparse.SUPPRESS)
    inbox_parser.add_argument("--session", help=argparse.SUPPRESS)
    inbox_parser.add_argument("--queue-dir", help="Wakeup queue directory")
    message_parser = sub.add_parser("message", help="Read one message; review threads stopped at a limit")
    message_actions = message_parser.add_subparsers(dest="message_action", required=True)
    message_show = message_actions.add_parser("show", help="One message, with its state")
    message_show.add_argument("id", help="The message ID or callback ID")
    message_show.add_argument("--json", action="store_true", help="Print JSON")
    message_show.add_argument("--queue-dir", help="Wakeup queue directory")
    message_alerts = message_actions.add_parser("alerts", help="Threads stopped at a limit")
    message_alerts.add_argument("--clear", action="store_true", help="Remove the alerts")
    message_alerts.add_argument("--queue-dir", help="Wakeup queue directory")

    ack_parser = sub.add_parser("ack", help="Mark a daemon callback as successfully received")
    ack_parser.add_argument("--queue-dir", help="Wakeup queue directory")
    ack_parser.add_argument("callback_id", nargs="?", help="Callback id to acknowledge")
    ack_parser.add_argument("--id", help="Callback id to acknowledge (same as the positional id)")
    ack_parser.add_argument("--message", help="Optional acknowledgement note")

    goal_parser = sub.add_parser("goal", help="Manage persistent long-running goal acknowledgements")
    goal_sub = goal_parser.add_subparsers(dest="goal_mode", required=True)
    goal_start_parser = goal_sub.add_parser("start", help="Create an active goal")
    goal_start_parser.add_argument("--id", help="Optional stable goal id")
    goal_start_parser.add_argument("--queue-dir", help="Queue root that owns the goal")
    goal_start_parser.add_argument("--session", required=True, help="Bound agent session")
    goal_start_parser.add_argument(
        "--agent",
        choices=AGENT_NAMES,
        help="Agent that owns the session (default: auto-detect from the environment)",
    )
    goal_start_parser.add_argument("--cwd", default=os.getcwd())
    goal_start_parser.add_argument("--task", required=True)
    goal_start_parser.add_argument(
        "--plan-file",
        required=True,
        help="Mutable UTF-8 YAML goal path that governs progress and completion",
    )
    goal_start_parser.add_argument("--idle-seconds", type=float, default=goals.DEFAULT_GOAL_IDLE_SECONDS)
    goal_check_parser = goal_sub.add_parser("check", help="Inspect and record the current YAML goal path")
    goal_check_parser.add_argument("--queue-dir", help="Queue root that owns the goal")
    goal_check_parser.add_argument("--id", required=True)
    goal_set_plan_parser = goal_sub.add_parser(
        "set-plan", help="Attach or replace the mutable YAML plan for an existing goal"
    )
    goal_set_plan_parser.add_argument("--queue-dir", help="Queue root that owns the goal")
    goal_set_plan_parser.add_argument("--id", required=True)
    goal_set_plan_parser.add_argument("--plan-file", required=True)
    goal_ack_parser = goal_sub.add_parser("ack", help="Acknowledge completion or blocked conditions")
    goal_ack_parser.add_argument("--queue-dir", help="Queue root that owns the goal")
    goal_ack_parser.add_argument("--id", required=True)
    goal_ack_parser.add_argument("--state", choices=["completed", "blocked_conditions"], required=True)
    goal_ack_parser.add_argument(
        "--plan-sha256",
        help="Digest printed by the latest goal check; required for completed",
    )
    goal_ack_parser.add_argument("--message")
    goal_ack_parser.add_argument("--condition")
    goal_ack_parser.add_argument("--email-to")
    goal_ack_parser.add_argument("--email-after", type=float, default=goals.DEFAULT_BLOCKED_EMAIL_SECONDS)
    goal_resume_parser = goal_sub.add_parser("resume", help="Explicitly resume a blocked goal")
    goal_resume_parser.add_argument("--queue-dir", help="Queue root that owns the goal")
    goal_resume_parser.add_argument("--id", required=True)

    cancel_parser = sub.add_parser("cancel", help="Cancel queued callbacks before they retry or complete")
    cancel_parser.add_argument("--queue-dir", help="Wakeup queue directory")
    cancel_target = cancel_parser.add_mutually_exclusive_group(required=True)
    cancel_target.add_argument("--id", help="Callback request id to cancel")
    cancel_target.add_argument("--all", action="store_true", help="Cancel all active, pending, and running callbacks")
    cancel_parser.add_argument("--message", help="Optional cancellation note")

    status_parser = sub.add_parser("status", help="List queued callbacks that still need handling")
    status_parser.add_argument("--queue-dir", help="Wakeup queue directory")
    status_parser.add_argument(
        "--state",
        action="append",
        choices=[layout.ACTIVE_STATE, "pending", "running", "failed"],
        help="Queue state to include; repeat as needed (default: pending and running)",
    )
    status_parser.add_argument("--limit", type=int, default=5, help="Maximum items to print")
    status_parser.add_argument("--quiet-empty", action="store_true", help="Print nothing when no items exist")
    status_parser.add_argument("--shell-hook", action="store_true", help="Use compact terminal-startup output")

    shell_hook_parser = sub.add_parser("install-shell-hook", help="Install an idempotent pending-status check in Bash startup")
    shell_hook_parser.add_argument("--rc-file", help="Bash rc file (default: ~/.bashrc)")
    shell_hook_parser.add_argument("--command", help="Executable path embedded in the hook")

    argv = sys.argv[1:]
    prompt = None
    # Before Python 3.12, argparse cannot interleave a positional (the node or expert
    # name), options and a nargs="*" prompt. The prompt always follows "--", so split
    # it off first; options before "--" then parse the same on every version.
    if argv[:1] == ["agent"] and argv[1:2] and argv[1] in ("continue", "fork", "expert") and "--" in argv:
        cut = argv.index("--")
        argv, prompt = argv[:cut], argv[cut + 1:]
    args = parser.parse_args(argv)
    if prompt is not None:
        args.agent_prompt = prompt
    if args.mode not in LOCAL_MODES and not args.mode.startswith("_"):
        from .install import canonical
        forwarded = canonical.forward(sys.argv[1:])
        if forwarded is not None:
            return forwarded
    if args.mode in ("session", "send", "reply", "inbox", "message"):
        from .commands import messages as message_commands
        if args.mode == "session":
            return {"register": message_commands.session_register, "list": message_commands.session_list,
                    "unregister": message_commands.session_unregister}[args.session_action](args)
        if args.mode == "message":
            return {"show": message_commands.message_show, "alerts": message_commands.message_alerts}[args.message_action](args)
        if args.mode == "inbox":
            return message_commands.inbox(args)
        args.operation = "done"  # what doctor rechecks: a message needs what any queued callback needs
        return invoke_with_diagnostics(args, message_commands.send if args.mode == "send" else message_commands.reply)
    if args.mode == "expert":
        from .commands import experts as expert_commands
        return expert_commands.command(args)
    if args.mode == "context":
        from .commands import contexts as context_commands
        return {"list": context_commands.list_contexts, "show": context_commands.show_context,
                "archive": context_commands.archive_context}[args.context_action](args)
    if args.mode == "migrate":
        from .install import migrate
        return migrate.command(args)
    if args.mode == "desktop":
        print("reins: the Desktop launcher is retired. Quit Codex Desktop and open it normally; callbacks use the "
              "native Codex queue. Move a saved desktop callback with: reins retry --id ID --callback-mode codex-queue",
              file=sys.stderr)
        return 2
    if args.mode == "retry":
        return callback_commands.retry_callback(args)
    if args.mode == "doctor":
        return diagnostics.doctor(args)
    if args.mode == "done":
        return invoke_with_diagnostics(args, work_commands.done)
    if args.mode == "run":
        if args.wrapped_command and args.wrapped_command[0] == "--":
            args.wrapped_command = args.wrapped_command[1:]
        return invoke_with_diagnostics(args, work_commands.run)
    if args.mode == "agent":
        return invoke_with_diagnostics(args, work_commands.agent)
    if args.mode == "_screen-worker":
        return task_worker.run_screen_worker(args)
    if args.mode == "_task-worker":
        return task_worker.run_task_worker(args)
    if args.mode == "_delivery-worker":
        return delivery_engine.delivery_worker_main()
    if args.mode == "_windows-coordinator":
        from .install import windows_service
        return windows_service.run_coordinator(Path(args.config_file))
    if args.mode == "daemon":
        return tower.daemon(args)
    if args.mode == "install-systemd":
        return services.install_systemd(args)
    if args.mode == "install-launchd":
        return services.install_launchd(args)
    if args.mode == "install-windows-task":
        return services.install_windows_task(args)
    if args.mode == "uninstall-windows-task":
        from .install import windows_service
        return windows_service.uninstall(args)
    if args.mode == "install-skill":
        return skill_install.install_skill(args)
    if args.mode in ("install-pi-extension", "pi"):
        try:
            return pi_channel.install_extension(args) if args.mode == "install-pi-extension" else pi_channel.managed_pi(args)
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"reins: {exc}", file=sys.stderr)
            return 2
    if args.mode == "template":
        return template_commands.template_command(args)
    if args.mode == "setup":
        return services.setup(args)
    if args.mode == "prompt-policy":
        return reminder_policy.callback_prompt_policy(args)
    if args.mode == "ack":
        return callback_commands.ack(args)
    if args.mode == "wait":
        return claude_session.wait(args)
    if args.mode == "claude":
        from .harness.claude import channel as claude_channel
        return claude_channel.launch(args)
    if args.mode == "_claude-channel":
        from .harness.claude import channel as claude_channel
        return claude_channel.serve(args)
    if args.mode == "goal":
        if args.goal_mode == "start":
            return goal_commands.goal_start(args)
        if args.goal_mode == "check":
            return goal_commands.goal_check(args)
        if args.goal_mode == "set-plan":
            return goal_commands.goal_set_plan(args)
        if args.goal_mode == "ack":
            return goal_commands.goal_ack(args)
        return goal_commands.goal_resume(args)
    if args.mode == "cancel":
        return callback_commands.cancel(args)
    if args.mode == "status":
        return callback_commands.status(args)
    if args.mode == "install-shell-hook":
        return skill_install.install_shell_hook(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
