"""Install the bundled Reins skills for each harness, and the shell startup hook."""
from __future__ import annotations

import argparse
import importlib.resources as resources
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


SHELL_HOOK_BEGIN = "# >>> harness-reins pending status >>>"
SHELL_HOOK_END = "# <<< harness-reins pending status <<<"
SKILL_TARGETS = ("codex", "claude", "pi", "dsh", "both", "all")


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
