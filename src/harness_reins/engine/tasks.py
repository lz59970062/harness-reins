"""Managed task records and their OS owners: launch, observe, recover and queue the callback."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from . import routes
from ..harness.pi import channel as pi_channel
from ..harness import AGENTS, CHILD_AGENT_NAMES, ChildOptions, get_agent, get_child_agent
from ..harness.base import DEFAULT_APPROVALS_REVIEWER, DEFAULT_APPROVAL_POLICY, DEFAULT_SANDBOX_MODE, DEFAULT_CLAUDE_PERMISSION_MODE
from ..runtime import worker_command
from ..owners import LaunchError, OwnerState, ScreenBackend, SystemdUserBackend, LaunchdBackend
from ..owners.windows import WindowsBackend
from ..core import capabilities, ids, records, results, targets
from ..store import layout
from ..system import files
from ..owners import host
from ..store import records as queue_store
from . import delivery as delivery_engine
from . import dispatch


MANAGED_WORKER_TOKEN_PREFIX = "reins_"
MANAGED_WORKER_HANDSHAKE_SECONDS = 1.0
MANAGED_LAUNCH_MAX_ATTEMPTS = 3
MANAGED_LAUNCH_RETRY_DELAY = 1.0
MANAGED_LAUNCH_RETRY_BACKOFF = 2.0
SCREEN_BIN_ENV = "REINS_SCREEN_BIN"
AGENT_PROMPT_FILE_NAME = "agent-prompt.txt"
AGENT_RESULT_FILE_NAME = "agent-result.txt"
STDIN_FILE_NAME = "stdin"
STDIN_WAIT_SECONDS = 0.2


def piped_input(stream=None, wait: float = STDIN_WAIT_SECONDS) -> bytes | None:
    """Data piped or redirected into this submission, such as a heredoc script, or None.

    ``reins run -- python3 - <<EOF`` hands the script to reins, not to the task, so reins
    keeps it for the task's stdin. A terminal, an empty input, or an open pipe that
    stays silent is not input and is left unread.
    """
    stream = sys.stdin if stream is None else stream
    try:
        if stream is None or stream.isatty():
            return None
        descriptor = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    if os.name == "nt":
        return None  # select() cannot wait on Windows pipes
    import select
    try:
        ready, _, _ = select.select([descriptor], [], [], wait)
    except (OSError, ValueError):
        return None
    if not ready:
        return None
    chunks = []
    while True:
        chunk = os.read(descriptor, 1 << 16)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks) or None


def write_private_bytes(path: Path, data: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
AGENT_SYSTEM_PROMPT_FILE_NAME = "agent-system-prompt.md"
CHILD_AGENT_PARENT_ENV_NAMES = tuple(
    dict.fromkeys(env_name for name in CHILD_AGENT_NAMES for env_name in get_child_agent(name).parent_env_names)
)


def screen_binary() -> str | None:
    configured = os.environ.get(SCREEN_BIN_ENV)
    return shutil.which(configured) if configured else shutil.which("screen")


def screen_required_error() -> str:
    installation = ("`brew install screen`" if sys.platform == "darwin"
                    else "`sudo apt install screen` or `sudo dnf install screen`")
    return (
        "GNU screen is required for durable task ownership but was not found. "
        f"Install it first (for example: {installation}) "
        f"and rerun the command. Override discovery with ${SCREEN_BIN_ENV}."
    )


def write_managed_task(root: Path, task: dict[str, object]) -> None:
    task_id = task.get("id")
    if not isinstance(task_id, str):
        raise ValueError("managed task requires an id")
    directory = layout.managed_task_dir(root, task_id)
    queue_store.private_directory(directory)
    queue_store.write_request(layout.managed_task_path(root, task_id), task)


def load_managed_task(path: Path) -> dict[str, object]:
    task = json.loads(files.read_text(path))
    if not isinstance(task, dict) or task.get("schema") != records.TASK_SCHEMA:
        raise ValueError(f"not an {records.TASK_SCHEMA} record; tasks from Long Task Callback need reins migrate")
    if task.get("id") != path.parent.name:
        raise ValueError("managed task id must match its directory")
    missing = capabilities.unsupported(task.get("requires"))
    if missing:
        raise ValueError("task requires unsupported capabilities: " + ", ".join(missing))
    get_agent(str(task.get("agent", "codex")))
    if task.get("task_kind") == "agent":
        get_child_agent(str(task.get("agent_worker", "")))
    if "execution_backend" not in task:
        raise ValueError("task records require an execution backend")
    backend = task["execution_backend"]
    if task.get("callback_mode") not in ("cli", "codex-queue", "desktop", "app-server", "manual"):
        raise ValueError("task requires an explicit callback mode")
    if task.get("callback_mode") == "codex-queue":
        from . import native_queue
        native_queue.validate_route(task)
    if task.get("callback_mode") == "app-server":
        from . import native
        native.validate_route(task)
    if set(task["requires"]) & {capabilities.PI_STEER, capabilities.PI_FOLLOW_UP}:
        if dispatch.request_agent(task) != "pi":
            raise ValueError("only Pi parents can require Pi delivery")
        pi_channel.validate_route(task)
    if backend not in ("screen", "systemd-user", "launchd", "windows-task"):
        raise ValueError(f"unsupported execution backend: {backend!r}")
    if not isinstance(task.get("queue_dir"), str):
        raise ValueError("managed task is missing queue metadata")
    if backend == "screen" and not isinstance(task.get("screen_session"), str):
        raise ValueError("managed task is missing screen metadata")
    return task


def report_fields(result: dict[str, object]) -> dict[str, object]:
    """The part of a child's report a callback envelope shows; claims stay marked as claims."""
    report = result.get("report")
    if isinstance(report, dict):
        return {"report_status": report.get("status"), "report_artifact_count": len(report.get("artifacts") or []),
                "report_error": result.get("report_error")}
    if result.get("kind") == "agent" and result.get("report_error"):
        return {"report_status": None, "report_artifact_count": 0, "report_error": result["report_error"]}
    return {}


def managed_callback_exists(root: Path, task_id: str) -> bool:
    """Whether the task's completion callback (its first) was already queued."""
    callback = ids.callback_id(task_id)
    if layout.ack_path(root, callback).exists():
        return True
    return any(
        layout.request_path(root, state, callback).exists()
        for state in (layout.ACTIVE_STATE, "pending", "running", "done", "failed", "canceled")
    )


def screen_owner_state(session: str) -> OwnerState:
    return ScreenBackend().probe(session)


def screen_session_exists(session: str) -> bool:
    """Legacy observation API; use tri-state probing to authorize launches."""
    return screen_owner_state(session) == OwnerState.ALIVE


def managed_task_namespace(task: dict[str, object]) -> argparse.Namespace:
    target = task.get("target")
    return argparse.Namespace(
        cwd=str(task["cwd"]),
        task=str(task["task"]),
        command=str(task["command"]),
        exit_code=task.get("exit_code"),
        message=task.get("message"),
        session=target.get("session") if isinstance(target, dict) and target.get("kind") == "session" else None,
        agent=str(task.get("agent", "codex")),
        queue_dir=str(task["queue_dir"]),
        goal_id=task.get("goal_id"),
        approvals_reviewer=str(task.get("approvals_reviewer") or DEFAULT_APPROVALS_REVIEWER),
        approval_policy=str(task.get("approval_policy") or DEFAULT_APPROVAL_POLICY),
        sandbox_mode=str(task.get("sandbox_mode") or DEFAULT_SANDBOX_MODE),
        permission_mode=str(task.get("permission_mode") or DEFAULT_CLAUDE_PERMISSION_MODE),
        strict=bool(task.get("strict", False)),
        dry_run=False,
        wrapped_command=[str(part) for part in task["wrapped_command"]],
        _callback_id=ids.callback_id(str(task["id"])),
        _callback_target=target,
        _callback_target_source=targets.binding_of(task, "managed-task"),
        _callback_agent=str(task.get("agent", "codex")),
        callback_format=str(task.get("callback_format", "compact")),
        _callback_route={k: task[k] for k in routes.ROUTE_FIELDS if k in task},
    )


def managed_task_prompt(
    task: dict[str, object],
    *,
    outcome: str,
    duration: float | None = None,
) -> str:
    args = managed_task_namespace(task)
    log_path = task.get("log_path")
    details = [
        f"Managed task id: {task['id']}",
        f"Execution backend: {task.get('execution_backend', 'screen')}",
        f"Owner: {task.get('execution_owner') or task.get('screen_session')}",
        f"Task log: {log_path}",
    ]
    if task.get("task_kind") == "agent":
        worker = str(task.get("agent_worker", "unknown"))
        details.extend(
            [
                f"Child agent: {get_child_agent(worker).display_name}",
                f"Agent result: {task.get('agent_result_path')}",
            ]
        )
        if task.get("agent_system_prompt_path"):
            details.extend([
                f"System prompt source: {task.get('agent_system_prompt_source')}",
                f"System prompt snapshot: {task['agent_system_prompt_path']}",
            ])
    if task.get("agent_template"):
        details.extend([
            f"Agent template: {task['agent_template']} v{task.get('agent_template_version')}",
            f"Template source: {task.get('agent_template_source') or 'built-in (legacy task)'}",
            f"Child model: {task.get('child_model') or 'CLI default'}",
            f"Child reasoning effort: {task.get('child_reasoning_effort') or 'CLI default'}",
        ])
    if task.get("agent_template") == "test" and task.get("agent_template_source", "builtin:test") == "builtin:test":
        details.extend([
            f"Test author process exit code: {task.get('exit_code', 'unknown')}",
            "This is a test-authoring handoff, NOT a test execution result. Tests are expected to be unexecuted; verify the report.",
            "First inspect the child exit status, result report and changed files. If the child failed, the report is empty, or delivery is blocked/partial, diagnose and resolve the incomplete handoff before running tests.",
            "The parent agent must check requirement coverage and proposed commands, then execute the tests and report actual commands and results.",
            "Distinguish implementation defects, test defects, environment failures and ambiguous requirements. Never weaken assertions merely to get green results; justify changes against the requirements.",
        ])
    if task.get("agent_template_handoff"):
        details.extend([
            "Template handoff instructions (saved at submission):",
            str(task["agent_template_handoff"]),
            "Check the child exit status and result artifacts before following the handoff. Process completion alone does not establish a successful result.",
        ])
    if outcome != "completed":
        reason = str(task.get("recovery_reason") or outcome)
        details.extend(
            [
                f"Recovery state: {reason}",
                "The task was not automatically restarted.",
                "Inspect the local task log, workspace outputs, and checkpoints.",
                "Then either recreate the task through the standard `reins run` flow, "
                "supplement its completed status if artifacts prove it finished, or record the exact blocked condition.",
            ]
        )
    original_message = task.get("message")
    args.message = "\n".join(
        ([str(original_message), ""] if isinstance(original_message, str) and original_message else []) + details
    )
    return dispatch.build_prompt(args, duration)


def managed_callback_request(
    task: dict[str, object], prompt: str, *, duration: float | None = None,
) -> dict[str, object]:
    args = managed_task_namespace(task)
    request = dispatch.make_request(args, prompt, duration=duration)
    request.update(
        {
            "managed_task_id": task["id"],
            "managed_task_state": task["state"],
            "execution_backend": task.get("execution_backend", "screen"),
            "screen_session": task.get("screen_session"),
            "log_path": task["log_path"],
            "outcome": task.get("outcome", "unknown"),
        }
    )
    result_path = layout.managed_result_path(Path(str(task["queue_dir"])), str(task["id"]))
    if result_path.exists():
        request["result_path"] = str(result_path)
    for key in ("report_status", "report_artifact_count", "report_error"):
        if key in task:
            request[key] = task[key]
    if isinstance(task.get("node"), dict):
        request["node_name"] = task["node"].get("name")
    if isinstance(task.get("exit_code"), int):
        request["exit_code"] = task["exit_code"]
    if task.get("recovery_reason"):
        request["recovery_reason"] = task["recovery_reason"]
    if task.get("task_kind") == "agent":
        request["task_kind"] = "agent"
        request["agent_worker"] = task.get("agent_worker")
        request["agent_result_path"] = task.get("agent_result_path")
        for key in ("expert", "agent_template", "agent_template_version", "agent_template_source",
                    "agent_template_handoff", "child_model", "child_reasoning_effort",
                    "agent_system_prompt_path", "agent_system_prompt_source"):
            if key in task:
                request[key] = task[key]
    return request


def queue_managed_task_callback(root: Path, task: dict[str, object]) -> bool:
    task_id = str(task["id"])
    if managed_callback_exists(root, task_id):
        if not task.get("callback_queued"):
            task["callback_queued"] = True
            task["callback_observed_at"] = time.time()
            write_managed_task(root, task)
        return False
    completed_at = task.get("completed_at")
    started_at = task.get("started_at")
    duration = (
        float(completed_at) - float(started_at)
        if isinstance(completed_at, (int, float)) and isinstance(started_at, (int, float))
        else None
    )
    prompt = managed_task_prompt(task, outcome=str(task.get("outcome", "unknown")), duration=duration)
    request = managed_callback_request(task, prompt, duration=duration)
    if dispatch.enqueue_existing_request(root, request, prompt) != 0:
        return False
    task["callback_queued"] = True
    task["callback_queued_at"] = time.time()
    write_managed_task(root, task)
    return True


def record_unfinished_result(root: Path, task: dict[str, object], state: str, reason: str, at: float) -> None:
    """Write the result of a task Reins never saw exit; an observed exit is never overwritten."""
    path = layout.managed_result_path(root, str(task["id"]))
    if path.exists():
        return
    queue_store.write_request(path, results.ended(task, state=state, recovery_reason=reason, at=at))


def mark_managed_task_interrupted(
    root: Path,
    task: dict[str, object],
    reason: str,
) -> bool:
    task["state"] = "interrupted"
    task["outcome"] = "unknown"
    task["recovery_reason"] = reason
    task["interrupted_at"] = time.time()
    task.pop("exit_code", None)
    write_managed_task(root, task)
    record_unfinished_result(root, task, "unknown" if "unknown" in reason else "interrupted", reason, task["interrupted_at"])
    return queue_managed_task_callback(root, task)


def mark_managed_task_foreign_host(root: Path, task: dict[str, object]) -> None:
    task["state"] = "foreign_host"
    task["outcome"] = "unknown"
    task["recovery_reason"] = "cross_host_recovery_unsupported"
    task["interrupted_at"] = time.time()
    task.pop("exit_code", None)
    write_managed_task(root, task)
    record_unfinished_result(root, task, "unknown", "cross_host_recovery_unsupported", task["interrupted_at"])
    print(
        f"reins: task {task['id']} belongs to another host; "
        "cross-host launch and callback recovery are unsupported",
        file=sys.stderr,
    )


def managed_launch_attempt_count(task: dict[str, object]) -> int:
    value = task.get("launch_attempt_count", 0)
    return value if isinstance(value, int) and value >= 0 else 0


def managed_launch_retry_delay(attempt_count: int) -> float:
    return MANAGED_LAUNCH_RETRY_DELAY * (MANAGED_LAUNCH_RETRY_BACKOFF ** max(0, attempt_count - 1))


def discard_managed_launch_credentials(task: dict[str, object]) -> None:
    task.pop("token", None)
    environment_path = task.get("environment_path")
    if not isinstance(environment_path, str):
        return
    path = Path(environment_path)
    try:
        path.unlink()
        queue_store.fsync_directory(path.parent)
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"reins: warning: could not remove failed launch environment {path}: {exc}", file=sys.stderr)


def mark_managed_task_launch_failed(root: Path, task: dict[str, object], reason: str) -> bool:
    task["state"] = "launch_failed"
    task["outcome"] = "unknown"
    task["recovery_reason"] = reason
    task["launch_failed_at"] = time.time()
    task.pop("next_launch_at", None)
    task.pop("exit_code", None)
    discard_managed_launch_credentials(task)
    write_managed_task(root, task)
    record_unfinished_result(root, task, "launch_failed", reason, task["launch_failed_at"])
    queue_managed_task_callback(root, task)
    return True


def record_managed_launch_failure(root: Path, task: dict[str, object], reason: str) -> bool:
    if task.get("state") == "launching" and "launch_attempt_count" not in task:
        return mark_managed_task_launch_failed(
            root,
            task,
            f"{reason}_legacy_attempt_count_unknown",
        )
    attempt_count = managed_launch_attempt_count(task)
    now = time.time()
    task["last_launch_failure"] = reason
    task["last_launch_failure_at"] = now
    if attempt_count >= MANAGED_LAUNCH_MAX_ATTEMPTS:
        return mark_managed_task_launch_failed(
            root,
            task,
            f"{reason}_after_{attempt_count}_attempts",
        )

    delay = managed_launch_retry_delay(attempt_count)
    task["state"] = "submitted"
    task["outcome"] = "pending"
    task["next_launch_at"] = now + delay
    write_managed_task(root, task)
    print(
        f"reins: managed task {task['id']} worker launch attempt {attempt_count}/"
        f"{MANAGED_LAUNCH_MAX_ATTEMPTS} failed ({reason}); retrying in {delay:.1f}s",
        file=sys.stderr,
    )
    return True


def launch_managed_screen(root: Path, task: dict[str, object]) -> bool:
    command = screen_binary()
    if command is None:
        return mark_managed_task_interrupted(root, task, "screen_required_but_unavailable")
    session = str(task["screen_session"])
    if screen_owner_state(session) != OwnerState.ABSENT:
        return False
    environment_path = Path(str(task["environment_path"]))
    if not environment_path.exists():
        return mark_managed_task_interrupted(root, task, "launch_environment_missing")
    attempt_count = managed_launch_attempt_count(task)
    if attempt_count >= MANAGED_LAUNCH_MAX_ATTEMPTS:
        return mark_managed_task_launch_failed(
            root,
            task,
            f"worker_start_handshake_failed_after_{attempt_count}_attempts",
        )
    attempt_count += 1
    task["state"] = "launching"
    task["launch_attempt_count"] = attempt_count
    task["launch_requested_at"] = time.time()
    task["launch_boot_id"] = host.current_boot_id()
    task["screen_submitted_at"] = time.time()
    task.pop("next_launch_at", None)
    write_managed_task(root, task)
    argv = worker_command("_screen-worker", "--task-file",
                          str(layout.managed_task_path(root, str(task["id"]))),
                          f"--token={task['token']}")
    try:
        ScreenBackend().launch(session, argv, Path(str(task["cwd"])), Path(str(task["log_path"])))
    except LaunchError as exc:
        if exc.uncertain:
            task["launch_uncertain"] = True
            task["launch_error"] = str(exc)
            write_managed_task(root, task)
            return True
        return record_managed_launch_failure(root, task, "screen_launcher_unavailable")
    print(f"reins: started managed task {task['id']} in screen session {session}", file=sys.stderr)
    return True


def select_execution_backend(args: argparse.Namespace) -> str:
    """Resolve once at submission; recovery never silently changes ownership."""
    # Direct API callers from 0.6 keep their historical screen behavior.
    choice = getattr(args, "backend", "screen")
    if choice not in ("auto", "screen", "systemd", "launchd", "windows-task"):
        raise ValueError(f"unsupported execution backend: {choice!r}")
    if sys.platform == "win32":
        if choice not in ("auto", "windows-task"):
            raise ValueError("native Windows requires --backend windows-task (or auto)")
        if WindowsBackend().available():
            return "windows-task"
        raise ValueError("Windows Task Scheduler is unavailable for this user; inspect scheduler access")
    if choice == "windows-task":
        raise ValueError("Windows Task Scheduler requires native Windows")
    if sys.platform not in ("linux", "darwin"):
        raise ValueError("Reins supports Linux, macOS and native Windows")
    if choice == "launchd" or (choice == "auto" and sys.platform == "darwin"):
        if LaunchdBackend().available():
            return "launchd"
        if choice == "launchd":
            raise ValueError("launchd GUI domain unavailable; log in to macOS or explicitly select --backend screen")
    if choice in ("auto", "systemd") and SystemdUserBackend().available():
        return "systemd-user"
    if choice == "systemd":
        raise ValueError("systemd user manager unavailable; start a user session or explicitly select --backend screen")
    if screen_binary() is None:
        raise ValueError(screen_required_error())
    return "screen"


def managed_owner_state(task: dict[str, object]) -> OwnerState:
    if task.get("execution_backend", "screen") == "screen":
        return screen_owner_state(str(task["screen_session"]))
    owner = task.get("execution_owner")
    if not isinstance(owner, str) or not owner:
        return OwnerState.UNKNOWN
    return native_backend(str(task["execution_backend"])).probe(owner)


def native_backend(name: str):
    if name == "windows-task":
        return WindowsBackend()
    if name == "launchd":
        return LaunchdBackend()
    if name == "systemd-user":
        return SystemdUserBackend()
    raise ValueError(f"unsupported native backend: {name!r}")


def collect_managed_owner(root: Path, task: dict[str, object]) -> None:
    if task.get("execution_backend") not in ("launchd", "windows-task") or task.get("owner_collected_at"):
        return
    owner = task.get("execution_owner")
    if (task.get("machine_id") == host.current_machine_id() and isinstance(owner, str)
            and native_backend(str(task["execution_backend"])).collect(owner)):
        task["owner_collected_at"] = time.time()
        write_managed_task(root, task)


def launch_managed_task(root: Path, task: dict[str, object]) -> bool:
    """Persist launch intent before contacting the independent OS owner.

    An ambiguous submission is reconciled, never resent. Per-attempt owner names
    and worker admission checks protect against delayed starts after a crash.
    """
    if task.get("execution_backend", "screen") == "screen":
        return launch_managed_screen(root, task)
    if not Path(str(task["environment_path"])).exists():
        return mark_managed_task_interrupted(root, task, "launch_environment_missing")
    backend = native_backend(str(task["execution_backend"]))
    attempt = managed_launch_attempt_count(task) + 1
    owner = backend.owner_name(str(task["id"]), attempt, root)
    task.update(state="launching", launch_attempt_count=attempt,
                launch_requested_at=time.time(), execution_owner=owner,
                launch_boot_id=host.current_boot_id())
    task.pop("next_launch_at", None)
    write_managed_task(root, task)
    argv = worker_command("_task-worker", "--task-file",
                          str(layout.managed_task_path(root, str(task["id"]))),
                          "--attempt", str(attempt))
    try:
        backend.launch(owner, argv, Path(str(task["cwd"])), Path(str(task["log_path"])))
    except LaunchError as exc:
        if not exc.uncertain:
            return record_managed_launch_failure(root, task, "native_launcher_unavailable")
        task["launch_uncertain"] = True
        task["launch_error"] = str(exc)
        write_managed_task(root, task)
        print(f"reins: task {task['id']} launch is uncertain; reconciling without resubmission", file=sys.stderr)
    return True


def recover_managed_tasks(root: Path) -> int:
    tasks_root = layout.managed_tasks_root(root)
    if not tasks_root.exists():
        return 0
    current_boot = host.current_boot_id()
    current_machine = host.current_machine_id()
    processed = 0
    for path in sorted(tasks_root.glob("*/task.json")):
        lock: tuple[object, Path] | None = None
        try:
            task = load_managed_task(path)
            task_id = str(task["id"])
            lock = queue_store.acquire_owner_lock(root, f"managed-task-{task_id}", blocking=False)
            if lock is None:
                continue
            # The worker may have completed while we acquired ownership.
            task = load_managed_task(path)
            state = task.get("state")
            if state == "completed":
                if queue_managed_task_callback(root, task):
                    processed += 1
                collect_managed_owner(root, task)
                continue
            if state in ("interrupted", "launch_failed"):
                if queue_managed_task_callback(root, task):
                    processed += 1
                collect_managed_owner(root, task)
                continue
            if state not in ("submitted", "launching", "running"):
                continue
            if task.get("machine_id") != current_machine:
                mark_managed_task_foreign_host(root, task)
                processed += 1
                continue
            # Results take priority over an unavailable manager or stale owner.
            result_path = layout.managed_result_path(root, task_id)
            if state in ("running", "launching") and result_path.exists():
                result = json.loads(result_path.read_text(encoding="utf-8"))
                task["exit_code"] = results.observed_exit(result, task_id)
                ended = results.seconds(result["observed"].get("ended_at"))
                if ended is not None:
                    task["completed_at"] = ended
                task.update(report_fields(result))
                task["state"] = "completed"
                task["outcome"] = "completed"
                write_managed_task(root, task)
                if queue_managed_task_callback(root, task):
                    processed += 1
                continue
            recorded_boot = (task.get("boot_id") if state == "running"
                             else task.get("launch_boot_id") or task.get("submission_boot_id"))
            rebooted = bool(current_boot and recorded_boot and recorded_boot != current_boot)
            if rebooted:
                reason = "interrupted_by_host_reboot" if state == "running" else "not_started_before_host_reboot"
                if mark_managed_task_interrupted(root, task, reason):
                    processed += 1
                continue
            if state in ("running", "launching"):
                owner_state = managed_owner_state(task)
                if owner_state != OwnerState.ABSENT:
                    # UNKNOWN is not evidence of death. In particular a broken
                    # user bus cannot authorize a second execution attempt.
                    continue
                if state == "running":
                    reason = ("screen_disappeared_before_completion" if task.get("execution_backend", "screen") == "screen"
                              else "execution_owner_disappeared_before_completion")
                    if mark_managed_task_interrupted(root, task, reason):
                        processed += 1
                    continue
                submitted_at = task.get("launch_requested_at", task.get("screen_submitted_at"))
                if isinstance(submitted_at, (int, float)) and time.time() - float(submitted_at) < MANAGED_WORKER_HANDSHAKE_SECONDS:
                    continue
                if task.get("execution_backend", "screen") != "screen" or task.get("launch_uncertain"):
                    # A collected short-lived unit may already have performed
                    # external work. Missing readiness is not proof of no work.
                    if mark_managed_task_interrupted(root, task, "execution_launch_outcome_unknown"):
                        processed += 1
                elif record_managed_launch_failure(root, task, "worker_start_handshake_failed"):
                    processed += 1
                continue
            next_launch_at = task.get("next_launch_at")
            if isinstance(next_launch_at, (int, float)) and time.time() < float(next_launch_at):
                continue
            if launch_managed_task(root, task):
                processed += 1
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            print(f"reins: warning: could not recover managed task {path}: {exc}", file=sys.stderr)
        finally:
            queue_store.release_owner_lock(lock, remove=False)
    return processed


def agent_prompt_text(parts: list[str]) -> str:
    prompt_parts = list(parts)
    if prompt_parts and prompt_parts[0] == "--":
        prompt_parts = prompt_parts[1:]
    if not prompt_parts:
        raise SystemExit("agent mode requires a task prompt after --")
    return prompt_parts[0] if len(prompt_parts) == 1 else " ".join(prompt_parts)


def read_agent_system_prompt(path: Path) -> str:
    """Require an actual nonblank UTF-8 file, never Pi's inline-path fallback."""
    if not path.is_file():
        raise ValueError(f"system prompt must be a readable file: {path}")
    text = path.read_bytes().decode("utf-8-sig")
    if not text.strip():
        raise ValueError(f"system prompt file must not be empty: {path}")
    return text


def agent_wrapped_command(
    worker: str,
    *,
    cwd: str,
    result_path: Path,
    sandbox_mode: str,
    permission_mode: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
    system_prompt_path: Path | None = None,
    node: dict[str, object] | None = None,
) -> list[str]:
    options = ChildOptions(
        cwd=cwd,
        result_path=result_path,
        sandbox_mode=sandbox_mode,
        permission_mode=permission_mode,
        model=model,
        reasoning_effort=reasoning_effort,
        system_prompt_path=system_prompt_path,
        keep_session=node is not None,
        fork_from=node.get("fork_from") if node else None,
        session_id=node.get("planned_session") if node else None,
        session_dir=Path(str(node["session_dir"])) if node else None,
    )
    return get_child_agent(worker).child_command(options, delivery_engine.agent_environment(worker))


def child_agent_environment(environment: dict[str, str]) -> dict[str, str]:
    child = AGENTS.child_environment(environment)
    # A fresh CLI child is not the Desktop conversation which submitted it.
    # Keep the parent's callback route in the task record, not this marker.
    child.pop("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", None)
    return child
