"""Where Reins keeps things: the single source of state, queue, task and lock paths."""
from __future__ import annotations

import argparse
import os
from pathlib import Path


ACTIVE_STATE = "active"
LOCKS_STATE = "locks"
TASKS_DIR_NAME = "tasks"
REINS_HOME_ENV = "REINS_HOME"
TARGET_LOCK_DIR_ENV = "REINS_TARGET_LOCK_DIR"
CALLBACK_HOOK_FILE_NAME = "callback-hook.md"


def queue_dir(args: argparse.Namespace | None = None) -> Path:
    explicit = getattr(args, "queue_dir", None) if args is not None else None
    path = explicit or os.environ.get("REINS_QUEUE_DIR")
    if path:
        return Path(path).expanduser()
    return daemon_state_dir() / "queue"


def ack_path(root: Path, request_id: str) -> Path:
    return root / "acks" / f"{request_id}.json"


def request_path(root: Path, state: str, request_id: str) -> Path:
    return root / state / f"{request_id}.json"


def goal_path(root: Path, goal_id: str) -> Path:
    if not goal_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in goal_id):
        raise ValueError("goal id must contain only letters, numbers, '-' or '_'")
    return root / "goals" / f"{goal_id}.json"


def goal_lock_id(goal_id: str) -> str:
    return f"goal-{goal_id}"


def target_lock_dir() -> Path:
    override = os.environ.get(TARGET_LOCK_DIR_ENV)
    if override:
        return Path(override).expanduser()
    return daemon_state_dir() / "target-locks"


def owner_lock_path(root: Path, request_id: str) -> Path:
    return root / LOCKS_STATE / f"{request_id}.lock"


def delivery_lock_id(request_id: str) -> str:
    return f"delivery-{request_id}"


def managed_tasks_root(root: Path) -> Path:
    root = root.expanduser().absolute()
    return root.parent / TASKS_DIR_NAME if root.name == "queue" else root / TASKS_DIR_NAME


def managed_task_dir(root: Path, task_id: str) -> Path:
    if not task_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for character in task_id):
        raise ValueError("task id must contain only letters, numbers, '-' or '_'")
    return managed_tasks_root(root) / task_id


def managed_task_path(root: Path, task_id: str) -> Path:
    return managed_task_dir(root, task_id) / "task.json"


def managed_result_path(root: Path, task_id: str) -> Path:
    return managed_task_dir(root, task_id) / "result.json"


def link_root(root: Path) -> Path:
    """Sessions and messages; beside the default queue, like its tasks."""
    root = root.expanduser().absolute()
    return root.parent / "link" if root.name == "queue" else root / "link"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()


def reins_home() -> Path:
    """Reins's own state root; it belongs to no single Agent profile."""
    return Path(os.environ.get(REINS_HOME_ENV) or "~/.reins").expanduser()


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()


def daemon_state_dir() -> Path:
    return reins_home()


def callback_hook_path() -> Path:
    return daemon_state_dir() / CALLBACK_HOOK_FILE_NAME


def service_proxy_env_path() -> Path:
    return daemon_state_dir() / "service-proxy.env"


def daemon_runtime_path() -> Path:
    return daemon_state_dir() / "daemon-runtime.json"
