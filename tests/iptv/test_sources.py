"""S-48 media folder list (iptv 1-1): names, K-28 fit, unreadable files."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from modules.iptv.sender.sources import digest, fits, list_sources, safe_name


def test_names_are_files_in_the_folder_only() -> None:
    for ok in ("movie.mp4", "시험 영상 (1).mp4", "a..b.mkv"):
        assert safe_name(ok), ok
    for bad in ("", ".", "..", "../x.mp4", "a/b.mp4", "a\\b.mp4", "x\0.mp4", None, 3):
        assert not safe_name(bad), bad


def test_fit_per_quality() -> None:
    small = fits({"width": 1280, "height": 720, "fps": 25.0, "error": None})
    assert small["HD"]["upscaled"] and small["UHD"]["upscaled"]
    assert "25 fps becomes 30 fps" in small["HD"]["why"]
    uhd = fits({"width": 3840, "height": 2160, "fps": 29.97, "error": None})
    assert uhd["UHD"] == {"ok": True, "upscaled": False, "why": None}
    broken = fits({"error": "no video stream"})
    assert broken["HD"] == {"ok": False, "upscaled": False, "why": "no video stream"}


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="needs ffprobe")
def test_list_skips_other_files_and_reports_unreadable_ones(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "broken.mp4").write_bytes(b"not a video")
    r = list_sources(tmp_path)
    assert [f["name"] for f in r["files"]] == ["broken.mp4"]
    assert r["files"][0]["error"] and not r["files"][0]["fits"]["HD"]["ok"]
    before = digest(tmp_path / "broken.mp4")
    (tmp_path / "broken.mp4").write_bytes(b"changed!!!!")
    assert digest(tmp_path / "broken.mp4") != before  # a changed original is made again
    assert list_sources(tmp_path / "none") == {"folder": str(tmp_path / "none"), "files": []}


def test_a_copied_original_keeps_its_digest_a_changed_one_does_not(tmp_path: Path) -> None:
    """2026-10-17 user: the offline kit carries the original and the UHD file made from
    it ("이미 만든걸 또 만들필요없게"); copied to another PC it has another name and change
    time. Its digest is what is in it."""
    import os

    a = tmp_path / "greenland2.mp4"
    a.write_bytes(os.urandom(9 * 1024 * 1024))
    b = tmp_path / "sample_1_original.mp4"
    b.write_bytes(a.read_bytes())
    os.utime(b, ns=(1_000_000_000, 1_000_000_000))
    assert digest(b) == digest(a)
    data = bytearray(a.read_bytes())
    data[-1] ^= 0xFF  # the end changed
    b.write_bytes(bytes(data))
    assert digest(b) != digest(a)
