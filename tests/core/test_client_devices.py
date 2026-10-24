"""Node client (02 3, DSC-014) and the Controller device list (02 11-0)."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

from controller.devices import DeviceList
from core.node import Node
from core.protocol.client import NodeClient
from core.protocol.envelope import ZlError


@pytest.fixture
def node(tmp_path: Path) -> Iterator[Node]:
    n = Node(tmp_path / "n", "sender", host="127.0.0.1", port=0, local_port=0)
    n.start()
    yield n
    n.stop()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def test_get_command_and_once_only_retry(node: Node) -> None:
    runs: list[int] = []
    node.api.command("/v1/echo", lambda r: runs.append(1) or {"x": r.params["x"]})  # type: ignore[func-returns-value]
    c = NodeClient("127.0.0.1", node.api.port, "boot-1")
    assert c.get("/v1/heartbeat", seq=7)["seq"] == 7
    assert c.command("/v1/echo", 0, x=5, command_id="same") == {"x": 5}
    assert c.command("/v1/echo", 0, x=9, command_id="same") == {"x": 5}  # remembered
    assert runs == [1]
    c.close()


def test_node_errors_come_back_as_zlerror(node: Node) -> None:
    c = NodeClient("127.0.0.1", node.api.port, "boot-1")
    with pytest.raises(ZlError) as e:
        c.get("/v1/nothing")
    assert e.value.code == "ZL-DSC-012"


def test_unreachable_node_is_dsc014_quickly() -> None:
    c = NodeClient("127.0.0.1", free_port(), "boot-1", "sender-pc")
    with pytest.raises(ZlError) as e:
        c.get("/v1/health", timeout_s=1)
    assert e.value.code == "ZL-DSC-014" and "sender-pc" in e.value.what


def test_reconnects_after_the_node_restarts(tmp_path: Path) -> None:
    n = Node(tmp_path / "a", "sender", host="127.0.0.1", port=0, local_port=0)
    n.start()
    port = n.api.port
    c = NodeClient("127.0.0.1", port, "boot-1")
    first = c.get("/v1/health")["boot_id"]
    n.stop()
    n2 = Node(tmp_path / "a", "sender", host="127.0.0.1", port=port, local_port=0)
    n2.start()
    try:
        assert c.get("/v1/health")["boot_id"] != first  # stale keep-alive replaced
    finally:
        n2.stop()


def test_device_list_add_resolve_replace_remove(tmp_path: Path, node: Node) -> None:
    path = tmp_path / "devices.json"
    dl = DeviceList(path)
    dev = dl.add("sender-pc", "127.0.0.1", node.api.port, "boot-1")
    assert dev.device_id == node.device_id and dev.role == "sender"
    again = DeviceList(path)  # persisted
    assert again.get("sender-pc") == dev and again.get(node.device_id) == dev
    again.add("tx", "127.0.0.1", node.api.port, "boot-1")  # same device, new name
    assert [d.name for d in again.all()] == ["tx"]
    with pytest.raises(ZlError) as e:
        again.get("sender-pc")
    assert e.value.code == "ZL-DSC-001"
    again.remove("tx")
    assert DeviceList(path).all() == []


def test_device_list_refuses_bad_names_and_dead_nodes(tmp_path: Path) -> None:
    dl = DeviceList(tmp_path / "d.json")
    with pytest.raises(ZlError):
        dl.add("bad name!", "127.0.0.1", 1, "b")
    with pytest.raises(ZlError) as e:
        dl.add("pc", "127.0.0.1", free_port(), "b")
    assert e.value.code == "ZL-DSC-014"
    assert not (tmp_path / "d.json").exists()


def test_a_connection_windows_aborted_is_retried_once(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GAP-150: the screen's Stop failed twice with WinError 10053 on a stale connection;
    the Linux forms were retried, this Windows one was not."""
    import http.client

    c = NodeClient("127.0.0.1", node.api.port, "boot-1")
    c.get("/v1/health")  # a kept-alive connection now exists
    real = http.client.HTTPConnection.request
    calls = []

    def aborted_once(self: http.client.HTTPConnection, *a: object, **k: object) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionAbortedError(10053, "aborted by the software in your host")
        real(self, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(http.client.HTTPConnection, "request", aborted_once)
    assert c.get("/v1/health")["boot_id"]  # answered on a fresh connection
    assert len(calls) == 2
