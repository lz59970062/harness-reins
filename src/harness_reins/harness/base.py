"""Agent command contracts, independent of task storage and execution backends.

Adapters construct argument vectors from explicit inputs. They never launch
processes, mutate the environment, or own callback retries and task state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

DEFAULT_APPROVALS_REVIEWER = "auto_review"
DEFAULT_APPROVAL_POLICY = "on-request"
DEFAULT_SANDBOX_MODE = "workspace-write"
DEFAULT_CLAUDE_PERMISSION_MODE = "auto"
CODEX_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max", "ultra")


@dataclass(frozen=True)
class ChildOptions:
    """Inputs shared by child-agent command builders.

    The caller supplies the prompt on stdin and runs the command in ``cwd``.
    ``child_result_mode`` tells the executor whether to capture stdout or read
    the result file written by the agent itself.
    """

    cwd: str
    result_path: Path
    sandbox_mode: str
    permission_mode: str
    model: str | None = None
    reasoning_effort: str | None = None
    system_prompt_path: Path | None = None
    # Context mode: keep the child's session so later nodes can fork it.
    keep_session: bool = False
    fork_from: str | None = None          # harness session (Pi: session file) to fork before running
    session_id: str | None = None         # preassigned new session id, where the harness accepts one
    session_dir: Path | None = None       # where the harness stores this node's session, if configurable


class ChildAgentAdapter(Protocol):
    """A fresh child process, independent of callback session transport."""

    name: str
    display_name: str
    parent_env_names: tuple[str, ...]
    child_result_mode: Literal["file", "stdout"]
    supports_model: bool
    supports_reasoning_effort: bool
    reasoning_efforts: tuple[str, ...]
    supports_system_prompt_file: bool
    supports_report_file: bool

    def executable(self, environment: Mapping[str, str]) -> str: ...

    def child_command(
        self, options: ChildOptions, environment: Mapping[str, str]
    ) -> list[str]: ...


class AgentAdapter(ChildAgentAdapter, Protocol):
    """A child-capable agent with a separately verified callback transport."""

    session_id_env: str
    detection_priority: int

    def matches_environment(self, environment: Mapping[str, str]) -> bool: ...

    def resume_command(
        self, request: Mapping[str, object], environment: Mapping[str, str]
    ) -> list[str]: ...


def resume_target(request: Mapping[str, object]) -> str:
    """Return the bound session, validated before any resume command is built.

    Reins only ever resumes an explicitly bound session; it never guesses one.
    """
    target = request.get("target")
    if not isinstance(target, dict):
        raise ValueError("request target must be an object")
    if target.get("kind") != "session":
        raise ValueError("request target kind must be 'session'")
    value = target.get("session")
    if not isinstance(value, str) or not value:
        raise ValueError("session target requires a non-empty value")
    return value
