"""Dispatch: bind who gets called back, build callback records and submit managed work."""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path
from ..core import capabilities, envelope, ids, records, results, targets
from . import resume_environment
from . import routes
from ..harness.claude import session as claude_session
from ..harness.pi import channel as pi_channel
from ..harness import AGENTS, AGENT_NAMES, get_agent
from ..harness.base import DEFAULT_APPROVALS_REVIEWER, DEFAULT_APPROVAL_POLICY, DEFAULT_SANDBOX_MODE, DEFAULT_CLAUDE_PERMISSION_MODE
from ..harness.claude.adapter import CLAUDE_PERMISSION_MODE_ENV
from ..owners import macos as macos_platform
from ..store import install_record, layout
from ..owners import host
from .. import runtime as reins_runtime
from ..store import records as queue_store
from ..system import shell
from . import delivery as delivery_engine
from . import contexts as contexts_engine
from . import goals
from . import tasks as managed_tasks


AGENT_DISPLAY_NAMES = {name: get_agent(name).display_name for name in AGENT_NAMES}
AGENT_THREAD_ID_ENVS = {name: get_agent(name).session_id_env for name in AGENT_NAMES}
STANDARD_CALLBACK_GUIDANCE = "\n".join([
    "Please inspect the result and any relevant artifacts.",
    "Decide whether the original goal is complete, blocked, or needs another action.",
    "Continue if the next step is clear and safe; otherwise ask the user one concise question.",
])


def build_prompt(
    args: argparse.Namespace,
    duration: float | None = None,
    acknowledgement: str | None = None,
) -> str:
    lines = [
        "[reins]",
        f"A long-running task explicitly called back into {agent_display_name(resolve_agent(args))}.",
        f"Callback time: {datetime.now().astimezone().isoformat(timespec='seconds')}",
        f"Task: {args.task}",
        f"Working directory: {args.cwd}",
    ]
    if args.command:
        lines.append(f"Command: {args.command}")
    if duration is not None:
        lines.append(f"Duration: {duration:.1f}s")
    if args.exit_code is not None:
        lines.append(f"Exit code: {args.exit_code}")
    if args.message:
        lines.extend(["", "Callback message:", args.message])

    lines.extend(["", STANDARD_CALLBACK_GUIDANCE])
    if acknowledgement:
        lines.extend(["", build_acknowledgement_text(acknowledgement, agent=resolve_agent(args), callback_mode=getattr(args, "callback_mode", None))])
    return "\n".join(lines)


def resolve_agent(args: argparse.Namespace | None = None) -> str:
    """Choose the callback adapter once, using explicit selection first."""
    if args is not None:
        cached = getattr(args, "_callback_agent", None)
        if isinstance(cached, str) and cached in AGENT_NAMES:
            return cached
        explicit = getattr(args, "agent", None)
        if explicit:
            if explicit not in AGENT_NAMES:
                raise SystemExit(f"Unknown agent {explicit!r}; expected one of: {', '.join(AGENT_NAMES)}")
            args._callback_agent = explicit
            return explicit
    agent = AGENTS.detect(os.environ)
    if args is not None:
        args._callback_agent = agent
    return agent


def agent_display_name(agent: str) -> str:
    return get_agent(agent).display_name if agent in AGENT_NAMES else agent


def request_agent(request: dict[str, object]) -> str:
    agent = request.get("agent", "codex")
    if not isinstance(agent, str):
        raise ValueError(f"unsupported request agent: {agent!r}")
    return get_agent(agent).name


def resolve_target(args: argparse.Namespace) -> tuple[dict[str, str], str]:
    if args.session:
        value = pi_channel.canonical_session(args.session) if resolve_agent(args) == "pi" else args.session
        return targets.session(value), "--session"

    agent = resolve_agent(args)
    env_name = get_agent(agent).session_id_env
    session = os.environ.get(env_name, "").strip()
    if session:
        if agent == "pi":
            session = pi_channel.canonical_session(session)
        return targets.session(session), env_name

    raise SystemExit(
        f"Cannot determine the callback session: {env_name} is unset. "
        f"Run from {agent_display_name(agent)} or pass --session <id>; Reins never guesses a target."
    )


def bind_target(args: argparse.Namespace) -> tuple[dict[str, str], str]:
    cached_target = getattr(args, "_callback_target", None)
    cached_source = getattr(args, "_callback_target_source", None)
    if isinstance(cached_target, dict) and isinstance(cached_source, str):
        return cached_target, cached_source

    target, source = resolve_target(args)
    args._callback_target = target
    args._callback_target_source = source
    print(f"reins: callback bound to session {target['session']} via {source}", file=sys.stderr)
    return target, source


def routing_text(request: dict[str, object]) -> str:
    target = request["target"]
    source = targets.binding_of(request)
    if not (isinstance(target, dict) and target.get("kind") == "session"):
        raise ValueError("callback target must be an explicit session")
    return "\n".join(
        [
            "Callback routing:",
            f"- Bound session: {target['session']}",
            f"- Binding source: {source}",
            "- Resume only this session; never redirect this callback to another thread.",
        ]
    )


def attach_routing_text(prompt: str, request: dict[str, object]) -> str:
    return f"{prompt}\n\n{routing_text(request)}"


def new_callback_id(queue: Path) -> str:
    """The first callback of a fresh root that no task or callback uses yet."""
    queue = queue.expanduser().absolute()

    def taken(root: str) -> bool:
        callback = ids.callback_id(root)
        return (layout.managed_task_dir(queue, root).exists() or layout.ack_path(queue, callback).exists()
                or any(layout.request_path(queue, state, callback).exists()
                       for state in (layout.ACTIVE_STATE, "pending", "running", "done", "failed", "canceled")))

    return ids.callback_id(ids.new_root(taken))


def make_request(
    args: argparse.Namespace, prompt: str, *, duration: float | None = None,
) -> dict[str, object]:
    target, target_source = bind_target(args)
    agent = resolve_agent(args)
    request = {
        "schema": records.CALLBACK_SCHEMA,
        "id": getattr(args, "_callback_id", None) or new_callback_id(layout.queue_dir(args)),
        "created_at": time.time(),
        "cwd": args.cwd,
        "agent": agent,
        "target": target,
        "origin": targets.origin(agent, target["session"], args.cwd, target_source),
        "prompt": prompt,
        "prompt_format": envelope.FORMAT if getattr(args, "callback_format", "compact") == "compact" else "full",
        "task": getattr(args, "task", "Task update"),
        "exit_code": getattr(args, "exit_code", None),
        "message": getattr(args, "message", None),
        "approvals_reviewer": getattr(args, "approvals_reviewer", None) or DEFAULT_APPROVALS_REVIEWER,
        "approval_policy": getattr(args, "approval_policy", None) or DEFAULT_APPROVAL_POLICY,
        "sandbox_mode": getattr(args, "sandbox_mode", None) or DEFAULT_SANDBOX_MODE,
    }
    if duration is not None:
        request["duration_seconds"] = duration
    request.update(routes.selection(args))
    request["requires"] = capabilities.required_for(request)
    if agent == "claude":
        request["permission_mode"] = getattr(args, "permission_mode", None) or os.environ.get(
            CLAUDE_PERMISSION_MODE_ENV, DEFAULT_CLAUDE_PERMISSION_MODE
        )
    goal_id = getattr(args, "goal_id", None)
    if goal_id:
        request["goal_id"] = goal_id
    return request


def ack_command(root: Path, request_id: str) -> str:
    """The command a session runs to acknowledge; it names the queue only when not the default."""
    arguments = ["ack", request_id]
    if root.expanduser().absolute() != layout.queue_dir(None).expanduser().absolute():
        arguments = ["ack", "--queue-dir", str(root), request_id]
    launcher = install_record.launcher()
    if launcher is not None:  # the canonical Reins, whatever is first on PATH
        return shell.format_command([launcher, *arguments])
    return reins_runtime.control_command(*arguments)


def prepare_request_for_queue(root: Path, request: dict[str, object], prompt: str) -> dict[str, object]:
    request_id = str(request["id"])
    request["queue_dir"] = str(root)
    request["lifecycle_state"] = "pending"
    acknowledgement = ack_command(root, request_id)
    routed_prompt = attach_routing_text(prompt, request)
    acknowledgement_text = build_acknowledgement_text(acknowledgement, agent=request_agent(request),
                                                      callback_mode=request.get("callback_mode"))
    request["prompt"] = f"{routed_prompt}\n\n{acknowledgement_text}"
    if request.get("prompt_format") == envelope.FORMAT:
        # Keep complete evidence locally; neither retries nor rendering counters
        # need to repeat command lines and boilerplate in the model context.
        if not re.fullmatch(r"[A-Za-z0-9_-]+", request_id):
            raise ValueError("callback id must be a safe filename")
        details_path = root.absolute() / "details" / f"{request_id}.md"
        queue_store.write_private_text(details_path, str(request["prompt"]))
        request["prompt_details_path"] = str(details_path)
        compact = envelope.render(request, details_path, acknowledgement)
        request["prompt_compact"] = compact
        request["prompt"] = compact + "\n" + envelope.REMINDER
        return request
    # Only remove the known generated wrapper, never task-specific messages,
    # template handoffs, recovery instructions, or routing constraints.
    compact = prompt
    for agent in AGENT_NAMES:
        prefix = f"[reins]\nA long-running task explicitly called back into {agent_display_name(agent)}.\n"
        if compact.startswith(prefix):
            compact = "[reins]\n" + compact[len(prefix):]
            suffix = "\n\n" + STANDARD_CALLBACK_GUIDANCE
            if compact.endswith(suffix):
                compact = compact[:-len(suffix)]
            break
    request["prompt_compact"] = (
        f"{attach_routing_text(compact, request)}\n\n"
        f"Callback acknowledgement (after inspecting the result):\n{acknowledgement}"
    )
    return request


def enqueue_existing_request(root: Path, request: dict[str, object], prompt: str) -> int:
    request_id = str(request["id"])
    target = layout.request_path(root, "pending", request_id)
    goal_lock: tuple[object, Path] | None = None
    try:
        queue_store.ensure_daemon_dirs(root)
        request = prepare_request_for_queue(root, request, prompt)
        goal_id = request.get("goal_id")
        goal: dict[str, object] | None = None
        if not request.get("goal_reminder") and goal_id is not None:
            if not isinstance(goal_id, str):
                raise ValueError("callback goal id must be a string")
            layout.goal_path(root, goal_id)
            goal_lock = queue_store.acquire_owner_lock(root, layout.goal_lock_id(goal_id), blocking=True)
            goal = goals.load_goal(root, goal_id)
            if goal.get("state") != "active":
                raise ValueError(f"goal {goal_id} is not active")
        queue_store.write_request(target, request)
        if goal is not None:
            goal["last_external_callback_at"] = float(request.get("created_at", time.time()))
            goal.pop("last_reminder_at", None)
            goals.write_goal(root, goal)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"reins: warning: failed to enqueue callback: {exc}", file=sys.stderr)
        return 1
    finally:
        if goal_lock is not None:
            queue_store.release_owner_lock(goal_lock, remove=False)

    print(f"reins: queued callback {request_id} in {root}", file=sys.stderr)
    return 0


def enqueue_request(args: argparse.Namespace, prompt: str) -> int:
    root = layout.queue_dir(args)
    request = make_request(args, prompt)
    # A persistence error can occur after publication. Diagnostics must not tell
    # the caller to blindly submit a duplicate callback in that case.
    args._health_work = {"state": "unknown", "id": str(request["id"])}
    resumable = request.get("callback_mode") != "manual" and (request.get("target") or {}).get("kind") == "session"
    if resumable:
        resume_environment.capture(root, str(request["id"]))
    result = enqueue_existing_request(root, request, prompt)
    if result != 0 and resumable:
        resume_environment.discard(root, str(request["id"]))
    if result == 0:
        args._health_work = {"state": "callback_queued", "id": str(request["id"])}
    return result


def queue_callback(args: argparse.Namespace, prompt: str) -> int:
    """Persist callback delivery; only the daemon may resume an agent."""
    if not args.dry_run:
        return enqueue_request(args, prompt)
    request = make_request(args, prompt)
    routed_prompt = attach_routing_text(prompt, request)
    print(routed_prompt)
    return 0


def load_request(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("request must be a JSON object")
    if not records.is_callback(data):
        raise ValueError(f"not an {records.CALLBACK_SCHEMA} record; queues from Long Task Callback need reins migrate")
    missing = capabilities.unsupported(data.get("requires"))
    if missing:
        raise ValueError("callback requires unsupported capabilities: " + ", ".join(missing))
    if data.get("callback_mode") not in ("cli", "codex-queue", "desktop", "app-server", "manual"):
        raise ValueError("callback requires an explicit mode")
    if not isinstance(data.get("id"), str) or data["id"] != path.stem:
        raise ValueError("request id must match its filename")
    if not isinstance(data.get("cwd"), str):
        raise ValueError("request cwd must be a string")
    if not isinstance(data.get("prompt"), str):
        raise ValueError("request prompt must be a string")
    if data.get("callback_mode") == "codex-queue":
        from . import native_queue
        native_queue.validate_route(data)
    if data.get("callback_mode") == "app-server":
        from . import native
        native.validate_route(data)
    if set(data["requires"]) & {capabilities.PI_STEER, capabilities.PI_FOLLOW_UP}:
        if request_agent(data) != "pi":
            raise ValueError("only Pi callbacks can require Pi delivery")
        pi_channel.validate_route(data)
    delivery_engine.resume_command(data)
    return data


def build_acknowledgement_text(command: str, agent: str = "codex", callback_mode: str | None = None) -> str:
    if agent == "codex" and callback_mode == "codex-queue":
        delivery_note = "This callback arrived through the native Codex queue; the original thread keeps its existing permissions."
    elif agent == "claude":
        delivery_note = (
            "This resume runs Claude Code headless with automatic permission handling "
            "and grants the callback queue directory as an additional working directory."
        )
    elif agent == "pi":
        delivery_note = (
            "This callback is delivered to the original PI Agent process through its native message API. "
            "Only Reins-managed sessions can recover in print mode after the entire original process exits."
        )
    else:
        delivery_note = (
            "This resume is configured to make the callback queue writable and to use "
            "automatic approval review when the Codex CLI supports it."
        )
    return "\n".join(
        [
            "Callback acknowledgement:",
            "After you have successfully resumed this callback and inspected the relevant result,",
            "mark the callback as received by running this command:",
            command,
            delivery_note,
            ("Once a Pi callback is published, an uncertain outcome requires manual inspection; automatic replay is suppressed."
             if agent == "pi" else "This native queue message is published once and waits for your ACK; do not submit it again."
             if callback_mode == "codex-queue" else "The wakeup daemon will retry this callback until the acknowledgement marker exists or retries are exhausted."),
        ]
    )


def submit_managed_run(args: argparse.Namespace) -> int:
    if not routes.preflight(args):
        return 125
    try:
        backend = managed_tasks.select_execution_backend(args)
    except ValueError as exc:
        args._health_backend_error = str(exc)
        print(f"reins: refusing to submit task: {exc}", file=sys.stderr)
        return 125
    args._health_backend = backend
    root = layout.queue_dir(args).expanduser().absolute()
    queue_store.ensure_daemon_dirs(root)
    try:
        goals.validate_managed_goal(root, getattr(args, "goal_id", None))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"reins: refusing to submit task: {exc}", file=sys.stderr)
        return 125
    target, target_source = bind_target(args)
    try:
        while True:
            task_id = ids.new_root()
            task_directory = layout.managed_task_dir(root, task_id)
            try:
                # Reserve atomically so concurrent submissions cannot share an ID.
                task_directory.mkdir(mode=0o700, parents=True, exist_ok=False)
            except FileExistsError:
                continue
            break
    except OSError as exc:
        print(f"reins: refusing to submit task because its durable record could not be written: {exc}", file=sys.stderr)
        return 125
    args._health_work = {"state": "unknown", "id": task_id}
    args._submitted_task_id = task_id
    environment_path = task_directory / "environment.json"
    log_path = task_directory / "attempt-1.log"
    task_kind = str(getattr(args, "task_kind", "command"))
    wrapped_command = list(getattr(args, "wrapped_command", []))
    agent_prompt_path: Path | None = None
    agent_result_path: Path | None = None
    agent_system_prompt_path: Path | None = None
    if task_kind == "agent":
        agent_prompt_path = task_directory / managed_tasks.AGENT_PROMPT_FILE_NAME
        agent_result_path = task_directory / managed_tasks.AGENT_RESULT_FILE_NAME
        if getattr(args, "agent_system_prompt_text", None) is not None:
            agent_system_prompt_path = task_directory / managed_tasks.AGENT_SYSTEM_PROMPT_FILE_NAME
        node_plan = getattr(args, "_node_plan", None)
        node = (contexts_engine.node_record(node_plan, task_id, task_directory, str(args.agent_worker))
                if node_plan else None)
        wrapped_command = managed_tasks.agent_wrapped_command(
            str(args.agent_worker),
            cwd=os.path.abspath(args.cwd),
            result_path=agent_result_path,
            sandbox_mode=str(args.sandbox_mode),
            permission_mode=str(args.permission_mode),
            model=getattr(args, "child_model", None),
            reasoning_effort=getattr(args, "child_reasoning_effort", None),
            system_prompt_path=agent_system_prompt_path,
            node=node,
        )
    else:
        node = None
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in ("INVOCATION_ID", "JOURNAL_STREAM", "SYSTEMD_EXEC_PID", "STY", "WINDOW",
                        macos_platform.OWNER_ENV, "XPC_SERVICE_NAME", "XPC_FLAGS")
    }
    task: dict[str, object] = {
        "schema": records.TASK_SCHEMA,
        "id": task_id,
        "submitted_at": time.time(),
        "execution_backend": backend,
        "callback_format": getattr(args, "callback_format", "compact"),
        "submission_boot_id": host.current_boot_id(),
        "machine_id": host.current_machine_id(),
        "state": "submitted",
        "outcome": "pending",
        "task": args.task,
        "cwd": os.path.abspath(args.cwd),
        "command": args.command or shell.format_command(wrapped_command),
        "wrapped_command": wrapped_command,
        "message": args.message,
        "queue_dir": str(root),
        "agent": resolve_agent(args),
        "target": target,
        "origin": targets.origin(resolve_agent(args), target["session"], os.path.abspath(args.cwd), target_source),
        "goal_id": getattr(args, "goal_id", None),
        "approvals_reviewer": getattr(args, "approvals_reviewer", None) or DEFAULT_APPROVALS_REVIEWER,
        "approval_policy": getattr(args, "approval_policy", None) or DEFAULT_APPROVAL_POLICY,
        "sandbox_mode": getattr(args, "sandbox_mode", None) or DEFAULT_SANDBOX_MODE,
        "permission_mode": getattr(args, "permission_mode", None) or DEFAULT_CLAUDE_PERMISSION_MODE,
        "strict": bool(args.strict),
        "screen_session": f"reins-{task_id}",
        "log_path": str(log_path),
        "environment_path": str(environment_path),
        "token": managed_tasks.MANAGED_WORKER_TOKEN_PREFIX + secrets.token_urlsafe(32),
    }
    task.update(routes.selection(args))
    # A tower that cannot deliver this route must leave the task alone, not
    # silently auto-deliver an explicit manual callback or downgrade a steer.
    task["requires"] = capabilities.required_for(task)
    if task_kind == "agent" and agent_prompt_path is not None and agent_result_path is not None:
        task.update(
            {
                "task_kind": "agent",
                "agent_worker": str(args.agent_worker),
                "agent_template": getattr(args, "template", None),
                "expert": getattr(args, "expert", None),
                "agent_template_version": getattr(args, "template_version", None),
                "agent_template_source": getattr(args, "template_source", None),
                "agent_template_handoff": getattr(args, "template_handoff", None),
                "child_model": getattr(args, "child_model", None),
                "child_reasoning_effort": getattr(args, "child_reasoning_effort", None),
                "agent_prompt_path": str(agent_prompt_path),
                "agent_result_path": str(agent_result_path),
            }
        )
        if node is not None:
            task["node"] = node
        if not getattr(args, "no_report", False):
            # The child writes its claims here; the worker validates them into result.json.
            task["report_path"] = str(task_directory / results.REPORT_FILE_NAME)
        if agent_system_prompt_path is not None:
            task.update({
                "agent_system_prompt_path": str(agent_system_prompt_path),
                "agent_system_prompt_source": args.agent_system_prompt_source,
            })
    try:
        queue_store.private_directory(task_directory)
        if agent_prompt_path is not None:
            prompt_text = str(args.agent_prompt_text)
            if task.get("report_path"):
                prompt_text += "\n\n" + results.REPORT_INSTRUCTIONS.format(path=task["report_path"])
            queue_store.write_private_text(agent_prompt_path, prompt_text)
        if agent_system_prompt_path is not None:
            queue_store.write_private_text(agent_system_prompt_path, args.agent_system_prompt_text)
        stdin_data = managed_tasks.piped_input() if task_kind == "command" else None
        if stdin_data is not None:
            stdin_path = task_directory / managed_tasks.STDIN_FILE_NAME
            managed_tasks.write_private_bytes(stdin_path, stdin_data)
            task["stdin_path"] = str(stdin_path)
        queue_store.write_request(environment_path, {"version": 1, "environment": environment})
        # Only the session itself starts a waiter; a script that names a session
        # with --session expects the daemon to deliver at once.
        live_wait = (task["agent"] == "claude" and target.get("kind") == "session"
                     and os.environ.get(claude_session.SESSION_ENV, "").strip() == str(target.get("session"))
                     and task["callback_mode"] != "manual"
                     and not claude_session.channel_is_held(str(target.get("session")))
                     and not claude_session.background_tasks_disabled(os.environ))
        if live_wait:
            # Before the task exists, so even one that ends at once waits for this session's waiter.
            claude_session.expect_live_waiter(str(target.get("session")), claude_session.live_wait_grace(os.environ))
        managed_tasks.write_managed_task(root, task)
        if task["callback_mode"] != "manual" and target.get("kind") == "session":
            resume_environment.capture(root, ids.callback_id(task_id), environment)
    except OSError as exc:
        print(f"reins: refusing to submit task because its durable record could not be written: {exc}", file=sys.stderr)
        return 125
    args._health_work = {"state": "task_persisted", "id": task_id}
    print(f"reins: submitted managed task {task_id}", file=sys.stderr)
    print(f"reins: execution backend: {backend}", file=sys.stderr)
    print(f"reins: callback mode: {task['callback_mode']}", file=sys.stderr)
    if task["callback_mode"] == "manual":
        print("reins: automatic callback disabled; inspect saved results and ACK from the bound session", file=sys.stderr)
    if backend == "screen":
        print(f"reins: screen session: {task['screen_session']}", file=sys.stderr)
        print("reins: screen fallback: service-stop survival depends on its containing service", file=sys.stderr)
    print(f"reins: task log: {log_path}", file=sys.stderr)
    if task.get("stdin_path"):
        print(f"reins: task stdin: the input piped into reins ({Path(str(task['stdin_path'])).stat().st_size} bytes)",
              file=sys.stderr)
    if agent_result_path is not None:
        print(f"reins: agent result: {agent_result_path}", file=sys.stderr)
    if agent_system_prompt_path is not None:
        print(f"reins: agent system prompt: {agent_system_prompt_path}", file=sys.stderr)
    if task["agent"] == "claude" and target.get("kind") == "session":
        if claude_session.channel_is_held(str(target.get("session"))):
            print("reins: Claude Code live callback: arrives through the reins channel; no `reins wait` needed",
                  file=sys.stderr)
        elif claude_session.background_tasks_disabled(os.environ):
            print("reins: Claude Code background tasks are disabled here, so do not run `reins wait`: end your turn; "
                  "Reins resumes this session with the callback when the task ends", file=sys.stderr)
        else:
            wait = reins_runtime.control_command("wait", "--queue-dir", str(root), "--task", task_id)
            print(f"reins: Claude Code live callback: run in the background (run_in_background): {wait}",
                  file=sys.stderr)
            grace = claude_session.live_wait_grace(os.environ)
            if live_wait and grace > 0:
                print(f"reins: start it now: the callback is held for this session's waiter for {grace:g}s, "
                      "then resumed headless", file=sys.stderr)
    return 0


def request_task(request: dict[str, object]) -> str:
    prompt = request.get("prompt")
    if isinstance(prompt, str):
        for line in prompt.splitlines():
            if line.startswith("Task: "):
                return line.removeprefix("Task: ").strip() or "long task"
    return "long task"


def request_target(request: dict[str, object]) -> str:
    target = request.get("target")
    if not isinstance(target, dict):
        return "unknown target"
    if target.get("kind") == "session":
        value = str(target.get("session", "unknown"))
        return f"session {value}"
    return "unknown target"
