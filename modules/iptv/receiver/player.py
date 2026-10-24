"""PC player (mpv by default, VLC selectable, S-26) and its own report (docs/modules/iptv.md
3.2, 3.6; 05 4.2, 4.6).

mpv (2026-10-10 user, GAP-152): hardware decoding straight into the window (``vaapi`` /
``d3d11va``) that does not give up at the first failed frame (a live join always starts
mid-GOP; mpv's default then stays in software for good): UHD HEVC on the T460's HD 520
went from 318 % to 22 % CPU and a clean picture. Its report comes over its JSON IPC: progress =
``estimated-frame-number``, drops = ``decoder-frame-drop-count`` + ``frame-drop-count``.
Both players: frames the decoder could not decode are counted from the player's own log
(``pde``, pb ``glitch``) — packets can be clean while the picture is broken.
mpv's decoder drops are also kept on their own (``pdd``, part of ``pdr``): a decoder that
falls behind and a picture shown too late are different causes (2026-10-23, GAP-180).
Its log lines carry mpv's own time; Agent Desktop keeps the log after the job ends
(``modules/iptv/desktop.py``) so a run can be looked into afterwards.

The player joins the group with its own socket (05 4.2 method A); nothing is relayed
from the analyzer. Whether it is really playing comes only from the player itself
(RCV-019/020, 20 GAP-20): VLC 3.0.20 does not update its statistics on RTP input, so the
progress is the frame number of VLC's scene filter (a tiny picture every 10 decoded
frames, file name = frame number, decided 2026-09-25, spec gap 38) plus ``<state>`` from
its local HTTP interface. Statistics (lost pictures / audio buffers) are trusted only
while they move; otherwise those counts are "not measured" (None), never 0.

One worker thread owns the process for the whole session: it starts VLC (the process
is bound to that thread, process_runner), reads the report once a second, and restarts
VLC when it died or showed no new frame for ``NONE_S`` (RCV-003/020, at most 3 per
hour). P2 runs this inside the node; P3 moves this part into Agent Desktop (spec gap 39).
State flow IDLE -> STARTING -> RUNNING -> STOPPING -> IDLE, FAULT when the process
could not be confirmed stopped.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.blocks.link_monitor import ipv4_of
from core.blocks.process_runner import ProcessRunner, ProcState, StartError, StopError
from core.lifecycle.state_flow import State, StateFlow
from core.protocol.envelope import ZlError
from modules.iptv.defaults import DEFAULTS

PLAYER_NICE = 10  # RCV-016
NONE_S = 10.0  # no new frame this long (or no first frame): "none", restart (iptv 3.6)
FIRST_FRAME_S = 10.0  # RCV-019
RESTARTS_PER_HOUR = 3  # RCV-003
SCENE_RATIO = 10  # one picture per 10 decoded frames (~3 a second at 30 fps)
READ_EVERY_S = 0.1  # iptv 3.6: on a fixed grid, never "work, then wait" (GAP-114)
STATS_FRESH_S = 1.0  # statistics are "alive" while their byte count moved this recently
FREEZE_S = 0.5  # iptv 3.6: no new displayed picture this long = freeze
FREEZE_SCENE_S = 1.0  # without live statistics: no new scene picture this long
HTTP_TIMEOUT_S = 0.5
FULL_SCREEN = "Full screen"
WINDOW_SIZE = re.compile(r"\((\d+)\D(\d+)\)")  # "Medium (1280x720)" of S-50
SCENE_FILE = re.compile(r"^f(\d+)\.png$")
IPC_TIMEOUT_S = 0.5
MPV_RCVBUF_BYTES = 16 * 1024 * 1024  # its input socket; = rmem_max of the node (06 4.3)
SOFT_FALLBACK_FRAMES = 300  # mpv: failed hardware frames in a row before software (10 s)
# Worst first when several happen in one second (iptv 3.1)
ORDER = ("none", "restart", "freeze", "glitch", "drop", "audio_gap", "ok")


def mpv_binary() -> str:
    if sys.platform == "win32":
        return shutil.which("mpv") or r"C:\Program Files\mpv\mpv.exe"
    return shutil.which("mpv") or "mpv"


def vlc_binary() -> str:
    """cvlc on Linux (no Qt interface), vlc.exe on Windows (V18 checks the path)."""
    if sys.platform == "win32":
        return shutil.which("vlc") or r"C:\Program Files\VideoLAN\VLC\vlc.exe"
    return shutil.which("cvlc") or "cvlc"


@dataclass(frozen=True)
class PlayerConfig:
    url: str  # rtp://@group:port
    work_dir: Path  # scene pictures, run record, log
    nic: str | None = None  # join on this NIC (--miface, 15 3.4)
    buffer_ms: int = 1000  # S-25
    window: str = str(DEFAULTS["S-50"])  # S-50 value as in the table
    headless: bool = False  # automatic tests: no window, no sound
    procs_dir: Path | None = None  # run records; the node's procs dir (12 4-3)
    binary: str = ""  # "": the player's own lookup at use (mpv_binary / vlc_binary)
    kind: str = str(DEFAULTS["S-26"])  # "mpv" | "VLC" (S-26)


@dataclass(frozen=True)
class PlaySecond:
    """One second of playback for the 1-second record (02 7: pb, pdr, pau, pfs, prs, pde, pdd)."""

    pb: str
    dropped: int | None = None  # pdr, None = not measured
    audio_lost: int | None = None  # pau
    freeze_started: bool = False  # pfs
    restarted: bool = False  # prs
    decode_errors: int | None = None  # pde: frames the decoder could not decode
    decoder_dropped: int | None = None  # pdd: the decoder's part of pdr, None = not measured

    def record_keys(self) -> dict[str, object]:
        out: dict[str, object] = {"pb": self.pb}
        if self.dropped is not None:  # written when measured, even 0 (02 7)
            out["pdr"] = self.dropped
        if self.decoder_dropped is not None:
            out["pdd"] = self.decoder_dropped
        if self.audio_lost is not None:
            out["pau"] = self.audio_lost
        if self.freeze_started:
            out["pfs"] = 1
        if self.restarted:
            out["prs"] = 1
        if self.decode_errors:
            out["pde"] = self.decode_errors
        return out


def vlc_argv(cfg: PlayerConfig, port: int, password: str, scene_dir: Path) -> list[str]:
    """Only options checked on VLC 3.0.20 (15 3.4); window size options are ◐ (V18)."""
    argv = [
        cfg.binary or vlc_binary(),
        "-I",
        "dummy",
        "--no-media-library",
        "--no-video-title-show",
    ]
    argv += ["--extraintf", "http", "--http-host", "127.0.0.1", "--http-port", str(port)]
    argv += ["--http-password", password, "--stats"]
    argv += [f"--network-caching={cfg.buffer_ms}"]
    argv += ["--video-filter=scene", "--scene-format=png", f"--scene-ratio={SCENE_RATIO}"]
    argv += ["--scene-width=64", "--scene-height=36", f"--scene-path={scene_dir}"]
    argv += ["--scene-prefix=f"]
    if cfg.nic:
        argv += [f"--miface={cfg.nic}"]
    if cfg.headless:
        argv += ["--vout=dummy", "--aout=dummy"]
    elif cfg.window == FULL_SCREEN:
        argv += ["--fullscreen"]
    elif m := WINDOW_SIZE.search(cfg.window):
        argv += [f"--width={m.group(1)}", f"--height={m.group(2)}"]
    return [*argv, cfg.url]


def mpv_argv(cfg: PlayerConfig, ipc_path: str) -> list[str]:
    """mpv 0.37 (T460, 2026-10-10). The group is joined on the card's address
    (ffmpeg ``localaddr``); warnings go to the log, where decode errors are counted."""
    ip = ipv4_of(cfg.nic) if cfg.nic else None
    if cfg.nic and not ip:  # 2026-10-17: it joined on the home Wi-Fi when the card was gone
        raise OSError(f"the test card {cfg.nic} has no address: the player waits for it")
    url = f"{cfg.url}?localaddr={ip}" if ip else cfg.url
    argv = [cfg.binary or mpv_binary(), "--no-config", "--quiet", "--msg-level=all=warn",
            "--msg-time"]  # fmt: skip  # seconds since mpv started, on every log line (GAP-180)
    # Decode straight into the window: in copy-back mode one failed copy (always at a
    # join) makes mpv give up the hardware decoder at once (T460, 2026-10-10). Software
    # only after SOFT_FALLBACK_FRAMES failed frames in a row: a PC without the hardware.
    hw = "no" if cfg.headless else ("d3d11va" if sys.platform == "win32" else "vaapi")
    argv += [f"--hwdec={hw}", f"--vd-lavc-software-fallback={SOFT_FALLBACK_FRAMES}"]
    argv += ["--cache=yes", f"--cache-secs={cfg.buffer_ms / 1000:g}"]
    # its input socket: ffmpeg's ~384 KB is 0.1 s of UHD (a URL option is not applied)
    argv += [f"--stream-lavf-o=buffer_size={MPV_RCVBUF_BYTES}"]
    argv += [f"--input-ipc-server={ipc_path}", "--title=ZeroLoss player"]
    if cfg.headless:
        argv += ["--vo=null", "--ao=null"]
    elif cfg.window == FULL_SCREEN:
        argv += ["--fs"]
    elif m := WINDOW_SIZE.search(cfg.window):
        argv += [f"--geometry={m.group(1)}x{m.group(2)}"]
    return [*argv, url]


def make_player(cfg: PlayerConfig, on_event: Callable[[str, str, str], None]) -> VlcPlayer:
    """The player S-26 names."""
    return MpvPlayer(cfg, on_event) if cfg.kind == "mpv" else VlcPlayer(cfg, on_event)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _int(xml: str, tag: str) -> int | None:
    m = re.search(rf"<{tag}>([^<]*)</{tag}>", xml)
    if not m:
        return None
    try:
        return int(float(m.group(1)))
    except ValueError:
        return None


@dataclass
class _Report:
    """What the worker last read; only the worker writes it (under the lock)."""

    running: bool = False
    frame: int | None = None  # scene frame number
    advanced_at: float | None = None  # monotonic time the frame number last grew
    shown: int | None = None  # displayed pictures (VLC statistics)
    shown_at: float | None = None  # monotonic time that number last grew
    stalled: bool = False  # no new picture for the freeze time, as of the last read
    stalls: int = 0  # stalls begun: one that started and ended between two seconds counts
    lost_pictures: int | None = None
    decoder_drops: int | None = None  # mpv decoder-frame-drop-count (part of lost_pictures)
    lost_abuffers: int | None = None
    stats_alive: bool = False
    restarts: int = 0
    gave_up: bool = False  # restart limit reached
    decode_errors: int | None = None  # damaged frames in the log so far (None: not read)


class VlcPlayer:
    # player-log lines that mean a frame came out damaged or not at all (iptv 3.1 glitch)
    DAMAGE = re.compile(rb"late video -> dropping frame|Could not find ref with POC")

    def __init__(
        self,
        cfg: PlayerConfig,
        on_event: Callable[[str, str, str], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.flow = StateFlow("player", lambda *a: None)
        self._event = on_event  # (code, level, text)
        self._clock = clock
        self._scene = cfg.work_dir / "scene"
        self._lock = threading.Lock()
        self._r = _Report()
        self._taken = _Report()  # what take_second() last returned from
        self._frozen = False
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self._runner: ProcessRunner | None = None
        self.lowered = False  # RCV-016: kept for a restarted player too
        self._restart_times: deque[float] = deque()
        self._started_at = 0.0
        self._port = 0
        self._auth = ""
        self._last_bytes: int | None = None
        self._bytes_at: float | None = None  # when the statistics byte count last moved
        self._password = ""
        self._log_at = 0  # how far the player log is read (damage lines)
        self._flowing: bool | None = None  # packets coming, as the node says (None: unknown)
        self._rearm = False

    # ---------------------------------------------------------------- commands

    def start(self) -> None:
        """Returns at once; the first frame is waited for by the warm-up gate."""
        self.flow.move(State.STARTING, reason="launch player")
        self._halt.clear()
        self._started_at = self._clock()
        self._thread = threading.Thread(target=self._work, name="iptv-player", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop and confirm the process is gone (12 4-3), then IDLE."""
        self.flow.move(State.STOPPING, reason="stop")
        self._halt.set()
        if self._thread is not None:
            self._thread.join(30)
        ok = self._thread is None or not self._thread.is_alive()
        self.flow.move(State.IDLE if ok and not self._stuck() else State.FAULT, reason="stopped")

    @property
    def first_frame(self) -> bool:
        with self._lock:
            return self._r.frame is not None

    def lower_priority(self) -> bool:
        """05 4.2-1 RCV-016: the measurement comes first when the PC is saturated —
        the player gives way (nice +10). Lowering needs no special right, raising the
        analyzer would (CAP_SYS_NICE). Linux; returns False when it could not."""
        self.lowered = True
        runner = self._runner
        return runner is not None and self._nice(runner)

    @staticmethod
    def _nice(runner: ProcessRunner) -> bool:
        if runner.pid is None or not hasattr(os, "setpriority"):
            return False  # Windows: BELOW_NORMAL priority class comes with V18
        try:
            os.setpriority(os.PRIO_PROCESS, runner.pid, PLAYER_NICE)
        except OSError:
            return False
        return True

    def data_flowing(self, flowing: bool) -> None:
        """GAP-181 (09 RCV-020): the node says whether packets come. While none come a
        still picture is expected: no restart for it, and a restart then (the process
        ended) is not counted. A player left stopped starts again when packets return."""
        with self._lock:
            back = flowing and self._flowing is False
            self._flowing = flowing
            if not (back and self._r.gave_up):
                return
            self._r.gave_up = False
            self._rearm = True  # the worker starts the count again (_restart)
        self._event("ZL-INFO", "info", "Packets are back — starting the player again")

    def take_second(self) -> PlaySecond:
        """The playback value of the second since the last call (iptv 3.1, 3.6)."""
        now = self._clock()
        with self._lock:
            r, before = self._r, self._taken
            self._taken = _Report(**r.__dict__)
        restarted = r.restarts > before.restarts
        dropped = audio = None
        if r.stats_alive and before.lost_pictures is not None and r.lost_pictures is not None:
            dropped = max(0, r.lost_pictures - before.lost_pictures)
        ddrop = None
        if r.stats_alive and before.decoder_drops is not None and r.decoder_drops is not None:
            ddrop = max(0, r.decoder_drops - before.decoder_drops)
        if r.stats_alive and before.lost_abuffers is not None and r.lost_abuffers is not None:
            audio = max(0, r.lost_abuffers - before.lost_abuffers)
        damaged = None
        if r.decode_errors is not None:
            damaged = max(0, r.decode_errors - (before.decode_errors or 0))
        if not r.running or r.gave_up or r.frame is None:
            pb = "none"
        elif r.stalled or r.stalls > before.stalls:  # the reader's own finding (GAP-114)
            since = now - (r.advanced_at or now)
            pb = "none" if r.stalled and since >= NONE_S else "freeze"
        elif restarted:
            pb = "restart"
        elif damaged:
            pb = "glitch"
        elif dropped:
            pb = "drop"
        elif audio:
            pb = "audio_gap"
        else:
            pb = "ok"
        if restarted and pb in ("ok", "glitch", "drop", "audio_gap", "freeze"):
            pb = "restart"  # worse than a freeze (ORDER): a new player holds its first picture
        stalled = pb in ("freeze", "none")
        started = stalled and not self._frozen
        self._frozen = stalled
        return PlaySecond(pb, dropped, audio, started, restarted, damaged,
                          ddrop if dropped is not None else None)  # fmt: skip

    # ---------------------------------------------------------------- worker

    def _work(self) -> None:
        try:
            self._launch()
            with contextlib.suppress(ZlError):  # a Stop may have come first
                self.flow.move(State.RUNNING, reason="player launched", expect=State.STARTING)
            due = self._clock() + READ_EVERY_S
            while not self._halt.wait(max(0.0, due - self._clock())):
                self._read()
                if self._needs_restart():
                    self._restart()
                due += READ_EVERY_S  # a fixed grid: the read's own time is not added
                if due < self._clock():
                    due = self._clock() + READ_EVERY_S  # fell behind: no burst of reads
        except Exception as e:  # never let the player take the measurement down
            self._event("ZL-RCV-003", "error", f"Player stopped unexpectedly: {e!r}")
        finally:
            self._end_process()

    def _launch(self) -> None:
        self._scene.mkdir(parents=True, exist_ok=True)
        for f in self._scene.glob("f*.png"):
            f.unlink(missing_ok=True)
        log = self.cfg.work_dir / "player.log"
        try:
            self._log_at = log.stat().st_size  # a restarted player appends: count anew
        except OSError:
            self._log_at = 0
        runner = ProcessRunner(
            f"player_{self.cfg.work_dir.name}",  # one record per receiver (12 4-3)
            self._command(),
            self.cfg.procs_dir or self.cfg.work_dir,
            log_path=log,
            stop_timeout_s=5.0,
        )
        try:
            runner.start(ready=self._ready, ready_timeout_s=10.0)
        except StartError as e:
            self._event("ZL-RCV-019", "error", f"Player did not start: {e}")
            with self._lock:
                self._r.running = False
            return
        self._runner = runner
        if self.lowered:
            self._nice(runner)
        self._last_bytes = self._bytes_at = None
        with self._lock:
            self._r.running = True
            self._r.frame = None
            self._r.advanced_at = self._clock()
            self._r.shown = self._r.shown_at = None  # a new VLC counts from 0
            self._r.stalled = False

    def _command(self) -> list[str]:
        self._port = _free_port()
        self._password = secrets.token_hex(8)
        self._auth = "Basic " + base64.b64encode(f":{self._password}".encode()).decode()
        return vlc_argv(self.cfg, self._port, self._password, self._scene)

    def _ready(self) -> bool:
        return self._status() is not None

    def _scan_log(self) -> None:
        """Count new damage lines of the player's log (it only grows while it runs)."""
        path = self.cfg.work_dir / "player.log"
        try:
            with path.open("rb") as f:
                f.seek(self._log_at)
                data = f.read()
        except OSError:
            return
        cut = data.rfind(b"\n") + 1  # a half-written last line waits for the next read
        found = len(self.DAMAGE.findall(data[:cut]))
        self._log_at += cut
        with self._lock:
            self._r.decode_errors = (self._r.decode_errors or 0) + found

    def _status(self) -> str | None:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self._port}/requests/status.xml",
            headers={"Authorization": self._auth},
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                return str(resp.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def _newest_frame(self) -> int | None:
        """Newest scene picture number; older pictures are removed (VLC closed them)."""
        found: list[tuple[int, Path]] = []
        try:
            for entry in self._scene.iterdir():
                m = SCENE_FILE.match(entry.name)
                if m:
                    found.append((int(m.group(1)), entry))
        except OSError:
            return None
        if not found:
            return None
        found.sort()
        for _, old in found[:-1]:
            old.unlink(missing_ok=True)
        return found[-1][0]

    def _read(self) -> None:
        runner = self._runner
        alive = runner is not None and runner.poll() is ProcState.RUNNING
        xml = self._status() if alive else None
        frame = self._newest_frame() if alive else None
        self._apply(alive, xml, frame, self._clock())
        self._scan_log()

    def _apply(self, alive: bool, xml: str | None, frame: int | None, now: float) -> None:
        """One reading into the report, and whether playback stands still (iptv 3.6)."""
        with self._lock:
            r = self._r
            r.running = alive  # the process; HTTP only adds statistics
            if frame is not None and frame != r.frame:
                r.frame, r.advanced_at = frame, now
            if xml is None:
                r.stats_alive = False  # not read: the displayed count is not known now
            else:
                read_bytes = _int(xml, "demuxreadbytes")
                moved = (
                    read_bytes is not None
                    and self._last_bytes is not None
                    and (read_bytes != self._last_bytes)
                )  # needs two readings to know
                if moved:
                    self._bytes_at = now
                self._last_bytes = read_bytes
                r.stats_alive = self._bytes_at is not None and now - self._bytes_at < (
                    STATS_FRESH_S)  # fmt: skip
                r.lost_pictures = _int(xml, "lostpictures")
                r.lost_abuffers = _int(xml, "lostabuffers")
                shown = _int(xml, "displayedpictures")
                if shown is not None and shown != r.shown:
                    r.shown, r.shown_at = shown, now
            stalled = _stands_still(r, now)
            if stalled and not r.stalled:
                r.stalls += 1
            r.stalled = stalled

    def _needs_restart(self) -> bool:
        with self._lock:
            r = self._r
            if r.gave_up:
                return False
            if not r.running:
                return True
            if self._flowing is False:  # no packets: a still picture is not the player's
                return False
            limit = NONE_S if r.frame is not None else FIRST_FRAME_S
            return r.advanced_at is not None and self._clock() - r.advanced_at >= limit

    def _restart(self) -> None:
        now = self._clock()
        with self._lock:
            rearm, self._rearm = self._rearm, False
        if rearm:
            self._restart_times.clear()
        while self._restart_times and now - self._restart_times[0] > 3600:
            self._restart_times.popleft()
        silent = self._flowing is False  # a restart while no packets come is not counted
        if not silent and len(self._restart_times) >= RESTARTS_PER_HOUR:
            with self._lock:
                self._r.gave_up = True
            self._event(
                "ZL-RCV-020",
                "error",
                f"Player is not playing and was restarted {RESTARTS_PER_HOUR} times this hour"
                " — left stopped",
            )
            return
        if not silent:
            self._restart_times.append(now)
        runner = self._runner
        closed = runner is not None and runner.poll() is ProcState.FAULT and runner.exit_code == 0
        if closed:  # it ended normally without being asked: its window was closed (RCV-004)
            self._event("ZL-RCV-004", "warn",
                        "Player window was closed — opening it again (the player is part of "
                        "the test)")  # fmt: skip
        else:
            self._event("ZL-RCV-020", "warn", "Player is not playing — restarting it")
        self._end_process()
        if self._halt.is_set():
            return
        self._launch()
        with self._lock:
            self._r.restarts += 1

    def _end_process(self) -> None:
        runner, self._runner = self._runner, None
        if runner is None or runner.state not in (ProcState.RUNNING, ProcState.FAULT):
            return
        try:
            runner.stop()
        except StopError as e:
            self._event("ZL-SYS-006", "error", f"Player did not stop: {e}")
            self._left = runner

    _left: ProcessRunner | None = None

    def _stuck(self) -> bool:
        return self._left is not None


def _stands_still(r: _Report, now: float) -> bool:
    """iptv 3.6 (GAP-114): no new picture for the freeze time, by the displayed-picture
    count while VLC's statistics move (every frame), else by the scene number."""
    if not r.running or r.frame is None:
        return False  # "none" is told by take_second
    if r.stats_alive and r.shown_at is not None:
        return now - r.shown_at >= FREEZE_S
    return r.advanced_at is not None and now - r.advanced_at >= FREEZE_SCENE_S


class MpvIpc:
    """mpv's JSON IPC: AF_UNIX socket (Linux) or named pipe (Windows, ◐ V18). One reader
    thread hands each answer to the request that waits for it; events are skipped. Any
    failure closes it: the next call connects again."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._stream: Any = None
        self._closers: tuple[Any, ...] = ()
        self._next = 0
        self._waiting: dict[int, tuple[threading.Event, list[Any]]] = {}

    def get(self, names: tuple[str, ...]) -> dict[str, Any] | None:
        """Property values (None for one mpv cannot give yet), or None: no answer."""
        try:
            with self._lock:
                stream = self._stream or self._open()
                asks = []
                for name in names:
                    self._next += 1
                    slot: tuple[threading.Event, list[Any]] = (threading.Event(), [])
                    self._waiting[self._next] = slot
                    asks.append((name, slot))
                    cmd = {"command": ["get_property", name], "request_id": self._next}
                    stream.write(json.dumps(cmd).encode() + b"\n")
                stream.flush()
            out = {}
            for name, (done, box) in asks:
                if not done.wait(IPC_TIMEOUT_S):
                    raise TimeoutError(name)
                msg = box[0]
                out[name] = msg.get("data") if msg.get("error") == "success" else None
            return out
        except (OSError, ValueError, TimeoutError):
            self.close()
            return None

    def _open(self) -> Any:
        """The raw stream for writing; the reader thread reads lines through a buffer."""
        if sys.platform == "win32":
            raw = open(self.path, "r+b", buffering=0)  # noqa: SIM115 (kept open)
            closers: tuple[Any, ...] = (raw,)
        else:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(IPC_TIMEOUT_S)
            try:
                sock.connect(self.path)
            except OSError:
                sock.close()
                raise
            sock.settimeout(None)
            raw = sock.makefile("rwb", buffering=0)
            closers = (raw, sock)
        self._stream, self._closers = raw, closers
        lines = io.BufferedReader(raw)
        threading.Thread(target=self._reader, args=(lines,), name="mpv-ipc", daemon=True).start()
        return raw

    def _reader(self, lines: Any) -> None:
        try:
            for line in lines:
                msg = json.loads(line)
                slot = self._waiting.pop(msg.get("request_id"), None) if "request_id" in msg \
                    else None  # fmt: skip
                if slot is not None:
                    slot[1].append(msg)
                    slot[0].set()
        except (OSError, ValueError):
            pass

    def close(self) -> None:
        with self._lock:
            closers, self._closers = self._closers, ()
            self._stream = None
            self._waiting.clear()
        for c in closers:
            with contextlib.suppress(OSError):
                c.close()


def ipc_path(work_dir: Path) -> str:
    """A short socket name (AF_UNIX allows 108 bytes; job folders are longer)."""
    name = f"zl-mpv-{os.getpid()}-{secrets.token_hex(4)}"
    if sys.platform == "win32":
        return rf"\\.\pipe\{name}"
    return str(Path(tempfile.gettempdir()) / f"{name}.sock")


class MpvPlayer(VlcPlayer):
    """mpv over its IPC (GAP-152): the frame number moves with every shown frame, so a
    freeze is told at FREEZE_S; drops are mpv's own counters."""

    DAMAGE = re.compile(rb"Error while decoding frame")
    PROPS = ("estimated-frame-number", "decoder-frame-drop-count", "frame-drop-count",
             "hwdec-current")  # fmt: skip

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self._ipc: MpvIpc | None = None
        self.hwdec: str | None = None  # what mpv decodes with ("vaapi-copy", "no", ...)

    def _command(self) -> list[str]:
        self._close_ipc()
        path = ipc_path(self.cfg.work_dir)
        self._ipc = MpvIpc(path)
        return mpv_argv(self.cfg, path)

    def _ready(self) -> bool:
        return self._ipc is not None and self._ipc.get(("pid",)) is not None

    def _read(self) -> None:
        runner, ipc = self._runner, self._ipc
        alive = runner is not None and runner.poll() is ProcState.RUNNING
        got = ipc.get(self.PROPS) if alive and ipc is not None else None
        self._apply_mpv(alive, got, self._clock())
        self._scan_log()

    def _apply_mpv(self, alive: bool, got: dict[str, Any] | None, now: float) -> None:
        frame = got.get("estimated-frame-number") if got else None
        drops = [got.get(k) for k in ("decoder-frame-drop-count", "frame-drop-count")] \
            if got else []  # fmt: skip
        if got:
            self.hwdec = got.get("hwdec-current")
        with self._lock:
            r = self._r
            r.running = alive
            r.stats_alive = isinstance(frame, int)
            if isinstance(frame, int) and frame != r.frame:
                r.frame, r.advanced_at = frame, now
                r.shown, r.shown_at = frame, now
            if drops and all(isinstance(d, int) for d in drops):
                r.lost_pictures = sum(int(d) for d in drops if d is not None)
                r.decoder_drops = drops[0]  # decoder-frame-drop-count (GAP-180)
            r.lost_abuffers = None  # mpv has no lost-audio counter: not measured
            stalled = _stands_still(r, now)
            if stalled and not r.stalled:
                r.stalls += 1
            r.stalled = stalled

    def _end_process(self) -> None:
        super()._end_process()
        self._close_ipc()

    def _close_ipc(self) -> None:
        ipc, self._ipc = self._ipc, None
        if ipc is not None:
            ipc.close()
            if not ipc.path.startswith("\\\\"):
                with contextlib.suppress(OSError):
                    os.unlink(ipc.path)
