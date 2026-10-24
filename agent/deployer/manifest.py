"""Install manifest (docs/02-data.md 14, docs/06-install.md 1 rules 1-3).

Everything an install made is listed in order; removal walks the list backwards and
deletes only exact names. A ``dir`` goes only when empty, unless ``tree`` (a module's
own cache). Things that must survive a normal removal (settings) are in ``keep``.
After removal every listed path is checked again; what is still there is ``left``.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

TYPES = (
    "file",
    "dir",
    "service",
    "firewall_rule",
    "registry_key",
    "account",
    "sysctl_file",
    "polkit_rule",
    "udev_rule",
    "sleep_mask",
    "autostart",
    "auto_login",
    "group_member",
)  # 02 14: modules use file and dir; node manifests the rest


@dataclass
class Item:
    type: str
    path: str
    tree: bool = False
    sha256: str | None = None


@dataclass
class Manifest:
    kind: str
    id: str
    build: str
    installed_utc: str = ""
    items: list[Item] = field(default_factory=list)
    keep: list[Item] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": self.kind,
            "id": self.id,
            "build": self.build,
            "installed_utc": self.installed_utc,
            "items": [asdict(i) for i in self.items],
            "keep": [asdict(i) for i in self.keep],
            "tools": list(self.tools),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Manifest:
        def items(key: str) -> list[Item]:
            out = []
            for raw in d.get(key) or []:
                item = Item(
                    str(raw["type"]), str(raw["path"]), bool(raw.get("tree")), raw.get("sha256")
                )
                if item.type not in TYPES:
                    raise ValueError(f"unknown item type {item.type}")
                out.append(item)
            return out

        return cls(
            kind=str(d["kind"]), id=str(d["id"]), build=str(d["build"]),
            installed_utc=str(d.get("installed_utc", "")), items=items("items"),
            keep=items("keep"), tools=[str(t) for t in d.get("tools") or []],
        )  # fmt: skip

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> Manifest | None:
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class Removal:
    removed: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    left: list[str] = field(default_factory=list)  # "path: why"


def remove_items(items: list[Item], protected: list[Item], dry_run: bool = False) -> Removal:
    """Remove ``items`` backwards by exact name. A non-empty plain ``dir`` that holds only
    ``protected`` paths counts as kept; one that holds anything else is left. ``dry_run``
    changes nothing but answers as if the earlier items were already gone."""
    out = Removal()
    guard = [Path(p.path) for p in protected]
    gone: set[Path] = set()
    for item in reversed(items):
        path = Path(item.path)
        if path in gone or (not path.exists() and not path.is_symlink()):
            continue
        if item.type == "file" or item.tree:
            if not dry_run:
                if item.type == "file":
                    path.unlink()
                else:
                    shutil.rmtree(path)
            gone.add(path)
            out.removed.append(item.path)
            continue
        inside = [p for p in sorted(path.iterdir()) if p not in gone]
        foreign = [p for p in inside if not _covered(p, guard)]
        if foreign:
            names = ", ".join(p.name for p in foreign[:5])
            out.left.append(f"{item.path}: not installed by ZeroLoss: {names}")
        elif inside:
            out.kept.append(item.path)
        else:
            if not dry_run:
                path.rmdir()
            gone.add(path)
            out.removed.append(item.path)
    return out


def _covered(path: Path, guard: list[Path]) -> bool:
    """``path`` is a protected path or lies on the way to one or inside one."""
    return any(path == g or g.is_relative_to(path) or path.is_relative_to(g) for g in guard)


def still_there(items: list[Item]) -> list[str]:
    return [i.path for i in items if Path(i.path).exists()]
