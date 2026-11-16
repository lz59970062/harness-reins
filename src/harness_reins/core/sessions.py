"""One view of an agent session, whichever harness hosts it.

See docs/design/sessions-tui.md section 4. Reins keeps no copy of a harness
session's state: a view is built from the harness each time it is needed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# A full session id, as Claude Code and Codex write them; shorter ids are prefixes or other records' ids.
FULL_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# Where the one process of a session is, if any.
ATTACHED = "attached"  # Hosted on the Reins side and shown in a Reins pane.
RUNNING = "running"    # Hosted on the Reins side, no Reins pane shows it.
FOREIGN = "foreign"    # Open in a window Reins does not host (Desktop, a terminal).
HEADLESS = "headless"  # A headless resume (`claude -p`) is running it.
STOPPED = "stopped"    # Its host still knows it, but no process runs.
CLOSED = "closed"      # No process and no host: only the conversation is left.
STATES = (ATTACHED, RUNNING, FOREIGN, HEADLESS, STOPPED, CLOSED)

# What its process is doing, when the harness says.
WORKING = "working"
IDLE = "idle"
BLOCKED = "blocked"    # Stopped on something that needs a person, such as a denied tool call.
ACTIVITIES = (WORKING, IDLE, BLOCKED)

# Who holds the process.
HOST_CLAUDE_BACKGROUND = "claude-background"
HOST_DESKTOP = "desktop"
HOST_TERMINAL = "terminal"
HOST_HEADLESS = "headless"


@dataclass(frozen=True)
class SessionView:
    agent: str
    session: str
    state: str
    activity: Optional[str] = None
    host: Optional[str] = None
    name: Optional[str] = None
    cwd: Optional[str] = None
    pid: Optional[int] = None
    handle: Optional[str] = None  # What the host attaches by, such as the id `claude attach` takes.
    started_at: Optional[float] = None

    def __post_init__(self) -> None:
        if self.state not in STATES:
            raise ValueError(f"unknown session state: {self.state!r}")
        if self.activity is not None and self.activity not in ACTIVITIES:
            raise ValueError(f"unknown session activity: {self.activity!r}")

    @property
    def has_process(self) -> bool:
        return self.state in (ATTACHED, RUNNING, FOREIGN, HEADLESS)

    @property
    def reins_hosted(self) -> bool:
        """Whether its host is on the Reins side, so Reins may attach to it or stop it.

        This is not the test for opening: a closed session is opened too, by
        resuming it on the Reins side first. A foreign window and a headless
        resume are never taken over.
        """
        return self.state in (ATTACHED, RUNNING, STOPPED)
