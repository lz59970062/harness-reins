"""Capabilities a reader needs to deliver a callback.

A callback lists in ``requires`` what delivering it depends on. A tower that
does not know one of them leaves the callback untouched in the queue instead
of delivering it halfway; after an upgrade it is delivered normally. This
replaces bumping a record version whenever route semantics change.
"""
from __future__ import annotations

from typing import Iterable, Mapping

CLI_RESUME = "deliver.cli-resume"        # resume the bound session through the harness CLI
CODEX_DESKTOP = "deliver.codex-desktop"  # submit a turn to a loaded Codex Desktop thread
CODEX_TOOL_OUTPUT = "deliver.codex-tool-output"  # native App Server, no CLI fallback
CODEX_QUEUE = "deliver.codex-queue"       # native message queue; receipt still requires ACK
PI_STEER = "deliver.pi-steer"            # Pi extension steers into the running session
PI_FOLLOW_UP = "deliver.pi-follow-up"    # Pi extension queues a follow-up message

KNOWN = frozenset({CLI_RESUME, CODEX_DESKTOP, CODEX_TOOL_OUTPUT, CODEX_QUEUE, PI_STEER, PI_FOLLOW_UP})


def required_for(route: Mapping[str, object]) -> list[str]:
    """What delivering a callback with this frozen route depends on."""
    needed = set()
    mode = route.get("callback_mode")
    if mode == "cli":
        needed.add(CLI_RESUME)
    elif mode == "desktop":
        needed.add(CODEX_DESKTOP)
    elif mode == "app-server":
        needed.add(CODEX_TOOL_OUTPUT)
    elif mode == "codex-queue":
        needed.add(CODEX_QUEUE)
    if route.get("pi_callback_protocol"):
        needed.add(PI_STEER if route.get("pi_delivery") == "steer" else PI_FOLLOW_UP)
    return sorted(needed)


def unsupported(requires: object) -> list[str]:
    """Capabilities this reader does not know; a malformed list counts as unsupported."""
    if not isinstance(requires, list) or not all(isinstance(item, str) for item in requires):
        return ["<malformed requires>"]
    return sorted(set(requires) - KNOWN)


def satisfied(requires: Iterable[str]) -> bool:
    return not unsupported(list(requires))
