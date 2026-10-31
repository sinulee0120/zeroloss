"""Generated videos on the sender (docs/modules/iptv.md 1-1-2, 4.3, 09 SND-020, USR-006):
made one at a time, checked before use, cancel and failures leave nothing half made."""

from __future__ import annotations

import itertools
import json
import random
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from core.events.bus import Event, EventBus
from modules.iptv.sender.commands import QUALITIES, prepare_command, send_command
from modules.iptv.sender.prepare import FRAME_S, Preparer, check_prepared, generated_path
from modules.iptv.sender.sources import av_end_gap
from tests.conftest import _ours_alive

HD = QUALITIES["HD"]
MUX = HD.muxrate_bps
needs_tsp = pytest.mark.skipif(shutil.which("tsp") is None, reason="needs tsp (the send chain)")
pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None,
    reason="needs ffmpeg (making is real here; test_prepare_only.py runs without)",
)


@pytest.fixture(scope="module")
def good(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("check") / "ch2.ts"
    subprocess.run(prepare_command("ffmpeg", HD, 2, out, seconds=3, preset="ultrafast"), check=True)
    return out


@pytest.fixture
def bus() -> Iterator[tuple[EventBus, list[Event]]]:
    b = EventBus()
    seen: list[Event] = []
    b.subscribe(seen.append)
    yield b, seen


def make(tmp_path: Path, bus: EventBus, **kw: Any) -> Preparer:
    kw.setdefault("seconds", 2.0)
    kw.setdefault("preset", "ultrafast")
    return Preparer(tmp_path / "data", bus, tmp_path / "procs", **kw)


def wait_idle(p: Preparer, limit: float = 60) -> dict[str, Any]:
    end = time.monotonic() + limit
    while time.monotonic() < end:
        st = p.status()
        if not st["busy"]:
            return st
        time.sleep(0.1)
    raise AssertionError(f"still busy: {p.status()}")


def leftovers(p: Preparer) -> list[str]:
    return sorted(x.name for x in p.folder.iterdir() if not x.name.endswith((".ts", ".json")))


# ---------------------------------------------------------------- 4.3 check


def test_good_file_passes_the_check(good: Path) -> None:
    assert check_prepared(good, MUX, 2) == []


def test_wrong_channel_or_rate_is_caught(good: Path) -> None:
    assert any("service/PIDs" in w for w in check_prepared(good, MUX, 1))
    assert any("bitrate" in w for w in check_prepared(good, 12_000_000, 2))


def test_a_lost_packet_is_caught(good: Path, tmp_path: Path) -> None:
    from modules.iptv.sender.commands import FIRST_ES_PID

    data = good.read_bytes()
    # a video packet (a null packet has no continuity counter to break): the first one
    # from packet 5000 on, whatever the encoder put where
    n = next(i for i in range(5000, len(data) // 188)
             if ((data[188 * i + 1] & 0x1F) << 8) | data[188 * i + 2] == FIRST_ES_PID)  # fmt: skip
    cut = tmp_path / "cut.ts"
    cut.write_bytes(data[: 188 * n] + data[188 * (n + 1) :])  # one packet missing
    assert any("priority 1" in w for w in check_prepared(cut, MUX, 2))


def movie(tmp: Path, video_s: float, audio_s: float) -> Path:
    """A user's movie whose sound does not end with its picture (a cut clip)."""
    src = tmp / f"movie_{video_s:g}_{audio_s:g}.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size=640x360:rate=25:duration={video_s}", "-f", "lavfi", "-i",
                    f"sine=frequency=500:sample_rate=44100:duration={audio_s}",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(src)],
                   check=True)  # fmt: skip
    return src


def looped(tmp: Path, ts: Path, times: int = 3) -> Path:
    """The real send chain (send_command) with only its network output swapped for a file
    and ``--infinite`` for a few repeats: what the receiver gets across the seams."""
    argv = send_command("tsp", ts, "239.255.0.1", 5004, MUX, 1)
    argv.remove("--realtime")
    at = argv.index("--infinite")
    argv[at : at + 1] = ["--repeat", str(times)]
    out = tmp / f"looped_{ts.stem}.ts"
    argv = [*argv[: argv.index("-O")], "-O", "file", str(out)]
    subprocess.run(argv, check=True, capture_output=True, timeout=120)
    return out


def largest_gap(ts: Path, stream: str) -> float:
    done = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries",
                           "packet=pts_time", "-of", "csv=p=0", str(ts)],
                          check=True, capture_output=True, text=True)  # fmt: skip
    times = sorted(float(x.split(",")[0]) for x in done.stdout.split() if x[:1].isdigit())
    return max(b - a for a, b in itertools.pairwise(times))


@needs_tsp
@pytest.mark.parametrize("audio_s", [3.4, 2.6])
def test_the_loop_seam_has_no_picture_gap(tmp_path: Path, audio_s: float) -> None:
    """GAP-114 (the user saw the picture stop at the end of a movie): sound longer or
    shorter than the picture. The made file ends both together, and across 3 loops of the
    real send chain no picture is missing for more than two frames (it was 0.37 s)."""
    out = tmp_path / "ch1.ts"
    subprocess.run(prepare_command("ffmpeg", HD, 1, out, source=movie(tmp_path, 3.0, audio_s),
                                   seconds=600, preset="ultrafast"), check=True)  # fmt: skip
    gap = av_end_gap(out, 3.0)
    assert gap is not None and gap <= FRAME_S, gap
    # one frame + ~20 ms the TS muxer leaves at the end (the same for generated videos);
    # under one frame is GAP-114 3, still open
    assert largest_gap(looped(tmp_path, out), "v:0") <= 2 * FRAME_S


@needs_tsp
def test_a_file_whose_sound_runs_on_is_caught(tmp_path: Path) -> None:
    """The old way (sound 0.4 s longer than the picture) leaves a 0.4 s still picture at
    every seam: the check finds it before the file is kept."""
    old = tmp_path / "old.ts"
    argv = prepare_command("ffmpeg", HD, 1, old, source=movie(tmp_path, 3.0, 3.4),
                           seconds=600, preset="ultrafast")  # fmt: skip
    at = argv.index("apad")
    del argv[at - 1 : at + 2]  # the command before GAP-114: no "-af apad -shortest"
    subprocess.run(argv, check=True)
    gap = av_end_gap(old, 3.0)
    assert gap is not None and gap > 0.3
    assert largest_gap(looped(tmp_path, old), "v:0") > 0.3  # what a person would see


def _strips(ts: Path, out: Path, vf_before: str = "") -> Path:
    """What the 4.3 marker check decodes: one small marker strip per frame."""
    from modules.iptv.marker import strip_filter

    vf = (vf_before + "," if vf_before else "") + strip_filter(1080)
    argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(ts),
            "-map", "0:v:0", "-vf", vf, "-f", "rawvideo", str(out)]  # fmt: skip
    subprocess.run(argv, check=True)
    return out


def test_marker_check_passes_a_good_file_and_catches_wrong_ones(good: Path, tmp_path: Path) -> None:
    """4.3 marker check (20 W-7, GAP-77): every frame must read (channel, 0, 1, 2, ...).
    A frame that shows another frame's number (as CI saw: frame 3 showing 4), a wrong
    channel and an unreadable picture are all caught."""
    from modules.iptv.sender.prepare import marker_problems

    ok = _strips(good, tmp_path / "ok.raw")
    assert marker_problems(ok, 2) == []
    first3 = "frame 0: CH2 #0; frame 1: CH2 #1; frame 2: CH2 #2"
    assert marker_problems(ok, 1) == [f"frame marker wrong in 90 of 90 frames ({first3})"]
    # frame 3 replaced by frame 4 (the CI case): the file order is 0 1 2 4 4 5 ...
    dup = _strips(good, tmp_path / "dup.raw", "select='not(eq(n\\,3))',setpts=N/30/TB")
    raw = dup.read_bytes()
    from modules.iptv.marker import STRIP_H, STRIP_W

    size = STRIP_W * STRIP_H
    dup.write_bytes(raw[: 3 * size] + raw[3 * size : 4 * size] + raw[3 * size :])
    got = marker_problems(dup, 2)
    assert got and got[0].startswith("frame marker wrong in") and "frame 3: CH2 #4" in got[0]
    blank = tmp_path / "blank.raw"
    blank.write_bytes(bytes(size * 2))
    assert marker_problems(blank, 2) == ["frame marker wrong in 2 of 2 frames "
                                         "(frame 0: unreadable; frame 1: unreadable)"]  # fmt: skip
    empty = tmp_path / "empty.raw"
    empty.write_bytes(b"")
    assert marker_problems(empty, 2) == ["frame marker: no frames decoded"]


def test_a_wrong_marker_fails_the_file_and_leaves_nothing(
    tmp_path: Path, bus: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file whose markers are wrong is never kept: SND-020, no file, no temp files."""
    import modules.iptv.sender.prepare as prep

    monkeypatch.setattr(
        prep,
        "marker_problems",
        lambda strips, ch: ["frame marker wrong in 1 of 60 frames (frame 3: CH1 #4)"],
    )
    b, seen = bus
    p = make(tmp_path, b)
    p.request("HD", MUX, [1])
    st = wait_idle(p)
    assert st["channels"][0]["state"] == "failed" and "frame marker" in st["channels"][0]["error"]
    assert p.ready("HD", 1, MUX) is None and leftovers(p) == []
    assert not list(p.folder.glob("*.ts"))
    (ev,) = [e for e in seen if e.code == "ZL-SND-020"]
    assert "frame marker wrong" in ev.text


# ---------------------------------------------------------------- jobs


def test_nothing_is_left_by_the_time_a_failure_or_a_keep_is_published(
    tmp_path: Path, bus: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Looking at the folder the moment a result shows must find it clean (a slow
    machine used to catch the temp files still there): checked inside the event."""
    import modules.iptv.sender.prepare as prep

    b, _ = bus
    p = make(tmp_path, b)
    published: list[tuple[str, list[str]]] = []

    def look(e: Event) -> None:
        if e.code == "ZL-SND-020" or e.text.endswith("video ready"):
            published.append((e.text, leftovers(p)))

    b.subscribe(look)
    monkeypatch.setattr(prep, "marker_problems", lambda strips, ch: ["frame marker wrong"])
    p.request("HD", MUX, [1])
    wait_idle(p)
    monkeypatch.undo()
    p.request("HD", MUX, [2])
    wait_idle(p)
    assert len(published) == 2, published  # one failure, one "ready"
    assert all(left == [] for _, left in published), published


def test_makes_checks_keeps_then_reuses(tmp_path: Path, bus: Any) -> None:
    b, seen = bus
    p = make(tmp_path, b)
    assert p.request("HD", MUX, [1, 3]) == [
        {"ch": 1, "state": "queued"},
        {"ch": 3, "state": "queued"},
    ]
    st = wait_idle(p)
    assert [(c["ch"], c["state"]) for c in st["channels"]] == [(1, "ready"), (3, "ready")]
    for ch in (1, 3):
        path = p.ready("HD", ch, MUX)
        assert path == generated_path(p.folder, "HD", ch, MUX)
        desc = json.loads(path.with_suffix(".json").read_text())
        assert desc["check"] == [] and desc["key"]["ch"] == ch and len(desc["sha256"]) == 64
    assert leftovers(p) == []
    assert p.request("HD", MUX, [1]) == [{"ch": 1, "state": "ready"}]  # not made twice
    assert not any(e.code == "ZL-SND-020" for e in seen)


def test_a_changed_description_means_make_again(tmp_path: Path, bus: Any) -> None:
    p = make(tmp_path, bus[0])
    p.request("HD", MUX, [1])
    wait_idle(p)
    path = p.ready("HD", 1, MUX)
    assert path is not None
    with path.open("ab") as f:
        f.write(b"\x47" + bytes(187))  # size no longer matches the description
    assert p.ready("HD", 1, MUX) is None


def test_a_new_way_of_making_means_make_again(
    tmp_path: Path, bus: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GAP-114: files made before a change of the ffmpeg command (here: sound padded and
    cut with the picture) are made again, not reused, without anyone deleting them."""
    p = make(tmp_path, bus[0])
    p.request("HD", MUX, [1])
    wait_idle(p)
    assert p.ready("HD", 1, MUX) is not None
    monkeypatch.setattr("modules.iptv.sender.prepare.prepare_command",
                        lambda *a, **k: [*prepare_command(*a, **k), "-new"])  # fmt: skip
    assert p.ready("HD", 1, MUX) is None


def test_cancel_removes_the_file_being_made(tmp_path: Path, bus: Any) -> None:
    b, seen = bus
    p = make(tmp_path, b, seconds=120)
    p.request("HD", MUX, [1, 2])
    end = time.monotonic() + 30
    while not (p.status()["channels"][0]["percent"] or 0) > 0:
        assert time.monotonic() < end
        time.sleep(0.1)
    assert p.status()["channels"][0]["left_s"] is not None
    assert p.cancel() == [1, 2]
    st = p.status()
    assert not st["busy"] and {c["state"] for c in st["channels"]} == {"cancelled"}
    assert list(p.folder.iterdir()) == [] and not _ours_alive()
    assert any(e.code == "ZL-INFO" and "cancelled" in e.text for e in seen)


def test_no_progress_fails_and_stops_the_program(tmp_path: Path, bus: Any) -> None:
    b, seen = bus
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\nsleep 30\n")
    fake.chmod(0o755)
    p = make(tmp_path, b, ffmpeg=str(fake), stall_s=1.0)
    p.request("HD", MUX, [1])
    st = wait_idle(p, 20)
    assert st["channels"][0]["state"] == "failed" and "no progress" in st["channels"][0]["error"]
    assert not _ours_alive() and list(p.folder.iterdir()) == []
    assert any(e.code == "ZL-SND-020" and e.level == "error" for e in seen)


def test_not_enough_disk_fails_before_starting(tmp_path: Path, bus: Any) -> None:
    p = make(tmp_path, bus[0], free_mb=lambda _: 10)
    p.request("HD", MUX, [1])
    st = wait_idle(p, 10)
    assert "not enough disk" in st["channels"][0]["error"]
    assert not (tmp_path / "procs" / "prepare_ch1.json").exists()


def test_ffmpeg_error_is_reported(tmp_path: Path, bus: Any) -> None:
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\necho 'Unknown encoder' >&2\nexit 1\n")
    fake.chmod(0o755)
    p = make(tmp_path, bus[0], ffmpeg=str(fake))
    p.request("HD", MUX, [2])
    err = wait_idle(p, 10)["channels"][0]["error"]
    assert "exited with 1" in err and "Unknown encoder" in err


def test_a_left_part_file_is_removed_at_start(tmp_path: Path, bus: Any) -> None:
    folder = tmp_path / "data" / "generated"
    folder.mkdir(parents=True)
    (folder / "ch1_HD_10M.part.ts").write_bytes(b"half")
    p = make(tmp_path, bus[0])
    assert list(p.folder.iterdir()) == []


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_monkey_requests_and_cancels(tmp_path: Path, bus: Any, seed: int) -> None:
    """Random requests, repeats and cancels (playbook): at most one ffmpeg at a time,
    every file present is complete and checked, nothing is left half made."""
    rng = random.Random(seed)
    p = make(tmp_path, bus[0], seconds=1.0)
    for _ in range(12):
        op = rng.choice(["req", "req", "cancel", "wait", "status"])
        if op == "req":
            p.request("HD", MUX, rng.sample([1, 2, 3], rng.randint(1, 3)))
        elif op == "cancel":
            p.cancel()
        elif op == "wait":
            time.sleep(rng.uniform(0, 0.8))
        assert len(_ours_alive()) <= 1
        assert len(p.status()["channels"]) <= 3
    wait_idle(p)
    assert leftovers(p) == [] and not _ours_alive()
    for ts in p.folder.glob("*.ts"):
        ch = int(ts.name[2])
        assert p.ready("HD", ch, MUX) == ts and check_prepared(ts, MUX, ch) == []


def test_the_encoder_speed_does_not_make_a_movie_again(tmp_path: Path) -> None:
    """2026-10-24: moving x265 to ultrafast counted every movie made at veryfast as
    another recipe and the Start began making all three again. Speed is not the stream."""
    from modules.iptv.sender.prepare import Preparer

    fast = Preparer(tmp_path / "a", EventBus(), tmp_path, preset="ultrafast")
    slow = Preparer(tmp_path / "b", EventBus(), tmp_path, preset="veryfast")
    for p in (fast, slow):
        p.seconds = 600.0
    key = {
        "quality": "UHD",
        "ch": 3,
        "muxrate_bps": 35_000_000,
        "source": Path("m.mp4"),
        "tag": "ab12cd34",
    }
    assert fast._key(**key) == slow._key(**key)  # type: ignore[arg-type]
