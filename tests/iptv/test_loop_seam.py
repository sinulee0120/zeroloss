"""Loop seams must never create errors, however often they happen (docs/modules/iptv.md 4.2,
docs/10-verification.md 5-1). A short file looped many times packs seams close together.

Uses TSDuck's own continuity checker as an independent judge, then feeds the looped
stream through our analyzer.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from core.blocks.ts_analyzer import TsAnalyzer
from modules.iptv.sender.commands import QUALITIES, prepare_command, send_command

REPO = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    not (shutil.which("tsp") and shutil.which("ffmpeg")), reason="needs ffmpeg and tsp"
)


@pytest.fixture(scope="module")
def short_file(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 2 s HD file: looping it N times gives N-1 seams in a few seconds."""
    out = tmp_path_factory.mktemp("seam") / "short.ts"
    subprocess.run(
        prepare_command("ffmpeg", QUALITIES["HD"], 1, out, seconds=2, preset="ultrafast"),
        check=True,
        timeout=120,
    )
    return out


def test_many_loop_seams_have_no_cc_or_network_errors(short_file: Path, tmp_path: Path) -> None:
    looped = tmp_path / "looped.ts"
    # 12 plays of a 2 s file = 11 seams, joined by the send-time pipeline.
    subprocess.run(
        [
            "tsp",
            "-I",
            "file",
            "--repeat",
            "12",
            str(short_file),
            "-P",
            "continuity",
            "--fix",
            "-P",
            "pcradjust",
            "--bitrate",
            "10000000",
            "-O",
            "file",
            str(looped),
        ],
        check=True,
        timeout=120,
    )
    # Independent judge: TSDuck's own continuity plugin must find nothing.
    out = subprocess.run(
        ["tsp", "-I", "file", str(looped), "-P", "continuity", "-O", "drop"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "packet index" not in out.stderr, out.stderr[:500]

    # Our analyzer: no TR 101 290 Priority 1 errors across every seam.
    a = TsAnalyzer()
    data = looped.read_bytes()
    for off in range(0, len(data) - len(data) % (7 * 188), 7 * 188):
        a.feed(data[off : off + 7 * 188], off)
    assert a.counters.cc_err == 0
    assert a.counters.p1_errors == 0
    assert a.counters.packets > 10000


def test_send_command_keeps_the_seam_plugins() -> None:
    """The sender pipeline must always include the seam fixers (regression guard)."""
    cmd = " ".join(send_command("tsp", Path("x.ts"), "239.1.1.1", 5004, 10_000_000, 1))
    assert "-P continuity --fix" in cmd
    assert "-P pcradjust --bitrate 10000000" in cmd
    assert "tsfixcc" not in cmd  # rejected: breaks ISO 13818-1 (iptv 4.2)
