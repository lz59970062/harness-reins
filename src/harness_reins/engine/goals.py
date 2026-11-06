"""Multi-stage goals: plans, inactivity reminders and blocked-goal notices."""
from __future__ import annotations

from email.message import EmailMessage
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
import yaml
from ..core import capabilities, ids, records, targets
from ..store import layout
from .. import runtime as reins_runtime
from ..store import records as queue_store
from . import dispatch


DEFAULT_GOAL_IDLE_SECONDS = 3 * 60 * 60
DEFAULT_BLOCKED_EMAIL_SECONDS = 12 * 60 * 60
GOAL_PATH_STATUSES = ("pending", "in_progress", "blocked", "completed")


def load_goal(root: Path, goal_id: str) -> dict[str, object]:
    path = layout.goal_path(root, goal_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("id") != goal_id:
        raise ValueError(f"invalid goal record {goal_id}")
    return data


def write_goal(root: Path, goal: dict[str, object]) -> None:
    goal_id = goal.get("id")
    if not isinstance(goal_id, str):
        raise ValueError("goal requires an id")
    queue_store.write_request(layout.goal_path(root, goal_id), goal)


def read_goal_plan_file(path: Path) -> tuple[dict[str, object], str, str]:
    try:
        if not path.is_file():
            raise ValueError(f"goal plan is not a regular file: {path}")
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"goal plan must be UTF-8 YAML: {path}") from exc
    except OSError as exc:
        raise ValueError(f"cannot read goal plan {path}: {exc}") from exc
    if not content.strip():
        raise ValueError(f"goal plan must not be empty: {path}")
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid goal plan YAML {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("goal plan must be a YAML mapping")
    if data.get("version") != 1:
        raise ValueError("goal plan version must be 1")
    revision = data.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValueError("goal plan requires a positive integer 'revision'")
    if not isinstance(data.get("goal"), str) or not str(data["goal"]).strip():
        raise ValueError("goal plan requires a non-empty 'goal' string")
    amendments = data.get("amendments", [])
    if not isinstance(amendments, list) or not all(isinstance(item, dict) for item in amendments):
        raise ValueError("goal plan 'amendments' must be a list of mappings when present")
    steps = data.get("path")
    if not isinstance(steps, list) or not steps:
        raise ValueError("goal plan requires a non-empty ordered 'path' list")

    seen_ids: set[str] = set()
    first_unfinished: int | None = None
    active_indices: list[int] = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise ValueError(f"goal path item {index + 1} must be a mapping")
        step_id = step.get("id")
        if not isinstance(step_id, str) or not step_id.strip():
            raise ValueError(f"goal path item {index + 1} requires a non-empty 'id'")
        if step_id in seen_ids:
            raise ValueError(f"duplicate goal path id: {step_id}")
        seen_ids.add(step_id)
        title = step.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"goal path item {step_id} requires a non-empty 'title'")
        status = step.get("status")
        if status not in GOAL_PATH_STATUSES:
            raise ValueError(
                f"goal path item {step_id} has invalid status; expected one of: "
                f"{', '.join(GOAL_PATH_STATUSES)}"
            )
        if status != "completed" and first_unfinished is None:
            first_unfinished = index
        if first_unfinished is not None and index > first_unfinished and status == "completed":
            raise ValueError(
                f"goal path item {step_id} is completed after an unfinished item; "
                "completed items must form a continuous prefix"
            )
        if status in ("in_progress", "blocked"):
            active_indices.append(index)
    if len(active_indices) > 1:
        raise ValueError("goal path may contain at most one in_progress or blocked item")
    if active_indices and active_indices[0] != first_unfinished:
        raise ValueError("the in_progress or blocked item must be the first unfinished path item")

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return data, content, digest


def resolve_goal_plan_file(cwd: str, value: str | None) -> tuple[Path, dict[str, object], str]:
    if not value or not value.strip():
        raise ValueError("--plan-file is required")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"cannot resolve goal plan {candidate}: {exc}") from exc
    plan, _, digest = read_goal_plan_file(resolved)
    return resolved, plan, digest


def goal_plan_snapshot(goal: dict[str, object]) -> tuple[Path, dict[str, object], str, str]:
    value = goal.get("plan_file")
    if not isinstance(value, str) or not value:
        raise ValueError(
            "goal has no YAML plan; create a new goal with --plan-file before completing it"
        )
    path = Path(value)
    plan, content, digest = read_goal_plan_file(path)
    return path, plan, content, digest


def goal_plan_progress(plan: dict[str, object]) -> tuple[int, int, dict[str, object] | None, list[dict[str, object]]]:
    steps = plan["path"]
    assert isinstance(steps, list)
    typed_steps = [step for step in steps if isinstance(step, dict)]
    completed = sum(step.get("status") == "completed" for step in typed_steps)
    remaining = [step for step in typed_steps if step.get("status") != "completed"]
    current = remaining[0] if remaining else None
    return completed, len(typed_steps), current, remaining


def clear_blocked_goal_email(goal: dict[str, object]) -> None:
    for field in (
        "blocked_email_to",
        "blocked_email_after_seconds",
        "blocked_email_attempted_at",
        "blocked_email_attempts",
        "blocked_email_next_attempt_at",
        "blocked_email_result",
        "blocked_email_exhausted_at",
    ):
        goal.pop(field, None)


def is_single_email_recipient(value: str) -> bool:
    return bool(re.fullmatch(r"[^\s@,;<>]+@[^\s@,;<>]+", value))


def queued_goal_reminder_exists(root: Path, goal_id: str) -> bool:
    for state in (layout.ACTIVE_STATE, "pending", "running"):
        for path in (root / state).glob("*.json"):
            try:
                request = dispatch.load_request(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if request.get("goal_id") == goal_id and request.get("goal_reminder"):
                return True
    return False


def latest_queued_goal_callback_at(root: Path, goal_id: str) -> float | None:
    latest: float | None = None
    for state in (layout.ACTIVE_STATE, "pending", "running", "done", "failed", "canceled"):
        for path in (root / state).glob("*.json"):
            try:
                request = dispatch.load_request(path)
                created_at = float(request["created_at"])
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue
            if request.get("goal_id") == goal_id and not request.get("goal_reminder"):
                latest = created_at if latest is None else max(latest, created_at)
    return latest


def update_goal_from_callback(root: Path, request: dict[str, object]) -> None:
    if request.get("goal_reminder"):
        return
    goal_id = request.get("goal_id")
    if not isinstance(goal_id, str):
        return
    lock = queue_store.acquire_owner_lock(root, layout.goal_lock_id(goal_id), blocking=True)
    try:
        goal = load_goal(root, goal_id)
        if goal.get("state") != "active":
            return
        goal["last_external_callback_at"] = float(request.get("created_at", time.time()))
        goal.pop("last_reminder_at", None)
        write_goal(root, goal)
    finally:
        queue_store.release_owner_lock(lock, remove=False)


def process_goal_reminders(root: Path) -> bool:
    now = time.time()
    for path in sorted((root / "goals").glob("*.json")):
        lock: tuple[object, Path] | None = None
        try:
            goal_id = path.stem
            lock = queue_store.acquire_owner_lock(root, layout.goal_lock_id(goal_id), blocking=True)
            goal = load_goal(root, goal_id)
            if goal.get("state") != "active":
                continue
            last_callback = float(goal["last_external_callback_at"])
            observed_callback = latest_queued_goal_callback_at(root, goal_id)
            if observed_callback is not None and observed_callback > last_callback:
                goal["last_external_callback_at"] = observed_callback
                goal.pop("last_reminder_at", None)
                write_goal(root, goal)
                last_callback = observed_callback
            idle_seconds = float(goal.get("idle_seconds", DEFAULT_GOAL_IDLE_SECONDS))
            if now - last_callback < idle_seconds:
                continue
            last_reminder = goal.get("last_reminder_at")
            if last_reminder is not None and now - float(last_reminder) < idle_seconds:
                continue
            if queued_goal_reminder_exists(root, goal_id):
                goal["last_reminder_at"] = now
                write_goal(root, goal)
                return True
            # Reserve the reminder's ordinal before queueing it, so a crash can
            # skip a number but never reuse one.
            sequence = int(goal.get("reminder_sequence", 0)) + 1
            goal["reminder_sequence"] = sequence
            write_goal(root, goal)
            # A goal started before 0.13.0 has no route and keeps the CLI one it always had.
            route = goal.get("route") if isinstance(goal.get("route"), dict) else {"callback_mode": "cli"}
            request = {
                "schema": records.CALLBACK_SCHEMA, "id": ids.callback_id(ids.goal_root(goal_id), sequence),
                "created_at": now, "cwd": goal["cwd"], **route, "requires": capabilities.required_for(route),
                "agent": goal.get("agent", "codex"),
                "target": goal["target"],
                "origin": targets.origin(goal.get("agent", "codex"), str(targets.session_of(goal["target"])), goal["cwd"],
                                         str(goal.get("target_source", "goal"))),
                "goal_id": goal_id, "goal_reminder": True, "prompt": "",
            }
            goal_ack_command = reins_runtime.control_command("goal", "ack", "--queue-dir", str(root), "--id", goal_id)
            goal_check_command = reins_runtime.control_command("goal", "check", "--queue-dir", str(root), "--id", goal_id)
            try:
                plan_path, plan, _, _ = goal_plan_snapshot(goal)
                completed, total, current, remaining = goal_plan_progress(plan)
                current_text = (
                    "none; final path item completed"
                    if current is None
                    else f"{current['id']} [{current['status']}] - {current['title']}"
                )
                remaining_text = (
                    "none"
                    if not remaining
                    else "; ".join(
                        f"{step['id']} [{step['status']}] - {step['title']}" for step in remaining
                    )
                )
                plan_text = (
                    f"Current YAML goal: {plan['goal']}\nPlan revision: {plan['revision']}\n"
                    f"YAML goal plan: {plan_path}\nProgress: {completed}/{total}\n"
                    f"Current item: {current_text}\nRemaining path: {remaining_text}\n"
                )
            except ValueError as exc:
                plan_text = (
                    f"YAML goal plan: {goal.get('plan_file', '(missing)')}\n"
                    f"Plan needs repair before progress can continue: {exc}\n"
                )
            prompt = (f"[goal-inactivity-reminder]\nGoal record: {goal.get('task', goal_id)}\n"
                      f"{plan_text}"
                      f"No new callback has been queued for {idle_seconds / 3600:.0f} hour(s). "
                      f"Run {goal_check_command} to inspect the ordered goal path, current item, and remaining items. "
                      "Update that YAML after each path item. Compare the actual work and artifacts with the plan. "
                      f"Only after the final path item is confirmed completed, run: {goal_ack_command} "
                      "--state completed --plan-sha256 <sha256 printed by goal check>. "
                      "If conditions are not ready, run: "
                      f"{goal_ack_command} --state blocked_conditions --condition \"specific missing prerequisite\". "
                      "Otherwise continue the goal and schedule the next callback.")
            if dispatch.enqueue_existing_request(root, request, prompt) != 0:
                return True
            goal["last_reminder_at"] = now
            write_goal(root, goal)
            return True
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            print(f"reins: warning: could not process goal reminder {path.name}: {exc}", file=sys.stderr)
        finally:
            queue_store.release_owner_lock(lock, remove=False)
    return False


def validate_managed_goal(root: Path, goal_id: object) -> None:
    if goal_id is None:
        return
    if not isinstance(goal_id, str):
        raise ValueError("goal id must be a string")
    goal = load_goal(root, goal_id)
    if goal.get("state") != "active":
        raise ValueError(f"goal {goal_id} is not active")


def process_blocked_goal_email(root: Path) -> bool:
    now = time.time()
    configured_recipient = os.environ.get("REINS_BLOCKED_EMAIL_TO")
    for path in sorted((root / "goals").glob("*.json")):
        lock: tuple[object, Path] | None = None
        try:
            goal_id = path.stem
            lock = queue_store.acquire_owner_lock(root, layout.goal_lock_id(goal_id), blocking=True)
            goal = load_goal(root, goal_id)
            if goal.get("state") != "blocked_conditions":
                continue
            recipient = goal.get("blocked_email_to")
            changed_at = float(goal.get("state_changed_at", now))
            delay = float(goal.get("blocked_email_after_seconds", DEFAULT_BLOCKED_EMAIL_SECONDS))
            if (
                not isinstance(recipient, str)
                or not recipient
                or recipient != configured_recipient
                or not is_single_email_recipient(recipient)
                or goal.get("blocked_email_result") == "accepted"
                or goal.get("blocked_email_exhausted_at")
                or now < changed_at + delay
            ):
                continue
            next_attempt = float(goal.get("blocked_email_next_attempt_at", changed_at + delay))
            if now < next_attempt:
                continue
            goal["blocked_email_attempted_at"] = now
            attempts = int(goal.get("blocked_email_attempts", 0)) + 1
            goal["blocked_email_attempts"] = attempts
            write_goal(root, goal)
            task = str(goal.get("task", goal.get("id"))).replace("\r", " ").replace("\n", " ")
            message = EmailMessage()
            message["To"] = recipient
            message["Subject"] = f"Long-task goal remains blocked: {task}"
            message.set_content(
                f"Goal: {task}\nBlocked condition: {goal.get('condition')}\n"
                f"State id: {goal.get('id')}\nThis confirms local MTA acceptance only."
            )
            try:
                result = subprocess.run([os.environ.get("REINS_SENDMAIL", "/usr/sbin/sendmail"), "-t", "-oi"],
                                        input=message.as_string(), text=True, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, timeout=30, check=False)
                goal["blocked_email_result"] = "accepted" if result.returncode == 0 else f"exit_{result.returncode}"
            except (OSError, subprocess.TimeoutExpired) as exc:
                goal["blocked_email_result"] = f"failed_{type(exc).__name__}"
            if goal["blocked_email_result"] != "accepted" and attempts < 3:
                goal["blocked_email_next_attempt_at"] = now + 3600 * (2 ** (attempts - 1))
            elif goal["blocked_email_result"] != "accepted":
                goal["blocked_email_exhausted_at"] = now
            write_goal(root, goal)
            return True
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"reins: warning: could not process blocked-goal email {path.name}: {exc}", file=sys.stderr)
        finally:
            queue_store.release_owner_lock(lock, remove=False)
    return False
