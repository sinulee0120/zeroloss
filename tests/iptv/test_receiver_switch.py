"""PC receiver channel change (03 F19, 20 GAP-90) over local multicast (TTL 0)."""

from __future__ import annotations

import itertools
import json
import socket
import time
from pathlib import Path
from typing import Any

import pytest

from core.blocks.udp_receiver import clock_ns
from core.events.bus import Event, EventBus
from core.lifecycle.state_flow import State
from core.protocol.envelope import ZlError
from modules.iptv import record_keys as k
from modules.iptv.receiver.session import ReceiverConfig, ReceiverSession
from modules.iptv.rtp_synth import NS_PER_S
from tests.blocks.test_leave_watch import frame
from tests.blocks.test_udp_receiver import multicast_works
from tests.iptv.test_receiver_session import Sender, wait_state

pytestmark = pytest.mark.skipif(not multicast_works(), reason="no multicast route")
G1, G2 = "239.255.78.11", "239.255.78.12"
_ports = itertools.count(46300)


class GroupSender(Sender):
    def __init__(self, group: str, port: int, seconds: float) -> None:
        super().__init__(port, seconds)
        self.group = group

    def _run(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 0)
        start = time.monotonic()
        for _, data, t in self.items:
            if self.stop.is_set():
                break
            delay = start + t / NS_PER_S - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            s.sendto(data, (self.group, self.port))
        s.close()


class OldGroupSource:
    """A raw view of a router that keeps sending the old group for ``keep_s`` seconds."""

    def __init__(self, port: int, keep_s: float) -> None:
        self.port = port
        self.until = time.monotonic() + keep_s

    def read(self, timeout_s: float) -> tuple[bytes, int] | None:
        time.sleep(min(timeout_s, 0.1))
        if time.monotonic() < self.until:
            return frame(G1, self.port), clock_ns()
        return None

    def close(self) -> None:
        pass


def make(tmp_path: Path, bus: EventBus, port: int, **kw: Any) -> ReceiverSession:
    cfg = ReceiverConfig("R1", "pc", 1, G1, port, cell_s=5)
    return ReceiverSession(cfg, tmp_path, bus, **kw)


def rows(tmp_path: Path, name: str = "R1_h000.jsonl") -> list[dict[str, Any]]:
    path = tmp_path / "raw" / "pc" / name
    return [json.loads(x) for x in path.read_text().splitlines()]


def wait_for(pred: Any, timeout_s: float) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_switch_keeps_real_time_clock_and_writes_the_zap(tmp_path: Path) -> None:
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe(events.append)
    p1, p2 = next(_ports), next(_ports)
    s = make(tmp_path, bus, p1)
    with GroupSender(G1, p1, 12), GroupSender(G2, p2, 30):
        s.start()
        assert wait_state(s, State.RUNNING)
        time.sleep(3)
        assert s.switch(2, G2, p2, join_method="A")["changed"]
        assert s.cfg.ch == 2 and s.slot == 1
        assert wait_for(lambda: (tmp_path / "raw/pc/R1_zaps.jsonl").exists(), 15)
        time.sleep(3)
        s.stop()
    assert s.flow.state is State.IDLE
    secs = [r for r in rows(tmp_path) if r["type"] == "s"]
    es = [r["e"] for r in secs]
    assert es == sorted(set(es))  # strictly increasing
    old = [r for r in secs if r["ch"] == 1]
    new = [r for r in secs if r["ch"] == 2]
    assert old and new
    assert new[0]["e"] - old[-1]["e"] >= 3  # warm-up of the switch is a gap, not a shift
    zaps = [json.loads(x) for x in (tmp_path / "raw/pc/R1_zaps.jsonl").read_text().splitlines()]
    assert len(zaps) == 1
    z = zaps[0]
    assert (z["from"], z["to"], z["join_method"], z["mode"]) == (1, 2, "A", "manual")
    assert z["join_failed"] is False and z["success"] is True
    assert z["basis"] == "first_packet" and z["leave_check"] == "not_checked"
    assert z["t_cmd"] <= z["t_leave"] <= z["t_join"] <= z["t_first_packet"]
    assert any(r.get(k.ZAP_COUNT) == 1 and r.get(k.ZAP_OK) == 1 for r in new)
    pieces = s.take_summaries()
    assert {x["ch"] for x in pieces} == {1, 2}
    assert max(x["join_segment"] for x in pieces) >= 1
    assert sum(x["loss"]["lost"] for x in pieces) == 0
    codes = [e.code for e in events]
    assert "ZL-RCV-018" not in codes and "ZL-NET-003" not in codes


def test_switch_to_a_silent_group_is_a_join_failure_and_records_go_on(tmp_path: Path) -> None:
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe(events.append)
    p1, p2 = next(_ports), next(_ports)
    s = make(tmp_path, bus, p1)
    with GroupSender(G1, p1, 12):
        s.start()
        assert wait_state(s, State.RUNNING)
        time.sleep(2)
        s.switch(2, G2, p2)
        assert wait_for(lambda: any(e.code == "ZL-NET-003" for e in events), 10)
        time.sleep(3)
        s.stop()
    secs = [r for r in rows(tmp_path) if r["type"] == "s" and r["ch"] == 2]
    assert secs and all(r["rx"] == 0 for r in secs)  # the cell turns red from these
    zaps = [json.loads(x) for x in (tmp_path / "raw/pc/R1_zaps.jsonl").read_text().splitlines()]
    assert zaps[-1]["join_failed"] is True and zaps[-1]["success"] is False
    assert any(r.get(k.ZAP_JOIN_FAILED) == 1 for r in secs)
    assert s.flow.state is State.IDLE


def test_refused_switches(tmp_path: Path) -> None:
    bus = EventBus()
    p1, p2 = next(_ports), next(_ports)
    s = make(tmp_path, bus, p1)
    with pytest.raises(ZlError) as e:
        s.switch(2, G2, p2)  # IDLE
    assert e.value.code == "ZL-RUN-012"
    with GroupSender(G1, p1, 8):
        s.start()
        with pytest.raises(ZlError):
            s.switch(2, G2, p2)  # still warming up
        assert wait_state(s, State.RUNNING)
        assert s.switch(1, G1, p1) == {"ch": 1, "changed": False}  # same channel: nothing
        s.stop()


def test_stop_during_a_switch_closes_the_zap_and_files(tmp_path: Path) -> None:
    bus = EventBus()
    p1, p2 = next(_ports), next(_ports)
    s = make(tmp_path, bus, p1)
    with GroupSender(G1, p1, 12), GroupSender(G2, p2, 30):
        s.start()
        assert wait_state(s, State.RUNNING)
        time.sleep(2)
        s.switch(2, G2, p2)
        s.stop()  # the zap is still waiting for its basis time
    assert s.flow.state is State.IDLE
    zaps = [json.loads(x) for x in (tmp_path / "raw/pc/R1_zaps.jsonl").read_text().splitlines()]
    assert zaps[-1]["end"] in ("done", "stopped")
    assert rows(tmp_path)[-1]["end_reason"] == "stop"


def test_second_switch_supersedes_the_open_zap(tmp_path: Path) -> None:
    bus = EventBus()
    p1, p2 = next(_ports), next(_ports)
    s = make(tmp_path, bus, p1)
    with GroupSender(G1, p1, 12), GroupSender(G2, p2, 30):
        s.start()
        assert wait_state(s, State.RUNNING)
        time.sleep(2)
        s.switch(2, G2, p2)
        s.switch(1, G1, p1)  # at once, before the first one ended
        time.sleep(1)
        s.stop()
    zaps = [json.loads(x) for x in (tmp_path / "raw/pc/R1_zaps.jsonl").read_text().splitlines()]
    assert [(z["from"], z["to"]) for z in zaps][:2] == [(1, 2), (2, 1)]
    assert zaps[0]["end"] == "superseded"
    es = [r["e"] for r in rows(tmp_path) if r["type"] == "s"]
    assert es == sorted(set(es))


@pytest.mark.parametrize(("keep_s", "state"), [(3.6, "not_processed"), (1.0, "ok")])
def test_leave_check_marks_a_router_that_keeps_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, keep_s: float, state: str
) -> None:
    monkeypatch.setattr("modules.iptv.receiver.session.nic_of", lambda ip: "eth0")
    bus = EventBus()
    events: list[Event] = []
    bus.subscribe(events.append)
    p1, p2 = next(_ports), next(_ports)
    s = make(tmp_path, bus, p1, leave_opener=lambda nic: OldGroupSource(p1, keep_s))
    with GroupSender(G1, p1, 12), GroupSender(G2, p2, 30):
        s.start()
        assert wait_state(s, State.RUNNING)
        time.sleep(2)
        s.switch(2, G2, p2)
        zaps = tmp_path / "raw/pc/R1_zaps.jsonl"
        assert wait_for(zaps.exists, 20)
        time.sleep(2)
        s.stop()
    z = json.loads(zaps.read_text().splitlines()[0])
    assert z["leave_check"] == state
    assert (z["t_last_old_packet"] is not None) is True
    has_event = any(e.code == "ZL-NET-005" for e in events)
    assert has_event is (state == "not_processed")
    new = [r for r in rows(tmp_path) if r["type"] == "s" and r["ch"] == 2]
    assert any(r.get(k.ZAP_LEAVE_NOT_PROCESSED) == 1 for r in new) is (state == "not_processed")


def test_held_group_is_not_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("modules.iptv.receiver.session.nic_of", lambda ip: "eth0")
    bus = EventBus()
    p1, p2 = next(_ports), next(_ports)
    s = make(
        tmp_path, bus, p1,
        held_elsewhere=lambda group, port: True,
        leave_opener=lambda nic: OldGroupSource(p1, 9.0),
    )  # fmt: skip
    with GroupSender(G1, p1, 12), GroupSender(G2, p2, 30):
        s.start()
        assert wait_state(s, State.RUNNING)
        time.sleep(2)
        s.switch(2, G2, p2)
        zaps = tmp_path / "raw/pc/R1_zaps.jsonl"
        assert wait_for(zaps.exists, 20)
        s.stop()
    assert json.loads(zaps.read_text().splitlines()[0])["leave_check"] == "not_checked"


def test_wifi_keys_follow_the_receiving_card(tmp_path: Path) -> None:
    """02 7 (GAP-104 3): the node's reading of the card the receiver joined on, every
    second; a channel or AP move is marked once; a wired card writes nothing."""
    readings = iter([
        {"rssi_dbm": -29.0, "rx_mbps": 867.0, "channel": 44, "width_mhz": 80, "band": "5GHz",
         "bssid": "a"},
        {"rssi_dbm": -31.0, "rx_mbps": 867.0, "channel": 44, "width_mhz": 80, "band": "5GHz",
         "bssid": "a"},
        {"rssi_dbm": -35.0, "rx_mbps": 650.0, "channel": 40, "width_mhz": 80, "band": "5GHz",
         "bssid": "b"},
    ])  # fmt: skip
    asked: list[str] = []

    def wifi_of(ip: str) -> dict[str, Any] | None:
        asked.append(ip)
        return next(readings, None)

    cfg = ReceiverConfig("R1", "pc", 1, G1, 46999, interface_ip="192.168.45.190", cell_s=5)
    s = ReceiverSession(cfg, tmp_path, EventBus(), wifi_of=wifi_of)
    first, second, third = s._wifi_keys(), s._wifi_keys(), s._wifi_keys()
    assert asked == ["192.168.45.190"] * 3
    assert first == {k.RSSI_DBM: -29.0, k.WIFI_MBPS: 867.0, k.WIFI_CH: 44, k.WIFI_BW: 80,
                     k.WIFI_BAND: "5GHz"}  # fmt: skip
    assert k.WIFI_CH_CHANGED not in second and k.WIFI_BSSID_CHANGED not in second
    assert third[k.WIFI_CH_CHANGED] == 1 and third[k.WIFI_BSSID_CHANGED] == 1
    assert s._wifi_keys() == {}  # wired (or no reading): no Wi-Fi keys at all
