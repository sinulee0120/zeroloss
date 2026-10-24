"""IPTV receiver session over local multicast (TTL 0): start, warm-up, record, stop."""

from __future__ import annotations

import itertools
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.events.bus import Event, EventBus
from core.lifecycle.state_flow import State
from core.protocol.envelope import ZlError
from modules.iptv.receiver.session import ReceiverConfig, ReceiverSession
from modules.iptv.rtp_synth import NS_PER_S, datagrams
from tests.blocks.test_udp_receiver import multicast_works

pytestmark = pytest.mark.skipif(not multicast_works(), reason="no multicast route")
GROUP = "239.255.78.1"
_ports = itertools.count(46100)


class Sender:
    """Plays synthetic datagrams in real time on a thread; ``skip`` drops indexes."""

    def __init__(self, port: int, seconds: float, skip: set[int] | None = None) -> None:
        self.items = datagrams(seconds)
        self.port = port
        self.skip = skip or set()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.stop = threading.Event()

    def _run(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 0)
        start = time.monotonic()
        for i, data, t in self.items:
            if self.stop.is_set():
                break
            delay = start + t / NS_PER_S - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            if i not in self.skip:
                s.sendto(data, (GROUP, self.port))
        s.close()

    def __enter__(self) -> Sender:
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop.set()
        self.thread.join()


@pytest.fixture
def bus() -> Iterator[tuple[EventBus, list[Event]]]:
    b = EventBus()
    got: list[Event] = []
    b.subscribe(got.append)
    yield b, got


def session(tmp_path: Path, bus: EventBus, port: int, **kw: Any) -> ReceiverSession:
    cfg = ReceiverConfig("R1", "pc", 1, GROUP, port, cell_s=kw.pop("cell_s", 5), **kw)
    return ReceiverSession(cfg, tmp_path, bus)


def wait_state(s: ReceiverSession, st: State, timeout_s: float = 10) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if s.flow.state is st:
            return True
        time.sleep(0.05)
    return False


def records(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "raw" / "pc" / "R1_h000.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()]


def test_full_cycle_counts_injected_loss(tmp_path: Path, bus: Any) -> None:
    b, events = bus
    port = next(_ports)
    s = session(tmp_path, b, port)
    lost = {3000, 3001, 3002, 3500}  # after warm-up (seconds 6 and 7)
    with Sender(port, 11, skip=lost):
        s.start()
        assert wait_state(s, State.RUNNING)
        time.sleep(9)
        s.stop()
    assert s.flow.state is State.IDLE
    rows = records(tmp_path)
    assert rows[0]["type"] == "header" and rows[-1]["end_reason"] == "stop"
    secs = [r for r in rows if r["type"] == "s"]
    assert [r["e"] for r in secs] == list(range(len(secs)))
    assert sum(r["lost"] for r in secs) == len(lost)
    assert all(r.get("hd", 0) == 0 for r in secs)
    summaries = s.take_summaries()
    assert sum(x["loss"]["lost"] for x in summaries) == len(lost)
    assert summaries[-1]["partial"] or summaries[-1]["duration_s"] == 5
    codes = [e.code for e in events]
    assert "ZL-RUN-009" not in codes and "ZL-RCV-018" not in codes


def test_no_stream_fails_start_and_rolls_back(tmp_path: Path, bus: Any) -> None:
    b, events = bus
    s = session(tmp_path, b, next(_ports), warmup_max_s=2)
    s.start()
    assert wait_state(s, State.IDLE)
    err = next(e for e in events if e.code == "ZL-RUN-009")
    assert "first_packet" in err.text
    assert not (tmp_path / "raw" / "pc").exists()  # nothing measured, no record file


def test_bad_group_is_rcv017_and_idle(tmp_path: Path, bus: Any) -> None:
    b = bus[0]
    cfg = ReceiverConfig("R1", "pc", 1, "not-an-ip", next(_ports))
    s = ReceiverSession(cfg, tmp_path, b)
    with pytest.raises(ZlError) as e:
        s.start()
    assert e.value.code == "ZL-RCV-017"
    assert s.flow.state is State.IDLE


def test_stop_during_warmup_and_refused_commands(tmp_path: Path, bus: Any) -> None:
    b = bus[0]
    port = next(_ports)
    s = session(tmp_path, b, port)
    with pytest.raises(ZlError):
        s.stop()  # IDLE: nothing to stop
    with Sender(port, 3):
        s.start()
        with pytest.raises(ZlError):
            s.start()  # already starting
        time.sleep(1)
        s.stop()
    assert s.flow.state is State.IDLE
    with pytest.raises(ZlError) as e:
        s.start()  # one session = one run
    assert e.value.code == "ZL-RUN-012"


def test_thread_crash_saves_files_and_faults(tmp_path: Path, bus: Any) -> None:
    b, events = bus
    port = next(_ports)
    s = session(tmp_path, b, port)
    with Sender(port, 8):
        s.start()
        assert wait_state(s, State.RUNNING)
        time.sleep(1.5)
        assert s.analyzer is not None

        def boom(*_: object, **__: object) -> None:
            raise RuntimeError("bug")

        s.analyzer.second = boom  # type: ignore[method-assign]
        assert wait_state(s, State.FAULT, 3)
    assert any(e.code == "ZL-RCV-018" for e in events)
    assert records(tmp_path)[-1]["end_reason"] == "fault"
    s.stop()  # cleanup after FAULT
    assert s.flow.state is State.IDLE


@settings(max_examples=6, deadline=None)
@given(
    ops=st.lists(
        st.one_of(
            st.just(("start", 0.0)),
            st.just(("stop", 0.0)),
            st.tuples(st.just("wait"), st.sampled_from([0.0, 0.3, 1.0, 4.5])),
        ),
        max_size=7,
    )
)
def test_monkey_start_stop_wait(tmp_path_factory: pytest.TempPathFactory, ops: list[Any]) -> None:
    """Invariants: refused commands change nothing; after the final stop the session is
    IDLE, no receiver thread is left, and every record file is closed with a footer."""
    d = tmp_path_factory.mktemp("m")
    b = EventBus()
    port = next(_ports)
    s = session(d, b, port)
    with Sender(port, 12):
        for op, arg in ops:
            before = s.flow.state
            if op == "wait":
                time.sleep(arg)
                continue
            try:
                s.start() if op == "start" else s.stop()
            except ZlError:
                assert s.flow.state is before
        if s.flow.state in (State.STARTING, State.RUNNING, State.FAULT):
            s.stop()
        assert wait_state(s, State.IDLE, 5)
    assert not [t for t in threading.enumerate() if t.name.startswith("iptv-") and t.is_alive()]
    for f in (d / "raw" / "pc").glob("*.jsonl") if (d / "raw" / "pc").exists() else []:
        last = json.loads(f.read_text().splitlines()[-1])
        assert last["type"] == "footer"


def test_card_host_m2u_and_station_counters_on_the_line(
    tmp_path: Path, bus: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """02 7 (GAP-148): per-second deltas of the plan's card, taken every second (warm-up
    too, so the first line is one second), absent when not read."""
    from modules.iptv.receiver import session as sm

    monkeypatch.setattr(sm, "nic_of", lambda ip: "wlan9")
    monkeypatch.setattr(sm, "is_wireless", lambda nic: True)
    monkeypatch.setattr(sm, "STATION_EVERY_S", 0.5)
    rx = itertools.count(0, 300)
    station = itertools.count(0, 7)

    class FakeM2u:
        opened: ClassVar[list[tuple[str, str, int]]] = []
        closed: ClassVar[int] = 0

        def __init__(self, nic: str, group: str, port: int) -> None:
            FakeM2u.opened.append((nic, group, port))

        def take(self) -> dict[str, int]:
            return {"fm": 0, "fu": 290}

        def close(self) -> None:
            FakeM2u.closed += 1

    def station_of(nic: str) -> dict[str, int]:
        n = next(station)
        return {"tx_retries": n, "beacon_loss": 0}

    b, _ = bus
    port = next(_ports)
    cfg = ReceiverConfig("R1", "pc", 1, GROUP, port, cell_s=5)
    s = ReceiverSession(
        cfg, tmp_path, b, m2u_opener=FakeM2u, station_of=station_of,  # type: ignore[arg-type]
        nic_reader=lambda nic: {"rx_packets": next(rx), "rx_dropped": 0},
        udp_reader=lambda: {},
    )  # fmt: skip
    with Sender(port, 9):
        s.start()
        assert wait_state(s, State.RUNNING)
        time.sleep(4)
        s.stop()
    secs = [r for r in records(tmp_path) if r["type"] == "s"]
    assert secs and all(r["np"] == 300 and r["nd"] == 0 for r in secs[:-1])  # one second each
    assert all("nm" not in r and "ne" not in r for r in secs)  # not read: absent, not 0
    assert all("ue" not in r for r in secs)
    assert all(r["fm"] == 0 and r["fu"] == 290 for r in secs)
    station_lines = [r for r in secs if "wr" in r]
    assert station_lines and all(r["wr"] > 0 and r["wbl"] == 0 and "wf" not in r
                                 for r in station_lines)  # fmt: skip
    assert FakeM2u.opened == [("wlan9", GROUP, port)] and FakeM2u.closed == 1


def test_the_warm_up_waits_for_steady_playback(tmp_path: Path, bus: Any) -> None:
    """05 2.1-2 (GAP-152): the first picture, then 2 s of "ok" in a row — a player's
    start-up (probing, hardware decoder, its input buffer) stays in the warm-up — or 5 s
    after the first picture at the latest."""
    from types import SimpleNamespace

    from modules.iptv.receiver import session as sm

    b, _ = bus
    s = session(tmp_path, b, next(_ports))
    shown = SimpleNamespace(first_frame=False)
    s.player = shown  # type: ignore[assignment]

    def second(pb: str) -> bool:
        s._tick_n += 1
        return s._settled(SimpleNamespace(pb=pb))

    assert not second("none")  # no picture yet
    shown.first_frame = True
    assert [second(pb) for pb in ("freeze", "glitch", "ok", "ok")] == [False, False, False, True]
    shown.first_frame = False  # a new player (channel change): from the start again
    assert not second("none")
    shown.first_frame = True
    got = [second("drop") for _ in range(sm.SETTLE_MAX_S + 1)]
    assert got == [False] * sm.SETTLE_MAX_S + [True]  # a PC that never plays cleanly
