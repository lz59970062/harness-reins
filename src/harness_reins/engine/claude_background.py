"""Claude Code background sessions: start, resume, stop and remove them.

docs/design/sessions-tui.md sections 5 and 6. Claude hosts these sessions
itself (`claude --bg`); Reins only runs its public commands and reads the
state back from `claude agents` (harness/claude/agents.py).

What Claude 2.1.325 does, measured on 2026-11-16:

- `claude --bg --resume <id>` on a closed session continues it under the same
  id, idle until something sends it a prompt. The Reins hook then delivers.
- On a stopped background session it continues the same id only when given
  no other flags: the session keeps the options it was started with, and new
  flags start a copy under another id.
- On a session open in another process it starts a copy and says so. Reins
  checks the state first, and removes a copy if one starts anyway.
- It refuses a directory the user has not trusted ("Workspace not trusted").
- The first `--bg` starts Claude's daemon, which stays in the cgroup of
  whatever started it: inside the Reins service, a restart of the service would
  kill every background session. So on Linux it starts in its own systemd scope.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Optional

from ..core import sessions
from ..harness.claude import agents as claude_agents
from ..harness.claude import session as claude_session

COMMAND_TIMEOUT_SECONDS = 60.0
_BACKGROUNDED = re.compile(r"backgrounded\s+\S+\s+([0-9a-f]{8})\b")
_UNTRUSTED = "Workspace not trusted"
# Agent commands a hosted session may run without the auto-mode classifier
# (design 6.4). Only bookkeeping on callbacks and goals: never `reins run`,
# which would let any command through, and never a bulk `cancel`.
AGENT_COMMANDS = ("ack", "goal check", "goal ack")
# A background resume that has not brought the session up within this long is
# not tried again: the daemon falls back to a headless resume instead of looping.
RETRY_AFTER_SECONDS = 180.0
DISABLE_ENV = "REINS_CLAUDE_BACKGROUND"
_RECORD_NAME = "claude-background.json"

Run = Callable[..., "subprocess.CompletedProcess[str]"]


class BackgroundError(Exception):
    """Claude did not start the session in the background; ``str()`` says why in plain words."""


class Untrusted(BackgroundError):
    pass


class Unsupported(BackgroundError):
    pass


class StartedCopy(BackgroundError):
    """Claude started a copy because another process has the session; the copy was removed."""


def allow_rules(reins_commands: Iterable[str]) -> list[str]:
    """Claude permission rules for the agent commands, for each way the session may name Reins."""
    rules = []
    for command in reins_commands:
        for mode in AGENT_COMMANDS:
            rule = f"Bash({command} {mode} *)"
            if rule not in rules:
                rules.append(rule)
    return rules


def settings_argument(reins_commands: Iterable[str]) -> str:
    return json.dumps({"permissions": {"allow": allow_rules(reins_commands)}}, separators=(",", ":"))


# Claude variables that configure the user's Claude rather than describe a running session: whose
# configuration, how it signs in, which provider. Everything else named CLAUDE* comes from a parent
# session (CLAUDECODE, CLAUDE_CODE_SESSION_ID, CLAUDE_CODE_CHILD_SESSION, ...) and is dropped.
_KEPT_CLAUDE_VARIABLES = frozenset({"CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
                                    "CLAUDE_CODE_CLIENT_CERT", "CLAUDE_CODE_CLIENT_KEY",
                                    "CLAUDE_CODE_CLIENT_KEY_PASSPHRASE"})
_KEPT_CLAUDE_PREFIXES = ("CLAUDE_CODE_USE_", "CLAUDE_CODE_SKIP_")


def _kept(key: str) -> bool:
    if key == claude_session.RESUMED_TARGET_ENV:
        return False
    if not key.startswith("CLAUDE"):
        return True
    return key in _KEPT_CLAUDE_VARIABLES or key.startswith(_KEPT_CLAUDE_PREFIXES)


def launch_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """The environment for a Claude command that starts a session of its own.

    A parent Claude session's variables would make the new one a child of it
    (it then saves no transcript), and a Reins resume marker would stop the
    hook from delivering. What says whose Claude this is and how it signs in
    stays; Reins passes it on without reading it.
    """
    clean = {key: value for key, value in environment.items() if _kept(key)}
    if hasattr(os, "getuid"):
        runtime = clean.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        if os.path.isdir(runtime):
            clean.setdefault("XDG_RUNTIME_DIR", runtime)
            bus = os.path.join(runtime, "bus")
            if os.path.exists(bus):
                clean.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={bus}")
    return clean


def scoped(argv: list[str], environment: Mapping[str, str]) -> list[str]:
    """``argv`` in a systemd user scope of its own on Linux, so Claude's daemon outlives whoever started it."""
    if not sys.platform.startswith("linux") or not environment.get("XDG_RUNTIME_DIR"):
        return argv
    systemd_run = shutil.which("systemd-run", path=environment.get("PATH"))
    if systemd_run is None:
        return argv
    return [systemd_run, "--user", "--scope", "--quiet", "--collect", "--no-ask-password",
            "--description=Claude Code background sessions started by Harness Reins", "--", *argv]


def _executable(environment: Mapping[str, str]) -> str:
    found = claude_session.discover_executable(environment)
    if found is None:
        raise Unsupported("the Claude Code CLI was not found")
    return found.path


_SUPPORT: dict[tuple[str, Optional[float]], bool] = {}


def _modified(path: str) -> Optional[float]:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def supports_background(environment: Mapping[str, str] = os.environ, *, run: Run = subprocess.run) -> bool:
    """Whether this Claude CLI has `--bg` (2.1.3xx and later); asked once per binary."""
    try:
        executable = _executable(environment)
    except Unsupported:
        return False
    key = (executable, _modified(executable))
    if key not in _SUPPORT:
        try:
            result = run([executable, "--help"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=COMMAND_TIMEOUT_SECONDS,
                         env=dict(environment), check=False)
            _SUPPORT[key] = result.returncode == 0 and "--bg" in (result.stdout or "")
        except (OSError, subprocess.TimeoutExpired):
            return False
    return _SUPPORT[key]


def _claude(arguments: list[str], environment: Mapping[str, str], *, cwd: Optional[str] = None,
            run: Run = subprocess.run, scope: bool = False) -> str:
    """Run one Claude command and return what it printed; BackgroundError when it fails."""
    clean = launch_environment(environment)
    argv = [_executable(environment), *arguments]
    if scope:
        argv = scoped(argv, clean)
    try:
        result = run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                     errors="replace", timeout=COMMAND_TIMEOUT_SECONDS, env=clean, cwd=cwd, check=False)
    except subprocess.TimeoutExpired as exc:
        raise BackgroundError(f"`claude {arguments[0]}` did not finish in {COMMAND_TIMEOUT_SECONDS:g} s") from exc
    except OSError as exc:
        raise BackgroundError(f"could not run Claude: {exc.strerror or exc}") from exc
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if _UNTRUSTED in output:
        raise Untrusted(f"Claude Code has not been trusted in {cwd}: run `claude` there once and accept")
    if result.returncode != 0:
        last = output.splitlines()[-1] if output else f"exit status {result.returncode}"
        raise BackgroundError(f"`claude {arguments[0]}` failed: {last}")
    return output


def _short_id(output: str) -> str:
    match = _BACKGROUNDED.search(output)
    if match is None:
        # It may still have started one; without its id Reins cannot check or remove it.
        print("reins: warning: Claude reported success but Reins could not read which background session it "
              "started; check `claude agents` for a session to `claude rm`. Claude said: "
              + (output.splitlines()[0] if output else "nothing"), file=sys.stderr, flush=True)
        raise BackgroundError("Claude did not say which background session it started")
    return match.group(1)


def start(cwd: str, environment: Mapping[str, str] = os.environ, *, settings: Optional[str] = None,
          permission_mode: Optional[str] = None, prompt: Optional[str] = None, run: Run = subprocess.run) -> str:
    """Start a new background session in ``cwd``, working on ``prompt`` when one is given; returns its short id."""
    arguments = ["--bg"]
    if settings:
        arguments += ["--settings", settings]
    if permission_mode:
        arguments += ["--permission-mode", permission_mode]
    if prompt:
        arguments += ["--", prompt]
    return _short_id(_claude(arguments, environment, cwd=cwd, run=run, scope=True))


def resume(session: str, cwd: str, environment: Mapping[str, str] = os.environ, *, stopped: bool,
           settings: Optional[str] = None, permission_mode: Optional[str] = None,
           extra: Iterable[str] = (), run: Run = subprocess.run) -> str:
    """Bring ``session`` up as a background session under its own id; returns the short id.

    A stopped background session gets no flags, or Claude would start a copy
    with them. A closed one is given ``settings``, ``permission_mode`` and
    ``extra``, which it then keeps.
    """
    arguments = ["--bg", "--resume", session]
    if not stopped:
        if settings:
            arguments += ["--settings", settings]
        if permission_mode:
            arguments += ["--permission-mode", permission_mode]
        arguments += list(extra)
    short = _short_id(_claude(arguments, environment, cwd=cwd, run=run, scope=True))
    if short != session[:8]:
        for command in ("stop", "rm"):
            try:
                _claude([command, short], environment, run=run)
            except BackgroundError:
                pass
        raise StartedCopy(f"session {session[:8]} is open in another Claude Code process")
    return short


def fork(session: str, cwd: str, environment: Mapping[str, str] = os.environ, *, name: Optional[str] = None,
         settings: Optional[str] = None, run: Run = subprocess.run) -> str:
    """Start a background copy of ``session``'s conversation under a new id; returns its short id.

    The original is left as it is, open or not.
    """
    arguments = ["--bg", "--resume", session, "--fork-session"]
    if name:
        arguments += ["--name", name]
    if settings:
        arguments += ["--settings", settings]
    short = _short_id(_claude(arguments, environment, cwd=cwd, run=run, scope=True))
    if short == session[:8]:
        raise BackgroundError(f"Claude resumed {session[:8]} instead of forking it")
    return short


def stop(handle: str, environment: Mapping[str, str] = os.environ, *, run: Run = subprocess.run) -> None:
    """Stop a background session's process; its conversation stays."""
    _claude(["stop", handle], environment, run=run)


def remove(handle: str, environment: Mapping[str, str] = os.environ, *, run: Run = subprocess.run) -> None:
    """Remove a background session from Claude's list; its transcript stays, so it reads as closed."""
    _claude(["rm", handle], environment, run=run)


def attach_command(handle: str, environment: Mapping[str, str] = os.environ) -> list[str]:
    return [_executable(environment), "attach", handle]


# The daemon's side: resume a closed session for a callback (design 6.1).

def _record_path(root: Path) -> Path:
    return root / _RECORD_NAME


def _recent(root: Path, session: str, now: float) -> bool:
    try:
        started = json.loads(_record_path(root).read_text(encoding="utf-8")).get(session)
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(started, (int, float)) and 0 <= now - started < RETRY_AFTER_SECONDS


def _remember(root: Path, session: str, now: float) -> None:
    path = _record_path(root)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            record = {}
    except (OSError, ValueError):
        record = {}
    record = {key: value for key, value in record.items()
              if isinstance(value, (int, float)) and now - value < RETRY_AFTER_SECONDS}
    record[session] = now
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(record), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        pass


def resume_for_callback(root: Path, session: str, cwd: str, environment: Mapping[str, str], *,
                        reins_commands: Iterable[str], permission_mode: Optional[str],
                        extra: Iterable[str] = (), run: Run = subprocess.run,
                        now: Optional[float] = None) -> Optional[str]:
    """Bring a closed session up in the background so the hook delivers to it.

    Returns why it did not when the caller should resume headless instead, or
    None when the session is now up (or another process has it): either way the
    callback waits for the hook. Never raises.
    """
    now = time.time() if now is None else now
    if environment.get(DISABLE_ENV, "").strip() == "0":
        return f"{DISABLE_ENV}=0"
    if not claude_session.hook_installed(claude_session.config_dir(environment) / "settings.json"):
        return "the Reins hook is not installed, so nothing would deliver to a background session"
    try:
        if not supports_background(environment, run=run):
            return "this Claude Code CLI has no --bg"
        view = claude_agents.session_view(session, environment, run=run)
        if view is not None and view.has_process:
            return None  # Someone has it: the hook there delivers, and a resume would start a copy.
        if _recent(root, session, now):
            return "a background resume of this session did not come up"
        stopped = view is not None and view.state == sessions.STOPPED
        resume(session, cwd, environment, stopped=stopped, settings=settings_argument(reins_commands),
               permission_mode=permission_mode, extra=extra, run=run)
    except StartedCopy:
        return None
    except BackgroundError as exc:
        return str(exc)
    except Exception as exc:  # A bug here must not stop delivery: the headless resume still works.
        return f"{type(exc).__name__}: {exc}"
    _remember(root, session, now)
    print(f"reins: resumed Claude session {session} in the background; its hook delivers the callback",
          file=sys.stderr, flush=True)
    return None
