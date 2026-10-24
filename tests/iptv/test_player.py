"""PC player and its own report (docs/modules/iptv.md 3.1, 3.2, 3.6; 09 RCV-003/019/020).

Unit part: the per-second value from reports (fake clock). Real part: VLC plays our own
RTP multicast on loopback (--miface=lo, 15 3.4) without a window; the frame number of
the scene filter proves progress; a killed VLC is restarted; Stop leaves nothing.
"""

from __future__ import annotations

import itertools
import os
import random
import shutil
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from modules.iptv.receiver import player as pl
from modules.iptv.receiver.player import PlayerConfig, VlcPlayer
from modules.iptv.sender.commands import QUALITIES, prepare_command, send_command

HAVE = bool(shutil.which("cvlc") and shutil.which("tsp") and shutil.which("ffmpeg"))
GROUP, PORT = "239.255.96.1", 5004


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def fake(tmp_path: Path) -> tuple[VlcPlayer, Clock]:
    c = Clock()
    p = VlcPlayer(PlayerConfig("rtp://@x:1", tmp_path), lambda *a: None, clock=c)
    return p, c


def set_report(p: VlcPlayer, **kw: Any) -> None:
    with p._lock:
        for k, v in kw.items():
            setattr(p._r, k, v)


def test_values_per_second(tmp_path: Path) -> None:
    p, c = fake(tmp_path)
    assert p.take_second().pb == "none"  # nothing yet
    p._apply(True, None, 10, c.t)
    assert p.take_second().pb == "ok"
    c.t += pl.FREEZE_SCENE_S
    p._apply(True, None, 10, c.t)  # the reader sees no new picture for the freeze time
    s = p.take_second()
    assert (s.pb, s.freeze_started) == ("freeze", True)
    c.t += 1
    p._apply(True, None, 10, c.t)
    assert p.take_second().freeze_started is False  # the same freeze
    c.t += pl.NONE_S
    p._apply(True, None, 10, c.t)
    assert p.take_second().pb == "none"
    p._apply(True, None, 40, c.t)
    assert p.take_second().pb == "ok"
    set_report(p, frame=50, advanced_at=c.t, restarts=1)
    s = p.take_second()
    assert (s.pb, s.restarted) == ("restart", True)
    assert s.record_keys() == {"pb": "restart", "prs": 1}


def status_xml(shown: int, read_bytes: int) -> str:
    return (
        f"<demuxreadbytes>{read_bytes}</demuxreadbytes><displayedpictures>{shown}"
        "</displayedpictures><lostpictures>0</lostpictures><lostabuffers>0</lostabuffers>"
    )


def play(tmp_path: Path, read_every: float, gap: tuple[float, float] | None = None,
         stats: bool = True, seconds: int = 300, read_from: float = 0.37) -> list[str]:  # fmt: skip
    """30 fps pictures (none inside ``gap``), a scene picture per 10 frames, read every
    ``read_every`` s, judged on the analyzer's own 1 s grid: the pb of each second."""
    p, c = fake(tmp_path)
    start = c.t
    reads = (start + read_from + k * read_every for k in itertools.count())
    seconds_at = (start + k for k in itertools.count(1))
    nxt_read, nxt_second = next(reads), next(seconds_at)
    out: list[str] = []
    while len(out) < seconds:
        if nxt_read < nxt_second:
            c.t = nxt_read
            t = c.t - start
            shown = sum(1 for f in range(int(t * 30) + 1)
                        if not (gap and gap[0] <= f / 30 < gap[1]))  # fmt: skip
            xml = status_xml(shown, int(t * 1_250_000)) if stats else None
            p._apply(True, xml, shown // pl.SCENE_RATIO, c.t)
            nxt_read = next(reads)
        else:
            c.t = nxt_second
            out.append(p.take_second().pb)
            nxt_second = next(seconds_at)
    return out


@pytest.mark.parametrize("stats", [True, False])
def test_a_reader_off_the_second_grid_makes_no_false_freeze(tmp_path: Path, stats: bool) -> None:
    """GAP-114 (real runs: one "freeze" second every ~43 s): the old reader worked, then
    waited 1 s (period 1.023 s) against the 1.000 s judging grid. Now neither that slow
    reader nor the real one finds a freeze in 300 s of clean playback."""
    for every in (1.023, pl.READ_EVERY_S, 0.25, 0.977):
        pbs = play(tmp_path, every, stats=stats)
        assert pbs.count("ok") == 300, (every, [i for i, b in enumerate(pbs) if b != "ok"])


def test_a_stall_between_two_seconds_is_still_a_freeze(tmp_path: Path) -> None:
    """A 0.6 s still picture that begins and ends between two judgements counts once."""
    pbs = play(tmp_path, pl.READ_EVERY_S, gap=(100.2, 100.8), seconds=110)
    assert pbs.count("freeze") == 1 and pbs[100] == "freeze", [
        i for i, b in enumerate(pbs) if b != "ok"
    ]


def test_a_hold_shorter_than_the_freeze_time_is_not_a_freeze(tmp_path: Path) -> None:
    pbs = play(tmp_path, pl.READ_EVERY_S, gap=(50.1, 50.3), seconds=60)
    assert pbs.count("ok") == 60  # 0.2 s: below iptv 3.6's 0.5 s


def test_statistics_only_when_they_move(tmp_path: Path) -> None:
    """RTP on VLC 3.0.20: frozen statistics mean "not measured", never 0 (iptv 3.2)."""
    p, c = fake(tmp_path)
    set_report(p, running=True, frame=10, advanced_at=c.t, lost_pictures=0, lost_abuffers=0)
    p.take_second()
    set_report(p, frame=20, advanced_at=c.t, lost_pictures=3, lost_abuffers=1)
    s = p.take_second()
    assert (s.pb, s.dropped, s.audio_lost) == ("ok", None, None)  # stats not alive
    set_report(p, frame=30, advanced_at=c.t, lost_pictures=5, stats_alive=True)
    s = p.take_second()
    assert (s.pb, s.dropped, s.audio_lost) == ("drop", 2, 0)
    assert s.record_keys() == {"pb": "drop", "pdr": 2, "pau": 0}  # measured: 0 written (02 7)


def test_random_reports_keep_the_rules(tmp_path: Path) -> None:
    """Monkey: pb is always a known value; none when not running; a freeze start is
    reported once per freeze; no freeze while every reading brings a new picture."""
    for seed in range(20):
        rnd = random.Random(seed)
        p, c = fake(tmp_path)
        frame, shown, stalled = 0, 0, False
        for _ in range(300):
            fresh = True
            for _ in range(rnd.choice([1, 4, 4, 8])):  # readings in this second
                c.t += rnd.choice([0.2, 0.25, 0.25, 1.0, 3.0])
                running = rnd.random() > 0.05
                if rnd.random() < 0.7:
                    frame, shown = frame + 1, shown + 10
                else:
                    fresh = False
                stats = status_xml(shown, int(c.t * 1000)) if rnd.random() < 0.5 else None
                p._apply(running, stats, frame, c.t)
            set_report(p, restarts=p._r.restarts + (rnd.random() < 0.05))
            s = p.take_second()
            assert s.pb in pl.ORDER
            if not p._r.running:
                assert s.pb == "none"
            if fresh and p._r.running and p._r.stalls == p._taken.stalls:
                assert s.pb != "freeze"
            now_stalled = s.pb in ("freeze", "none")
            assert s.freeze_started == (now_stalled and not stalled)
            stalled = now_stalled


def test_vlc_arguments() -> None:
    cfg = PlayerConfig("rtp://@239.1.1.1:5004", Path("/w"), nic="eth1", buffer_ms=1500)
    argv = pl.vlc_argv(cfg, 1234, "pw", Path("/w/scene"))
    assert "--miface=eth1" in argv and "--network-caching=1500" in argv
    assert "--width=1280" in argv and argv[-1] == "rtp://@239.1.1.1:5004"
    full = pl.vlc_argv(PlayerConfig("u", Path("/w"), window="Full screen"), 1, "p", Path("/s"))
    assert "--fullscreen" in full
    head = pl.vlc_argv(PlayerConfig("u", Path("/w"), headless=True), 1, "p", Path("/s"))
    assert "--vout=dummy" in head and not any(a.startswith("--width") for a in head)


# ---------------------------------------------------------------- real VLC


@pytest.fixture(scope="module")
def stream(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    if not HAVE:
        pytest.skip("needs cvlc, tsp and ffmpeg")
    hd = tmp_path_factory.mktemp("player") / "hd.ts"
    subprocess.run(
        prepare_command("ffmpeg", QUALITIES["HD"], 1, hd, seconds=4, preset="ultrafast"),
        check=True,
        capture_output=True,
        timeout=300,
    )
    tx = subprocess.Popen(
        send_command("tsp", hd, GROUP, PORT, 10_000_000, 77, ttl=1, local_address="127.0.0.1"),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    yield
    tx.terminate()
    tx.wait(10)


def vlc_pids() -> set[int]:
    """Our players only: a VLC process (by name) started with the scene filter. Matching
    the command line alone also matched a shell that merely mentioned the option."""
    out = subprocess.run(["pgrep", "-x", "vlc"], capture_output=True, text=True)
    pids = set()
    for x in out.stdout.split():
        try:
            if b"--scene-prefix=f" in Path(f"/proc/{x}/cmdline").read_bytes():
                pids.add(int(x))
        except OSError:
            continue  # ended meanwhile
    return pids


def player_pids() -> set[int]:
    """Our players of either kind (S-26): VLC with the scene filter, or mpv with our window
    title (GAP-152)."""
    pids = vlc_pids()
    out = subprocess.run(["pgrep", "-x", "mpv"], capture_output=True, text=True)
    for x in out.stdout.split():
        try:
            if b"--title=ZeroLoss player" in Path(f"/proc/{x}/cmdline").read_bytes():
                pids.add(int(x))
        except OSError:
            continue
    return pids


def wait(cond: Any, timeout_s: float) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


def test_real_vlc_plays_restarts_and_stops(stream: None, tmp_path: Path) -> None:
    before = vlc_pids()
    events: list[tuple[str, str, str]] = []
    cfg = PlayerConfig(f"rtp://@{GROUP}:{PORT}", tmp_path, nic="lo", headless=True)
    p = VlcPlayer(cfg, lambda *e: events.append(e))
    p.start()
    try:
        assert wait(lambda: p.first_frame, pl.FIRST_FRAME_S), "no first frame (RCV-019)"
        # VLC shows its first picture, then holds it ~1 s while the buffer fills (seen
        # with the 0.1 s reader, GAP-114); the warm-up waits past that (05 2.1-2)
        time.sleep(2.5)
        p.take_second()
        time.sleep(2.2)
        assert p.take_second().pb == "ok"  # pictures kept coming
        assert len(list((tmp_path / "scene").glob("f*.png"))) <= 2  # old pictures removed
        (pid,) = vlc_pids() - before
        os.kill(pid, signal.SIGKILL)  # the player dies (RCV-003)
        assert wait(lambda: p._r.restarts == 1 and p.first_frame, 15)
        assert any(code == "ZL-RCV-020" for code, _, _ in events)
        seen = {p.take_second().pb}
        time.sleep(2.2)
        seen.add(p.take_second().pb)
        assert "ok" in seen or "restart" in seen
    finally:
        p.stop()
    assert p.flow.state.value == "IDLE"
    assert vlc_pids() - before == set()  # nothing left (12 4-3)


def test_real_vlc_without_a_stream_never_plays(tmp_path: Path, monkeypatch: Any) -> None:
    """RCV-019/020: no stream -> no first frame -> none; restarts stop at the limit."""
    if not shutil.which("cvlc"):
        pytest.skip("needs cvlc")
    monkeypatch.setattr(pl, "FIRST_FRAME_S", 2.0)
    monkeypatch.setattr(pl, "RESTARTS_PER_HOUR", 1)
    before = vlc_pids()
    events: list[tuple[str, str, str]] = []
    cfg = PlayerConfig("rtp://@239.255.96.9:5004", tmp_path, nic="lo", headless=True)
    p = VlcPlayer(cfg, lambda *e: events.append(e))
    p.start()
    try:
        assert wait(lambda: p._r.gave_up, 20)
        assert not p.first_frame
        assert p.take_second().pb == "none"
        assert [c for c, lv, _ in events if lv == "error"] == ["ZL-RCV-020"]
    finally:
        p.stop()
    assert vlc_pids() - before == set()


def test_real_vlc_paused_is_freeze_then_none_then_restarted(
    stream: None, tmp_path: Path, monkeypatch: Any
) -> None:
    """10 "재생기는 있는데 재생 안 함" ①: SIGSTOP -> freeze, then none, then restart."""
    monkeypatch.setattr(pl, "NONE_S", 4.0)
    before = vlc_pids()
    cfg = PlayerConfig(f"rtp://@{GROUP}:{PORT}", tmp_path, nic="lo", headless=True)
    p = VlcPlayer(cfg, lambda *e: None)
    p.start()
    try:
        assert wait(lambda: p.first_frame, pl.FIRST_FRAME_S)
        time.sleep(1.5)
        p.take_second()
        (pid,) = vlc_pids() - before
        os.kill(pid, signal.SIGSTOP)
        seen = []
        end = time.monotonic() + 20
        while time.monotonic() < end and p._r.restarts == 0:
            time.sleep(1.0)
            seen.append(p.take_second().pb)
        assert "freeze" in seen and "none" in seen, seen
        assert p._r.restarts == 1 and pid not in vlc_pids()  # the stopped one is gone
        assert wait(lambda: p.first_frame, pl.FIRST_FRAME_S)
    finally:
        p.stop()
    assert vlc_pids() - before == set()


@pytest.mark.parametrize(("code", "expect"), [(0, "ZL-RCV-004"), (-9, "ZL-RCV-020")])
def test_a_closed_window_is_told_apart_from_a_crash(tmp_path: Path, code: int,
                                                    expect: str) -> None:  # fmt: skip
    """09 RCV-004: the player ended normally without being asked (its window closed) ->
    "the player is part of the test", opened again; a crash stays RCV-020."""
    from core.blocks.process_runner import ProcState

    events: list[tuple[str, str, str]] = []
    p = VlcPlayer(PlayerConfig("rtp://@239.255.9.9:5004", tmp_path, headless=True),
                  lambda *e: events.append(e))  # fmt: skip

    class Ended:
        state = ProcState.FAULT
        exit_code = code

        def poll(self) -> ProcState:
            return ProcState.FAULT

        def stop(self) -> None:
            return None

    launched: list[int] = []
    p._runner = Ended()  # type: ignore[assignment]
    p._launch = lambda: launched.append(1)  # type: ignore[method-assign]
    p._restart()
    assert [c for c, _, _ in events] == [expect] and launched == [1]
    assert p._r.restarts == 1  # counted like any restart (limit, prs)


def test_no_packets_no_restart_count_and_back_again(tmp_path: Path) -> None:
    """GAP-181 (09 RCV-020): the router went off and on; 80 s without packets made three
    restarts and left the player stopped although the stream came back."""
    events: list[str] = []
    c = Clock()
    p = VlcPlayer(PlayerConfig("rtp://@x:1", tmp_path), lambda code, *a: events.append(code),
                  clock=c)  # fmt: skip
    launched: list[int] = []
    p._launch = lambda: launched.append(1)  # type: ignore[method-assign]
    p._end_process = lambda: None  # type: ignore[method-assign]
    set_report(p, running=True, frame=5, advanced_at=c.t)
    c.t += pl.NONE_S + 1
    assert p._needs_restart()  # packets come, no new picture: the player's fault
    p.data_flowing(False)
    assert not p._needs_restart()  # no packets: a still picture is expected
    set_report(p, running=False)
    for _ in range(pl.RESTARTS_PER_HOUR + 2):  # the process ends again and again
        assert p._needs_restart()
        p._restart()
    assert not p._r.gave_up and len(p._restart_times) == 0  # none counted
    p.data_flowing(True)
    for _ in range(pl.RESTARTS_PER_HOUR):
        p._restart()
    p._restart()  # with packets coming the limit holds
    assert p._r.gave_up and events[-1] == "ZL-RCV-020"
    assert not p._needs_restart()
    p.data_flowing(False)
    p.data_flowing(True)  # packets are back: started again, the count from zero
    assert not p._r.gave_up and events[-1] == "ZL-INFO"
    p._restart()
    assert len(p._restart_times) == 1 and not p._r.gave_up
    p.data_flowing(True)  # no change: nothing happens
    assert events.count("ZL-INFO") == 1
