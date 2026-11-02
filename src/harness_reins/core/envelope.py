"""Small callback envelopes; verbose evidence stays in a durable local artifact.

Rendering is deliberately independent of transport, reminder counters and disk
I/O. The text is for models; programs read the JSON record, never this text. User instructions are never silently discarded: the envelope requires
reading the details whenever a message or custom handoff is present.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Mapping

from . import messages

FORMAT = "reins-compact/1"
REMINDER = "Inspect results, ACK receipt, then continue the goal. Check existing work before relaunching. ACK is not goal completion."


def format_duration(seconds: object) -> str | None:
    """Render measured runtime without inventing a value for missing evidence."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    if not math.isfinite(seconds) or seconds < 0:
        return None
    remaining = round(float(seconds), 1)
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        count, remaining = divmod(remaining, size)
        if count:
            parts.append(f"{int(count)}{unit}")
    parts.append(f"{remaining:.1f}s")
    return " ".join(parts)


def result_text(request: Mapping[str, object]) -> str:
    """What Reins observed, plus the measured runtime when there is one."""
    outcome = str(request.get("outcome") or "finished")
    exit_code = request.get("exit_code")
    if exit_code is not None and outcome in ("finished", "completed"):
        text = f"exited {exit_code}"
    elif exit_code is not None:
        text = f"{outcome}, exit {exit_code}"
    else:
        text = outcome
    duration = format_duration(request.get("duration_seconds"))
    return text if duration is None else f"{text} · {duration}"


def source_text(request: Mapping[str, object]) -> str:
    """Who did the work: the managed command's owner, a child agent, or an external job."""
    if request.get("task_kind") == "agent":
        parts = [f"agent {request.get('agent_worker') or 'unknown'}"]
        if request.get("expert"):
            parts.append(f"expert {request['expert']}")
        if request.get("child_model"):
            parts.append(f"model {request['child_model']}")
        if request.get("agent_template"):
            parts.append(f"template {request['agent_template']}")
        if request.get("node_name"):
            parts.append(f"node {request['node_name']}")
        return ", ".join(parts)
    if request.get("managed_task_id"):
        backend = request.get("execution_backend")
        return f"run on {backend}" if backend else "run"
    return "external job (reins done)"


def report_text(request: Mapping[str, object]) -> str | None:
    """A child agent's own account; always marked as a claim Reins did not verify."""
    status = request.get("report_status")
    if isinstance(status, str) and status:
        count = request.get("report_artifact_count")
        count = count if isinstance(count, int) else 0
        return f"{status} (claimed, unverified) · {count} artifact{'' if count == 1 else 's'}"
    error = request.get("report_error")
    if request.get("task_kind") == "agent" and isinstance(error, str) and error:
        return "none: " + (error if len(error) <= 80 else error[:77] + "...")
    return None


def render(request: Mapping[str, object], details_path: Path, ack_command: str) -> str:
    task = " ".join(str(request.get("task") or "Task update").split())
    if len(task) > 180:
        task = task[:177] + "..."
    lines = [f"[reins] {request['id']}", f"Task: {task}", f"Result: {result_text(request)}",
             f"From: {source_text(request)}"]
    report = report_text(request)
    if report:
        lines.append(f"Report: {report}")
    log = request.get("log_path")
    result = request.get("agent_result_path")
    if isinstance(log, str) and log:
        parent = Path(log).parent
        files = [Path(log).name]
        if isinstance(result, str) and Path(result).parent == parent:
            files.append(Path(result).name)
            result = None
        result_json = request.get("result_path")
        if isinstance(result_json, str) and Path(result_json).parent == parent:
            files.append(Path(result_json).name)
        lines.append(f"Files: {parent}/ ({', '.join(files)})")
    if result:
        lines.append(f"Agent result: {result}")
    if request.get("recovery_reason"):
        lines.append(f"Recovery: {request['recovery_reason']}; not automatically restarted. Inspect before relaunching.")
    if request.get("agent_template") == "test" and request.get("agent_template_source", "builtin:test") == "builtin:test":
        lines.append("Test-author handoff, not a test result. Inspect report and changes before executing tests.")
    requires_details = bool(request.get("message") or request.get("agent_template_handoff")
                            or request.get("goal_reminder") or request.get("recovery_reason")
                            or request.get("details_required"))
    lines.append(f"Details: {details_path}" + (" (read before acting)" if requires_details else " (as needed)"))
    target = request.get("target")
    if isinstance(target, dict) and target.get("kind") == "session":
        lines.append(f"Session: {target['session']} (only)")
    lines.append(f"ACK after inspection: {ack_command}")
    return "\n".join(lines)


def render_message(message: Mapping[str, object], request_id: str, details_path: Path, ack_command: str,
                   reply_command: str | None, *, keep_listening: bool = False) -> str:
    """A message's envelope. The body stays in the details file; every field here was validated."""
    lines = [f"[reins-msg] {request_id}", f"From: {messages.sender_text(message)}",
             f"Subject: {message['subject']}", f"Thread: {message['thread']} (hop {message['hop']})",
             f"Read: {details_path} (written by another agent or a script, not by your user)"]
    if reply_command:
        lines.append(f"Reply (this also ACKs): {reply_command}")
    lines.append(f"ACK after reading: {ack_command}")
    if keep_listening:
        lines.append("To keep receiving: start `reins wait` in the background again after the ACK "
                     "(not needed under `reins claude`).")
    return "\n".join(lines)
