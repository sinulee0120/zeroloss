"""10994 local window: SSE state stream (docs/02-data.md 2, 11-ui 1.12, 12 1-0)."""

from __future__ import annotations

import http.client
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import core.node as node_mod
from core.events.bus import Event
from core.lifecycle.state_flow import State
from core.node import Node


@pytest.fixture
def node(tmp_path: Path) -> Iterator[Node]:
    n = Node(tmp_path, "controller", host="127.0.0.1", port=0, local_port=0, lost_after_s=1.0)
    n.start()
    yield n
    n.stop()


class Sse:
    def __init__(self, n: Node) -> None:
        self.conn = http.client.HTTPConnection("127.0.0.1", n.local.port, timeout=5)
        self.conn.request("GET", "/v1/stream")
        self.resp = self.conn.getresponse()
        assert self.resp.status == 200
        assert self.resp.getheader("Content-Type", "").startswith("text/event-stream")

    def next(self) -> dict[str, Any]:
        while True:
            line = self.resp.fp.readline()
            if not line:
                raise EOFError
            if line.startswith(b"data: "):
                item: dict[str, Any] = json.loads(line[6:])
                return item

    def close(self) -> None:
        self.resp.close()  # the response owns the socket once it is streaming
        self.conn.close()


def subscribers(n: Node) -> int:
    return len(n.bus._subscribers)


def wait_for(cond: Any, timeout_s: float = 3.0) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_local_window_listens_on_loopback_only(node: Node) -> None:
    assert node.local._server.server_address[0] == "127.0.0.1"


def test_snapshot_first_then_live_events(node: Node) -> None:
    s = Sse(node)
    first = s.next()
    assert first["type"] == "snapshot" and first["health"]["state"] == "IDLE"
    node.flow.move(State.STARTING)
    ev = s.next()
    assert ev["type"] == "event" and "IDLE -> STARTING" in ev["event"]["text"]
    assert ev["state"] == "STARTING"
    node.bus.publish(Event("ZL-TAB-005", "warn", "Wi-Fi weak"))
    assert s.next()["event"]["code"] == "ZL-TAB-005"
    s.close()


def test_two_screens_see_the_same_events(node: Node) -> None:
    a, b = Sse(node), Sse(node)
    a.next(), b.next()
    node.bus.publish(Event("ZL-INFO", "info", "hello"))
    assert a.next()["event"]["text"] == b.next()["event"]["text"] == "hello"
    a.close()
    b.close()


def test_closing_the_screen_unsubscribes(node: Node) -> None:
    base = subscribers(node)
    s = Sse(node)
    s.next()
    assert subscribers(node) == base + 1
    s.close()  # no event follows: the server must notice by itself
    assert wait_for(lambda: subscribers(node) == base)


def test_slow_screen_is_dropped_and_gets_fresh_state_again(
    node: Node, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(node_mod, "STREAM_QUEUE", 5)
    s = Sse(node)
    s.next()
    total = 20000  # far more than the OS socket buffers hold
    for i in range(total):  # the screen reads nothing meanwhile
        node.bus.publish(Event("ZL-INFO", "info", f"e{i} " + "x" * 500))
    got = 0
    with pytest.raises(EOFError):
        while True:
            s.next()
            got += 1
    assert got < total
    again = Sse(node)
    assert again.next()["type"] == "snapshot"
    again.close()


def test_node_stop_ends_the_stream(tmp_path: Path) -> None:
    n = Node(tmp_path, "controller", host="127.0.0.1", port=0, local_port=0)
    n.start()
    s = Sse(n)
    s.next()
    n.stop()
    with pytest.raises((EOFError, OSError)):
        for _ in range(100):
            s.next()


def test_local_requests_do_not_keep_an_orphan_run_alive(node: Node) -> None:
    node.flow.move(State.STARTING)
    node.flow.move(State.RUNNING)
    s = Sse(node)
    s.next()
    end = time.monotonic() + 3
    while node.flow.state is State.RUNNING and time.monotonic() < end:
        c = http.client.HTTPConnection("127.0.0.1", node.local.port, timeout=2)
        c.request("GET", "/v1/health")
        c.getresponse().read()
        time.sleep(0.2)
    assert node.flow.state is State.IDLE  # Controller silent 1 s -> safe stop
    s.close()
