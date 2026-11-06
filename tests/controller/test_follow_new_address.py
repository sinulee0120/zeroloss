"""2026-11-05: a new router gave t460-rx a new address (192.168.45.94 -> 192.168.1.237);
the app kept asking the old one, "not answering", its tablets gone. User: "리시버 앱이
자동으로 해야하는데", and chose: save it by itself when the same PC answers on this PC's
wired test network (never the home Wi-Fi, never the sender's 10.0.0.x). Made-up IDs."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from controller.service import ControllerService
from core.protocol.envelope import ZlError


def _svc(host: str) -> tuple[ControllerService, list[tuple[str, int]]]:
    s = ControllerService.__new__(ControllerService)
    dev = SimpleNamespace(name="rx", host=host, port=10992, device_id="dev-rx")
    s.devices = SimpleNamespace(all=lambda: [dev])  # type: ignore[assignment]
    saved: list[tuple[str, int]] = []

    def set_address(d: Any, h: str, p: int) -> dict[str, Any]:
        if h == "192.168.1.9":
            raise ZlError("DSC-016", "another PC answers there")
        saved.append((h, p))
        return {}

    s._set_address = set_address  # type: ignore[method-assign,assignment]
    cards: list[dict[str, Any]] = [
        {"nic": "enp0s31f6", "ip": "192.168.1.237"},  # its cable on the test router
        {"nic": "wlx", "ip": "192.168.1.114", "wifi": {}},  # its test Wi-Fi card
        {"nic": "wlp4s0", "ip": "192.168.0.13", "wifi": {}},  # home Wi-Fi
    ]
    mine = {c["ip"] for c in cards}  # another PC answers anywhere else: no cards of rx

    def links_at(host: str, port: int, d: Any) -> list[dict[str, Any]]:
        return cards if host in mine else []

    s._links_at = links_at  # type: ignore[method-assign,assignment]
    return s, saved


def test_a_pc_answering_on_the_wired_test_network_is_followed() -> None:
    s, saved = _svc("192.168.45.94")
    row = {"device_id": "dev-rx", "port": 10992,
           "addresses": ["192.168.0.13", "192.168.1.237"]}  # fmt: skip
    s._follow(row, ["192.168.1.232"])  # this PC's wired card on the new router
    assert saved == [("192.168.1.237", 10992)]  # not its home Wi-Fi address


def test_found_at_its_test_wifi_card_it_is_kept_at_its_cable() -> None:
    """2026-11-06: t460-rx answered first on its test Wi-Fi card (192.168.1.114) and was
    saved there; that card comes and goes with the tests. Its cable is kept instead."""
    s, saved = _svc("192.168.45.94")
    s._follow({"device_id": "dev-rx", "port": 10992, "addresses": ["192.168.1.114"]},
              ["192.168.1.232"])  # fmt: skip
    assert saved == [("192.168.1.237", 10992)]


def test_not_followed_onto_wifi_the_sender_or_when_it_still_answers() -> None:
    s, saved = _svc("192.168.45.94")
    s._follow({"device_id": "dev-rx", "addresses": ["192.168.0.13"]}, ["192.168.1.232"])
    s._follow({"device_id": "dev-rx", "addresses": ["10.0.0.1"]}, ["10.0.0.100"])
    s._follow({"device_id": "dev-rx", "addresses": ["192.168.1.237"]}, [])  # cable out
    s._follow({"device_id": "other", "addresses": ["192.168.1.237"]}, ["192.168.1.232"])
    s._follow({"device_id": "dev-rx", "addresses": ["192.168.1.9"]}, ["192.168.1.232"])
    assert saved == []
    s, saved = _svc("192.168.1.237")  # still answers where it is: nothing to do
    s._follow({"device_id": "dev-rx", "addresses": ["192.168.1.237", "192.168.1.50"]},
              ["192.168.1.232"])  # fmt: skip
    assert saved == []


def test_the_saved_test_card_follows_the_new_address(tmp_path: Any) -> None:
    """2026-11-05: the choice kept t460-rx's card at 192.168.45.94 and Start stayed grey."""
    from types import MethodType

    from controller.setup import SetupStore

    s = ControllerService.__new__(ControllerService)
    dev = SimpleNamespace(name="rx", host="192.168.45.94", port=10992, device_id="dev-rx")
    s.devices = SimpleNamespace(  # type: ignore[assignment]
        all=lambda: [dev], move=lambda did, h, p: (dev, "192.168.45.94:10992")
    )
    s.setup = SetupStore(tmp_path / "setup.json", {})
    s.setup.update({"nics": {"rx": "192.168.45.94", "tx": "10.0.0.1"}})
    s.run = SimpleNamespace(boot_id="")  # type: ignore[assignment]
    s._moved = MethodType(lambda self, *a: None, s)  # type: ignore[method-assign]
    import controller.service as service_mod

    class Health:
        def __init__(self, *a: Any) -> None: ...
        def get(self, *a: Any, **k: Any) -> dict[str, Any]:
            return {"device_id": "dev-rx"}

        def close(self) -> None: ...

    monkey = pytest.MonkeyPatch()
    monkey.setattr(service_mod, "NodeClient", Health)
    try:
        s._set_address(dev, "192.168.1.237", 10992)
    finally:
        monkey.undo()
    assert s.setup.get()["nics"] == {"rx": "192.168.1.237", "tx": "10.0.0.1"}
