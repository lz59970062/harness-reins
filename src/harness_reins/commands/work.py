"""reins run, reins agent and reins done: say who does the work; the callback returns to the bound session."""
from __future__ import annotations

import argparse
import sys
import os
from pathlib import Path

from ..core import experts as expert_rules
from ..engine import contexts as contexts_engine
from ..store import contexts
from ..store import layout
from ..store import records as queue_store
from ..harness import get_child_agent
from ..templates import load_template
from ..system import shell
from ..engine import dispatch
from ..engine import tasks as managed_tasks


def done(args: argparse.Namespace) -> int:
    callback_code = dispatch.queue_callback(args, dispatch.build_prompt(args))
    return callback_code if args.strict else 0


def run(args: argparse.Namespace) -> int:
    if not args.wrapped_command:
        raise SystemExit("run mode requires a command after --")

    args.command = args.command or shell.format_command(args.wrapped_command)
    dispatch.bind_target(args)
    if args.dry_run:
        print(
            "\n".join(
                [
                    "[reins-submission-dry-run]",
                    f"Task: {args.task}",
                    f"Working directory: {os.path.abspath(args.cwd)}",
                    f"Command: {args.command}",
                    f"Execution: daemon -> {getattr(args, 'backend', 'auto')} backend -> independent worker -> task",
                    "No task record was written and no command was started.",
                ]
            )
        )
        return 0
    return dispatch.submit_managed_run(args)


DEFAULT_TASK_TITLE = "long task"


def agent(args: argparse.Namespace) -> int:
    """reins agent <harness>|continue|fork: every agent task is a named node."""
    root = layout.queue_dir(args).expanduser().absolute()
    lock = None
    if not args.dry_run:  # a dry run checks the rules but writes nothing
        queue_store.ensure_daemon_dirs(root)
        lock = queue_store.acquire_owner_lock(root, contexts.lock_id(), blocking=True)
    try:
        mode = args.agent_worker if args.agent_worker in ("continue", "fork") else "new"
        title = args.task if getattr(args, "task", None) not in (None, DEFAULT_TASK_TITLE) else None
        try:
            node_plan = contexts_engine.plan(root, mode=mode, ref=getattr(args, "node", None),
                                             name=getattr(args, "name", None), task_title=title)
        except ValueError as exc:
            raise SystemExit(f"reins: {exc}") from exc
        if node_plan["parent"] is not None:
            _inherit(args, node_plan["parent"])
        elif args.agent_worker == "expert":
            _apply_expert(args)
        if title is None:
            args.task = node_plan["name"]
        args._node_plan = node_plan
        status = _submit_agent(args)
        submitted = getattr(args, "_submitted_task_id", None)
        if status == 0 and submitted and not args.dry_run:
            task = managed_tasks.load_managed_task(layout.managed_task_path(root, str(submitted)))
            contexts_engine.record(root, task)
            print(f"reins: node {node_plan['name']} ({submitted}) in context {task['node']['context']}", file=sys.stderr)
        return status
    finally:
        if lock is not None:
            queue_store.release_owner_lock(lock, remove=False)


def _inherit(args: argparse.Namespace, parent: dict[str, object]) -> None:
    """A continued or forked node runs where and as its parent did."""
    args.agent_worker = str(parent["agent_worker"])
    args.cwd = str(parent["cwd"])
    args.child_model = getattr(args, "child_model", None) or parent.get("child_model")
    args.child_reasoning_effort = parent.get("child_reasoning_effort")
    args.permission_mode = str(parent.get("permission_mode") or getattr(args, "permission_mode", "auto"))
    args.sandbox_mode = str(parent.get("sandbox_mode") or getattr(args, "sandbox_mode", "workspace-write"))
    args.expert = parent.get("expert")  # recorded only; its instructions are already in the session
    system_prompt = parent.get("agent_system_prompt_path")
    args._inherited_system_prompt = str(system_prompt) if system_prompt else None


def _apply_expert(args: argparse.Namespace) -> None:
    """A new chain run by a configured expert: its harness, model, effort and instructions."""
    from . import experts as expert_commands
    try:
        expert = expert_commands.get(args.expert, Path(args.experts).expanduser() if getattr(args, "experts", None) else None)
    except ValueError as exc:
        raise SystemExit(f"reins: {exc}") from exc
    args.agent_worker = str(expert["harness"])
    args.child_model = expert.get("model")
    args.child_reasoning_effort = expert.get("reasoning")
    args.no_report = bool(getattr(args, "no_report", False) or not expert.get("report", True))
    args._expert_settings = expert


def _submit_agent(args: argparse.Namespace) -> int:
    args.agent_prompt_text = managed_tasks.agent_prompt_text(list(args.agent_prompt))
    if getattr(args, "_expert_settings", None) is not None and args.agent_prompt_text.strip():
        args.agent_prompt_text = expert_rules.prompt(args._expert_settings, args.agent_prompt_text)
    args.template = getattr(args, "template", None)
    args.template_file = getattr(args, "template_file", None)
    args.template_version = None
    args.template_source = None
    args.template_handoff = None
    args.child_model = getattr(args, "child_model", None)
    args.child_reasoning_effort = getattr(args, "child_reasoning_effort", None)
    system_prompt_file = getattr(args, "system_prompt_file", None) or getattr(args, "_inherited_system_prompt", None)
    args.agent_system_prompt_source = None
    args.agent_system_prompt_text = None
    if system_prompt_file is not None and not str(system_prompt_file).strip():
        raise SystemExit("--system-prompt-file must not be empty")
    if not args.agent_prompt_text.strip():
        raise SystemExit("agent mode requires non-empty task requirements")
    if args.child_model is not None and not args.child_model.strip():
        raise SystemExit("--model must not be empty")
    child_adapter = get_child_agent(args.agent_worker)
    args.no_report = bool(getattr(args, "no_report", False) or not child_adapter.supports_report_file)
    if args.child_model and not getattr(child_adapter, "supports_model", True):
        raise SystemExit(f"{child_adapter.display_name} children do not support --model")
    if args.child_reasoning_effort and not child_adapter.supports_reasoning_effort:
        raise SystemExit(f"{child_adapter.display_name} children do not support --reasoning-effort")
    if args.template is not None or args.template_file is not None:
        try:
            profile = load_template(args.template, template_file=args.template_file)
        except (OSError, ValueError) as exc:
            raise SystemExit(f"reins: {exc}") from exc
        args.template = profile.name
        args.template_version = profile.version
        args.template_source = profile.source
        args.template_handoff = profile.handoff
        args.child_model = args.child_model or profile.model_for(args.agent_worker)
        args.child_reasoning_effort = args.child_reasoning_effort or profile.reasoning_effort_for(args.agent_worker)
        system_prompt_file = system_prompt_file or profile.system_prompt_file_for(args.agent_worker)
        args.agent_prompt_text = profile.render(args.agent_prompt_text)
    if args.child_reasoning_effort and args.child_reasoning_effort not in child_adapter.reasoning_efforts:
        raise SystemExit(f"unsupported {child_adapter.display_name} reasoning effort {args.child_reasoning_effort!r}")
    if system_prompt_file is not None:
        if not child_adapter.supports_system_prompt_file:
            raise SystemExit(f"{child_adapter.display_name} children do not support --system-prompt-file")
        try:
            source = Path(system_prompt_file).expanduser().resolve()
            args.agent_system_prompt_text = managed_tasks.read_agent_system_prompt(source)
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            raise SystemExit(f"reins: could not load system prompt file: {exc}") from exc
        args.agent_system_prompt_source = str(source)
    args.task_kind = "agent"
    args.command = args.command or f"reins agent {args.agent_worker}"
    args.wrapped_command = []
    dispatch.bind_target(args)
    if args.dry_run:
        print(
            "\n".join(
                [
                    "[reins-agent-submission-dry-run]",
                    f"Task: {args.task}",
                    f"Working directory: {os.path.abspath(args.cwd)}",
                    f"Child agent: {child_adapter.display_name}",
                    f"Template: {args.template or 'none'} (version {getattr(args, 'template_version', None)})",
                    f"Template source: {args.template_source or 'none'}",
                    f"Child model: {args.child_model or 'CLI default'}",
                    f"Child reasoning effort: {args.child_reasoning_effort or 'CLI default'}",
                    f"System prompt source: {args.agent_system_prompt_source or 'CLI default'}",
                    f"Expanded prompt:\n{args.agent_prompt_text}",
                    f"Template handoff: {args.template_handoff or 'none'}",
                    f"Execution: daemon -> {getattr(args, 'backend', 'auto')} backend -> independent worker -> child agent",
                    "No task record was written and no child agent was started.",
                ]
            )
        )
        return 0
    return dispatch.submit_managed_run(args)
