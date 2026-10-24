"""Primed mode (docs/modules/iptv.md 4.5, S-18): the sender sends only where the
receiver's prime comes from, from port P; no prime for a while stops the flow; the
prime of the player leaves from the player's port through a raw socket. Loopback only
(no NAT here: the source the sender sees is the receiver itself)."""

from __future__ import annotations

import itertools
import shutil
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path

import pytest

from core.blocks import udp_prime
from core.events.bus import Event, EventBus
from core.lifecycle.state_flow import State
from modules.iptv import run as rules
from modules.iptv.sender.commands import QUALITIES, prepare_command
from modules.iptv.sender.prime import (
    PrimedSender,
    PrimeFlow,
    parse_prime,
    parse_stop,
    prime_payload,
    stop_payload,
)
from modules.iptv.sender.session import SenderConfig
from tests.iptv.test_sender import Rx, tsp_pids

HAVE_TOOLS = bool(shutil.which("tsp") and shutil.which("ffmpeg"))
live = pytest.mark.skipif(not HAVE_TOOLS, reason="needs ffmpeg and tsp")
_bases = itertools.count(46000, 100)


@pytest.fixture(scope="module")
def hd_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not HAVE_TOOLS:
        pytest.skip("needs ffmpeg and tsp")
    out = tmp_path_factory.mktemp("prep") / "ch1.ts"
    subprocess.run(prepare_command("ffmpeg", QUALITIES["HD"], 1, out, seconds=4,
                                   preset="ultrafast"), check=True, timeout=300)  # fmt: skip
    return out


def test_ports_payload_and_plan_rules() -> None:
    ports = {rules.prime_ports(5004, ch, f) for ch in (1, 2, 3) for f in (0, 1)}
    flat = [x for t in ports for x in t]
    assert len(ports) == 6 and len(set(flat)) == 18  # never two uses of one port
    assert rules.prime_ports(5004, 1, 0) == (5104, 5105, 5106)
    assert parse_prime(prime_payload("R1", 2, 1)) == ("R1", 2, 1)
    for bad in (b"", b"ZLPRIME R1 x 0", b"HELLO R1 1 0", b"ZLPRIME R1 1"):
        assert parse_prime(bad) is None
    assert parse_stop(stop_payload("R1", 2, 1)) == ("R1", 2, 1)
    assert parse_prime(stop_payload("R1", 2, 1)) is None  # a stop is never a prime
    assert parse_stop(prime_payload("R1", 2, 1)) is None
    assert parse_stop(b"ZLPRIME R1 2 1 GO") is None
    base = {"schema_version": 1, "module": "iptv", "test_type": "pc", "quality": "HD",
            "duration_s": 60, "devices": {"sender": "tx", "receivers": [{"device": "rx",
            "ch": 1}]}, "hosts": {"tx": "10.0.0.2"}}  # fmt: skip
    auto = rules.validate(base)
    assert auto.prime_setting == "Auto" and not auto.primed and auto.sender_ip == "10.0.0.2"
    assert rules.path_note(auto)["path"] == "igmp"
    primed = rules.probe_fallback(auto)
    assert primed is not None and primed.primed and rules.probe_fallback(primed) is None
    assert rules.path_note(primed)["path"] == "primed"
    assert "not the real IPTV path" in rules.path_note(primed)["note"]
    on = rules.validate({**base, "settings": {"S-18": "On"}})
    assert on.primed and rules.probe_fallback(on) is None
    off = rules.validate({**base, "settings": {"S-18": "Off"}})
    assert rules.probe_fallback(off) is None
    with pytest.raises(Exception, match="S-18"):
        rules.validate({**base, "settings": {"S-18": "Maybe"}})
    # a copy per receiver flow: the player too when playback is on (S-49 On is the default)
    assert rules.link_need(on, "tx")["bps"] == 2 * on.muxrate_bps
    assert rules.link_need(on, "rx")["bps"] == 2 * on.muxrate_bps
    senders, receivers = rules.start_steps(on)
    assert [s.params["prime"] for s in senders] == [
        {"p": 5104, "q": 5105, "flow": 0}, {"p": 5114, "q": 5115, "flow": 1}]  # fmt: skip
    assert "dest_ip" not in senders[0].params
    flows = [{"p": 5104, "q": 5105, "r": 5106, "f": 0}, {"p": 5114, "q": 5115, "r": 5116, "f": 1}]
    assert receivers[0].params["prime"] == {"sender_ip": "10.0.0.2", "flows": flows}
    listen, send, _ = rules.probe_steps(on)
    assert listen[0].params["port"] == 5106 and send[0].params["prime"]["flow"] == 0


def test_raw_prime_packet_is_valid_udp() -> None:
    pkt = udp_prime.packet("192.168.1.10", 5116, "10.0.0.2", 5114, b"ZLPRIME R1 1 1")
    ip, udp = pkt[:20], pkt[20:]
    assert udp_prime.checksum(ip) == 0  # header checksum right
    src, dst = socket.inet_aton("192.168.1.10"), socket.inet_aton("10.0.0.2")
    pseudo = src + dst + struct.pack("!BBH", 0, 17, len(udp))
    assert udp_prime.checksum(pseudo + udp) == 0  # UDP checksum right
    assert struct.unpack("!HH", udp[:4]) == (5116, 5114) and udp[8:] == b"ZLPRIME R1 1 1"


def test_raw_prime_leaves_from_the_given_port_without_binding_it() -> None:
    """The player's port stays with the player: the prime is sent from it, not bound."""
    player = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    player.bind(("127.0.0.1", 0))
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.bind(("127.0.0.1", 0))
    sender.settimeout(2)
    port = player.getsockname()[1]
    try:
        udp_prime.send_from_port("127.0.0.1", port, "127.0.0.1", sender.getsockname()[1],
                                 b"ZLPRIME R1 1 1")  # fmt: skip
    except PermissionError:
        pytest.skip("needs CAP_NET_RAW (the installed service has it)")
    data, src = sender.recvfrom(100)
    assert data == b"ZLPRIME R1 1 1" and src == ("127.0.0.1", port)
    player.close()
    sender.close()


def flow_state(s: PrimedSender) -> State:
    """Read fresh: the prime thread changes it."""
    return s.flow.state


@live
def test_primed_sender_follows_the_prime_and_stops_without_it(
    hd_file: Path, tmp_path: Path
) -> None:
    base = next(_bases)
    p, q, r = base, base + 1, base + 2
    events: list[Event] = []
    bus = EventBus()
    bus.subscribe(events.append)
    cfg = SenderConfig(1, hd_file, "0.0.0.0", 0, 10_000_000, ttl=1, local_address="127.0.0.1")
    s = PrimedSender(cfg, PrimeFlow("R1", 0, p, q), tmp_path, bus, lost_s=3)
    s.start()
    assert s.flow.state is State.STARTING and s.status()["tx_bitrate_bps"] is None
    rx = Rx(r)
    try:
        time.sleep(1)
        assert s.dest is None and s._session is None  # nothing sent before a prime
        rx.sock.send_to(prime_payload("OTHER", 1, 0), "127.0.0.1", q)  # wrong run: ignored
        time.sleep(0.8)
        assert s.dest is None
        priming = threading.Event()

        def prime_every_second() -> None:  # the receiver's keep-alive, faster for the test
            while not priming.wait(1.0 if s.primes else 0.0):
                for port in (p, q):
                    rx.sock.send_to(prime_payload("R1", 1, 0), "127.0.0.1", port)

        th = threading.Thread(target=prime_every_second, daemon=True)
        th.start()
        end = time.monotonic() + 15
        while flow_state(s) is not State.RUNNING and time.monotonic() < end:
            time.sleep(0.1)
        assert flow_state(s) is State.RUNNING and s.dest == ("127.0.0.1", r)
        time.sleep(3)
        st = s.status()
        assert st["prime"]["dest"] == f"127.0.0.1:{r}" and st["prime"]["primes"] >= 2
        assert rx.a.measuring and sum(x["lost"] for x in rx.records) == 0
        got = [x for x in rx.records if x["rx"]]
        assert got, "the stream came back to the receiver's port"
        priming.set()
        th.join()
        end = time.monotonic() + 8  # no more primes: stopped after lost_s (SND-021)
        while s._session is not None and time.monotonic() < end:
            time.sleep(0.2)
        assert s._session is None and any(e.code == "ZL-SND-021" for e in events)
    finally:
        s.stop()
        rx.close()
    assert flow_state(s) is State.IDLE and not tsp_pids()


@live
def test_stop_prime_ends_the_flow_at_once_without_snd_021(hd_file: Path, tmp_path: Path) -> None:
    """03 F19 in primed mode: the receiver that leaves the channel stops its flow."""
    base = next(_bases)
    p, q, r = base, base + 1, base + 2
    events: list[Event] = []
    bus = EventBus()
    bus.subscribe(events.append)
    cfg = SenderConfig(1, hd_file, "0.0.0.0", 0, 10_000_000, ttl=1, local_address="127.0.0.1")
    s = PrimedSender(cfg, PrimeFlow("R1", 0, p, q), tmp_path, bus, lost_s=3)
    s.start()
    rx = Rx(r)
    other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    other.bind(("127.0.0.1", 0))
    priming = threading.Event()

    def keep_priming() -> None:
        while not priming.wait(0.5):
            for port in (p, q):
                rx.sock.send_to(prime_payload("R1", 1, 0), "127.0.0.1", port)

    th = threading.Thread(target=keep_priming, daemon=True)
    th.start()
    try:
        end = time.monotonic() + 15
        while flow_state(s) is not State.RUNNING and time.monotonic() < end:
            time.sleep(0.1)
        assert s.dest == ("127.0.0.1", r)
        other.sendto(stop_payload("R1", 1, 0), ("127.0.0.1", q))  # not our receiver
        time.sleep(0.8)
        assert s._session is not None
        priming.set()
        th.join()
        rx.sock.send_to(stop_payload("R1", 1, 0), "127.0.0.1", q)
        end = time.monotonic() + 3
        while (s._session is not None or s.dest is not None) and time.monotonic() < end:
            time.sleep(0.1)
        assert s._session is None and s.dest is None
        time.sleep(4)  # past lost_s: no "no prime" error after a stop
        assert not any(e.code == "ZL-SND-021" for e in events)
    finally:
        priming.set()
        other.close()
        s.stop()
        rx.close()
    assert flow_state(s) is State.IDLE and not tsp_pids()
