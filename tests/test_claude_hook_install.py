from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from harness_reins.install import skills as skill_install

COMMAND = "/opt/reins _claude-hook"


class ClaudeHookInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.settings = self.home / "settings.json"
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def read(self) -> dict:
        return json.loads(self.settings.read_text(encoding="utf-8"))

    def test_it_keeps_what_the_user_has_and_installs_once(self) -> None:
        theirs = {"hooks": [{"type": "command", "command": "their-tool"}]}
        self.settings.write_text(json.dumps({"model": "x", "hooks": {"Stop": [theirs], "PreToolUse": [theirs]}}))
        skill_install.configure_claude_hook(COMMAND)
        skill_install.configure_claude_hook("/moved/reins _claude-hook")
        settings = self.read()
        self.assertEqual(settings["model"], "x")
        self.assertEqual(settings["hooks"]["PreToolUse"], [theirs])
        self.assertEqual(settings["hooks"]["Stop"][0], theirs)
        for event in skill_install.CLAUDE_HOOK_EVENTS:
            entry = settings["hooks"][event][-1]["hooks"][0]
            self.assertEqual(entry["command"], "/moved/reins _claude-hook")
            self.assertTrue(entry["asyncRewake"])
            self.assertEqual(sum("_claude-hook" in json.dumps(group) for group in settings["hooks"][event]), 1)

    def test_removing_it_leaves_only_what_the_user_had(self) -> None:
        theirs = {"hooks": [{"type": "command", "command": "their-tool"}]}
        original = {"model": "x", "hooks": {"Stop": [theirs]}}
        self.settings.write_text(json.dumps(original))
        skill_install.configure_claude_hook(COMMAND)
        skill_install.configure_claude_hook(None)
        self.assertEqual(self.read(), original)

    def test_settings_it_cannot_read_are_left_alone(self) -> None:
        self.settings.write_text("{not json")
        with self.assertRaises(ValueError):
            skill_install.configure_claude_hook(COMMAND)
        self.assertEqual(self.settings.read_text(), "{not json")

    def test_only_setup_changes_settings(self) -> None:
        arguments = dict(path=None, force=True, keep_existing=False, target="claude")
        with contextlib.redirect_stdout(io.StringIO()):
            skill_install.install_skill(argparse.Namespace(**arguments))
            self.assertFalse(self.settings.exists())
            skill_install.install_skill(argparse.Namespace(**arguments, claude_hook=COMMAND))
        self.assertIn(COMMAND, self.settings.read_text())


if __name__ == "__main__":
    unittest.main()
