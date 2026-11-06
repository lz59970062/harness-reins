"""reins goal: start, check, plan, acknowledge and resume multi-stage goals."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from ..core import ids
from ..store import layout
from ..store import records as queue_store
from ..engine import dispatch
from ..engine import goals
from ..engine import routes


def goal_start(args: argparse.Namespace) -> int:
    root = layout.queue_dir(args)
    queue_store.ensure_daemon_dirs(root)
    target, source = dispatch.resolve_target(args)
    goal_id = args.id or ids.new_goal_id()
    try:
        path = layout.goal_path(root, goal_id)
    except ValueError as exc:
        print(f"reins: error: {exc}", file=sys.stderr)
        return 2
    if args.idle_seconds <= 0:
        print("reins: error: --idle-seconds must be positive", file=sys.stderr)
        return 2
    try:
        plan_path, plan, plan_digest = goals.resolve_goal_plan_file(
            args.cwd, getattr(args, "plan_file", None)
        )
    except ValueError as exc:
        print(f"reins: error: {exc}", file=sys.stderr)
        return 2
    try:
        # Reminders go the way this session's job results go: a Codex Desktop thread through its
        # queue, a Pi session through its channel, the rest through the CLI.
        route = routes.selection(args)
    except ValueError as exc:
        print(f"reins: error: {exc}", file=sys.stderr)
        return 2
    lock = queue_store.acquire_owner_lock(root, layout.goal_lock_id(goal_id), blocking=True)
    try:
        if path.exists():
            print(f"reins: goal {goal_id} already exists", file=sys.stderr)
            return 1
        now = time.time()
        goals.write_goal(root, {"version": 1, "id": goal_id, "task": args.task, "cwd": args.cwd, "target": target,
                          "target_source": source, "agent": dispatch.resolve_agent(args), "state": "active", "created_at": now,
                          "last_external_callback_at": now, "idle_seconds": args.idle_seconds,
                          "plan_file": str(plan_path), "plan_goal": plan["goal"],
                          "plan_sha256_at_start": plan_digest, "route": route})
    finally:
        queue_store.release_owner_lock(lock, remove=False)
    print(goal_id)
    return 0


def goal_check(args: argparse.Namespace) -> int:
    root = layout.queue_dir(args)
    try:
        layout.goal_path(root, args.id)
    except ValueError as exc:
        print(f"reins: error: {exc}", file=sys.stderr)
        return 2
    lock = queue_store.acquire_owner_lock(root, layout.goal_lock_id(args.id), blocking=True)
    try:
        try:
            goal = goals.load_goal(root, args.id)
            plan_path, plan, content, digest = goals.goal_plan_snapshot(goal)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"reins: error: {exc}", file=sys.stderr)
            return 1
        if goal.get("state") == "completed":
            print(f"reins: error: completed goal {args.id} is terminal", file=sys.stderr)
            return 1
        completed, total, current, remaining = goals.goal_plan_progress(plan)
        now = time.time()
        goal["last_plan_checked_at"] = now
        goal["last_plan_check_sha256"] = digest
        goal["plan_goal"] = plan["goal"]
        goal["plan_revision"] = plan["revision"]
        goal["plan_completed_items"] = completed
        goal["plan_total_items"] = total
        goals.write_goal(root, goal)
    finally:
        queue_store.release_owner_lock(lock, remove=False)

    print(f"Goal: {plan['goal']}")
    print(f"Plan revision: {plan['revision']}")
    print(f"Plan file: {plan_path}")
    print(f"Progress: {completed}/{total} path items completed")
    if current is None:
        print("Current item: none; the final path item is completed")
    else:
        print(
            f"Current item: {current['id']} [{current['status']}] - {current['title']}"
        )
        print("Remaining path:")
        for step in remaining:
            print(f"- {step['id']} [{step['status']}] - {step['title']}")
    print(f"Plan SHA-256: {digest}")
    print("\n--- YAML goal plan begin ---")
    print(content, end="" if content.endswith("\n") else "\n")
    print("--- YAML goal plan end ---")
    print(
        "Compare actual work and artifacts with every path item above. Do not report the goal "
        "complete unless the final item and every preceding item are confirmed completed."
    )
    return 0


def goal_set_plan(args: argparse.Namespace) -> int:
    root = layout.queue_dir(args)
    try:
        layout.goal_path(root, args.id)
    except ValueError as exc:
        print(f"reins: error: {exc}", file=sys.stderr)
        return 2
    lock = queue_store.acquire_owner_lock(root, layout.goal_lock_id(args.id), blocking=True)
    try:
        try:
            goal = goals.load_goal(root, args.id)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"reins: error: {exc}", file=sys.stderr)
            return 1
        if goal.get("state") == "completed":
            print(f"reins: error: completed goal {args.id} is terminal", file=sys.stderr)
            return 1
        try:
            plan_path, plan, plan_digest = goals.resolve_goal_plan_file(
                str(goal.get("cwd", os.getcwd())), args.plan_file
            )
        except ValueError as exc:
            print(f"reins: error: {exc}", file=sys.stderr)
            return 2
        goal.update(
            {
                "plan_file": str(plan_path),
                "plan_goal": plan["goal"],
                "plan_revision": plan["revision"],
                "plan_sha256_when_attached": plan_digest,
                "plan_attached_at": time.time(),
            }
        )
        for field in (
            "last_plan_checked_at",
            "last_plan_check_sha256",
            "plan_completed_items",
            "plan_total_items",
        ):
            goal.pop(field, None)
        goals.write_goal(root, goal)
    finally:
        queue_store.release_owner_lock(lock, remove=False)
    print(f"reins: goal {args.id} now follows YAML plan {plan_path}")
    print("reins: run goal check before acknowledging completion")
    return 0


def goal_ack(args: argparse.Namespace) -> int:
    root = layout.queue_dir(args)
    try:
        layout.goal_path(root, args.id)
    except ValueError as exc:
        print(f"reins: error: {exc}", file=sys.stderr)
        return 2
    if args.state == "blocked_conditions":
        if not args.condition or not args.condition.strip():
            print("reins: error: --condition is required for blocked_conditions", file=sys.stderr)
            return 2
        if args.email_after <= 0:
            print("reins: error: --email-after must be positive", file=sys.stderr)
            return 2
        if args.email_to and not goals.is_single_email_recipient(args.email_to):
            print("reins: error: invalid email recipient", file=sys.stderr)
            return 2
        configured_recipient = os.environ.get("REINS_BLOCKED_EMAIL_TO")
        if args.email_to and args.email_to != configured_recipient:
            print("reins: error: --email-to must match REINS_BLOCKED_EMAIL_TO", file=sys.stderr)
            return 2
    lock = queue_store.acquire_owner_lock(root, layout.goal_lock_id(args.id), blocking=True)
    try:
        try:
            goal = goals.load_goal(root, args.id)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"reins: error: {exc}", file=sys.stderr)
            return 1
        if goal.get("state") == "completed" and args.state != "completed":
            print(f"reins: error: completed goal {args.id} is terminal", file=sys.stderr)
            return 1
        if args.state == "completed":
            try:
                plan_path, plan, _, plan_digest = goals.goal_plan_snapshot(goal)
            except ValueError as exc:
                print(f"reins: error: {exc}", file=sys.stderr)
                return 1
            supplied_digest = getattr(args, "plan_sha256", None)
            if not supplied_digest:
                print(
                    f"reins: error: run 'reins goal check --id {args.id}' and pass its digest "
                    "with --plan-sha256 before completing the goal",
                    file=sys.stderr,
                )
                return 2
            checked_digest = goal.get("last_plan_check_sha256")
            if supplied_digest != plan_digest or checked_digest != plan_digest:
                print(
                    f"reins: error: YAML goal plan changed or was not checked: {plan_path}; "
                    f"rerun 'reins goal check --id {args.id}' and compare the work again",
                    file=sys.stderr,
                )
                return 1
            completed_items, total_items, current, _ = goals.goal_plan_progress(plan)
            if current is not None:
                print(
                    f"reins: error: goal path is only {completed_items}/{total_items} complete; "
                    f"current item is {current['id']} [{current['status']}] - {current['title']}",
                    file=sys.stderr,
                )
                return 1
        now = time.time()
        goal["state"] = args.state
        goal["state_changed_at"] = now
        if args.message:
            goal["message"] = args.message
        if args.state == "blocked_conditions":
            goals.clear_blocked_goal_email(goal)
            goal["condition"] = args.condition
            goal["blocked_email_to"] = args.email_to
            goal["blocked_email_after_seconds"] = args.email_after
        else:
            goal.pop("condition", None)
            goals.clear_blocked_goal_email(goal)
            goal["completion_plan_sha256"] = plan_digest
            goal["completion_plan_revision"] = plan["revision"]
            goal["completion_plan_checked_at"] = goal["last_plan_checked_at"]
        goals.write_goal(root, goal)
    finally:
        queue_store.release_owner_lock(lock, remove=False)
    print(f"reins: goal {args.id} acknowledged as {args.state}")
    return 0


def goal_resume(args: argparse.Namespace) -> int:
    root = layout.queue_dir(args)
    try:
        layout.goal_path(root, args.id)
    except ValueError as exc:
        print(f"reins: error: {exc}", file=sys.stderr)
        return 2
    lock = queue_store.acquire_owner_lock(root, layout.goal_lock_id(args.id), blocking=True)
    try:
        try:
            goal = goals.load_goal(root, args.id)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"reins: error: {exc}", file=sys.stderr)
            return 1
        if goal.get("state") == "completed":
            print("reins: error: completed goals are terminal", file=sys.stderr)
            return 1
        if goal.get("state") != "blocked_conditions":
            print("reins: error: only blocked goals can be resumed", file=sys.stderr)
            return 1
        now = time.time()
        goal.update({"state": "active", "state_changed_at": now, "last_external_callback_at": now})
        goal.pop("condition", None)
        goal.pop("last_reminder_at", None)
        goals.clear_blocked_goal_email(goal)
        goals.write_goal(root, goal)
    finally:
        queue_store.release_owner_lock(lock, remove=False)
    print(f"reins: goal {args.id} resumed")
    return 0
