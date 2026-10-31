"""Evidence of trouble (docs/03-features.md F23, S-30; 20 GAP-154).

2026-10-10 user: "engineers believe it when they see it broken", "a pcap shows whether a
CC was lost". When a measured second has trouble (lost packets, CC or P1 errors, a
playback glitch, freeze or drop), this keeps what this PC received around it:

- ``<run>_ev_e<second>_ch<ch>.json`` (always): the second, why, the files, the clip times.
- ``<run>_ev_e<second>_ch<ch>.pcap`` (always, F23 "packet clip"): every datagram from
  ``PRE_S`` before that second to ``POST_S`` after it, byte for byte with its arrival
  time (nanoseconds). IP and UDP headers are written from the stream's group and port
  (no sender address is kept per packet); Wireshark shows the RTP sequence numbers and
  each TS packet's CC.
- ``<run>_ev_e<second>_ch<ch>.jpg`` (S-30 On, the screen): the same packets' video
  rendered after the window closed (ffmpeg, lowest priority), 1 s before the trouble to
  3 s after, 4 frames a second in a grid: what the picture was, glitches included,
  wherever the player window was. (F23 planned player snapshots; rendering the received
  packets needs no screen and no player, GAP-154.)

F23 limits: at most ``PER_HOUR`` events an hour keep files (later ones the json only),
pcaps together at most ``PCAP_BUDGET_MB``, the json only when the disk has less than
``FREE_MIN_MB``. The files sit with the 1-second records, so the records retrieval
(02 2-13) brings them to the report folder.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import re
import shutil
import struct
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.blocks import rtp_header
from modules.iptv import record_keys as k

NS = 1_000_000_000
PRE_S = 10.0  # before the second with trouble (F23: 10 s before ~ 5 s after)
POST_S = 5.0  # after it
PER_HOUR = 20
# 2026-10-16 night: 25 events in 2.5 min, each a 72 MB pcap + a UHD software render, filled
# the T460's 4 threads and it dropped 64,038 packets itself. Files at most this often,
# one picture at a time (the next waits its turn or is left out).
MIN_GAP_S = 60.0
PCAP_BUDGET_MB = 5 * 1024  # F23: the evidence folder's limit
FREE_MIN_MB = 2048
BAD_PLAYBACK = ("glitch", "freeze", "drop")
LINKTYPE_IPV4 = 228

Render = Callable[[Path, Path, float], None]  # (ts file, jpg, seconds into the clip)


def trouble(rec: dict[str, Any]) -> list[str]:
    """Why a 1-second record deserves evidence (empty: it does not)."""
    why = []
    if rec.get(k.LOST):
        why.append(f"lost {rec[k.LOST]}")
    if rec.get(k.CC_ERRORS):
        why.append(f"CC errors {rec[k.CC_ERRORS]}")
    if rec.get(k.P1_ERRORS) and not rec.get(k.CC_ERRORS):
        why.append(f"TR 101 290 P1 {rec[k.P1_ERRORS]}")
    if rec.get(k.PLAYBACK) in BAD_PLAYBACK:
        why.append(f"playback {rec[k.PLAYBACK]}")
    return why


HELD_FACTOR = 3.0  # a pause this many times the usual time between arrivals


BREATHE_EVERY = 500  # packets between short rests of a clip's work (receiverPC 2026-10-23)


def _breathe(i: int) -> None:
    """A clip is ~53 k packets of Python work; without rests it held the interpreter
    ~1 s and the receiver's 1-second counts went uneven for ~10 s after each save."""
    if i % BREATHE_EVERY == BREATHE_EVERY - 1:
        time.sleep(0.0005)


def _behind() -> None:
    """This thread below the receiving ones (Linux: per-thread nice); never raises."""
    if hasattr(os, "setpriority") and hasattr(threading, "get_native_id"):
        with contextlib.suppress(OSError):
            os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)


def reading(packets: list[tuple[bytes, int]], mark_ns: int) -> dict[str, Any] | None:
    """What the packet clip shows, in words (2026-10-16 user "ㅇㅇ" to: the report should
    say which packet went missing and how the arrivals paused, without Wireshark).
    Missing RTP numbers with the pause around each, out-of-order and duplicates. None:
    not RTP."""
    seqs: list[tuple[int, int]] = []
    for i, (data, t) in enumerate(packets):
        _breathe(i)
        h = rtp_header.parse(data)
        if h is not None:
            seqs.append((h.seq, t))
    if len(seqs) < 2:
        return None
    # between arrival groups (Wi-Fi delivers an aggregate at one instant; 0.1 ms and up)
    steps = [b[1] - a[1] for a, b in itertools.pairwise(seqs) if b[1] - a[1] >= 100_000]
    usual = sorted(steps)[len(steps) // 2] / 1e6 if steps else 0.0
    seen: set[int] = {seqs[0][0]}
    missing: list[dict[str, Any]] = []
    dups = back = 0
    for (s0, t0), (s1, t1) in itertools.pairwise(seqs):
        d = (s1 - s0) & 0xFFFF
        if s1 in seen:
            dups += 1
        elif d == 0 or d > 0x8000:
            back += 1
        elif d > 1:
            gap = (t1 - t0) / 1e6
            missing.append({"seq": (s0 + 1) & 0xFFFF, "count": d - 1,
                            "at_s": round((t0 - mark_ns) / NS, 3), "gap_ms": round(gap, 2),
                            "held": bool(usual and gap >= HELD_FACTOR * usual)})  # fmt: skip
        seen.add(s1)
    lines = []
    for m in missing[:5]:
        n = f"RTP {m['seq']}" + (
            f"-{(int(m['seq']) + int(m['count']) - 1) & 0xFFFF}" if m["count"] > 1 else ""
        )
        if m["held"]:
            lines.append(f"{n} never came: nothing arrived for {m['gap_ms']:g} ms (usually "
                         f"{usual:.1f} ms between arrivals), then the next one. The link held "
                         "and gave up on it (radio retries).")  # fmt: skip
        else:
            lines.append(f"{n} never came, with no pause in the arrivals ({m['gap_ms']:g} ms): "
                         "it was gone before the last link.")  # fmt: skip
    if back:
        lines.append(f"{back} packet(s) came out of order.")
    if dups:
        lines.append(f"{dups} packet(s) came twice.")
    return {
        "missing": missing,
        "usual_ms": round(usual, 2),
        "out_of_order": back,
        "duplicates": dups,
        "text": " ".join(lines) or "Every RTP packet in the clip came.",
    }


def _checksum(header: bytes) -> int:
    s: int = sum(struct.unpack("!10H", header))
    s = (s & 0xFFFF) + (s >> 16)
    s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def pcap_bytes(
    packets: list[tuple[bytes, int]], group: str, port: int, to_real_ns: int = 0
) -> bytes:
    """A nanosecond pcap (LINKTYPE_IPV4) of UDP datagrams to ``group:port``."""
    dst = bytes(int(x) for x in group.split("."))
    out = [struct.pack("<IHHiIII", 0xA1B23C4D, 2, 4, 0, 0, 65535, LINKTYPE_IPV4)]
    for ident, (data, t) in enumerate(packets):
        _breathe(ident)
        udp = struct.pack("!HHHH", port, port, 8 + len(data), 0) + data
        ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), ident & 0xFFFF, 0, 16, 17,
                         0, b"\0\0\0\0", dst)  # fmt: skip
        ip = ip[:10] + struct.pack("!H", _checksum(ip)) + ip[12:]
        real = t + to_real_ns
        frame = ip + udp
        out.append(struct.pack("<IIII", real // NS, real % NS, len(frame), len(frame)) + frame)
    return b"".join(out)


def ts_bytes(packets: list[tuple[bytes, int]]) -> bytes:
    """The MPEG-TS inside the datagrams (RTP stripped; TS over UDP as it is)."""
    out = []
    for i, (data, _) in enumerate(packets):
        _breathe(i)
        h = rtp_header.parse(data)
        if h is not None:
            out.append(data[h.payload_offset : h.payload_end])
        elif data[:1] == b"\x47":
            out.append(data)
    return b"".join(out)


class Evidence:
    def __init__(
        self,
        folder: Path,
        run_id: str,
        ch: int,
        group: str,
        port: int,
        render: Render | None = None,
        free_mb: Callable[[Path], int] | None = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.folder, self.run_id, self.ch, self.group, self.port = folder, run_id, ch, group, port
        self._render = render
        self._free_mb = free_mb or (lambda p: shutil.disk_usage(p).free // (1 << 20))
        self._clock = clock_ns
        self._ring: deque[tuple[bytes, int]] = deque()
        self._open: dict[str, Any] | None = None  # a window waiting for its POST_S
        self._hour: list[int] = []  # when this hour's events with files began
        self.taken = 0
        self.pcap_bytes = 0
        self.skipped: list[str] = []  # why an event kept less
        self.drawing: list[threading.Thread] = []

    def feed(self, data: bytes, arrival_ns: int) -> None:
        self._ring.append((data, arrival_ns))
        keep_from = arrival_ns - int((PRE_S + 1 + POST_S + 1) * NS)
        while self._ring and self._ring[0][1] < keep_from:
            self._ring.popleft()

    def on_second(self, rec: dict[str, Any], now_ns: int) -> Path | None:
        """Call after each measured second with its record. Returns the json written."""
        written = None
        if self._open is not None and now_ns >= self._open["until"]:
            # off the measuring thread (2026-10-16): writing a 72 MB pcap here made the
            # node's 1-second loop ~0.7 s late
            written = self._write(self._open, background=True)
            self._open = None
        why = trouble(rec)
        if why and self._open is None:  # trouble inside an open window is in its clip
            e = int(rec.get(k.ELAPSED, 0))
            self._open = {"e": e, "why": why, "files": self._allowed(now_ns, e),
                          "start": now_ns - int((1 + PRE_S) * NS), "mark": now_ns - NS,
                          "until": now_ns + int(POST_S * NS)}  # fmt: skip
        return written

    def flush(self) -> Path | None:
        """The test stops or the channel changes: an open window is written as it is."""
        if self._open is None:
            return None
        o, self._open = self._open, None
        return self._write(o)

    def _allowed(self, now_ns: int, e: int) -> bool:
        """Files for this event (else the json only, with why)."""
        self._hour = [t for t in self._hour if now_ns - t < 3600 * NS]
        if self._hour and now_ns - self._hour[-1] < MIN_GAP_S * NS:
            self.skipped.append(f"e{e}: the last clip was less than {MIN_GAP_S:g} s ago")
            return False
        if len(self._hour) >= PER_HOUR:
            self.skipped.append(f"e{e}: {PER_HOUR} events this hour already")
            return False
        try:
            free = self._free_mb(self.folder)
        except OSError:
            free = 0
        if free < FREE_MIN_MB:
            self.skipped.append(f"e{e}: {free} MB free (needs {FREE_MIN_MB})")
            return False
        self._hour.append(now_ns)
        return True

    def _write(self, o: dict[str, Any], background: bool = False) -> Path:
        packets = [p for p in self._ring if o["start"] <= p[1] <= o["until"]]
        stem = f"{self.run_id}_ev_e{o['e']:06d}_ch{self.ch}"
        to_real = time.time_ns() - self._clock()
        path = self.folder / f"{stem}.json"
        if not background:
            self._files(o, packets, stem, to_real)
            return path
        t = threading.Thread(target=self._files, args=(o, packets, stem, to_real),
                             name="iptv-evidence-write", daemon=True)  # fmt: skip
        self.drawing.append(t)
        t.start()
        return path

    def _files(self, o: dict[str, Any], packets: list[tuple[bytes, int]], stem: str,
               to_real: int) -> None:  # fmt: skip
        _behind()  # after the receiving and the counting (a background thread)
        self.folder.mkdir(parents=True, exist_ok=True)
        info: dict[str, Any] = {
            "e": o["e"], "ch": self.ch, "why": o["why"], "packets": len(packets),
            "from_s": -PRE_S - 1, "to_s": POST_S, "group": f"{self.group}:{self.port}",
            "pcap": None, "picture": None, "kept_less": None,
            "reading": reading(packets, o["mark"]) if packets else None,
        }  # fmt: skip
        self.taken += 1
        files = bool(o["files"]) and bool(packets)
        if not o["files"]:
            info["kept_less"] = self.skipped[-1] if self.skipped else "limit"
        if files:
            data = pcap_bytes(packets, self.group, self.port, to_real)
            if self.pcap_bytes + len(data) <= PCAP_BUDGET_MB * (1 << 20):
                pcap = self.folder / f"{stem}.pcap"
                pcap.write_bytes(data)
                info["pcap"] = pcap.name
                self.pcap_bytes += len(data)
            else:
                info["kept_less"] = f"pcaps reached {PCAP_BUDGET_MB} MB"
        drawing = any(t.is_alive() for t in self.drawing if t.name == "iptv-evidence")
        if files and self._render is not None and drawing:  # one picture at a time
            info["kept_less"] = "no picture: the one before was still being made"
        elif files and self._render is not None:
            ts = self.folder / f".{stem}.ts"
            ts.write_bytes(ts_bytes(packets))
            jpg = self.folder / f"{stem}.jpg"
            at = (o["mark"] - packets[0][1]) / NS  # the trouble second's start in the clip
            info["picture"] = jpg.name
            t = threading.Thread(target=self._draw, args=(ts, jpg, at), name="iptv-evidence",
                                 daemon=True)  # fmt: skip
            self.drawing.append(t)
            t.start()
        path = self.folder / f"{stem}.json"
        tmp = path.with_name(path.name + ".tmp")  # the records fetch never sees half a file
        tmp.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    def _draw(self, ts: Path, jpg: Path, at_s: float) -> None:
        try:
            assert self._render is not None
            self._render(ts, jpg, at_s)
        except Exception:  # a picture that cannot be made leaves the pcap and the json
            pass
        finally:
            ts.unlink(missing_ok=True)


def ffmpeg_render(procs_dir: Path, ffmpeg: str = "ffmpeg", seconds: float = 4.0,
                  before: float = 1.0, crop: str = "") -> Render:  # fmt: skip
    """1 s before the trouble to 3 s after, 4 frames a second, 4 x 4 grid, 960 px each
    (a tablet's screen video: its whole ``seconds`` spread over the 16, ``crop`` to the
    picture). Run through process_runner (12 4-3), at the lowest priority on Linux."""
    import sys

    from core.blocks.process_runner import ProcessRunner, ProcState

    def render(ts: Path, jpg: Path, at_s: float) -> None:
        start = max(0.0, at_s - before)
        vf = f"fps={16 / seconds:g},{crop + ',' if crop else ''}scale=960:-2,tile=4x4:padding=4"
        argv = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{start:.2f}",
                "-i", str(ts), "-t", f"{seconds:g}", "-vf", vf,
                "-frames:v", "1", "-q:v", "3", str(jpg)]  # fmt: skip
        if sys.platform.startswith("linux"):
            argv = ["nice", "-n", "19", *argv]  # the measuring comes first
        runner = ProcessRunner("ffmpeg_evidence", argv, procs_dir)
        runner.start()
        end = time.monotonic() + 120
        while runner.poll() is ProcState.RUNNING and time.monotonic() < end:
            time.sleep(0.2)
        runner.stop()

    return render


FREEZE_NOISE_DB = -60  # freezedetect: below this difference two frames are the same
FREEZE_MIN_S = 0.4  # a held picture this long counts (a 30 fps screen repeats none)
FROZEN_BORDER = "drawbox=x=0:y=0:w=iw:h=ih:color=red@0.9:t=28"
_FREEZE = re.compile(r"freeze_(start|end): ([\d.]+)")

ScreenRender = Callable[[Path, Path, float], dict[str, Any]]


def screen_freezes(text: str, length_s: float) -> list[tuple[float, float]]:
    """freezedetect's lines -> (start, end) seconds; one still frozen at the end ends
    with the video."""
    out: list[tuple[float, float]] = []
    start: float | None = None
    for kind, value in _FREEZE.findall(text):
        if kind == "start":
            start = float(value)
        elif start is not None:
            out.append((start, float(value)))
            start = None
    if start is not None:
        out.append((start, length_s))
    # one freeze can come in adjacent pieces (receiverPC 2026-10-31: 1.24-2.27, 2.27-4.93,
    # 4.93-5.97 on one tablet): pieces that touch are one
    merged: list[tuple[float, float]] = []
    for a, b in out:
        if merged and a - merged[-1][1] <= 0.05:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def screen_render(procs_dir: Path, ffmpeg: str, crop: str, length_s: float = 0.0
                  ) -> ScreenRender:  # fmt: skip
    """A tablet's screen video as the PCs' grid, with every frame of a frozen stretch in a
    red border (2026-10-31 user: "같은 화면이 보였다는걸 어떻게 알려주면 좋을까": a freeze is
    as big a problem as a glitch, but sixteen near-identical tiles hide it). The frozen
    stretches come from ffmpeg's freezedetect on the picture only; returned as seconds
    into the video."""
    import subprocess
    import sys

    from core.blocks.process_runner import NO_WINDOW

    def render(video: Path, jpg: Path, at_s: float) -> dict[str, Any]:
        argv = [ffmpeg, "-hide_banner", "-nostats", "-i", str(video), "-vf",
                f"{crop},freezedetect=n={FREEZE_NOISE_DB}dB:d={FREEZE_MIN_S}", "-map", "0:v",
                "-f", "null", "-"]  # fmt: skip
        if sys.platform.startswith("linux"):
            argv = ["nice", "-n", "19", *argv]
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=120,
                                  check=False, creationflags=NO_WINDOW)  # fmt: skip
            frozen = screen_freezes(done.stderr, length_s)
        except (OSError, subprocess.SubprocessError):
            frozen = []
        start = max(0.0, at_s - 1.0)  # the grid's own window (ffmpeg_render)
        boxes = ",".join(f"{FROZEN_BORDER}:enable='between(t,{a - start:.2f},{b - start:.2f})'"
                         for a, b in frozen if b > start)  # fmt: skip
        grid_crop = f"{crop},{boxes}" if boxes else crop
        ffmpeg_render(procs_dir, ffmpeg, crop=grid_crop)(video, jpg, at_s)
        return {"frozen": [[round(a, 2), round(b, 2)] for a, b in frozen]}

    return render
