"""Find and load role modules from ``module.json`` (docs/12-architecture.md 1-1, 1-3, 2).

Core never names a module in code: it scans the modules folder and loads each ``entry``
string. A module that cannot be used is disabled with its reason (INS-024); Core and
the other modules keep working.
"""

from __future__ import annotations

import importlib
import json
import re
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

CORE_API = 1
REQUIRED = ("id", "name", "protocol_version", "core_api_min", "core_api_max", "entry")
_ID = re.compile(r"^[a-z][a-z0-9_]*$")


@runtime_checkable
class NodeModule(Protocol):
    """What the object returned by a module's ``entry`` must provide."""

    def attach(self, node: Any) -> None: ...

    def safe_stop(self, reason: str) -> None:
        """Stop sending, receiving, playback and companion tasks, and confirm. Must be
        safe to call in any state, more than once."""
        ...


@dataclass
class ModuleInfo:
    folder: Path
    manifest: dict[str, Any] = field(default_factory=dict)
    instance: NodeModule | None = None
    reason: str = ""  # why it is disabled; empty when enabled

    @property
    def id(self) -> str:
        return str(self.manifest.get("id", self.folder.name))

    @property
    def tools(self) -> list[str]:
        """Every tool any role of this module may need (12 2 ``tools``)."""
        return sorted({t for names in (self.manifest.get("tools") or {}).values() for t in names})

    @property
    def enabled(self) -> bool:
        return self.instance is not None and not self.reason


def discover(modules_dir: Path, core_api: int = CORE_API) -> list[ModuleInfo]:
    """Every folder with a module.json, sorted by name; unusable ones carry a reason."""
    found: list[ModuleInfo] = []
    if not modules_dir.is_dir():
        return found
    bind_package(modules_dir)
    for folder in sorted(p for p in modules_dir.iterdir() if p.is_dir()):
        path = folder / "module.json"
        if not path.exists():
            continue
        info = ModuleInfo(folder)
        info.reason = _load(info, path, core_api)
        if info.reason:
            info.instance = None
        found.append(info)
    return found


def bind_package(modules_dir: Path) -> None:
    """``modules.<id>`` imports from this folder first. Installed modules live outside
    the program, in the Deployer's module folder (docs/06-install.md 12)."""
    folder = str(modules_dir.resolve())
    pkg = sys.modules.get("modules")
    if pkg is None:
        try:
            pkg = importlib.import_module("modules")
        except ImportError:
            pkg = types.ModuleType("modules")
            pkg.__path__ = []
            sys.modules["modules"] = pkg
    paths = [str(Path(p).resolve()) for p in getattr(pkg, "__path__", [])]
    if folder not in paths:
        pkg.__path__ = [folder, *paths]


def _load(info: ModuleInfo, path: Path, core_api: int) -> str:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return f"module.json cannot be read: {e}"
    if not isinstance(manifest, dict):
        return "module.json is not an object"
    info.manifest = manifest
    missing = [k for k in REQUIRED if k not in manifest]
    if missing:
        return f"module.json is missing {', '.join(missing)}"
    if manifest["id"] != info.folder.name or not _ID.match(str(manifest["id"])):
        return f"id {manifest['id']!r} must equal the folder name and be lowercase"
    lo, hi = manifest["core_api_min"], manifest["core_api_max"]
    if not (isinstance(lo, int) and isinstance(hi, int)) or not lo <= core_api <= hi:
        return f"needs Core API {lo}..{hi}, this Core is {core_api}"
    tools = manifest.get("tools", {})
    if not isinstance(tools, dict) or not all(
        isinstance(v, list) and all(isinstance(x, str) for x in v) for v in tools.values()
    ):
        return "tools must map each role to a list of tool names"
    target, _, func = str(manifest["entry"]).partition(":")
    if not target or not func:
        return f"entry {manifest['entry']!r} must be 'package.module:function'"
    try:
        instance = getattr(importlib.import_module(target), func)()
    except Exception as e:  # a broken module must not stop the node
        return f"entry {manifest['entry']} failed: {e!r}"
    if not isinstance(instance, NodeModule):
        return f"entry {manifest['entry']} does not return attach() and safe_stop()"
    info.instance = instance
    return ""


def load_run_entry(modules_dir: Path, module_id: str, core_api: int = CORE_API) -> Any:
    """The Controller-side rules of one module (``run_entry`` in module.json). Raises
    ZlError INS-024 when the module is missing, disabled or has no run_entry."""
    from core.protocol.envelope import ZlError

    info = next((m for m in discover(modules_dir, core_api) if m.id == module_id), None)
    if info is None or not info.enabled:
        why = info.reason if info else "not installed"
        raise ZlError("INS-024", f"Module {module_id} cannot be used", why)
    target, _, func = str(info.manifest.get("run_entry", "")).partition(":")
    if not target or not func:
        raise ZlError("INS-024", f"Module {module_id} has no run_entry")
    try:
        return getattr(importlib.import_module(target), func)()
    except Exception as e:
        raise ZlError("INS-024", f"Module {module_id} run_entry failed", repr(e)) from e


def load_desktop_entries(
    modules_dir: Path, core_api: int = CORE_API
) -> tuple[dict[str, Any], dict[str, str]]:
    """Agent Desktop side of every enabled module that has one (``desktop_entry``, 03 F20),
    and the modules whose entry failed with why (shown, never hidden)."""
    out: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for info in discover(modules_dir, core_api):
        entry = str(info.manifest.get("desktop_entry", ""))
        if not info.enabled or not entry:
            continue
        target, _, func = entry.partition(":")
        try:
            out[info.id] = getattr(importlib.import_module(target), func)()
        except Exception as e:  # one broken module must not take the window down
            errors[info.id] = repr(e)
    return out, errors
