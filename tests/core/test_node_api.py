"""Node Core API over real HTTP (docs/02-data.md 3-5, DSC-012, SYS-008, CTL-008)."""

from __future__ import annotations

import http.client
import json
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.lifecycle.state_flow import State
from core.node import Node, load_device_id
from core.protocol.envelope import BOOT_HEADER, PROTOCOL_VERSION, Request


@pytest.fixture
def node(tmp_path: Path) -> Iterator[Node]:
    n = Node(
        tmp_path, role="receiver", host="127.0.0.1", port=0, local_port=0, build="20260924.1200"
    )
    n.start()
    yield n
    n.stop()


def call(
    conn: http.client.HTTPConnection,
    method: str,
    path: str,
    body: object = None,
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
) -> tuple[int, dict[str, Any]]:
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    conn.request(method, path, body=data, headers=headers or {})
    resp = conn.getresponse()
    return resp.status, json.loads(resp.read())


def connect(node: Node) -> http.client.HTTPConnection:
    return http.client.HTTPConnection("127.0.0.1", node.api.port, timeout=5)


def test_health_and_keep_alive(node: Node) -> None:
    conn = connect(node)
    for _ in range(3):  # same connection each time (keep-alive)
        status, res = call(conn, "GET", "/v1/health")
        assert status == 200 and res["ok"]
    d = res["data"]
    assert d["build"] == "20260924.1200"
    assert d["protocol_version"] == PROTOCOL_VERSION
    assert d["state"] == "IDLE" and d["role"] == "receiver"
    uuid.UUID(d["device_id"])


def test_heartbeat_echoes_seq_and_carries_events(node: Node) -> None:
    conn = connect(node)
    node.flow.move(State.STARTING, reason="test")
    _, res = call(conn, "GET", "/v1/heartbeat?seq=41")
    hb = res["data"]
    assert hb["seq"] == 41 and hb["state"] == "STARTING"
    assert set(hb["resources"]) == {"cpu_pct", "mem_free_pct", "disk_free_mb", "proc_mem_mb"}
    texts = [e["text"] for e in hb["events"]]
    assert any("IDLE -> STARTING" in t for t in texts)
    time.sleep(0.05)  # more than one CPU clock tick
    _, res = call(conn, "GET", "/v1/heartbeat?seq=42")
    assert res["data"]["events"] == []  # only new events each time
    assert res["data"]["resources"]["cpu_pct"] is not None  # second reading has a delta


def test_heartbeat_needs_integer_seq(node: Node) -> None:
    conn = connect(node)
    for path in ("/v1/heartbeat", "/v1/heartbeat?seq=x"):
        status, res = call(conn, "GET", path)
        assert status == 400 and res["error"]["code"] == "ZL-DSC-012"


def test_info_reports_address_seen(node: Node) -> None:
    _, res = call(connect(node), "GET", "/v1/info")
    assert res["data"]["seen_from_ip"] == "127.0.0.1"


def test_unknown_paths_bad_json_and_big_body(node: Node) -> None:
    conn = connect(node)
    assert call(conn, "GET", "/v1/nope")[0] == 404
    node.api.command("/v1/echo", lambda r: r.params)
    status, res = call(conn, "POST", "/v1/echo", raw=b"{not json")
    assert status == 400 and res["error"]["code"] == "ZL-DSC-012"
    status, res = call(conn, "POST", "/v1/echo", body={"generation": 0})
    assert status == 400 and "command_id" in res["error"]["detail"]
    conn2 = connect(node)
    conn2.putrequest("POST", "/v1/echo")
    conn2.putheader("Content-Length", str(2 << 20))
    conn2.endheaders()
    resp = conn2.getresponse()
    assert resp.status == 413
    assert call(connect(node), "GET", "/v1/health")[0] == 200  # still serving


def test_commands_run_once_and_detect_controller_restart(node: Node) -> None:
    runs: list[Request] = []

    def echo(r: Request) -> dict[str, int]:
        runs.append(r)
        return {"n": len(runs)}

    node.api.command("/v1/echo", echo)
    restarts: list[int] = []
    node.on_controller_restart = lambda: restarts.append(1)
    conn = connect(node)
    body = {"command_id": "c-1", "generation": 1, "params": {"x": 1}}
    first = call(conn, "POST", "/v1/echo", body, {BOOT_HEADER: "boot-A"})[1]
    again = call(conn, "POST", "/v1/echo", body, {BOOT_HEADER: "boot-A"})[1]
    assert first == again == {"ok": True, "data": {"n": 1}}
    assert restarts == []
    call(conn, "POST", "/v1/echo", {**body, "command_id": "c-2"}, {BOOT_HEADER: "boot-B"})
    assert restarts == [1]


def test_handler_bug_is_reported_not_hidden(node: Node) -> None:
    def broken(_: Request) -> None:
        raise KeyError("oops")

    node.api.command("/v1/broken", broken)
    status, res = call(connect(node), "POST", "/v1/broken", {"command_id": "b", "generation": 0})
    assert status == 500 and res["error"]["code"] == "ZL-SYS-008"
    _, hb = call(connect(node), "GET", "/v1/heartbeat?seq=1")
    assert any(e["code"] == "ZL-SYS-008" for e in hb["data"]["events"])
    log = (node.data_dir / "logs" / "events.log").read_text()
    assert "ZL-SYS-008" in log


def test_device_id_survives_restart_boot_id_does_not(tmp_path: Path) -> None:
    a = Node(tmp_path, "sender", host="127.0.0.1", port=0, local_port=0)
    b = Node(tmp_path, "sender", host="127.0.0.1", port=0, local_port=0)
    try:
        assert a.device_id == b.device_id == load_device_id(tmp_path)
        assert a.boot_id != b.boot_id
    finally:
        a.api.stop()
        b.api.stop()
    (tmp_path / "device_id").write_text("garbage")
    assert load_device_id(tmp_path) != "garbage"


def test_stop_closes_port(tmp_path: Path) -> None:
    n = Node(tmp_path, "sender", host="127.0.0.1", port=0, local_port=0)
    n.start()
    port = n.api.port
    n.stop()
    with pytest.raises(OSError):
        http.client.HTTPConnection("127.0.0.1", port, timeout=1).request("GET", "/v1/health")


@settings(max_examples=20, deadline=None)
@given(
    reqs=st.lists(
        st.one_of(
            st.tuples(st.just("GET"), st.sampled_from(["/v1/health", "/v1/heartbeat?seq=5", "/x"])),
            st.tuples(st.just("POST"), st.sampled_from(["c1", "c2", "c3", "bad"])),
        ),
        max_size=25,
    )
)
def test_random_burst_from_many_clients(
    tmp_path_factory: pytest.TempPathFactory, reqs: list[tuple[str, str]]
) -> None:
    """Invariants: parallel clients never break the server, each command ID runs once."""
    n = Node(tmp_path_factory.mktemp("n"), "receiver", host="127.0.0.1", port=0, local_port=0)
    n.start()
    runs: list[str] = []
    lock = threading.Lock()

    def handler(r: Request) -> dict[str, str]:
        with lock:
            runs.append(r.command_id)
        return {}

    n.api.command("/v1/cmd", handler)
    errors: list[BaseException] = []

    def client(chunk: list[tuple[str, str]]) -> None:
        try:
            conn = connect(n)
            for method, arg in chunk:
                if method == "GET":
                    call(conn, "GET", arg)
                elif arg == "bad":
                    call(conn, "POST", "/v1/cmd", raw=b"[")
                else:
                    call(conn, "POST", "/v1/cmd", {"command_id": arg, "generation": 0})
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=client, args=(reqs[i::4],)) for i in range(4)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        assert errors == []
        assert sorted(runs) == sorted(set(runs))
        assert call(connect(n), "GET", "/v1/health")[0] == 200
    finally:
        n.stop()


def test_heartbeat_links_and_link_change_events(tmp_path: Path) -> None:
    """05 1.8-1.9: links every second; speed drop, Wi-Fi move and link down become events."""
    from core.blocks.link_monitor import Link, Wifi

    wifi = Wifi("02:00:00:00:00:01", "5GHz", 36, 80)
    readings = iter(
        [
            [
                Link("eth0", "10.0.0.2", True, 2500, "full", 1),
                Link("wlan0", "10.0.0.9", True, None, None, 1, True, wifi),
            ],
            [
                Link("eth0", "10.0.0.2", True, 100, "full", 1),
                Link(
                    "wlan0",
                    "10.0.0.9",
                    True,
                    None,
                    None,
                    1,
                    True,
                    Wifi("02:00:00:00:00:01", "5GHz", 149, 40),
                ),
            ],
            [
                Link("eth0", "10.0.0.2", False, None, None, 2),
                Link(
                    "wlan0",
                    "10.0.0.9",
                    True,
                    None,
                    None,
                    1,
                    True,
                    Wifi("02:00:00:00:00:01", "5GHz", 149, 40),
                ),
            ],
        ]
    )
    last: list[Link] = []

    def reader() -> list[Link]:
        nonlocal last
        last = next(readings, last)
        return last

    n = Node(tmp_path, role="agent", host="127.0.0.1", port=0, local_port=0)
    n.links_reader = reader
    n.start()
    try:
        conn = connect(n)
        codes: list[str] = []
        texts: list[str] = []
        links: list[dict[str, Any]] = []
        end = time.monotonic() + 6
        while time.monotonic() < end and "ZL-NET-015" not in codes:
            _, res = call(conn, "GET", "/v1/heartbeat?seq=1")
            links = res["data"]["links"] or links
            for e in res["data"]["events"]:
                codes.append(e["code"])
                texts.append(e["text"])
            time.sleep(0.3)
        assert any("2500 Mbps -> 100 Mbps" in t for t in texts)
        assert "ZL-NET-016" in codes and "ZL-NET-015" in codes
        by_nic = {x["nic"]: x for x in links}
        assert by_nic["wlan0"]["wifi"]["channel"] == 149 and "link_mbps" not in by_nic["wlan0"]
        assert "wifi" not in by_nic["eth0"]
    finally:
        n.stop()


def test_a_new_controller_is_seen_on_a_get_too(node: Node) -> None:
    """02 3 / 20 GAP-49: a restarted Controller usually asks with GET first (heartbeat,
    health). That must stop the old run at once, not after the 10 s watchdog. A local
    screen or CLI (10994) with its own ID must never count as a new Controller."""
    restarts: list[int] = []
    node.on_controller_restart = lambda: restarts.append(1)
    conn = http.client.HTTPConnection("127.0.0.1", node.api.port, timeout=5)
    call(conn, "GET", "/v1/health", None, {BOOT_HEADER: "boot-A"})
    call(conn, "GET", "/v1/heartbeat?seq=1", None, {BOOT_HEADER: "boot-A"})
    assert restarts == []
    local = http.client.HTTPConnection("127.0.0.1", node.local.port, timeout=5)
    call(local, "GET", "/v1/health", None, {BOOT_HEADER: "desktop-1"})
    call(local, "GET", "/v1/health", None, {BOOT_HEADER: "cli-2"})
    call(conn, "GET", "/v1/health", None, {BOOT_HEADER: "boot-A"})
    assert restarts == []  # the screen and the CLI did not look like a new Controller
    call(conn, "GET", "/v1/heartbeat?seq=2", None, {BOOT_HEADER: "boot-B"})
    assert restarts == [1]
