"""F23 evidence (GAP-154): the packets around a trouble second, and its picture."""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Any

import pytest

from core.records.recorder import record_run
from modules.iptv.receiver import evidence as ev

NS = ev.NS
TS = b"\x47" + bytes(187)


def rtp(seq: int) -> bytes:
    return struct.pack("!BBHII", 0x80, 33, seq & 0xFFFF, seq * 3000, 0x1234) + TS * 7


def read_pcap(data: bytes) -> tuple[tuple[int, ...], list[tuple[int, int, bytes]]]:
    head = struct.unpack("<IHHiIII", data[:24])
    out, i = [], 24
    while i < len(data):
        sec, nsec, incl, _ = struct.unpack("<IIII", data[i : i + 16])
        out.append((sec, nsec, data[i + 16 : i + 16 + incl]))
        i += 16 + incl
    return head, out


def test_which_seconds_deserve_evidence() -> None:
    assert ev.trouble({"lost": 1, "cc": 1, "p1": 1}) == ["lost 1", "CC errors 1"]
    assert ev.trouble({"p1": 2}) == ["TR 101 290 P1 2"]
    assert ev.trouble({"pb": "glitch"}) == ["playback glitch"]
    assert ev.trouble({"lost": 0, "cc": 0, "pb": "ok"}) == []


def test_the_pcap_is_what_was_received_with_its_arrival_times() -> None:
    pkts = [(rtp(n), 5 * NS + n * 1000) for n in range(3)]
    head, recs = read_pcap(ev.pcap_bytes(pkts, "239.1.1.1", 5004, to_real_ns=10 * NS))
    assert head[0] == 0xA1B23C4D and head[-1] == ev.LINKTYPE_IPV4  # nanosecond pcap
    assert [(s, n) for s, n, _ in recs] == [(15, 0), (15, 1000), (15, 2000)]
    ip = recs[1][2]
    assert ip[16:20] == bytes([239, 1, 1, 1]) and ip[9] == 17  # UDP to the group
    assert ev._checksum(ip[:10] + b"\0\0" + ip[12:20]) == struct.unpack("!H", ip[10:12])[0]
    assert struct.unpack("!HH", ip[20:24]) == (5004, 5004) and ip[28:] == rtp(1)  # exact


def test_the_video_is_the_ts_inside_the_rtp() -> None:
    assert ev.ts_bytes([(rtp(1), 0), (TS * 2, 0), (b"junk", 0)]) == TS * 7 + TS * 2


class Rig:
    def __init__(self, tmp: Path, free: int = 100_000, render: bool = True) -> None:
        self.drawn: list[tuple[Path, float]] = []
        self.t = 100 * NS
        self.seq = 0

        def draw(ts: Path, jpg: Path, at: float) -> None:
            self.drawn.append((ts, at))
            jpg.write_bytes(b"jpg")

        self.e = ev.Evidence(tmp, "R1", 1, "239.1.1.1", 5004, render=draw if render else None,
                             free_mb=lambda p: free, clock_ns=lambda: self.t)  # fmt: skip
        self.sec = 0

    def second(self, rec: dict[str, Any] | None = None) -> Path | None:
        for _ in range(10):  # 10 packets a second
            self.seq += 1
            self.t += NS // 10
            self.e.feed(rtp(self.seq), self.t)
        self.sec += 1
        written = self.e.on_second({"e": self.sec, **(rec or {})}, self.t)
        for t in self.e.drawing:
            t.join(5)
        return written


def test_a_trouble_second_keeps_10_s_before_and_5_s_after(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    for _ in range(30):
        r.second()
    assert r.second({"lost": 1, "cc": 1}) is None  # e31: the window opens
    got = [r.second() for _ in range(5)]
    path = next(p for p in got if p is not None)
    info = json.loads(path.read_text())
    assert path.name == "R1_ev_e000031_ch1.json" and info["why"] == ["lost 1", "CC errors 1"]
    assert info["pcap"] == "R1_ev_e000031_ch1.pcap" and info["picture"] == "R1_ev_e000031_ch1.jpg"
    _, recs = read_pcap((tmp_path / info["pcap"]).read_bytes())
    assert len(recs) == info["packets"] and 159 <= len(recs) <= 161  # 11 s + 5 s
    ((ts, at),) = r.drawn
    assert not ts.exists()  # the temporary video went with the drawing
    assert 10.0 <= at <= 10.2  # the trouble second starts 10 s into the clip
    assert all(record_run(p.name) == ("R1", None) for p in tmp_path.iterdir())


def test_s30_off_keeps_the_packets_and_no_picture(tmp_path: Path) -> None:
    r = Rig(tmp_path, render=False)
    r.second({"pb": "freeze"})
    path = next(p for p in (r.second() for _ in range(6)) if p is not None)
    info = json.loads(path.read_text())
    assert info["pcap"] and info["picture"] is None and r.drawn == []


def test_twenty_events_an_hour_then_the_json_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ev, "MIN_GAP_S", 0.0)  # the hour's limit alone
    r = Rig(tmp_path)
    infos = []
    for _ in range(22):
        r.second({"lost": 1})
        infos += [json.loads(p.read_text()) for p in (r.second() for _ in range(6)) if p]
    kept = [i for i in infos if i["pcap"]]
    assert len(infos) == 22 and len(kept) == ev.PER_HOUR
    assert "20 events this hour already" in infos[-1]["kept_less"]


def test_a_full_disk_keeps_the_json_only(tmp_path: Path) -> None:
    r = Rig(tmp_path, free=100)
    r.second({"lost": 3})
    info = json.loads(next(p for p in (r.second() for _ in range(6)) if p).read_text())
    assert info["pcap"] is None and "100 MB free" in info["kept_less"]


def test_a_stop_writes_the_open_window(tmp_path: Path) -> None:
    r = Rig(tmp_path)
    r.second({"lost": 1})
    path = r.e.flush()
    assert path is not None and json.loads(path.read_text())["pcap"]
    assert r.e.flush() is None


def test_the_clip_says_which_packet_went_missing_and_how_the_arrivals_paused() -> None:
    """2026-10-16 night run, read by hand from the pcap: groups of ~10 every ~3.3 ms, then
    16.8 ms of nothing and 42554 next -- 42553 never came. The report says it now."""
    ms = 1_000_000
    pk, t = [], 0
    for seq in range(42500, 42553):
        t += 3 * ms if seq % 10 == 0 else 1000  # an aggregate every ~3 ms
        pk.append((rtp(seq), t))
    t += 17 * ms
    pk += [(rtp(42554), t), (rtp(42555), t + 1000)]
    r = ev.reading(pk, mark_ns=t)
    assert r is not None and [m["seq"] for m in r["missing"]] == [42553]
    assert r["missing"][0]["held"] and "RTP 42553 never came" in r["text"]
    assert "17 ms" in r["text"] and "radio retries" in r["text"]
    steady = [(rtp(s), s * ms) for s in (1, 2, 4, 5)]  # a hole with no pause
    r = ev.reading(steady, mark_ns=0)
    assert r is not None and "no pause" in r["text"] and not r["missing"][0]["held"]
    assert ev.reading([(b"not rtp", 1), (b"x", 2)], 0) is None


def test_files_at_most_once_a_minute(tmp_path: Path) -> None:
    """2026-10-16 night: 25 clips in 2.5 min (pcap + UHD render each) filled the T460's
    CPU and it dropped 64,038 packets itself. Trouble within a minute: the json only."""
    r = Rig(tmp_path)
    infos = []
    for _ in range(4):  # trouble every 7 s
        r.second({"lost": 1})
        infos += [json.loads(p.read_text()) for p in (r.second() for _ in range(6)) if p]
    kept = [i for i in infos if i["pcap"]]
    assert len(infos) == 4 and len(kept) == 1
    assert "less than 60 s ago" in infos[-1]["kept_less"]
