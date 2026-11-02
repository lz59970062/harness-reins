"""The environment the tower runs with: agent executables and the service proxy file."""
from __future__ import annotations

import argparse
import os
import shlex
import shutil
import uuid
from pathlib import Path
from ..harness.claude import session as claude_session
from ..harness.claude.adapter import CLAUDE_BIN_ENV
from ..harness.dsh import session as dsh_session
from ..harness.dsh.adapter import DSH_BIN_ENV
from ..harness.pi.adapter import PI_BIN_ENV
from ..store import layout
from ..store import records as queue_store
from ..harness.codex import app_server
from ..engine import tasks as managed_tasks


PROXY_ENV_FILE_ENV = "REINS_PROXY_ENV_FILE"
PROXY_ENV_NAMES = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


def codex_bin_path(args: argparse.Namespace) -> str:
    if args.codex_bin:
        return str(Path(args.codex_bin).expanduser())
    command = shutil.which("codex")
    return command or "codex"


def claude_bin_path(args: argparse.Namespace) -> str:
    claude_bin = getattr(args, "claude_bin", None)
    if claude_bin:
        return str(Path(claude_bin).expanduser())
    found = claude_session.discover_executable(os.environ, which=shutil.which)
    return found.path if found is not None else "claude"


def dsh_bin_path(args: argparse.Namespace) -> str:
    dsh_bin = getattr(args, "dsh_bin", None)
    if dsh_bin:
        return str(Path(dsh_bin).expanduser())
    found = dsh_session.discover_executable(os.environ, which=shutil.which)
    return found.path if found is not None else "dsh"


def pi_bin_path(args: argparse.Namespace) -> str:
    pi_bin = getattr(args, "pi_bin", None)
    if pi_bin:
        return str(Path(pi_bin).expanduser())
    command = shutil.which("pi")
    return command or "pi"


def parse_proxy_environment_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"could not read proxy environment file {path}: {exc}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            raise ValueError(f"invalid environment assignment in {path}:{line_number}")
        name, value = line.split("=", 1)
        name = name.strip()
        if name not in PROXY_ENV_NAMES:
            continue
        value = value.strip()
        if value.startswith(("'", '"')):
            try:
                parsed = shlex.split(value, comments=False, posix=True)
            except ValueError as exc:
                raise ValueError(f"invalid quoted value in {path}:{line_number}") from exc
            if len(parsed) != 1:
                raise ValueError(f"invalid proxy value in {path}:{line_number}")
            value = parsed[0]
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f"unsafe proxy value in {path}:{line_number}")
        if any(char.isspace() or char in "'\\\"" for char in value):
            raise ValueError(
                f"proxy value in {path}:{line_number} contains characters unsafe for a systemd environment file; "
                "percent-encode them in the proxy URL"
            )
        values[name] = value
    return values


def write_proxy_environment_file(path: Path, values: dict[str, str]) -> None:
    if not values:
        raise ValueError("proxy environment source did not define any supported proxy variables")
    path.parent.mkdir(parents=True, exist_ok=True)
    contents = "".join(f"{name}={values[name]}\n" for name in PROXY_ENV_NAMES if name in values)
    if os.name == "nt":
        queue_store.write_private_text(path, contents)
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            os.chmod(temporary, 0o600)
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        queue_store.fsync_directory(path.parent)
    except Exception:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def configure_proxy_environment(args: argparse.Namespace) -> Path | None:
    source = getattr(args, "proxy_env_file", None)
    inherit = bool(getattr(args, "inherit_proxy", False))
    clear = bool(getattr(args, "clear_proxy", False))
    if sum(bool(option) for option in (source, inherit, clear)) > 1:
        raise ValueError("--proxy-env-file, --inherit-proxy, and --clear-proxy cannot be used together")
    target = layout.service_proxy_env_path()
    if clear:
        try:
            target.unlink()
            queue_store.fsync_directory(target.parent)
        except FileNotFoundError:
            pass
        print(f"Cleared proxy environment file at {target}; values were not printed.")
        return None
    if source:
        values = parse_proxy_environment_file(Path(source).expanduser())
    elif inherit:
        values = {name: os.environ[name] for name in PROXY_ENV_NAMES if os.environ.get(name)}
        for name, value in values.items():
            if any(char.isspace() or char in "'\\\"\x00\n\r" for char in value):
                raise ValueError(
                    f"proxy value in environment variable {name} contains characters unsafe for a systemd environment file; "
                    "percent-encode them in the proxy URL"
                )
    else:
        return target if target.exists() else None

    write_proxy_environment_file(target, values)
    print(f"Configured proxy environment file at {target}; values are not printed.")
    return target


def configured_proxy_environment() -> dict[str, str]:
    path = layout.service_proxy_env_path()
    return parse_proxy_environment_file(path) if path.exists() else {}


def load_service_proxy_environment() -> None:
    source = Path(os.environ.get(PROXY_ENV_FILE_ENV, layout.service_proxy_env_path())).expanduser()
    values = parse_proxy_environment_file(source) if source.exists() else {}
    for name in PROXY_ENV_NAMES:
        os.environ.pop(name, None)
    os.environ.update(values)


def daemon_environment(args: argparse.Namespace, *, include_proxy_values: bool = True) -> dict[str, str]:
    values = {
        "PYTHONUNBUFFERED": "1",
        "REINS_CODEX_BIN": codex_bin_path(args),
        CLAUDE_BIN_ENV: claude_bin_path(args),
        DSH_BIN_ENV: dsh_bin_path(args),
        PI_BIN_ENV: pi_bin_path(args),
        managed_tasks.SCREEN_BIN_ENV: managed_tasks.screen_binary() or "screen",
        app_server.DESKTOP_APP_SERVER_ENV: os.environ.get(app_server.DESKTOP_APP_SERVER_ENV, "1"),
        "PATH": getattr(args, "path", None) or os.environ.get("PATH", ""),
        PROXY_ENV_FILE_ENV: str(layout.service_proxy_env_path()),
        layout.REINS_HOME_ENV: str(layout.daemon_state_dir().resolve()),
    }
    if include_proxy_values:
        values.update(configured_proxy_environment())
    return values
