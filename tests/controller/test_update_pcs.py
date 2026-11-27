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
        _node_os=lambda dev, d: "linux",
        _tablet_apps=tablet_apps,
        node=SimpleNamespace(bus=SimpleNamespace(publish=said.append)),
    )  # fmt: skip
    r = ControllerService.update_pcs(svc, None)  # type: ignore[arg-type]
    rows = {x["device"]: x for x in r["devices"]}
    assert rows["good"]["updated"] and not rows["bad"]["updated"]
    assert waited == ["good"]


def test_a_windows_pc_gets_the_windows_setup(tmp_path: Path) -> None:
    """RUN-013 (2026-11-28 user "Run 13도 하고"): Update PCs sent every PC the .deb."""
    build = "20261128.0001"
    pk = tmp_path / "data" / "packages"
    pk.mkdir(parents=True)
    names = (f"zeroloss-node-agent-{build}.zlp", f"zeroloss-node-agent-windows-{build}.zlp",
             f"zeroloss-module-iptv-{build}.zlm")  # fmt: skip
    for name in names:
        (pk / name).write_bytes(b"x")
    port = free_port()
    devs = [SimpleNamespace(name=n, host="127.0.0.1", port=port) for n in ("ubuntu", "win")]
    sent: list[tuple[str, str]] = []

    def install(name: str, part: Path) -> dict[str, Any]:
        sent.append((name, part.name))
        return {"result": "installed"}

    deploy = SimpleNamespace(
        install=install,
        health=lambda name: {"os": "windows" if name == "win" else "linux"},
    )  # fmt: skip
    svc = SimpleNamespace(
        data_dir=tmp_path / "data", devices=SimpleNamespace(all=lambda: devs),
        run=SimpleNamespace(build=build, modules_dir=tmp_path / "app" / "modules", boot_id="b",
                            while_idle=lambda what: contextlib.nullcontext()),
        _deploy=lambda: deploy, _node_os=ControllerService._node_os,
        _wants_tablet_app=lambda dev, b: False, _tablet_apps=lambda dev, b: [],
        node=SimpleNamespace(bus=SimpleNamespace(publish=lambda e: None)),
    )  # fmt: skip
    r = ControllerService.update_pcs(svc, None)  # type: ignore[arg-type]
    assert all(x["updated"] for x in r["devices"])
    assert sent == [("ubuntu", names[0]), ("ubuntu", names[2]),
                    ("win", names[1]), ("win", names[2])]  # fmt: skip
    (pk / names[1]).unlink()  # a Controller folder without the Windows package
    sent.clear()
    again = ControllerService.update_pcs(svc, None)  # type: ignore[arg-type]
    rows = {x["device"]: x for x in again["devices"]}
    assert rows["ubuntu"]["updated"] and not rows["win"]["updated"]
    assert "no Windows PC package" in rows["win"]["why"] and all(n == "ubuntu" for n, _ in sent)
