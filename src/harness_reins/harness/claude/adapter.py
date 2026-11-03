"""Claude Code CLI argument construction and parent-session isolation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from ..base import ChildOptions, DEFAULT_CLAUDE_PERMISSION_MODE, resume_target

CLAUDE_THREAD_ID_ENV = "CLAUDE_CODE_SESSION_ID"
CLAUDE_MARKER_ENV = "CLAUDECODE"
CLAUDE_BIN_ENV = "REINS_CLAUDE_BIN"
CLAUDE_PERMISSION_MODE_ENV = "REINS_CLAUDE_PERMISSION_MODE"


class ClaudeAdapter:
    name = "claude"
    display_name = "Claude Code"
    session_id_env = CLAUDE_THREAD_ID_ENV
    parent_env_names = (CLAUDE_THREAD_ID_ENV, CLAUDE_MARKER_ENV)
    # Preserve legacy auto-detection when a nested shell retains both agents'
    # environment markers: Claude's explicit marker/session takes precedence.
    detection_priority = 10
    child_result_mode: Literal["file", "stdout"] = "stdout"
    supports_model = True
    supports_reasoning_effort = False
    reasoning_efforts: tuple[str, ...] = ()
    supports_system_prompt_file = False
    supports_report_file = True

    def matches_environment(self, environment: Mapping[str, str]) -> bool:
        marker = environment.get(CLAUDE_MARKER_ENV, "").strip().lower()
        return marker in {"1", "true", "yes", "on"} or bool(
            environment.get(self.session_id_env, "").strip()
        )

    def executable(self, environment: Mapping[str, str]) -> str:
        return environment.get(CLAUDE_BIN_ENV, "claude")

    def resume_command(
        self, request: Mapping[str, object], environment: Mapping[str, str]
    ) -> list[str]:
        permission_mode = request.get("permission_mode")
        if not isinstance(permission_mode, str) or not permission_mode:
            permission_mode = environment.get(CLAUDE_PERMISSION_MODE_ENV, DEFAULT_CLAUDE_PERMISSION_MODE)
        cmd = [self.executable(environment), "-p", "--permission-mode", permission_mode]
        for key in ("queue_dir", "link_dir"):
            if isinstance(request.get(key), str) and request[key]:
                cmd.extend(["--add-dir", request[key]])
        cmd.extend(["--resume", resume_target(request)])
        return cmd

    def child_command(
        self, options: ChildOptions, environment: Mapping[str, str]
    ) -> list[str]:
        if options.reasoning_effort:
            raise ValueError("--reasoning-effort is supported only for Codex children")
        cmd = [self.executable(environment), "-p"]
        if not options.keep_session:
            cmd.append("--no-session-persistence")
        if options.fork_from:
            cmd.extend(["--resume", options.fork_from, "--fork-session"])
        if options.keep_session and options.session_id:
            cmd.extend(["--session-id", options.session_id])
        if options.model:
            cmd.extend(["--model", options.model])
        cmd.extend(["--permission-mode", options.permission_mode, "--output-format", "text"])
        return cmd
