"""Google Antigravity CLI (agy) argument construction and session routing.

An Antigravity conversation is a callback parent. While a window (or any agy
process) has the conversation open, a background ``reins wait`` in it receives
the callback, and agy wakes the agent when that command exits. Once nothing has
it open, the daemon appends a turn headless with ``agy --resume ID -p``. Reins
never resumes a conversation another agy process has open: the daemon checks
the presence lock agy holds for it (see ``session.window_open``).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Literal

from ..base import ChildOptions, resume_target

AGY_BIN_ENV = "REINS_AGY_BIN"
AGY_CONVERSATION_ENV = "ANTIGRAVITY_CONVERSATION_ID"
AGY_MARKER_ENV = "ANTIGRAVITY_AGENT"
AGY_MARKER_VALUES = frozenset({"1", "true", "yes", "on"})
# What agy sets in the shell of each command its agent runs. All of it belongs
# to that one conversation and its language server, and the CSRF token is a
# credential: none of it may reach a child agent or a stored resume environment.
# ANTIGRAVITY_APP_DATA_DIR only says where agy keeps its data, so it is kept.
# Credentials among them: never written to disk or given to a task or a resumed process, not even as part of
# an environment that otherwise passes on whole.
AGY_SECRET_ENV_NAMES = ("ANTIGRAVITY_CSRF_TOKEN", "ANTIGRAVITY_SIDECAR_UI_TOKEN")
AGY_SESSION_ENV_NAMES = (
    AGY_CONVERSATION_ENV,
    AGY_MARKER_ENV,
    "ANTIGRAVITY_TRAJECTORY_ID",
    "ANTIGRAVITY_SOURCE_METADATA",
    "ANTIGRAVITY_CSRF_TOKEN",
    "ANTIGRAVITY_LS_ADDRESS",
    "ANTIGRAVITY_LS_VERSION",
    "ANTIGRAVITY_PROJECT_ID",
    "ANTIGRAVITY_AGENTAPI_EXE",
    "ANTIGRAVITY_SIDECAR_UI_TOKEN",
    "ANTIGRAVITY_SIDECAR_WEB_PORT",
    "ANTIGRAVITY_PERM_GRANTS",
    "ANTIGRAVITY_PLUGIN_SHIM_DIR",
    "ANTIGRAVITY_CONTROL_OUT",
)


def resume_input(prompt: str) -> str:
    """The callback prompt as the one stream-json message ``agy -p`` reads from stdin.

    Text print mode takes its prompt only from the command line, which would
    put the whole callback in the process list and hit argument limits.
    """
    return json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n"


class AgyAdapter:
    name = "agy"
    display_name = "Antigravity"
    session_id_env = AGY_CONVERSATION_ENV
    parent_env_names = AGY_SESSION_ENV_NAMES
    secret_env_names = AGY_SECRET_ENV_NAMES
    # Between Claude Code (10) and DSH (30): a shell agy started inside another
    # agent's session carries that agent's markers too, and agy's are the newest.
    detection_priority = 20
    child_result_mode: Literal["file", "stdout"] = "stdout"
    supports_model = False
    supports_reasoning_effort = False
    reasoning_efforts: tuple[str, ...] = ()
    supports_system_prompt_file = False
    supports_report_file = False
    # agy's print mode takes its prompt only as an argument or as stream-json whose answer
    # needs parsing; until that is built, `reins agent agy` is refused as an unknown child.
    runs_as_child = False

    @property
    def callback_delivery(self) -> Literal["resume", "live"]:
        # The presence lock that keeps the daemon from resuming beside an open
        # window is checked with flock, which exists only on Linux and macOS.
        return "live" if os.name == "nt" else "resume"

    def matches_environment(self, environment: Mapping[str, str]) -> bool:
        if environment.get(AGY_CONVERSATION_ENV, "").strip():
            return True
        return environment.get(AGY_MARKER_ENV, "").strip().lower() in AGY_MARKER_VALUES

    def executable(self, environment: Mapping[str, str]) -> str:
        return environment.get(AGY_BIN_ENV, "agy")

    def resume_command(
        self, request: Mapping[str, object], environment: Mapping[str, str]
    ) -> list[str]:
        # The prompt arrives on stdin as one stream-json message (resume_input);
        # stream-json input needs stream-json output, which the worker discards.
        cmd = [self.executable(environment), "--resume", resume_target(request)]
        for key in ("queue_dir", "link_dir"):
            if isinstance(request.get(key), str) and request[key]:
                cmd.extend(["--add-dir", request[key]])
        cmd.extend(["--input-format", "stream-json", "--output-format", "stream-json", "--print", ""])
        return cmd

    def child_command(
        self, options: ChildOptions, environment: Mapping[str, str]
    ) -> list[str]:
        raise ValueError("Antigravity child agents are not supported yet; use claude, codex, pi or dsh")
