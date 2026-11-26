"""Update PCs (2026-11-26 review): a PC whose update failed never comes back on this
build, so its tablets are not waited for (2 minutes each, the screen gives up at 30)."""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from controller.service import ControllerService
from core.protocol.envelope import ZlError
from tests.agent.test_deployer_e2e import free_port


def test_a_failed_pc_is_not_waited_for(tmp_path: Path) -> None:
    build = "20261126.0001"
    pk = tmp_path / "data" / "packages"
    pk.mkdir(parents=True)
    for name in (f"zeroloss-node-agent-{build}.zlp", f"zeroloss-module-iptv-{build}.zlm"):
        (pk / name).write_bytes(b"x")
    port = free_port()  # nothing answers: the health is unknown, the Deployer is asked
    devs = [SimpleNamespace(name=n, host="127.0.0.1", port=port) for n in ("good", "bad")]

    def install(name: str, part: Path) -> dict[str, Any]:
        if name == "bad":
            raise ZlError("INS-014", "The Deployer on bad does not answer")
        return {"result": "installed"}

    waited: list[str] = []
    said: list[Any] = []

    def tablet_apps(dev: Any, b: str) -> list[dict[str, Any]]:
        waited.append(dev.name)
        return []

    svc = SimpleNamespace(
        data_dir=tmp_path / "data", devices=SimpleNamespace(all=lambda: devs),
        run=SimpleNamespace(build=build, modules_dir=tmp_path / "app" / "modules", boot_id="b",
                            while_idle=lambda what: contextlib.nullcontext()),
        _deploy=lambda: SimpleNamespace(install=install),
        _wants_tablet_app=lambda dev, b: False,
        _tablet_apps=tablet_apps,
        node=SimpleNamespace(bus=SimpleNamespace(publish=said.append)),
    )  # fmt: skip
    r = ControllerService.update_pcs(svc, None)  # type: ignore[arg-type]
    rows = {x["device"]: x for x in r["devices"]}
    assert rows["good"]["updated"] and not rows["bad"]["updated"]
    assert waited == ["good"]
