"""The latest choice wins, nothing else is thrown away (iptv 1-1-2, GAP-116).

The user picked a movie, pressed Start, waited, pressed Stop, pressed Start: 11 minutes
again each time, because Stop and Cancel removed the file being made. Now a request with
``only`` stops just the making the new choice does not need; the rest goes on where it
was. Runs without ffmpeg: making is a fake that moves the percent and obeys cancel.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from core.events.bus import EventBus
from modules.iptv.sender.prepare import Job, Preparer

MUX = 10_000_000


@pytest.fixture
def prep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Preparer:
    gate = threading.Event()  # a test opens it to let the making end

    def fake_make(self: Preparer, job: Job) -> None:
        while not gate.is_set():
            if self._cancel.is_set():
                with self._lock:
                    job.state, job.percent = "cancelled", None
                return
            with self._lock:
                job.percent = min(99.0, (job.percent or 0.0) + 1.0)
            time.sleep(0.01)
        with self._lock:
            job.state, job.percent = "ready", None

    monkeypatch.setattr(Preparer, "_make", fake_make)
    p = Preparer(tmp_path / "cache", EventBus(), tmp_path / "procs")
    p.gate = gate  # type: ignore[attr-defined]
    return p


def movie(tmp_path: Path, name: str) -> Path:
    f = tmp_path / "media" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(name.encode())
    return f


def state(p: Preparer) -> list[tuple[int, str | None, str]]:
    return [(j["ch"], j.get("source"), j["state"]) for j in p.status()["channels"]]


def wait_for(cond: Any, s: float = 5.0) -> None:
    end = time.monotonic() + s
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)


def test_the_same_choice_again_goes_on_where_it_was(prep: Preparer, tmp_path: Path) -> None:
    a = movie(tmp_path, "그린랜드 2.mp4")
    prep.request("HD", MUX, [1], {1: a}, only=True)
    wait_for(lambda: (prep.status()["channels"][0]["percent"] or 0) >= 20)
    before = prep.status()["channels"][0]["percent"]
    assert prep.request("HD", MUX, [1], {1: a}, only=True) == [{"ch": 1, "state": "making"}]
    assert prep.status()["channels"][0]["percent"] >= before  # not made from the start
    prep.gate.set()  # type: ignore[attr-defined]
    wait_for(lambda: not prep.status()["busy"])
    assert [s for _, _, s in state(prep)] == ["ready"]


def test_another_movie_stops_only_the_old_making(prep: Preparer, tmp_path: Path) -> None:
    a, b = movie(tmp_path, "a.mp4"), movie(tmp_path, "b.mp4")
    prep.request("HD", MUX, [1, 2], {1: a, 2: a})
    wait_for(lambda: prep.status()["channels"][0]["state"] == "making")
    # the newest choice: CH1 gets another movie, CH2 keeps its own
    prep.request("HD", MUX, [1, 2], {1: b, 2: a}, only=True)
    names = [(ch, src) for ch, src, _ in state(prep)]
    assert (1, "a.mp4") not in names and (1, "b.mp4") in names and (2, "a.mp4") in names
    assert all(s != "cancelled" for _, _, s in state(prep))
    prep.gate.set()  # type: ignore[attr-defined]
    wait_for(lambda: not prep.status()["busy"])
    assert sorted(state(prep)) == [(1, "b.mp4", "ready"), (2, "a.mp4", "ready")]


def test_without_only_nothing_is_stopped(prep: Preparer, tmp_path: Path) -> None:
    a, b = movie(tmp_path, "a.mp4"), movie(tmp_path, "b.mp4")
    prep.request("HD", MUX, [1], {1: a})
    wait_for(lambda: prep.status()["channels"][0]["state"] == "making")
    prep.request("HD", MUX, [2], {2: b})  # e.g. an old client: queued after the first
    assert [s for _, _, s in state(prep)] == ["making", "queued"]
    prep.gate.set()  # type: ignore[attr-defined]
    wait_for(lambda: not prep.status()["busy"])


def test_the_whole_movie_is_a_file_of_its_own(prep: Preparer, tmp_path: Path) -> None:
    """S-55 (GAP-118): the whole movie and its first 10 minutes are kept apart, each
    made once; switching to the whole movie stops only the 10-minute making."""
    a = movie(tmp_path, "그린랜드 2.mp4")
    prep.request("HD", MUX, [1], {1: a}, only=True)
    wait_for(lambda: prep.status()["channels"][0]["state"] == "making")
    prep.request("HD", MUX, [1], {1: a}, only=True, whole=True)
    (job,) = prep.status()["channels"]
    assert job["whole"] is True and job["source"] == "그린랜드 2.mp4"
    prep.gate.set()  # type: ignore[attr-defined]
    wait_for(lambda: not prep.status()["busy"])
    from modules.iptv.sender.prepare import generated_path, source_tag

    short, long = source_tag(a), source_tag(a, whole=True)
    assert long == short + "_whole"
    assert generated_path(prep.folder, "HD", 1, MUX, short) != generated_path(
        prep.folder, "HD", 1, MUX, long
    )


def test_a_controller_restart_between_tests_keeps_the_making() -> None:
    """GAP-124 (real PCs, 2026-10-09 02:36:35Z): reopening the window restarts the idle
    Controller; the sender's node safe-stopped and cancelled a movie at 45.8 %. Between
    tests a Controller restart leaves the making alone; Reset All still stops it."""
    import threading

    from core.lifecycle.state_flow import CONTROLLER_RESTARTED
    from modules.iptv.module import IptvModule

    class Making:
        cancelled = 0

        def cancel(self) -> list[int]:
            self.cancelled += 1
            return [1]

    m = IptvModule.__new__(IptvModule)
    making = Making()
    m.preparer, m.perf = making, None  # type: ignore[assignment]
    m.senders, m.receivers, m._lock = {}, {}, threading.Lock()
    m.safe_stop(CONTROLLER_RESTARTED)
    assert making.cancelled == 0
    m.safe_stop("Reset All")
    assert making.cancelled == 1


def _made(prep: Preparer, src: Path, tag: str) -> Path:
    """A finished file as the making leaves it: the .ts and its description."""
    import json

    from modules.iptv.sender.prepare import generated_path

    ts = generated_path(prep.folder, "UHD", 1, MUX, tag)
    ts.write_bytes(b"made")
    desc = {"key": prep._key("UHD", 1, MUX, src, tag), "check": [], "bytes": 4}
    ts.with_suffix(".json").write_text(json.dumps(desc), encoding="utf-8")
    return ts


def test_a_file_made_from_a_copy_is_used_on_another_pc(prep: Preparer, tmp_path: Path) -> None:
    """2026-10-17: the kit's UHD file goes with the original (sample_1_original); the
    sender uses it at Start instead of making it again for hours."""
    import os

    from modules.iptv.sender.prepare import source_tag

    here = tmp_path / "greenland2.mp4"
    here.write_bytes(os.urandom(5 * 1024 * 1024))
    made = _made(prep, here, source_tag(here))
    there = tmp_path / "sample_1_original.mp4"
    there.write_bytes(here.read_bytes())
    os.utime(there, ns=(1_000_000_000, 1_000_000_000))
    assert prep.ready("UHD", 1, MUX, there) == made
    assert prep.request("UHD", MUX, [1], {1: there}) == [{"ch": 1, "state": "ready"}]


def test_a_file_made_before_the_content_digest_is_still_used(
    prep: Preparer, tmp_path: Path
) -> None:
    """Made under name + size + change time (the ProBook's UHD files, hours each)."""
    from modules.iptv.sender.prepare import source_tag

    src = tmp_path / "그린랜드 2.mp4"
    src.write_bytes(b"x" * 1000)
    old = _made(prep, src, source_tag(src, legacy=True))
    assert source_tag(src) != source_tag(src, legacy=True)
    assert prep.ready("UHD", 1, MUX, src) == old


def test_the_sender_lists_the_videos_it_has_made(prep: Preparer, tmp_path: Path) -> None:
    """The Source video list marks what starts at once (2026-10-17)."""
    from modules.iptv.sender.prepare import source_tag

    src = tmp_path / "sample_1_original.mp4"
    src.write_bytes(b"y" * 2000)
    _made(prep, src, source_tag(src))
    (got,) = prep.made()
    assert got == {"quality": "UHD", "ch": 1, "mbps": MUX // 1_000_000, "whole": False,
                   "digest": source_tag(src)}  # fmt: skip
    stale = prep.folder / "ch2_UHD_10M_fdeadbeef.json"  # another recipe: made again
    stale.write_text('{"key": {"quality": "UHD", "ch": 2, "muxrate_bps": 10000000, '
                     '"recipe": "old"}, "check": [], "bytes": 0}', encoding="utf-8")  # fmt: skip
    stale.with_suffix(".ts").write_bytes(b"")
    assert len(prep.made()) == 1
