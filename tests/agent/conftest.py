"""Shared helpers for Deployer tests: packages from real module folders, a fake node."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from agent.deployer.modules import Agent, Layout, ModuleStore
from agent.deployer.package import build_package

REPO = Path(__file__).resolve().parents[2]


class FakeAgent(Agent):
    def __init__(self) -> None:
        self.running_test = False
        self.restarts = 0
        self.comes_back = True

    def busy(self) -> bool:
        return self.running_test

    def restart(self) -> bool:
        self.restarts += 1
        return self.comes_back


def module_package(src: Path, out_dir: Path, build: str = "20260925.1200", **info: Any) -> Path:
    meta = json.loads((src / "module.json").read_text(encoding="utf-8"))
    body = {
        "schema_version": 1, "kind": "module", "id": meta["id"], "build": build, "os": "any",
        "core_api_min": meta["core_api_min"], "core_api_max": meta["core_api_max"], **info,
    }  # fmt: skip
    return build_package(src, out_dir / f"zeroloss-module-{meta['id']}-{build}.zlm", body)


def demo_module(root: Path, mid: str = "demo", text: str = "one") -> Path:
    """A tiny second module (10 4-1: removing A must not touch B)."""
    folder = root / mid
    (folder / "sub").mkdir(parents=True, exist_ok=True)
    (folder / "module.json").write_text(json.dumps({
        "id": mid, "name": mid, "protocol_version": 1, "core_api_min": 1, "core_api_max": 1,
        "entry": f"modules.{mid}.module:create"}), encoding="utf-8")  # fmt: skip
    (folder / "__init__.py").write_text("")
    (folder / "module.py").write_text(f"TEXT = {text!r}\n")
    (folder / "sub" / "data.txt").write_text(text)
    return folder


def snapshot(*roots: Path) -> dict[str, str]:
    """Every file and folder under the roots with its content (the "before" state)."""
    out: dict[str, str] = {}
    for root in roots:
        for dirpath, dirs, files in os.walk(root):
            for d in dirs:
                out[str(Path(dirpath, d))] = "<dir>"
            for f in files:
                p = Path(dirpath, f)
                out[str(p)] = p.read_bytes().hex()[:64]
    return out


@pytest.fixture
def store(tmp_path: Path) -> tuple[ModuleStore, FakeAgent, Layout]:
    layout = Layout(tmp_path / "data", tmp_path / "program" / "modules")
    # made by the node install, not by a module (Core and Deployer own them, 06 12)
    layout.modules_dir.mkdir(parents=True)
    (layout.data_dir / "modules").mkdir(parents=True)
    for folder in (layout.incoming, layout.rollback, layout.deployer / "manifests"):
        folder.mkdir(parents=True)
    agent = FakeAgent()
    return ModuleStore(layout, agent), agent, layout
