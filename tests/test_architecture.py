"""Layer boundaries: imports may only point down the dependency order.

The order follows the dispatch-to-acknowledge story (docs/design/architecture.md):

    cli -> commands -> install -> tower -> engine -> harness, owners, store -> core, system

harness and owners hold no state, so they must not reach store, engine or
anything above. Lazy imports inside functions count too. Existing exceptions
are listed in KNOWN_VIOLATIONS with the step that removes them; the test
fails on any new violation and on any listed one that no longer exists, so
the list can only shrink.
"""
from __future__ import annotations

import ast
from pathlib import Path
import unittest

SRC = Path(__file__).resolve().parents[1] / "src"

ALLOWED = {
    "core": set(),
    "system": set(),
    "store": {"core", "system"},
    "owners": {"core", "system"},
    "harness": {"core", "system"},
    "engine": {"core", "system", "store", "owners", "harness"},
    "tower": {"core", "system", "store", "owners", "harness", "engine"},
    "install": {"core", "system", "store", "owners", "harness", "engine", "tower"},
    "commands": {"core", "system", "store", "owners", "harness", "engine", "tower", "install"},
    "cli": {"core", "system", "store", "owners", "harness", "engine", "tower", "install", "commands"},
}

# Top-level modules that belong to a layer without living in its package.
LAYER_OF_MODULE = {
    "harness_reins.runtime": "system",
    "harness_reins.cli": "cli",
    "harness_reins.__main__": "cli",
    "harness_reins._entry": "cli",
    # Expert agent templates; they move into engine with the expert work.
    "harness_reins.templates": "engine",
    "harness_reins.template_registration": "engine",
}

# module -> imported module. Step numbers refer to docs/design/architecture.md.
KNOWN_VIOLATIONS = {
    # Step 5/6: transports stop reading and writing the queue themselves;
    # the engine hands them a callback and records the outcome.
    "harness_reins.harness.claude.channel -> harness_reins.store.layout",
    "harness_reins.harness.claude.channel -> harness_reins.store.records",
    "harness_reins.harness.claude.session -> harness_reins.engine.delivery",
    "harness_reins.harness.claude.session -> harness_reins.engine.dispatch",
    "harness_reins.harness.claude.session -> harness_reins.engine.reminders",
    "harness_reins.harness.claude.session -> harness_reins.store.layout",
    "harness_reins.harness.claude.session -> harness_reins.store.records",
    "harness_reins.harness.codex.app_server -> harness_reins.store.layout",
    "harness_reins.harness.pi.channel -> harness_reins.cli",
    "harness_reins.harness.pi.channel -> harness_reins.engine.delivery",
    "harness_reins.harness.pi.channel -> harness_reins.engine.dispatch",
    "harness_reins.harness.pi.channel -> harness_reins.install.services",
    "harness_reins.harness.pi.channel -> harness_reins.install.skills",
    "harness_reins.harness.pi.channel -> harness_reins.store.layout",
    "harness_reins.harness.pi.channel -> harness_reins.store.records",
    "harness_reins.harness.pi.channel -> harness_reins.tower.main",
    # Process identity and peer checks are OS primitives that still live in
    # the owner backends; they move to harness_reins.system.
    "harness_reins.harness.codex.app_server -> harness_reins.owners.macos",
    "harness_reins.harness.pi.channel -> harness_reins.owners.windows",
}


def layer(module: str) -> str | None:
    if module in LAYER_OF_MODULE:
        return LAYER_OF_MODULE[module]
    parts = module.split(".")
    if len(parts) < 2:
        return None  # the harness_reins package itself (its version)
    return parts[1] if parts[1] in ALLOWED else None


def modules() -> dict[str, Path]:
    found = {}
    for path in (SRC / "harness_reins").rglob("*.py"):
        if "__pycache__" in path.parts or "-" in path.stem:
            continue
        parts = [p for p in path.relative_to(SRC).with_suffix("").parts if p != "__init__"]
        found[".".join(parts)] = path
    return found


def imports(module: str, path: Path, known: set[str]) -> set[str]:
    package = module.split(".") if path.name == "__init__.py" else module.split(".")[:-1]
    targets = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                source = ".".join(base + ([node.module] if node.module else []))
            else:
                source = node.module or ""
            for alias in node.names:
                child = f"{source}.{alias.name}"
                targets.add(child if child in known else source)
        elif isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
    return {t for t in targets if t.startswith("harness_reins.")}


def violations() -> set[str]:
    found = modules()
    result = set()
    for module, path in found.items():
        source_layer = layer(module)
        if source_layer is None:
            continue
        for target in imports(module, path, set(found)):
            target_layer = layer(target)
            if target_layer is None or target_layer == source_layer:
                continue
            if target_layer not in ALLOWED[source_layer]:
                result.add(f"{module} -> {target}")
    return result


class ArchitectureTests(unittest.TestCase):
    def test_every_package_has_a_layer(self):
        unassigned = sorted(m for m in modules() if layer(m) is None and m != "harness_reins")
        self.assertEqual(unassigned, [], "assign new top-level packages a layer in ALLOWED")

    def test_imports_point_down_the_layers(self):
        current = violations()
        new = sorted(current - KNOWN_VIOLATIONS)
        fixed = sorted(KNOWN_VIOLATIONS - current)
        self.assertEqual(new, [], "new upward or sideways import; restructure instead of adding an exception")
        self.assertEqual(fixed, [], "these known violations are gone; delete them from KNOWN_VIOLATIONS")


if __name__ == "__main__":
    unittest.main()
