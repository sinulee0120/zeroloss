"""The tablets' app and adb reach every Linux PC (2026-10-24 user: new PCs too): a fresh
SSH install takes the newest tablet package, Update PCs sends it to a PC already on this
build when its tablet app differs, never to a Windows PC."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from controller import service as svc
from controller.ssh_install import newest_tablet


def test_the_newest_tablet_package_goes_with_an_ssh_install(tmp_path: Path) -> None:
    for b in ("20261024.1120", "20261024.1323"):
        (tmp_path / f"zeroloss-tablet-{b}.zlt").write_bytes(b"x")
    assert [p.name for p in newest_tablet(tmp_path)] == ["zeroloss-tablet-20261024.1323.zlt"]
    assert newest_tablet(tmp_path / "none") == []


@pytest.mark.parametrize(("info", "wants"), [
    ({"os": "Linux-6.8", "tablet_app": None}, True),  # a fresh SSH install
    ({"os": "Linux-6.8", "tablet_app": "20261024.1212"}, True),  # an older app
    ({"os": "Linux-6.8", "tablet_app": "20261024.1323"}, False),  # has it
    ({"os": "Windows-11", "tablet_app": None}, False),  # drives no tablets
])  # fmt: skip
def test_who_gets_the_tablet_package(monkeypatch: pytest.MonkeyPatch, info: dict[str, Any],
                                     wants: bool) -> None:  # fmt: skip
    class Client:
        def __init__(self, *a: Any) -> None:
            pass

        def get(self, path: str, timeout_s: float = 0) -> dict[str, Any]:
            return info

        def close(self) -> None:
            pass

    monkeypatch.setattr(svc, "NodeClient", Client)
    me = SimpleNamespace(run=SimpleNamespace(boot_id="b"))
    dev = SimpleNamespace(host="h", port=1, name="pc")
    assert svc.ControllerService._wants_tablet_app(me, dev, "20261024.1323") is wants  # type: ignore[arg-type]
