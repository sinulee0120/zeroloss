"""GAP-155: a device is the PC with its device ID, whatever answers at its address.

2026-10-12 a search saved t460-rx at the sender's address 10.0.0.1; from then every
question to t460-rx was answered by the sender: the screen saw two PCs on the WAN side,
could not choose the sender, and hid the address entry (the row "answered"). The user
(2026-10-15): "let the user pick among the IPs; when there is nothing to pick, type it",
"the app doing it by itself is serious".
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from controller.devices import DeviceList
from controller.service import ControllerService
from controller.setup import sides
from core.node import Node
from core.protocol.envelope import Request, ZlError

REPO = Path(__file__).resolve().parents[2]


def _node(path: Path, name: str) -> Node:
    n = Node(path / name, "agent", host="127.0.0.1", port=0, local_port=0,
             modules_dir=REPO / "modules", lost_after_s=60)  # fmt: skip
    n.start()
    return n


@pytest.fixture
def two(tmp_path: Path) -> Iterator[tuple[Node, Node]]:
    a, b = _node(tmp_path, "a"), _node(tmp_path, "b")
    yield a, b
    a.stop()
    b.stop()


@pytest.fixture
def svc(tmp_path: Path, two: tuple[Node, Node]) -> Iterator[ControllerService]:
    s = ControllerService(tmp_path / "ctl", tmp_path / "reports", REPO / "modules",
                          host="127.0.0.1", port=0, local_port=0)  # fmt: skip
    s.start()
    s.devices.add("tx", "127.0.0.1", two[0].api.port, "b")
    s.devices.add("rx", "127.0.0.1", two[1].api.port, "b")
    yield s
    s.stop()


def _point(svc: ControllerService, name: str, port: int) -> None:
    """Saved at another PC's address, as 2026-10-12."""
    path = svc.devices.path
    doc = json.loads(path.read_text(encoding="utf-8"))
    for d in doc["devices"]:
        if d["name"] == name:
            d["port"] = port
    path.write_text(json.dumps(doc), encoding="utf-8")
    svc.devices = DeviceList(path)


def req(**params: object) -> Request:
    return Request("c1", 0, None, dict(params))


def test_a_device_whose_address_another_pc_answers_is_not_answering(
    svc: ControllerService, two: tuple[Node, Node]
) -> None:
    _point(svc, "rx", two[0].api.port)
    got = sides(svc.devices, "b")
    assert got["tx"]["reachable"] and not got["rx"]["reachable"]
    assert got["rx"]["why"] == "tx answers at 127.0.0.1, not rx" and got["rx"]["other"] == "tx"
    assert got["rx"]["host"] == "127.0.0.1" and "127.0.0.1" not in got["rx"]["addresses"]


def test_a_picked_address_is_used_only_when_that_pc_answers(
    svc: ControllerService, two: tuple[Node, Node]
) -> None:
    _point(svc, "rx", two[0].api.port)
    with pytest.raises(ZlError) as e:  # the sender's address: refused, nothing changes
        svc.devices_address(req(name="rx", host="127.0.0.1", port=two[0].api.port))
    assert e.value.code == "ZL-DSC-016" and e.value.what == "tx answers at 127.0.0.1, not rx"
    assert svc.devices.get("rx").port == two[0].api.port
    assert svc.devices.get("tx").port == two[0].api.port  # the sender's entry untouched
    r = svc.devices_address(req(name="rx", host="127.0.0.1", port=two[1].api.port))
    assert r["port"] == two[1].api.port and svc.devices.get("rx").port == two[1].api.port
    assert sides(svc.devices, "b")["rx"]["reachable"]


def test_adding_a_name_at_another_pcs_address_never_swaps_the_list(
    svc: ControllerService, two: tuple[Node, Node]
) -> None:
    with pytest.raises(ZlError) as e:
        svc.devices.add("rx", "127.0.0.1", two[0].api.port, "b")
    assert e.value.code == "ZL-DSC-016"
    names = {d.name: d.port for d in svc.devices.all()}
    assert names == {"tx": two[0].api.port, "rx": two[1].api.port}
    svc.devices.add("sender", "127.0.0.1", two[0].api.port, "b")  # the same PC renamed: fine
    names = {d.name: d.port for d in svc.devices.all()}
    assert names == {"sender": two[0].api.port, "rx": two[1].api.port}


def test_the_search_offers_addresses_and_never_moves_a_device(
    svc: ControllerService, two: tuple[Node, Node], monkeypatch: pytest.MonkeyPatch
) -> None:
    import controller.service as service_mod

    def found(*a: object, **k: object) -> dict[str, object]:
        assert k.get("save") is False
        return {"found": [{"known": True, "name": "rx", "address": "192.168.45.177"}],
                "moved": [], "kept": []}  # fmt: skip

    monkeypatch.setattr(service_mod, "discover", found)
    port = svc.devices.get("rx").port
    assert svc._relocate()
    assert svc._found == {"rx": ["192.168.45.177"]}
    assert svc.devices.get("rx").port == port  # not moved
    # 2026-10-16 (GAP-162): a PC that answers offers only the addresses it has now
    live = sides(svc.devices, "b", svc._found)["rx"]
    cards = [str(x["ip"]) for x in live["links"] or [] if x.get("ip")]
    if any(not c.startswith("127.") for c in cards):
        assert "192.168.45.177" not in live["addresses"]
    svc.devices.get("rx").host  # noqa: B018


def test_the_idle_check_counts_only_the_pc_itself(
    svc: ControllerService, two: tuple[Node, Node]
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    _point(svc, "rx", two[0].api.port)
    clients: dict[str, object] = {}
    with ThreadPoolExecutor(2) as pool:
        assert svc._idle_round(clients, pool) == {"tx": True, "rx": False}  # type: ignore[arg-type]
    for c in clients.values():
        c.close()  # type: ignore[attr-defined]


def test_the_cards_a_pc_was_seen_with_survive_a_restart(tmp_path: Path) -> None:
    """2026-10-17: after the app restarted, the ProBook (its node off) was known only at
    10.0.0.1; Reset All could not reach its Deployer at 192.168.0.27."""
    from controller.devices import Device, DeviceList

    d = DeviceList(tmp_path / "devices.json")
    d._devices["probook"] = Device("probook", "10.0.0.1", 10992, "id-1", "agent", "")
    d.note_links("probook", [{"nic": "enp37s0", "ip": "10.0.0.1"},
                             {"nic": "wlo1", "ip": "192.168.0.27", "wifi": None}])  # fmt: skip
    again = DeviceList(tmp_path / "devices.json")
    assert [c["ip"] for c in again.cards["probook"]] == ["10.0.0.1", "192.168.0.27"]
