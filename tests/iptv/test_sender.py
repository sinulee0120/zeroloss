"""IPTV sender: command lines (iptv 4.1, 4.4) and a live tsp session on 127.0.0.1."""

from __future__ import annotations

import itertools
import os
import random
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.blocks import rtp_header
from core.blocks.udp_receiver import UdpReceiver, clock_ns
from core.events.bus import Event, EventBus
from core.lifecycle.state_flow import State
from core.protocol.envelope import ZlError
from modules.iptv.receiver.analyzer import StreamAnalyzer
from modules.iptv.sender.commands import (
    QUALITIES,
    new_ssrc,
    parse_rate,
    prepare_command,
    send_command,
)
from modules.iptv.sender.session import SenderConfig, SenderSession

HAVE_TOOLS = bool(shutil.which("tsp") and shutil.which("ffmpeg"))
_ports = itertools.count(47100)

# ------------------------------------------------------------------ commands


def test_ssrc_keeps_channel_in_top_bits() -> None:
    rng = random.Random(1)
    values = {new_ssrc(7, rng) for _ in range(50)}
    assert {v >> 24 for v in values} == {7} and len(values) == 50
    with pytest.raises(ValueError):
        new_ssrc(0)


def test_send_command_uses_confirmed_options() -> None:
    cmd = send_command(
        "tsp", Path("ch1.ts"), "239.1.1.1", 5004, 35_000_000, 0x01ABCDEF, 16, "10.0.0.2"
    )
    text = " ".join(cmd)
    for part in (
        "-I file ch1.ts --infinite",
        "-P continuity --fix",
        "-P pcradjust --bitrate 35000000",
        "-P regulate --bitrate 35000000 --packet-burst 7",
        "-O ip 239.1.1.1:5004 --rtp --payload-type 33",
        f"--ssrc-identifier {0x01ABCDEF}",
        "--packet-burst 7 --enforce-burst --ttl 16 --local-address 10.0.0.2",
    ):
        assert part in text, part
    assert "tsfixcc" not in text


def test_prepare_command_per_quality() -> None:
    uhd = " ".join(prepare_command("ffmpeg", QUALITIES["UHD"], 2, Path("o.ts")))
    assert "libx265" in uhd and "strict-cbr=1:hrd=1" in uhd and "-muxrate 35000000" in uhd
    assert f"-b:v {int(35_000_000 * 0.88)}" in uhd and "-pat_period 0.08" in uhd
    assert "ZeroLoss CH2" in uhd and "testsrc2=size=3840x2160" in uhd
    hd = " ".join(prepare_command("ffmpeg", QUALITIES["HD"], 1, Path("o.ts"), Path("in.mp4")))
    assert "libx264" in hd and "nal-hrd=cbr" in hd and "-i in.mp4" in hd and "testsrc2" not in hd


def test_parse_rate() -> None:
    line = '* bitrate_monitor: ZLRATE{ "bitrate": 10014976, "net": 9499112, "status": "normal" }'
    assert parse_rate(line) == 10014976
    assert parse_rate("* tsp: user interrupt") is None
    assert parse_rate("ZLRATE{ broken") is None


# ------------------------------------------------------------------ live

live = pytest.mark.skipif(not HAVE_TOOLS, reason="needs ffmpeg and tsp")


@pytest.fixture(scope="module")
def hd_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not HAVE_TOOLS:
        pytest.skip("needs ffmpeg and tsp")
    out = tmp_path_factory.mktemp("prep") / "ch1.ts"
    subprocess.run(
        prepare_command("ffmpeg", QUALITIES["HD"], 1, out, seconds=4, preset="ultrafast"),
        check=True,
        timeout=300,
    )
    return out


class Rx:
    """Unicast receiver + analyzer on a thread."""

    def __init__(self, port: int) -> None:
        self.sock = UdpReceiver(port, None, "127.0.0.1", rcvbuf_bytes=4 << 20)
        self.a = StreamAnalyzer(1, clock_ns(), playback=False)
        self.ssrcs: set[int] = set()
        self.records: list[dict[str, Any]] = []
        self.halt = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self) -> None:
        nxt = time.monotonic() + 1
        while not self.halt.is_set():
            p = self.sock.receive(0.05)
            if p:
                h = rtp_header.parse(p.data)
                if h:
                    self.ssrcs.add(h.ssrc)
                self.a.on_datagram(p.data, p.arrival_ns)
            if time.monotonic() >= nxt:
                nxt += 1
                rec = self.a.second(clock_ns())
                if rec:
                    self.records.append(rec)

    def close(self) -> None:
        self.halt.set()
        self.t.join()
        self.sock.close()


def tsp_pids() -> list[int]:
    out = subprocess.run(["pgrep", "-x", "tsp"], capture_output=True, text=True)
    return [int(x) for x in out.stdout.split()]


def state(s: SenderSession) -> State:
    """Read fresh: the watch thread changes it."""
    return s.flow.state


def cfg(file: Path, port: int, **kw: Any) -> SenderConfig:
    return SenderConfig(1, file, "127.0.0.1", port, 10_000_000, ttl=1, **kw)


@live
def test_send_receive_clean(hd_file: Path, tmp_path: Path) -> None:
    port = next(_ports)
    rx = Rx(port)
    s = SenderSession(cfg(hd_file, port), tmp_path, EventBus())
    try:
        s.start()
        assert s.flow.state is State.RUNNING
        assert s.last_rate is not None and abs(s.last_rate - 10_000_000) < 100_000
        time.sleep(9)  # loops the 4 s file twice
        s.stop()
    finally:
        rx.close()
    assert state(s) is State.IDLE
    assert rx.a.measuring
    assert sum(r["lost"] for r in rx.records) == 0
    assert sum(r["p1"] for r in rx.records) == 0  # loop seams included
    assert {x >> 24 for x in rx.ssrcs} == {1}
    assert s._runner is not None and s._runner.pid not in tsp_pids()


def test_missing_file_is_snd001(tmp_path: Path) -> None:
    s = SenderSession(cfg(tmp_path / "none.ts", 1), tmp_path, EventBus())
    with pytest.raises(ZlError) as e:
        s.start()
    assert e.value.code == "ZL-SND-001" and s.flow.state is State.IDLE


@live
def test_not_stable_in_time_rolls_back(hd_file: Path, tmp_path: Path) -> None:
    got: list[Event] = []
    bus = EventBus()
    bus.subscribe(got.append)
    s = SenderSession(cfg(hd_file, next(_ports), start_limit_s=1.5), tmp_path, bus)
    with pytest.raises(ZlError) as e:
        s.start()
    assert e.value.code == "ZL-SND-019"
    assert s.flow.state is State.IDLE
    assert s._runner is not None and s._runner.pid not in tsp_pids()
    assert any(x.code == "ZL-SND-019" for x in got)


@live
def test_tsp_death_restarts_once_then_fault(hd_file: Path, tmp_path: Path) -> None:
    got: list[Event] = []
    bus = EventBus()
    bus.subscribe(got.append)
    s = SenderSession(cfg(hd_file, next(_ports)), tmp_path, bus)
    s.start()
    first = s._runner.pid  # type: ignore[union-attr]
    assert first is not None
    os.kill(first, signal.SIGKILL)
    end = time.monotonic() + 20
    while (s.restarts == 0 or s._runner.pid == first) and time.monotonic() < end:  # type: ignore[union-attr]
        time.sleep(0.2)
    assert s.restarts == 1 and state(s) is State.RUNNING
    second = s._runner.pid  # type: ignore[union-attr]
    assert second is not None
    os.kill(second, signal.SIGKILL)
    end = time.monotonic() + 10
    while state(s) is not State.FAULT and time.monotonic() < end:
        time.sleep(0.2)
    assert state(s) is State.FAULT
    assert [x.code for x in got].count("ZL-SND-005") == 2, [(x.code, x.text) for x in got]
    s.stop()
    assert s.flow.state is State.IDLE


@live
@settings(max_examples=4, deadline=None)
@given(ops=st.lists(st.sampled_from(["start", "stop", "wait"]), max_size=6))
def test_monkey_start_stop(
    hd_file: Path, tmp_path_factory: pytest.TempPathFactory, ops: list[str]
) -> None:
    """Invariants: refused commands change nothing; after cleanup no tsp is left."""
    s = SenderSession(cfg(hd_file, next(_ports)), tmp_path_factory.mktemp("m"), EventBus())
    for op in ops:
        before = s.flow.state
        if op == "wait":
            time.sleep(0.5)
            continue
        try:
            s.start() if op == "start" else s.stop()
        except ZlError:
            assert s.flow.state is before
    if s.flow.state in (State.RUNNING, State.FAULT):
        s.stop()
    assert s.flow.state is State.IDLE
    assert s._runner is None or s._runner.pid not in tsp_pids()


def test_uhd_is_made_with_the_fastest_x265_preset() -> None:
    """2026-10-24: a 10-minute UHD clip took the ProBook two hours at "veryfast"."""
    from pathlib import Path

    from modules.iptv.sender.commands import QUALITIES, prepare_command

    uhd = prepare_command("ffmpeg", QUALITIES["UHD"], 1, Path("o.ts"), seconds=1)
    hd = prepare_command("ffmpeg", QUALITIES["HD"], 1, Path("o.ts"), seconds=1)
    assert uhd[uhd.index("-preset") + 1] == "ultrafast"
    assert hd[hd.index("-preset") + 1] == "veryfast"
    assert "strict-cbr=1:hrd=1:aq-mode=2:log-level=error" in uhd  # still forced
