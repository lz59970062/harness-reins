"""Install the bundled Reins skills for each harness, and the shell startup hook."""
from __future__ import annotations

import argparse
import importlib.resources as resources
import json
import os
import shlex
import shutil
import sys
import uuid
from pathlib import Path
from ..harness.pi import channel as pi_channel
from .. import template_registration
from ..store import layout
from .. import runtime as reins_runtime
from ..system import shell


SHELL_HOOK_BEGIN = "# >>> harness-reins pending status >>>"
SHELL_HOOK_END = "# <<< harness-reins pending status <<<"
SKILL_TARGETS = ("codex", "claude", "pi", "dsh", "both", "all")
CLAUDE_HOOK_COMMAND = "_claude-hook"
CLAUDE_HOOK_EVENTS = ("SessionStart", "Stop")
CLAUDE_HOOK_TIMEOUT_SECONDS = 7 * 86400  # An idle window can wait this long; its next Stop starts the hook again.


def install_skill_tree(target: Path, *, include_codex_plugin: bool, force: bool, keep_existing: bool = False,
                       focused: str | None = None) -> int:
    """Install the bundled skill.

    Codex, Claude and Pi each get a focused SKILL.md for their own callback
    delivery plus the shared full text as REFERENCE.md.
    """
    if keep_existing and (target / "SKILL.md").is_file():
        print(f"Keeping existing harness-reins skill at {target}")
        return 0
    if target.exists() and not force:
        print(
            f"Skill already exists at {target}. Re-run with --force to overwrite.",
            file=sys.stderr,
        )
        return 1

    skills = resources.files("harness_reins").joinpath("assets").joinpath("skills")
    package_root = skills.joinpath("reference")
    with template_registration.preserve_installed_index(target):
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)

        for item in package_root.iterdir():
            if item.name == "agents" and not include_codex_plugin:
                continue  # agents/openai.yaml is Codex plugin metadata; other agents ignore it.
            destination = target / item.name
            with resources.as_file(item) as source:
                if item.is_dir():
                    shutil.copytree(source, destination)
                else:
                    shutil.copy2(source, destination)
        if focused:
            os.replace(target / "SKILL.md", target / "REFERENCE.md")
            with resources.as_file(skills.joinpath(focused).joinpath("SKILL.md")) as source:
                shutil.copy2(source, target / "SKILL.md")

    print(f"Installed harness-reins skill to {target}")
    return 0


def install_skill(args: argparse.Namespace) -> int:
    if args.path:
        return install_skill_tree(
            Path(args.path).expanduser() / "harness-reins",
            include_codex_plugin=True,
            force=args.force,
            keep_existing=bool(getattr(args, "keep_existing", False)),
        )
    target_name = getattr(args, "target", None) or "both"
    keep = bool(getattr(args, "keep_existing", False))
    status = 0
    if target_name in ("codex", "both", "all"):
        status |= install_skill_tree(layout.codex_home() / "skills" / "harness-reins", include_codex_plugin=True, force=args.force,
                                     keep_existing=keep, focused="codex")
    if target_name in ("claude", "both", "all"):
        status |= install_skill_tree(layout.claude_home() / "skills" / "harness-reins", include_codex_plugin=False, force=args.force,
                                     keep_existing=keep, focused="claude")
        if getattr(args, "claude_hook", None):  # Only setup passes one; install-skill leaves settings alone.
            try:
                print(f"Installed the Reins hook in {configure_claude_hook(args.claude_hook)}")
            except (OSError, ValueError) as exc:
                print(f"reins: warning: could not add the Claude Code hook, so open windows need `reins wait`: {exc}",
                      file=sys.stderr)
    if target_name in ("pi", "all"):
        status |= install_skill_tree(pi_channel.profile_dir() / "skills" / "harness-reins", include_codex_plugin=False,
                                     force=args.force, keep_existing=keep, focused="pi")
    if target_name in ("dsh", "all"):
        status |= install_skill_tree(layout.dsh_home() / "skills" / "harness-reins", include_codex_plugin=False,
                                     force=args.force, keep_existing=keep, focused="dsh")
    return status


def shell_hook_text(command: str) -> str:
    quoted = shlex.quote(command)
    return "\n".join(
        [
            SHELL_HOOK_BEGIN,
            'if [[ -z "${CODEX_LONG_TASK_STATUS_SHOWN:-}" ]]; then',
            "    export CODEX_LONG_TASK_STATUS_SHOWN=1",
            "    if command -v timeout >/dev/null 2>&1; then",
            f"        timeout 2s {quoted} status --shell-hook --quiet-empty 2>/dev/null || true",
            "    else",
            f"        {quoted} status --shell-hook --quiet-empty 2>/dev/null || true",
            "    fi",
            "fi",
            SHELL_HOOK_END,
        ]
    )


def install_shell_hook(args: argparse.Namespace) -> int:
    rc_file = Path(args.rc_file or "~/.bashrc").expanduser()
    command = args.command or reins_runtime.console_script_path() or sys.argv[0]
    block = shell_hook_text(command)
    existing = rc_file.read_text(encoding="utf-8") if rc_file.exists() else ""

    start = existing.find(SHELL_HOOK_BEGIN)
    end = existing.find(SHELL_HOOK_END)
    if start >= 0 and end >= start:
        end += len(SHELL_HOOK_END)
        updated = existing[:start].rstrip() + "\n\n" + block + existing[end:]
    else:
        updated = existing.rstrip() + "\n\n" + block + "\n"

    rc_file.parent.mkdir(parents=True, exist_ok=True)
    mode = rc_file.stat().st_mode if rc_file.exists() else None
    tmp = rc_file.with_name(f".{rc_file.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(updated, encoding="utf-8")
    if mode is not None:
        tmp.chmod(mode)
    os.replace(tmp, rc_file)
    print(f"Installed long-task pending-status hook in {rc_file}")
    return 0


def _without_reins_hook(groups: object) -> list[object]:
    """A settings event's hook groups with Reins's entry gone and everything else untouched."""
    kept = []
    for group in groups if isinstance(groups, list) else []:
        entries = group.get("hooks") if isinstance(group, dict) else None
        if not isinstance(entries, list):
            kept.append(group)
            continue
        others = [entry for entry in entries
                  if not (isinstance(entry, dict) and CLAUDE_HOOK_COMMAND in str(entry.get("command", "")))]
        if others:
            kept.append({**group, "hooks": others})
    return kept


def configure_claude_hook(command: str | None, settings_path: Path | None = None) -> Path:
    """Add the delivery hook to Claude Code's user settings, or remove it when ``command`` is None."""
    path = settings_path or layout.claude_home() / "settings.json"
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(settings, dict) or not isinstance(settings.get("hooks", {}), dict):
        raise ValueError(f"{path} does not hold a settings object")
    hooks = dict(settings.get("hooks", {}))
    for event in CLAUDE_HOOK_EVENTS:
        groups = _without_reins_hook(hooks.get(event, []))
        if command is not None:
            groups.append({"hooks": [{"type": "command", "command": command, "async": True, "asyncRewake": True,
                                      "timeout": CLAUDE_HOOK_TIMEOUT_SECONDS}]})
        if groups:
            hooks[event] = groups
        else:
            hooks.pop(event, None)
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if path.exists():
        tmp.chmod(path.stat().st_mode)
    os.replace(tmp, path)
    return path


def claude_hook_command(executable: str | None, queue_dir: str | None) -> str:
    arguments = [CLAUDE_HOOK_COMMAND, *(["--queue-dir", str(queue_dir)] if queue_dir else [])]
    return shell.format_command([executable, *arguments]) if executable else reins_runtime.control_command(*arguments)


def install_claude_hook(args: argparse.Namespace) -> int:
    """`reins install-claude-hook [--remove]`."""
    try:
        command = None if args.remove else claude_hook_command(getattr(args, "command", None), args.queue_dir)
        path = configure_claude_hook(command)
    except (OSError, ValueError) as exc:
        print(f"reins: could not change the Claude Code hook: {exc}", file=sys.stderr)
        return 2
    print(f"{'Removed the Reins hook from' if args.remove else 'Installed the Reins hook in'} {path}; "
          "windows that are already open pick the change up after their next turn")
    return 0
