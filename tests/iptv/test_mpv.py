"""mpv as the PC player (S-26 default, GAP-152): its arguments, its report over JSON IPC,
the decode errors of both players' logs (pb glitch, pde), and a real mpv."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from modules.iptv.receiver import player as pl
from modules.iptv.receiver.player import MpvIpc, MpvPlayer, PlayerConfig, VlcPlayer, make_player
from modules.iptv.sender.commands import QUALITIES, prepare_command, send_command
from tests.iptv.test_player import Clock, player_pids, wait

HAVE = bool(shutil.which("mpv") and shutil.which("tsp") and shutil.which("ffmpeg"))
GROUP, PORT = "239.255.96.2", 5006


def test_mpv_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pl, "ipv4_of", lambda nic: {"eth1": "192.168.45.177"}.get(nic))
    cfg = PlayerConfig("rtp://@239.1.1.1:5004", Path("/w"), nic="eth1", buffer_ms=1500,
                       binary="mpv")  # fmt: skip
    argv = pl.mpv_argv(cfg, "/tmp/zl.sock")
    assert argv[-1] == "rtp://@239.1.1.1:5004?localaddr=192.168.45.177"  # joins on that card
    assert f"--stream-lavf-o=buffer_size={pl.MPV_RCVBUF_BYTES}" in argv  # 16 MB input
    assert "--input-ipc-server=/tmp/zl.sock" in argv and "--cache-secs=1.5" in argv
    hw = [a for a in argv if a.startswith("--hwdec=")]
    assert hw == ["--hwdec=vaapi"] or hw == ["--hwdec=d3d11va"]  # into the window, no copy
    assert f"--vd-lavc-software-fallback={pl.SOFT_FALLBACK_FRAMES}" in argv
    assert "--msg-time" in argv  # every log line has mpv's time (GAP-180)
    assert "--geometry=1280x720" in argv and "--title=ZeroLoss player" in argv
    full = pl.mpv_argv(PlayerConfig("u", Path("/w"), window="Full screen"), "/s")
    assert "--fs" in full and full[-1] == "u"  # no card: the OS picks it
    head = pl.mpv_argv(PlayerConfig("u", Path("/w"), headless=True), "/s")
    assert {"--vo=null", "--ao=null", "--hwdec=no"} <= set(head)
    assert len(pl.ipc_path(Path("/very/long/" + "x" * 200))) < 100  # AF_UNIX allows 108


def test_the_player_s26_names() -> None:
    def no(*a: Any) -> None:
        return None

    assert type(make_player(PlayerConfig("u", Path("/w")), no)) is MpvPlayer  # default
    assert type(make_player(PlayerConfig("u", Path("/w"), kind="VLC"), no)) is VlcPlayer


def fake_mpv(tmp_path: Path) -> tuple[MpvPlayer, Clock]:
    c = Clock()
    p = MpvPlayer(PlayerConfig("rtp://@x:1", tmp_path), lambda *a: None, clock=c)
    return p, c


def got(frame: int | None, dec: int = 0, vo: int = 0) -> dict[str, Any]:
    return {"estimated-frame-number": frame, "decoder-frame-drop-count": dec,
            "frame-drop-count": vo, "hwdec-current": "vaapi"}  # fmt: skip


def test_mpv_report_per_second(tmp_path: Path) -> None:
    p, c = fake_mpv(tmp_path)
    p._apply_mpv(True, got(None), c.t)
    assert p.take_second().pb == "none" and not p.first_frame  # no picture yet
    for f in range(10, 40):  # a frame every 1/30 s
        c.t += 1 / 30
        p._apply_mpv(True, got(f), c.t)
    s = p.take_second()
    assert (s.pb, s.dropped, s.audio_lost) == ("ok", 0, None)  # mpv has no audio count
    assert p.first_frame and p.hwdec == "vaapi"
    p._apply_mpv(True, got(41, dec=2, vo=1), c.t)
    s = p.take_second()
    assert (s.pb, s.dropped, s.decoder_dropped) == ("drop", 3, 2)  # 2 decoder + 1 output
    assert s.record_keys() == {"pb": "drop", "pdr": 3, "pdd": 2}
    c.t += pl.FREEZE_S
    p._apply_mpv(True, got(41, dec=2, vo=1), c.t)  # no new frame for the freeze time
    s = p.take_second()
    assert (s.pb, s.freeze_started) == ("freeze", True)
    c.t += 0.1
    p._apply_mpv(True, got(42, dec=2, vo=1), c.t)
    s = p.take_second()
    assert s.pb == "ok" and s.record_keys() == {"pb": "ok", "pdr": 0, "pdd": 0}  # measured 0
    p._apply_mpv(True, None, c.t)  # no IPC answer: the counts are not measured
    s = p.take_second()
    assert s.dropped is None and s.decoder_dropped is None
    assert s.record_keys() == {"pb": s.pb}  # not measured: no pdr, no pdd
    p._apply_mpv(False, None, c.t)
    assert p.take_second().pb == "none"


def test_decoder_errors_from_the_log_are_glitch(tmp_path: Path) -> None:
    p, c = fake_mpv(tmp_path)
    log = tmp_path / "player.log"
    p._apply_mpv(True, got(10), c.t)
    c.t += 0.1
    p._apply_mpv(True, got(13), c.t)
    p.take_second()
    log.write_bytes(b"[ffmpeg/video] hevc: Could not find ref with POC 8\n"
                    b"Error while decoding frame (hardware decoding)!\n"
                    b"Error while decoding frame!\nError while deco")  # fmt: skip
    p._scan_log()
    c.t += 0.1
    p._apply_mpv(True, got(16), c.t)
    s = p.take_second()
    assert (s.pb, s.decode_errors) == ("glitch", 2)  # the half line waits
    assert s.record_keys() == {"pb": "glitch", "pdr": 0, "pdd": 0, "pde": 2}  # 0 = measured
    with log.open("ab") as f:
        f.write(b"ding frame!\n")
    p._scan_log()
    c.t += 0.1
    p._apply_mpv(True, got(19), c.t)
    assert p.take_second().decode_errors == 1
    p._scan_log()
    c.t += 0.1
    p._apply_mpv(True, got(22), c.t)
    s = p.take_second()
    assert (s.pb, s.decode_errors) == ("ok", 0) and "pde" not in s.record_keys()


def test_vlc_counts_its_own_damage_lines(tmp_path: Path) -> None:
    p = VlcPlayer(PlayerConfig("rtp://@x:1", tmp_path), lambda *a: None, clock=Clock())
    (tmp_path / "player.log").write_bytes(
        b"[00007dc29c0051d0] avcodec decoder error: more than 5 seconds of late video -> "
        b"dropping frame (computer too slow ?)\n"
        b"[hevc @ 0x7dc29c0455c0] Could not find ref with POC 18\n"
        b"[00007dc274006920] main filter error: Failed to create video converter\n"
    )  # T460, 2026-10-10, UHD HEVC in software
    p._scan_log()
    assert p._r.decode_errors == 2


class FakeMpv:
    """An mpv IPC end: answers get_property by request_id, sends events in between."""

    def __init__(self, path: str, values: dict[str, Any]) -> None:
        self.path, self.values = path, values
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(2)
        self.conns: list[socket.socket] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            self.conns.append(conn)
            threading.Thread(target=self._talk, args=(conn,), daemon=True).start()

    def _talk(self, conn: socket.socket) -> None:
        with conn, conn.makefile("rb") as lines:
            for line in lines:
                cmd = json.loads(line)
                name = cmd["command"][1]
                conn.sendall(b'{"event":"audio-reconfig"}\n')
                if name in self.values:
                    ans = {"data": self.values[name], "error": "success"}
                else:
                    ans = {"error": "property unavailable"}
                conn.sendall(json.dumps({**ans, "request_id": cmd["request_id"]}).encode() + b"\n")

    def close(self) -> None:
        self.srv.close()
        for c in self.conns:
            with contextlib.suppress(OSError):
                c.shutdown(socket.SHUT_RDWR)  # its reader holds the file: close is not enough
            c.close()


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="mpv IPC here is a Unix socket")
def test_ipc_answers_skips_events_and_comes_back(tmp_path: Path) -> None:
    path = str(tmp_path / "m.sock")
    ipc = MpvIpc(path)
    assert ipc.get(("pid",)) is None  # nothing listening yet
    srv = FakeMpv(path, {"pid": 42, "estimated-frame-number": 7})
    try:
        assert ipc.get(("pid", "estimated-frame-number", "hwdec-current")) == {
            "pid": 42, "estimated-frame-number": 7, "hwdec-current": None}  # fmt: skip
        srv.values["estimated-frame-number"] = 8
        assert ipc.get(("estimated-frame-number",)) == {"estimated-frame-number": 8}
        srv.close()  # mpv went away
        assert wait(lambda: ipc.get(("pid",)) is None, 3)
        os.unlink(path)
        srv = FakeMpv(path, {"pid": 43})  # a restarted mpv on the same name
        assert ipc.get(("pid",)) == {"pid": 43}
    finally:
        ipc.close()
        srv.close()


# ---------------------------------------------------------------- real mpv


@pytest.fixture(scope="module")
def stream(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    if not HAVE:
        pytest.skip("needs mpv, tsp and ffmpeg")
    hd = tmp_path_factory.mktemp("mpv") / "hd.ts"
    subprocess.run(prepare_command("ffmpeg", QUALITIES["HD"], 1, hd, seconds=4,
                                   preset="ultrafast"),
                   check=True, capture_output=True, timeout=300)  # fmt: skip
    tx = subprocess.Popen(
        send_command("tsp", hd, GROUP, PORT, 10_000_000, 78, ttl=1, local_address="127.0.0.1"),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    yield
    tx.terminate()
    tx.wait(10)


def test_real_mpv_plays_freezes_restarts_and_stops(
    stream: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The group joined on lo (localaddr), pictures every second, SIGSTOP -> freeze then
    none, a new mpv after it, nothing left after stop (12 4-3)."""
    monkeypatch.setattr(pl, "NONE_S", 4.0)
    before = player_pids()
    events: list[tuple[str, str, str]] = []
    cfg = PlayerConfig(f"rtp://@{GROUP}:{PORT}", tmp_path, nic="lo", headless=True)
    p = MpvPlayer(cfg, lambda *e: events.append(e))
    p.start()
    try:
        assert wait(lambda: p.first_frame, pl.FIRST_FRAME_S), "no first frame (RCV-019)"
        time.sleep(2.5)  # past the join (its decode errors belong to the warm-up)
        p.take_second()
        time.sleep(2.2)
        assert p.take_second().pb == "ok"
        (pid,) = player_pids() - before
        os.kill(pid, signal.SIGSTOP)
        seen = []
        end = time.monotonic() + 20
        while time.monotonic() < end and p._r.restarts == 0:
            time.sleep(1.0)
            seen.append(p.take_second().pb)
        assert "freeze" in seen and "none" in seen, seen
        assert p._r.restarts == 1 and pid not in player_pids()
        assert any(code == "ZL-RCV-020" for code, _, _ in events)
        assert wait(lambda: p.first_frame, pl.FIRST_FRAME_S)
    finally:
        p.stop()
    assert p.flow.state.value == "IDLE"
    assert player_pids() - before == set()
    assert not list(Path("/tmp").glob(f"zl-mpv-{os.getpid()}-*.sock"))  # its socket removed


def test_desktop_keeps_the_player_log(tmp_path: Path) -> None:
    """GAP-180: the log outlives its job folder; only the newest LOGS_KEPT stay."""
    from modules.iptv import desktop

    for i in range(desktop.LOGS_KEPT + 2):
        job = tmp_path / "jobs" / f"R{i:02d}_ch1_player"
        job.mkdir(parents=True)
        (job / "player.log").write_text(f"run {i}\n", encoding="utf-8")
        os.utime(job / "player.log", (1000 + i, 1000 + i))
        desktop.keep_log(job)
        assert not (job / "player.log").exists()
    kept = sorted(p.name for p in (tmp_path / "player-logs").glob("*.log"))
    assert kept == [f"R{i:02d}_ch1_player.log" for i in range(2, desktop.LOGS_KEPT + 2)]
    desktop.keep_log(tmp_path / "jobs" / "never-started")  # no log: nothing happens
