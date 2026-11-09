"""Reminder cadence and the user hook appended to delivered callbacks."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
import yaml
from ..core import envelope
from ..system import windows_io
from ..store import layout
from ..store import records as queue_store
from . import dispatch


CALLBACK_HOOK_SECTION = "[reins-user-hook]"
CALLBACK_PROMPT_POLICY_FILE_NAME = "callback-prompts.yaml"
DEFAULT_CALLBACK_PROMPT_POLICY = {"system_every": 4, "user_every": 3}


def read_callback_prompt_policy(path: Path | None = None) -> dict[str, int]:
    source = path or layout.daemon_state_dir() / CALLBACK_PROMPT_POLICY_FILE_NAME
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(DEFAULT_CALLBACK_PROMPT_POLICY)
    if not isinstance(data, dict) or any(key not in DEFAULT_CALLBACK_PROMPT_POLICY for key in data):
        raise ValueError("callback prompt policy accepts only system_every and user_every")
    policy = dict(DEFAULT_CALLBACK_PROMPT_POLICY)
    policy.update(data)
    if any(type(value) is not int or value < 1 for value in policy.values()):
        raise ValueError("callback prompt intervals must be positive integers (1 means every callback)")
    return policy


def positive_prompt_interval(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("interval must be a positive integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("interval must be a positive integer")
    return parsed


def callback_prompt_policy(args: argparse.Namespace) -> int:
    path = layout.daemon_state_dir() / CALLBACK_PROMPT_POLICY_FILE_NAME
    updates = {key: getattr(args, key, None) for key in DEFAULT_CALLBACK_PROMPT_POLICY}
    updates = {key: value for key, value in updates.items() if value is not None}
    lock = None
    try:
        if updates:
            if any(type(value) is not int or value < 1 for value in updates.values()):
                raise ValueError("callback prompt intervals must be positive integers")
            lock = queue_store.acquire_path_lock(path.with_suffix(".lock"), blocking=True)
        policy = read_callback_prompt_policy(path)
        if updates:
            policy.update(updates)
            queue_store.write_private_text(path, yaml.safe_dump(policy, sort_keys=True))
        print(f"Callback prompt policy: {path}")
        print(yaml.safe_dump(policy, sort_keys=True), end="")
        return 0
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"reins: could not configure callback prompts: {exc}", file=sys.stderr)
        return 2
    finally:
        if lock is not None:
            queue_store.release_owner_lock(lock, remove=False)


def allocate_prompt_cadence(
    root: Path, request: dict[str, object], policy_path: Path | None = None,
) -> dict[str, object] | None:
    """Allocate once per callback at first delivery, under an atomic ledger lock.

    The ledger stores both the sequence and decisions in one atomic replacement,
    so a crash/retry cannot consume a second ordinal or change reminder decisions.
    """
    target = request.get("target")
    request_id = request.get("id")
    if (not isinstance(target, dict) or target.get("kind") != "session"
            or not isinstance(target.get("session"), str) or not target["session"]
            or not isinstance(request_id, str) or not request_id):
        return None  # Without a bound session there is no conversation counter.
    agent = dispatch.request_agent(request)
    session = target["session"]
    digest = hashlib.sha256(json.dumps([agent, session]).encode("utf-8")).hexdigest()
    path = root / "prompt-counters" / f"{digest}.json"
    lock = None
    try:
        lock = queue_store.acquire_owner_lock(root, f"prompt-cadence-{digest}", blocking=True)
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = {"version": 1, "agent": agent, "session": session, "count": 0, "callbacks": {}}
        if (not isinstance(state, dict) or state.get("version") != 1
                or state.get("agent") != agent or state.get("session") != session
                or type(state.get("count")) is not int or state["count"] < 0
                or not isinstance(state.get("callbacks"), dict)):
            raise ValueError(f"invalid callback prompt counter {path}")
        callbacks = state["callbacks"]
        sequences = set()
        for allocation in callbacks.values():
            if (not isinstance(allocation, dict)
                    or any(type(allocation.get(key)) is not int or allocation[key] < 1
                           for key in ("sequence", "system_every", "user_every"))):
                raise ValueError(f"invalid callback prompt allocation in {path}")
            if allocation["sequence"] > state["count"]:
                raise ValueError(f"invalid callback prompt sequence in {path}")
            for category in ("system", "user"):
                due = (allocation["sequence"] - 1) % allocation[f"{category}_every"] == 0
                if type(allocation.get(f"{category}_due")) is not bool or allocation[f"{category}_due"] != due:
                    raise ValueError(f"invalid callback prompt decision in {path}")
            sequences.add(allocation["sequence"])
        if len(callbacks) != state["count"] or len(sequences) != state["count"]:
            raise ValueError(f"inconsistent callback prompt counter {path}")
        if request_id in callbacks:
            return dict(callbacks[request_id])
        policy = read_callback_prompt_policy(policy_path)
        sequence = state["count"] + 1
        allocation = {
            "sequence": sequence, **policy,
            "system_due": (sequence - 1) % policy["system_every"] == 0,
            "user_due": (sequence - 1) % policy["user_every"] == 0,
        }
        state["count"] = sequence
        callbacks[request_id] = allocation
        queue_store.write_request(path, state)
        return allocation
    except (OSError, ValueError, RuntimeError, yaml.YAMLError) as exc:
        print(f"reins: prompt cadence unavailable; keeping full reminders: {exc}", file=sys.stderr)
        return None
    finally:
        if lock is not None:
            queue_store.release_owner_lock(lock, remove=False)


def select_delivery_prompt(payload: dict[str, object]) -> str:
    prompt = str(payload["prompt"])
    request = payload.get("request")
    queue_root = payload.get("queue_dir")
    hook_path = payload.get("callback_hook_path")
    cadence = None
    if isinstance(request, dict) and isinstance(queue_root, str) and queue_root:
        policy_path = Path(hook_path).parent / CALLBACK_PROMPT_POLICY_FILE_NAME if isinstance(hook_path, str) and hook_path else None
        cadence = allocate_prompt_cadence(Path(queue_root), request, policy_path)
    if cadence is not None:
        if not cadence["system_due"] and request.get("prompt_format") != "full" and isinstance(request.get("prompt_compact"), str):
            prompt = request["prompt_compact"]
        if request.get("prompt_format") != envelope.FORMAT:
            prompt += (f"\n\nReminder cadence: callback #{cadence['sequence']}; "
                       f"system every {cadence['system_every']}; user every {cadence['user_every']}.")
    if (cadence is None or cadence["user_due"]) and isinstance(hook_path, str) and hook_path:
        prompt = attach_callback_hook(prompt, Path(hook_path))
    if isinstance(request, dict) and request.get("moved_from"):
        prompt = (f"[reins] This was bound to session {request['moved_from']}, which was left through /clear or a "
                  "context continuation. What that session knew may not be in this conversation, so read the "
                  f"task's saved results before you act on it.\n\n{prompt}")
    payload["prompt"] = prompt  # Both Desktop and CLI transports use this choice.
    return prompt


def ensure_callback_hook_file() -> Path:
    path = layout.callback_hook_path()
    if os.name == "nt":
        queue_store.private_directory(path.parent)
        try:
            windows_io.open_private_text(path).close()
        except FileExistsError:
            pass
        print(f"Callback prompt hook: {path}")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.open("x", encoding="utf-8").close()
    except FileExistsError:
        pass
    print(f"Callback prompt hook: {path}")
    return path


def attach_callback_hook(prompt: str, path: Path | None = None) -> str:
    source = path or layout.callback_hook_path()
    try:
        hook = source.read_text(encoding="utf-8")
    except FileNotFoundError:
        return prompt
    except (OSError, UnicodeError) as exc:
        print(f"reins: warning: could not load callback prompt hook {source}: {exc}", file=sys.stderr)
        return prompt
    if not hook.strip():
        return prompt
    return f"{prompt.rstrip()}\n\n{CALLBACK_HOOK_SECTION}\n{hook.rstrip()}"
