"""Codex CLI argument construction; no subprocess or filesystem operations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Literal

from ..base import (
    ChildOptions,
    CODEX_REASONING_EFFORTS,
    DEFAULT_APPROVAL_POLICY,
    DEFAULT_APPROVALS_REVIEWER,
    DEFAULT_SANDBOX_MODE,
    resume_target,
)

CODEX_THREAD_ID_ENV = "CODEX_THREAD_ID"
CODEX_BIN_ENV = "REINS_CODEX_BIN"


class CodexAdapter:
    name = "codex"
    display_name = "Codex"
    session_id_env = CODEX_THREAD_ID_ENV
    parent_env_names = (CODEX_THREAD_ID_ENV,)
    detection_priority = 0
    child_result_mode: Literal["file", "stdout"] = "file"
    supports_model = True
    supports_reasoning_effort = True
    reasoning_efforts = CODEX_REASONING_EFFORTS
    supports_system_prompt_file = False
    supports_report_file = True

    def matches_environment(self, environment: Mapping[str, str]) -> bool:
        return bool(environment.get(self.session_id_env, "").strip())

    def executable(self, environment: Mapping[str, str]) -> str:
        return environment.get(CODEX_BIN_ENV, "codex")

    def resume_command(
        self, request: Mapping[str, object], environment: Mapping[str, str]
    ) -> list[str]:
        # The session already ran in its cwd; Codex's first-run git/trust check
        # would otherwise refuse to resume any session started outside a repo.
        cmd = [self.executable(environment), "exec", "resume", "--all", "--skip-git-repo-check"]
        for key, default in (
            ("approvals_reviewer", DEFAULT_APPROVALS_REVIEWER),
            ("approval_policy", DEFAULT_APPROVAL_POLICY),
            ("sandbox_mode", DEFAULT_SANDBOX_MODE),
        ):
            value = request.get(key, default)
            if isinstance(value, str) and value:
                cmd.extend(["-c", f"{key}={json.dumps(value)}"])
        roots = [request[key] for key in ("queue_dir", "link_dir") if isinstance(request.get(key), str) and request[key]]
        if roots:
            cmd.extend(["-c", f"sandbox_workspace_write.writable_roots={json.dumps(roots)}"])
        cmd.append(resume_target(request))
        cmd.append("-")
        return cmd

    def child_command(
        self, options: ChildOptions, environment: Mapping[str, str]
    ) -> list[str]:
        # A kept session is forked by later nodes; --json reports its thread id.
        # The caller chose the cwd with --cwd; Codex's git/trust check would
        # otherwise end a child started outside a repository within a second.
        cmd = [self.executable(environment), "exec", "--json" if options.keep_session else "--ephemeral",
               "--skip-git-repo-check"]
        if options.model:
            cmd.extend(["--model", options.model])
        if options.reasoning_effort:
            cmd.extend(["-c", f"model_reasoning_effort={json.dumps(options.reasoning_effort)}"])
        # --approve-for-me conflicts with --sandbox in Codex 0.153.4. Keep the
        # requested sandbox and set approval configuration explicitly instead.
        cmd.extend([
            "-C", options.cwd,
            "-s", options.sandbox_mode,
            "-c", f"approvals_reviewer={json.dumps(DEFAULT_APPROVALS_REVIEWER)}",
            "-c", f"approval_policy={json.dumps(DEFAULT_APPROVAL_POLICY)}",
            "-o", str(options.result_path),
        ])
        if options.fork_from:
            cmd.extend(["fork", options.fork_from])
        cmd.append("-")
        return cmd
