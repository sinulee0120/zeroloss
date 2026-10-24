"""Default values (docs/02-data.md 11, docs/16-settings.md): the only source.

Core's settings live in ``core/settings/defaults.json``, each module's in
``modules/<id>/defaults.json`` (a module's settings go away with the module, 12 1-3).
Each entry keeps the settings table's default text in ``doc``; a test compares it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


def load_defaults(path: Path) -> dict[str, Any]:
    """``{"S-15": 16, ...}`` from one defaults file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"{path}: schema_version {data.get('schema_version')}")
    return {sid: entry["value"] for sid, entry in data["settings"].items()}


CORE_DEFAULTS = load_defaults(Path(__file__).with_name("defaults.json"))


def load_schema(modules_dir: Path | None = None) -> dict[str, dict[str, Any]]:
    """Every setting with its default and screen data (``ui``): Core's and each module's
    (16 1, F14). Keyed by S-ID, sorted."""
    files = [Path(__file__).with_name("defaults.json")]
    if modules_dir is not None and modules_dir.is_dir():
        files += sorted(modules_dir.glob("*/defaults.json"))
    out: dict[str, dict[str, Any]] = {}
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        owner = None if f.parent == Path(__file__).parent else f.parent.name
        for sid, entry in data["settings"].items():
            out[sid] = {"value": entry["value"], **(entry.get("ui") or {})}
            if owner is not None:  # a module's own setting: shown for that test only
                out[sid]["module"] = owner
    return dict(sorted(out.items()))
