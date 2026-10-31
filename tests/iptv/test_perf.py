"""RCV-015 pieces (05 4.2-1): ffmpeg speed, vainfo, cancel on safe stop."""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import pytest

from modules.iptv.receiver import perf
from modules.iptv.receiver.perf import PerfCheck, hw_decode, parse_speed

VAINFO = """vainfo: Driver version: Intel iHD driver
      VAProfileH264Main               : VAEntrypointVLD
      VAProfileH264High               : VAEntrypointEncSlice
      VAProfileHEVCMain               : VAEntrypointEncSlice
"""
# vainfo of the T460 dev PC, as printed (a tab after the colon; the profile has a suffix)
REAL_VAINFO = (
    "      VAProfileH264Main               :\tVAEntrypointVLD\n"
    "      VAProfileH264High               :\tVAEntrypointVLD\n"
    "      VAProfileH264High               :\tVAEntrypointEncSliceLP\n"
    "      VAProfileH264ConstrainedBaseline:\tVAEntrypointVLD\n"
    "      VAProfileHEVCMain               :\tVAEntrypointVLD\n"
)


def test_speed_and_vainfo() -> None:
    log = "frame=  150 fps=45 q=-0.0 size=N/A speed=1.02x\\rframe=  300 speed=2.37x \\n"
    assert parse_speed(log) == 2.37 and parse_speed("no speed here") is None
    if not __import__("sys").platform.startswith("linux"):
        pytest.skip("vainfo is Linux")
    assert hw_decode("HD", lambda a: REAL_VAINFO) is True  # the sender's HD file is H.264 High
    assert hw_decode("UHD", lambda a: REAL_VAINFO) is True
    assert hw_decode("HD", lambda a: VAINFO) is False  # Main can decode, High cannot: not enough
    assert hw_decode("UHD", lambda a: VAINFO) is False  # only an encoder entry point
    assert hw_decode("UHD", lambda a: None) is None  # no vainfo: unknown


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="vainfo is Linux")
def test_warn_texts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pc = PerfCheck(tmp_path, tmp_path / "procs", vainfo=lambda a: VAINFO, idle_cpu=lambda: 70.0)
    monkeypatch.setattr(pc, "_run", lambda *a, **k: 0)
    (tmp_path / "perf").mkdir()
    (tmp_path / "perf" / "decode_UHD.log").write_text("speed=0.8x")
    r = pc._judge("UHD", pc._measure("UHD", tmp_path / "x.ts"))
    assert r["decode_speed"] == 0.8 and r["hw_decode"] is False and r["idle_cpu_pct"] == 70.0
    why = r["warn"]["why"]
    assert "0.8x real time (needs 1.5x)" in why and "no hardware decoding" in why
    assert "70% busy" in why and "S-49" in r["warn"]["fix"] and "in HD" in r["warn"]["fix"]


def test_how_busy_is_read_on_every_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GAP-113 (real: "56% busy" shown again after the PC had gone quiet): the decoding
    result is kept 24 h, the CPU load is now-value and read on each check."""
    load = [70.0]
    pc = PerfCheck(
        tmp_path, tmp_path / "procs", vainfo=lambda a: REAL_VAINFO, idle_cpu=lambda: load[0]
    )
    monkeypatch.setattr(pc, "_run", lambda *a, **k: 0)
    (tmp_path / "perf").mkdir()
    (tmp_path / "perf" / "decode_HD.log").write_text("speed=4.0x")
    pc.sample("HD", 10_000_000).write_bytes(b"x")
    first = pc.check("HD", 10_000_000, measure=True)
    assert first["idle_cpu_pct"] == 70.0 and "70% busy" in first["warn"]["why"]
    load[0] = 12.0  # the busy program was closed
    again = pc.check("HD", 10_000_000, measure=False)  # only what is kept
    assert again["idle_cpu_pct"] == 12.0 and again["warn"] is None
    assert again["decode_speed"] == 4.0 and again["measured_utc"] == first["measured_utc"]


def test_the_advice_fits_the_quality(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GAP-113: an HD plan is not told to "test in HD"."""
    pc = PerfCheck(tmp_path, tmp_path / "procs", vainfo=lambda a: None, idle_cpu=lambda: 90.0)
    kept = {"state": "ready", "hw_decode": None, "decode_speed": 3.0, "measured_utc": "t"}
    assert "in HD" not in pc._judge("HD", kept)["warn"]["fix"]
    assert "in HD" in pc._judge("UHD", kept)["warn"]["fix"]


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_cancel_stops_a_sample_being_made(tmp_path: Path) -> None:
    pc = PerfCheck(tmp_path, tmp_path / "procs", sample_s=600)  # long on purpose
    assert pc.check("UHD", 35_000_000, measure=True)["state"] == "making_sample"
    end = time.monotonic() + 10
    while not pc._running and time.monotonic() < end:
        time.sleep(0.05)
    assert pc.busy()
    pc.cancel()
    end = time.monotonic() + 15
    while pc.busy() and time.monotonic() < end:
        time.sleep(0.1)
    assert not pc.busy() and not pc.sample("UHD", 35_000_000).exists()
    assert not list((tmp_path / "perf").glob("*.part")) and not list(
        (tmp_path / "procs").glob("*.json")
    )
    assert perf.MIN_SPEED == 1.5


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="vainfo is Linux")
def test_fast_enough_decoding_needs_no_vainfo_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GAP-161 (2026-10-16): the T460 decoded the UHD sample at 1.77x and mpv plays it in
    hardware, yet vainfo listed no HEVC decoder and every start warned "too slow"."""
    pc = PerfCheck(tmp_path, tmp_path / "procs", vainfo=lambda a: VAINFO, idle_cpu=lambda: 10.0)
    monkeypatch.setattr(pc, "_run", lambda *a, **k: 0)
    (tmp_path / "perf").mkdir()
    (tmp_path / "perf" / "decode_UHD.log").write_text("speed=1.77x")
    r = pc._judge("UHD", pc._measure("UHD", tmp_path / "x.ts"))
    assert r["hw_decode"] is False and r["decode_speed"] == 1.77 and r["warn"] is None


def test_how_busy_is_read_before_the_decode_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-16: the T460 (1.84x, VA-API HEVC) warned "already busy" at every start;
    the load was read right after the decode test, which used every core."""
    load = [10.0]
    pc = PerfCheck(tmp_path, tmp_path / "procs", vainfo=lambda a: REAL_VAINFO,
                   idle_cpu=lambda: load[0])  # fmt: skip

    def decode(*a: object, **k: object) -> int:
        load[0] = 95.0  # the decode test itself
        return 0

    monkeypatch.setattr(pc, "_run", decode)
    (tmp_path / "perf").mkdir()
    (tmp_path / "perf" / "decode_UHD.log").write_text("speed=1.84x")
    pc.sample("UHD", 35_000_000).write_bytes(b"x")
    r = pc.check("UHD", 35_000_000, measure=True)
    assert r["idle_cpu_pct"] == 10.0 and r["warn"] is None


def test_a_gpu_the_node_cannot_open_falls_back_to_the_cpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-16 T460: the node's user is not in "render"; -hwaccel vaapi exited at once
    and the check said "did not finish". The CPU decode then gives the speed."""
    pc = PerfCheck(tmp_path, tmp_path / "procs", vainfo=lambda a: REAL_VAINFO,
                   idle_cpu=lambda: 10.0)  # fmt: skip
    (tmp_path / "perf").mkdir()
    log = tmp_path / "perf" / "decode_UHD.log"
    runs: list[list[str]] = []

    def run(name: str, argv: list[str], path: Path, limit: float) -> int:
        runs.append(argv)
        hw = "-hwaccel" in argv
        path.write_text("No device available for decoder" if hw else "speed=1.84x")
        return 1 if hw else 0

    monkeypatch.setattr(pc, "_run", run)
    r = pc._judge("UHD", pc._measure("UHD", tmp_path / "x.ts"))
    assert len(runs) == 2 and "-hwaccel" not in runs[1] and log.exists()
    assert r["decode_speed"] == 1.84 and r["warn"] is None
