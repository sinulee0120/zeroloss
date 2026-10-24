"""GAP-153: a node the plain route does not reach is asked from this PC's other address.

2026-10-10 the Windows Controller had two default routes (home Wi-Fi and the test
router's LAN); the sender on the router WAN side (10.0.0.1) was asked through the
Wi-Fi and the Controller stayed in FAULT. Bound to the test NIC's address it answered.
"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator

import pytest

from core.protocol import source_pick
from core.protocol.client import NodeClient
from core.protocol.envelope import ZlError

REAL_LOCAL_ADDRESSES = source_pick.local_addresses  # conftest stubs it during each test


class _Ok(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = json.dumps({"ok": True, "data": {"from": self.client_address[0]}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[int]:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Ok)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _only_bound(monkeypatch: pytest.MonkeyPatch, opened: list[str | None]) -> None:
    """The plain route (no source address) never connects, as through the Wi-Fi."""
    real = NodeClient._open

    def fake(self: NodeClient, source: str | None, timeout_s: float):  # type: ignore[no-untyped-def]
        opened.append(source)
        if source is None:
            raise TimeoutError("timed out")
        return real(self, source, timeout_s)

    monkeypatch.setattr(NodeClient, "_open", fake)


def test_find_keeps_the_address_that_connects() -> None:
    got = source_pick.find("10.0.0.1", 10992, addresses=lambda: ["192.168.0.10", "192.168.45.58"],
                           connects=lambda a, h, p: a == "192.168.45.58")  # fmt: skip
    assert got == "192.168.45.58"
    assert source_pick.source_for("10.0.0.1", 10992) == "192.168.45.58"


def test_find_searches_at_most_every_30_s() -> None:
    calls: list[str] = []
    t = [100.0]

    def connects(a: str, h: str, p: int) -> bool:
        calls.append(a)
        return False

    two = ["a", "b"]
    assert (
        source_pick.find("h", 1, addresses=lambda: two, connects=connects, now=lambda: t[0]) is None
    )
    assert (
        source_pick.find("h", 1, addresses=lambda: two, connects=connects, now=lambda: t[0]) is None
    )
    assert len(calls) == 2  # the second call did not search
    t[0] += source_pick.RETRY_S
    source_pick.find("h", 1, addresses=lambda: two, connects=connects, now=lambda: t[0])
    assert len(calls) == 4


def test_one_address_is_nothing_to_choose() -> None:
    assert source_pick.find("h", 1, addresses=lambda: ["a"], connects=lambda *x: True) is None


def test_off_windows_nothing_is_searched() -> None:
    assert REAL_LOCAL_ADDRESSES(platform="linux") == []  # Linux sends by route (wan_route)


def test_the_client_answers_from_the_address_that_reaches_the_node(
    server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str | None] = []
    _only_bound(monkeypatch, opened)
    monkeypatch.setattr(source_pick, "local_addresses", lambda: ["10.255.255.1", "127.0.0.1"])
    monkeypatch.setattr(source_pick, "_connects", lambda a, h, p: a == "127.0.0.1")
    c = NodeClient("127.0.0.1", server, "boot-1", "probook-sender")
    assert c.get("/v1/health")["from"] == "127.0.0.1"
    assert opened == [None, "127.0.0.1"]
    c2 = NodeClient("127.0.0.1", server, "boot-1", "probook-sender")
    c2.get("/v1/health")
    assert opened[2:] == ["127.0.0.1"]  # kept: straight from that address


def test_a_kept_address_that_fails_is_forgotten(
    server: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(source_pick, "_chosen", {("127.0.0.1", server): "10.255.255.1"})
    monkeypatch.setattr(source_pick, "local_addresses", lambda: [])
    real = NodeClient._open

    def fake(self: NodeClient, source: str | None, timeout_s: float):  # type: ignore[no-untyped-def]
        if source is not None:
            raise TimeoutError("timed out")
        return real(self, source, timeout_s)

    monkeypatch.setattr(NodeClient, "_open", fake)
    c = NodeClient("127.0.0.1", server, "boot-1")
    with pytest.raises(ZlError) as e:
        c.get("/v1/health")
    assert e.value.code == "ZL-DSC-014"
    assert source_pick.source_for("127.0.0.1", server) is None
    assert c.get("/v1/health")["from"] == "127.0.0.1"  # the plain route again
