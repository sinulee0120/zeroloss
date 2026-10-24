"""Frame marker (docs/modules/iptv.md 3.4, V12): burnt in by the prepare command, read
back from every decoded frame, also from a small capture; never guessed from noise."""

from __future__ import annotations

import random
import subprocess
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from modules.iptv.marker import Marker, cell_px, read
from modules.iptv.sender.commands import QUALITIES, prepare_command


def gray_frames(ts: Path, size: tuple[int, int] | None = None) -> list[bytes]:
    vf = ["-vf", f"scale={size[0]}:{size[1]}"] if size else []
    raw = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(ts), *vf,
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    w, h = size or tuple(int(v) for v in QUALITIES["HD"].size.split("x"))
    n = w * h
    return [raw[i : i + n] for i in range(0, len(raw) - n + 1, n)]


@pytest.fixture(scope="module")
def hd_ch2(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("marker") / "ch2.ts"
    cmd = prepare_command("ffmpeg", QUALITIES["HD"], 2, out, seconds=3, preset="ultrafast")
    subprocess.run(cmd, check=True)
    return out


def test_cell_sizes() -> None:
    assert cell_px(2160) == 30 and cell_px(1080) == 16


def test_every_frame_reads_back_in_order(hd_ch2: Path) -> None:
    frames = gray_frames(hd_ch2)
    assert len(frames) == 90
    assert [read(f, 1920, 1080) for f in frames] == [Marker(2, i) for i in range(90)]


def test_small_capture_still_reads(hd_ch2: Path) -> None:
    frames = gray_frames(hd_ch2, (640, 360))
    got = [read(f, 640, 360, source_height=1080) for f in frames]
    assert got == [Marker(2, i) for i in range(len(frames))]


def test_uhd_reads(tmp_path: Path) -> None:
    out = tmp_path / "uhd.ts"
    cmd = prepare_command("ffmpeg", QUALITIES["UHD"], 3, out, seconds=0.5, preset="ultrafast")
    subprocess.run(cmd, check=True)
    frames = gray_frames(out, (1280, 720))
    got = [read(f, 1280, 720, source_height=2160) for f in frames]
    assert got == [Marker(3, i) for i in range(len(frames))], _why(out, got)


def _why(ts: Path, got: list[Marker | None]) -> str:
    """What a failure needs to be understood (seen only on CI so far): the frame types
    in the file and the ffmpeg/x265 build that made it."""
    types = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "frame=pict_type", "-of", "csv=p=0", str(ts)],
        capture_output=True, text=True,
    ).stdout.split()  # fmt: skip
    version = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout
    return f"read {[m.frame if m else None for m in got]} types {types} {version.splitlines()[0]}"


def test_no_marker_is_none() -> None:
    assert read(bytes(1920 * 1080), 1920, 1080) is None  # all black
    assert read(b"\xff" * (1920 * 1080), 1920, 1080) is None  # all white
    assert read(b"", 1920, 1080) is None
    assert read(bytes(64 * 36), 64, 36) is None  # cells too small to read


def test_broken_end_reference_is_none(hd_ch2: Path) -> None:
    frame = bytearray(gray_frames(hd_ch2)[10])
    c, y0 = 16, 1080 - 32
    for y in range(y0, y0 + c):  # paint the last (white) reference cell black
        for x in range(c * 22, c * 23):
            frame[y * 1920 + x] = 0
    assert read(bytes(frame), 1920, 1080) is None


@settings(max_examples=40, deadline=None)
@given(st.integers(0, 2**32 - 1))
def test_random_pictures_never_crash(seed: int) -> None:
    rng = random.Random(seed)
    w, h = rng.choice([(1920, 1080), (640, 360), (320, 180)])
    got = read(rng.randbytes(w * h), w, h)
    assert got is None or (0 <= got.ch < 4 and 0 <= got.frame < 65536)
