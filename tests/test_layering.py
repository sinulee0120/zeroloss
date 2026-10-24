"""Structure rules from CLAUDE.md: dependencies point down only (L2 -> L1 -> L0).

Core must not import controller, agent or modules; modules must not import each other.
"""

import ast
import importlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ["core", "controller", "agent", "agent.deployer", "modules", "modules.iptv"]


def _imports(py: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def test_packages_import() -> None:
    for name in PACKAGES:
        importlib.import_module(name)


def test_core_imports_nothing_above_it() -> None:
    forbidden = ("controller", "agent", "modules")
    for py in (ROOT / "core").rglob("*.py"):
        bad = {n for n in _imports(py) if n.split(".")[0] in forbidden}
        assert not bad, f"{py.relative_to(ROOT)} imports {sorted(bad)}"


def test_controller_and_agent_do_not_import_modules() -> None:
    """12-architecture 1-1: modules are reached only through module.json entries."""
    for top in ("controller", "agent"):
        for py in (ROOT / top).rglob("*.py"):
            bad = {n for n in _imports(py) if n.split(".")[0] == "modules"}
            assert not bad, f"{py.relative_to(ROOT)} imports {sorted(bad)}"


def test_modules_do_not_import_each_other() -> None:
    mods = [p for p in (ROOT / "modules").iterdir() if p.is_dir() and (p / "__init__.py").exists()]
    for mod in mods:
        others = {f"modules.{m.name}" for m in mods if m != mod}
        for py in mod.rglob("*.py"):
            bad = {n for n in _imports(py) if any(n.startswith(o) for o in others)}
            assert not bad, f"{py.relative_to(ROOT)} imports {sorted(bad)}"


def test_deployer_uses_no_core_code() -> None:
    """12-architecture 1-1: the Deployer (L0) keeps working when the node is broken, so
    it uses the standard library and its own package only."""
    import sys

    for py in (ROOT / "agent" / "deployer").rglob("*.py"):
        for name in _imports(py):
            top = name.split(".")[0]
            ours = name == "agent" or name.startswith("agent.deployer")
            assert ours or top in sys.stdlib_module_names, f"{py.relative_to(ROOT)} imports {name}"
