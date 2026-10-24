"""IPTV PC receiver session: one channel, analyzer side (docs/05-standards.md 2.1-2, 4.2).

Threads (4.2): the receive thread only receives into a buffer; the second thread,
once per second, feeds the analyzer, writes the 1-second record and makes 5-minute
summaries. Nothing is written per packet.

Start: open + join -> warm-up -> RUNNING (t = 0). A failed open/join (RCV-017) or
warm-up (RUN-009) rolls back to IDLE. Stop: close measurement (last second, open gaps
become loss) -> flush summaries -> close the record file -> leave the group -> IDLE.
A crashed thread (RCV-018) saves what it has, closes the files and ends in FAULT.
Playback (VLC, player.py) joins with its own socket; this session never relays packets
(05 4.2). It starts right after the join, the warm-up waits for its first frame
(RCV-019), each second takes the player's own value (iptv 3.1), and it stops after the
measurement is closed and before the group is left (CLAUDE.md stop order).
One session = one run: after it is stopped, a new run makes a new session (a restart
would reuse the record file and restart elapsed time), so a second start is refused.

Channel change (``switch``, 03 F19, 20 GAP-90): the old analysis is closed (a partial last
second is recorded), the player stops, the socket closes (= Leave), a new one opens
(= Join), a new analyzer and a new player start. ``e`` keeps the session's real-time
clock (one tick per second) so the seconds of the change are a gap, not a shift. The
zap's times go to ``<run>_zaps.jsonl`` and its numbers ride on the next 1-second line.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.blocks import wifi_power
from core.blocks.host_counters import Deltas, nic_counters, udp_counters
from core.blocks.leave_watch import FrameSource, LeaveResult, LeaveWatch, PacketSource
from core.blocks.link_monitor import nic_of, read_station
from core.blocks.m2u_check import M2uCounter, is_wireless
from core.blocks.udp_prime import send_from_port
from core.blocks.udp_receiver import UdpReceiver, clock_ns
from core.desktop_jobs import DesktopJobs
from core.events.bus import Event, EventBus, Level
from core.lifecycle.state_flow import State, StateFlow
from core.protocol.envelope import ZlError
from core.records.jsonl import append_jsonl
from core.records.recorder import Recorder, RecordWriteError
from modules.iptv import record_keys as k
from modules.iptv.defaults import DEFAULTS
from modules.iptv.receiver.analyzer import StreamAnalyzer
from modules.iptv.receiver.evidence import Evidence, ffmpeg_render
from modules.iptv.receiver.player import PlayerConfig, VlcPlayer, make_player
from modules.iptv.receiver.remote_player import RemotePlayer
from modules.iptv.receiver.zap import Zap
from modules.iptv.sender.prime import prime_payload, stop_payload
from modules.iptv.summary_rules import make_summarizer, shape
from modules.iptv.warmup import START_MAX_S

CPU_BUSY_PCT = 90.0  # RCV-016 (05 4.2-1)
REJOIN_AFTER_S = 3  # seconds with nothing before the group is joined again
CPU_BUSY_S = 10
DESKTOP_WAIT_S = 5.0  # 03 F20 / RCV-010: Agent Desktop must be there to start playback
PRIME_EVERY_S = 10.0  # before a NAT forgets the path (iptv 4.5, RFC 4787 REQ-5)

NS_PER_S = 1_000_000_000


@dataclass(frozen=True)
class ReceiverConfig:
    run_id: str
    device: str
    ch: int
    group: str
    port: int
    interface_ip: str = "0.0.0.0"
    warmup_max_s: float = START_MAX_S
    cell_s: int = 300
    build: str = ""
    playback: dict[str, Any] | None = None  # {player, buffer_ms, window} or None (S-49 Off)
    # primed mode (iptv 4.5): {"sender_ip", "run_id", "flows": [{"f", "p", "q", "r"}, ...]}
    prime: dict[str, Any] | None = None
    evidence: bool = False  # S-30 On: the picture too (the packet clip is always kept)

    def flow_port(self, f: int) -> int | None:
        for x in (self.prime or {}).get("flows") or []:
            if x.get("f") == f:
                return int(x["r"])
        return None


class ReceiverSession:
    def __init__(
        self,
        cfg: ReceiverConfig,
        data_dir: Path,
        bus: EventBus,
        receiver_factory: Callable[[ReceiverConfig], UdpReceiver] | None = None,
        procs_dir: Path | None = None,
        cpu_of: Callable[[], float | None] = lambda: None,
        desktop: DesktopJobs | None = None,
        held_elsewhere: Callable[[str, int], bool] = lambda group, port: False,
        wifi_of: Callable[[str], dict[str, Any] | None] = lambda ip: None,
        leave_opener: Callable[[str], FrameSource] = PacketSource,
        m2u_opener: Callable[[str, str, int], M2uCounter] = M2uCounter,
        station_of: Callable[[str], dict[str, int]] = read_station,
        nic_reader: Callable[[str], dict[str, int]] = nic_counters,
        udp_reader: Callable[[], dict[str, int]] = udp_counters,
    ) -> None:
        self._held_elsewhere = held_elsewhere  # another receiver of this node has the group
        self._wifi_of = wifi_of  # the node's reading of the receiving card (GAP-104 3)
        self._last_wifi: dict[str, Any] | None = None
        self._leave_opener = leave_opener
        # 02 7 card and host counters (GAP-148): the plan's NIC, never loopback
        self._m2u_opener = m2u_opener
        self._station_of = station_of
        self._nic_reader = nic_reader
        self._udp = Deltas(udp_reader)
        self._nic: str | None = None
        self._power_nic: str | None = None  # Wi-Fi power save held off by us (GAP-167)
        self._silent_s = 0  # seconds in a row with no datagram (rejoin, 2026-10-17)
        self._rejoin_said = False
        self._nic_deltas: Deltas | None = None
        self._m2u: M2uCounter | None = None
        self._station_keys: dict[str, int] = {}  # the last 10 s reading, on the next line
        # 05 2.1-2 (GAP-152): playback has settled — seconds of "ok" in a row, and since when
        # the player shows pictures (a new player starts both again)
        self._ok_run = 0
        self._shown_at: int | None = None
        self._desktop = desktop  # 03 F20: the player runs in Agent Desktop (None: here)
        self._cpu_of = cpu_of  # PC CPU %, once a second (RCV-016)
        self._busy_s = 0
        self.protected_at: float | None = None  # when the player was made to give way
        self.cfg = cfg
        self.slot = cfg.ch  # the channel it started on: the node's key for this receiver
        self._data_dir = data_dir
        self._procs = procs_dir  # the node's run-record folder for the player (12 4-3)
        self.bus = bus
        self.flow = StateFlow(f"receiver {cfg.device}", self._changed)
        self.records_dir = data_dir / "raw" / cfg.device
        self.summaries: list[dict[str, Any]] = []
        self.last_1s: dict[str, Any] | None = None
        self.analyzer: StreamAnalyzer | None = None
        self._factory = receiver_factory or _open_receiver
        self._sock: UdpReceiver | None = None
        self._recorder: Recorder | None = None
        self._summarizer = make_summarizer(cfg.cell_s)
        self._buf: deque[tuple[bytes, int]] = deque()
        self._halt = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()  # summaries / last_1s readers
        self._faulted = False
        self._used = False
        self._told_format = False
        self.player: VlcPlayer | RemotePlayer | None = None
        # one tick = one second; ``e`` of a record = tick - _e0 (the session's real-time clock)
        self._tick_lock = threading.Lock()  # analyzer, socket, cfg and player swaps (G-12)
        self._switch_lock = threading.Lock()  # one channel change at a time
        self._switching = False
        self._tick_n = 0
        self._e0: int | None = None
        self._mono0 = time.monotonic()
        self._lost_carry = 0  # loss of the analyzers already closed (heartbeat lost_total)
        self._rx_stop = threading.Event()  # ends the receive thread of one channel
        self._zap: Zap | None = None
        self._pending_keys: dict[str, int] = {}  # a finished zap's numbers, on the next line
        self._zap_path = self.records_dir / f"{cfg.run_id}_zaps.jsonl"
        self._watchers: list[threading.Thread] = []
        self._told_leave_unchecked = False
        self._success_timeout_s = float(DEFAULTS["S-47"])
        self._evidence: Evidence | None = None  # S-30, made when measuring starts

    @property
    def _player_dir(self) -> Path:
        return self._data_dir / "player" / f"{self.cfg.device}_ch{self.cfg.ch}"

    @property
    def measuring(self) -> bool:
        """Measuring has begun (a channel change in between does not end it)."""
        return self._e0 is not None

    @property
    def switching(self) -> bool:
        return self._switching

    @property
    def lost_total(self) -> int | None:
        """Loss since measuring began, over all channels (heartbeat, 02 5)."""
        if self._e0 is None:
            return None
        a = self.analyzer
        return self._lost_carry + (a.lost_total if a is not None and a.measuring else 0)

    # ---------------------------------------------------------------- commands

    def start(self) -> None:
        if self._used:
            raise ZlError(
                "RUN-012", "This receiver run has ended", "A new run needs a new session."
            )
        self.flow.move(State.STARTING, reason=f"join {self.cfg.group}:{self.cfg.port}")
        self._used = True
        if self.cfg.playback is not None and self._desktop is not None \
                and not self._desktop.wait_connected(DESKTOP_WAIT_S):  # fmt: skip
            self._event("ZL-RCV-010", "error", "Agent Desktop is not running on this PC")
            self.flow.move(State.STOPPING, reason="no Agent Desktop")
            self.flow.move(State.IDLE, reason="no Agent Desktop")
            raise ZlError(
                "RCV-010", "Agent Desktop is not running on this PC",
                "The player needs the screen of a logged-in user (03 F20).",
                "Log in on this PC (automatic login is best), or turn PC playback off (S-49).",
            )  # fmt: skip
        try:
            self._sock = self._factory(self.cfg)
        except OSError as e:
            self._event("ZL-RCV-017", "error", f"Cannot join {self.cfg.group}: {e}")
            self.flow.move(State.STOPPING, reason="join failed")
            self.flow.move(State.IDLE, reason="join failed")
            raise ZlError(
                "RCV-017",
                f"Cannot join {self.cfg.group}:{self.cfg.port}",
                str(e),
                "Check the receive network card and the channel address.",
            ) from e
        if self._sock.rcvbuf_short:
            self._event(
                "ZL-RCV-001",
                "warn",
                f"Receive buffer {self._sock.rcvbuf_applied} B, asked "
                f"{self._sock.rcvbuf_requested} B",
            )
        self._open_counters()
        self.analyzer = StreamAnalyzer(
            self.cfg.ch,
            clock_ns(),
            playback=self.cfg.playback is not None,
            warmup_max_s=self.cfg.warmup_max_s,
        )
        if self.cfg.prime is not None:
            try:
                self._send_primes()  # before anything else: a player prime needs a raw socket
            except OSError as e:
                self._close_socket()
                self.flow.move(State.STOPPING, reason="prime failed")
                self.flow.move(State.IDLE, reason="prime failed")
                self._event("ZL-RCV-024", "error", f"CH{self.cfg.ch} cannot send the prime: {e}")
                raise ZlError(
                    "RCV-024", "Cannot send the prime for the player", str(e),
                    "Run the node as the installed service (CAP_NET_RAW) or turn PC "
                    "playback off (S-49).",
                ) from e  # fmt: skip
        if self.cfg.playback is not None:
            if self._desktop is not None:
                job = f"{self.cfg.run_id}/{self.cfg.device}/ch{self.cfg.ch}/player"  # G-13
                self.player = RemotePlayer(self._player_config(), job, self._desktop,
                                           self._player_event)  # fmt: skip
            else:
                self.player = make_player(self._player_config(), self._player_event)
            self.player.start()
        self._halt.clear()
        self._faulted = False
        self._rx_stop = threading.Event()
        self._threads = [
            threading.Thread(
                target=self._receive_loop, args=(self._sock, self._rx_stop), name="iptv-rx",
                daemon=True,
            ),
            threading.Thread(target=self._second_loop, name="iptv-1s", daemon=True),
        ]  # fmt: skip
        if self.cfg.prime is not None:
            self._threads.append(
                threading.Thread(target=self._prime_loop, name="iptv-prime", daemon=True)
            )
        if self._nic is not None and is_wireless(self._nic):
            # GAP-167: a dozing card has its frames held by the access point, then gets
            # them all at once; a TV box does not doze while it plays
            self._power_nic = self._nic
            what = wifi_power.hold_off(self._nic)
            self._event("ZL-INFO", "info", f"Wi-Fi power save on {self._nic}: {what}")
            self._threads.append(
                threading.Thread(target=self._station_loop, name="iptv-station", daemon=True)
            )
        for t in self._threads:
            t.start()

    def stop(self) -> None:
        """Normal stop, also the cleanup after FAULT. Refused while IDLE or STOPPING."""
        self.flow.move(State.STOPPING, reason="stop")
        self._halt.set()
        with self._switch_lock:  # a channel change under way ends first
            self._shutdown()
            self._finish_files("stop")
            self._stop_player()
            self._close_socket()
        if self._power_nic is not None:  # back as it was (GAP-167)
            wifi_power.release(self._power_nic)
            self._power_nic = None
        self.flow.move(State.IDLE, reason="stopped")

    def take_summaries(self) -> list[dict[str, Any]]:
        with self._lock:
            out, self.summaries = self.summaries, []
            return out

    # ---------------------------------------------------------------- threads

    def _receive_loop(self, sock: UdpReceiver, stop: threading.Event) -> None:
        try:
            while not self._halt.is_set() and not stop.is_set():
                p = sock.receive(0.2)
                if p is not None:
                    self._buf.append((p.data, p.arrival_ns))
        except Exception as e:
            if not stop.is_set():  # a channel change closes its socket after stopping this
                self._crash(e)

    def _second_loop(self) -> None:
        next_tick = time.monotonic() + 1.0
        self._mono0 = next_tick - 1.0
        try:
            while not self._halt.wait(max(0.0, next_tick - time.monotonic())):
                next_tick += 1.0
                self._one_second()
        except RecordWriteError as e:  # 09 RCV-009: a new file failed too -> stop
            self._crash(e, "ZL-RCV-009", f"Cannot write the 1-second record: {e}")
        except Exception as e:
            self._crash(e)

    def _one_second(self, final: bool = False) -> None:
        """Close one second. ``final``: the last, partial second of the analysis (stop or
        channel change); it is the second in progress, so its ``e`` is one more."""
        with self._tick_lock:
            if not final:
                self._tick_n += 1
            a = self.analyzer
            if (self._switching and not final) or a is None or self._sock is None:
                return  # a channel change is between two analyzers
            self._close_second(a, final)

    def _rejoin_when_silent(self, sock: UdpReceiver, got: int) -> None:
        """2026-10-17: the USB Wi-Fi card was moved to another port; the group membership
        went with the old card and the test recorded nothing for 10 minutes while
        RUNNING. Nothing for REJOIN_AFTER_S: join again on the card's address (it may
        come back as a new card); told once when it fails and once when it works."""
        if got:
            if self._silent_s >= REJOIN_AFTER_S:
                self._event("ZL-INFO", "info", f"CH{self.cfg.ch}: the stream comes again",
                            {"clears": ["ZL-NET-015"]})  # fmt: skip
            self._silent_s, self._rejoin_said = 0, False
            return
        self._silent_s += 1
        if self._silent_s % REJOIN_AFTER_S or sock.group is None:
            return
        why = sock.rejoin()
        if why is None:
            self._event("ZL-INFO", "info", f"CH{self.cfg.ch}: nothing for {self._silent_s} s, "
                        f"joined {sock.group} again on {self.cfg.interface_ip}")  # fmt: skip
        elif not self._rejoin_said:
            self._rejoin_said = True
            self._event("ZL-NET-015", "warn", f"CH{self.cfg.ch}: no stream for "
                        f"{self._silent_s} s and the test card cannot join ({why}): "
                        "the card is gone or has no address")  # fmt: skip

    def _close_second(self, a: StreamAnalyzer, final: bool) -> None:
        sock = self._sock
        assert sock is not None
        ev = self._evidence
        got = 0
        while self._buf:
            data, t = self._buf.popleft()
            a.on_datagram(data, t)
            got += 1
            if ev is not None:
                ev.feed(data, t)
        self._rejoin_when_silent(sock, got)
        now = clock_ns()
        if final:
            a.seq.close()
        was_measuring = a.measuring
        self._protect_measurement()
        if self.player is not None:
            self.player.data_flowing(got > 0)  # GAP-181: a still picture without packets
        play = self.player.take_second() if self.player is not None else None
        e0 = self._e0
        counters = self._counter_keys()  # every second, written or not (warm-up)
        rec = a.second(
            now,
            host_drops_total=sock.own_drops(),
            first_frame=self._settled(play) if self.player is not None else None,
            playback=play.record_keys() if play is not None else None,
            elapsed=None if e0 is None else self._tick_n - e0 + (1 if final else 0),
        )
        if not was_measuring and a.measuring:
            if e0 is None:
                self._e0 = self._tick_n + 1
                self._recorder = Recorder(
                    self.records_dir,
                    self.cfg.run_id,
                    self.cfg.device,
                    self.cfg.build,
                    {"ch": self.cfg.ch, "group": f"{self.cfg.group}:{self.cfg.port}"},
                )
                self._evidence = self._make_evidence()
                self._event("ZL-INFO", "info", "Warm-up done, measuring", a.startup.to_dict())
                if self.flow.state is State.STARTING:
                    self.flow.move(State.RUNNING, reason="warm-up done")
            else:
                self._event(
                    "ZL-INFO",
                    "info",
                    f"CH{self.cfg.ch}: measuring again after the channel change",
                    {**a.startup.to_dict(), "ch": self.cfg.ch},
                )
        if a.failed and self.flow.state is State.STARTING:
            self._event(
                "ZL-RUN-009",
                "error",
                f"Start failed: warm-up not done ({', '.join(a.startup.unmet)})",
                a.startup.to_dict(),
            )
            self._halt.set()
            threading.Thread(target=self._rollback, name="iptv-rollback", daemon=True).start()
            return
        if a.udp_ts and not self._told_format:  # RCV-022, once
            self._told_format = True
            self._event(
                "ZL-RCV-022",
                "warn",
                "Stream is MPEG-TS over UDP, but Transport (S-10) is RTP",
                {"fix": "Set the sender to RTP, or change S-10"},
            )
        self._advance_zap(a, play.pb if play is not None else None)
        if rec is not None and self._recorder is not None:
            self._emit(rec | counters)
            if self._evidence is not None:
                self._keep_evidence(self._evidence.on_second(rec, now))

    def _make_evidence(self) -> Evidence:
        """F23 (2026-10-10, GAP-154): the packet clip of each trouble second always; with
        S-30 On also the picture rendered from it."""
        ffmpeg = shutil.which("ffmpeg") if self.cfg.evidence else None
        render = ffmpeg_render(self._procs, ffmpeg) if ffmpeg and self._procs else None
        return Evidence(self.records_dir, self.cfg.run_id, self.cfg.ch, self.cfg.group,
                        self.cfg.port, render=render, clock_ns=clock_ns)  # fmt: skip

    def _keep_evidence(self, written: Path | None) -> None:
        if written is not None:
            self._event("ZL-INFO", "info", f"CH{self.cfg.ch}: evidence kept ({written.name})",
                        {"evidence": written.name})  # fmt: skip

    def _emit(self, rec: dict[str, Any]) -> None:
        if self._pending_keys:  # a finished zap's numbers (02 7)
            rec.update(self._pending_keys)
            self._pending_keys = {}
        rec.update(self._wifi_keys())
        assert self._recorder is not None
        moved = self._recorder.write_second(rec["e"], {k: v for k, v in rec.items() if k != "e"})
        if moved:  # 09 RCV-009: one write error, the record goes on in a new file
            self._event("ZL-RCV-009", "warn", f"Record file write failed, new file: {moved}")
        closed = self._summarizer.add(rec)
        with self._lock:
            self.last_1s = rec
            self.summaries += [self._shape(p) for p in closed]

    def _wifi_keys(self) -> dict[str, Any]:
        """02 7 Wi-Fi keys of the receiving card (wireless only; nothing for wired)."""
        w = self._wifi_of(self.cfg.interface_ip)
        if not w:
            return {}
        out = {key: w.get(src) for key, src in (
            (k.RSSI_DBM, "rssi_dbm"), (k.WIFI_MBPS, "rx_mbps"), (k.WIFI_CH, "channel"),
            (k.WIFI_BW, "width_mhz"), (k.WIFI_BAND, "band"),
        ) if w.get(src) is not None}  # fmt: skip
        last = self._last_wifi
        if last is not None:
            spot = ("channel", "width_mhz", "band")
            if any(w.get(x) != last.get(x) for x in spot):
                out[k.WIFI_CH_CHANGED] = 1
            if w.get("bssid") != last.get("bssid"):
                out[k.WIFI_BSSID_CHANGED] = 1
        self._last_wifi = w
        return out

    def _settled(self, play: Any) -> bool:
        """The warm-up's playback condition: the first picture, then SETTLE_OK_S seconds of
        "ok" in a row (a player's start-up — probing, hardware decoder, its input buffer —
        is a start boundary, not an error), or SETTLE_MAX_S after the first picture at the
        latest (a PC that cannot play cleanly is measured and warned about, not refused)."""
        assert self.player is not None
        if not self.player.first_frame:
            self._ok_run, self._shown_at = 0, None
            return False
        if self._shown_at is None:
            self._shown_at = self._tick_n
        self._ok_run = self._ok_run + 1 if play is not None and play.pb == "ok" else 0
        return self._ok_run >= SETTLE_OK_S or self._tick_n - self._shown_at >= SETTLE_MAX_S

    def _open_counters(self) -> None:
        """02 7 (GAP-148): the card the plan names (none for loopback or an unknown
        address), read once now so the first line has deltas."""
        nic = nic_of(self.cfg.interface_ip)
        self._nic = nic if nic is not None and nic != "lo" else None
        if self._nic is not None:
            card = self._nic
            self._nic_deltas = Deltas(lambda: self._nic_reader(card))
            self._nic_deltas.take()
        self._udp.take()
        self._open_m2u()

    def _open_m2u(self) -> None:
        """``fm``/``fu`` on a Wi-Fi card for the group (not primed: unicast has no group).
        Without permission (CAP_NET_RAW) the keys are absent."""
        if self._nic is None or self.cfg.prime is not None or not is_wireless(self._nic):
            return
        try:
            self._m2u = self._m2u_opener(self._nic, self.cfg.group, self.cfg.port)
        except OSError:
            self._m2u = None

    def _counter_keys(self) -> dict[str, int]:
        out: dict[str, int] = {}
        if self._nic_deltas is not None:
            d = self._nic_deltas.take()
            out |= {key: d[src] for key, src in (
                (k.NIC_PACKETS, "rx_packets"), (k.NIC_MULTICAST, "multicast"),
                (k.NIC_DROPPED, "rx_dropped"), (k.NIC_ERRORS, "rx_errors"),
            ) if src in d}  # fmt: skip
        u = self._udp.take()
        out |= {key: u[src] for key, src in (
            (k.UDP_IN_ERRORS, "InErrors"), (k.UDP_RCVBUF_ERRORS, "RcvbufErrors"),
            (k.UDP_NO_PORTS, "NoPorts"),
        ) if src in u}  # fmt: skip
        if self._m2u is not None:
            m = self._m2u.take()
            out |= {key: m[src] for key, src in (
                (k.M2U_MULTICAST, "fm"), (k.M2U_UNICAST, "fu")) if src in m}  # fmt: skip
        with self._lock:
            out |= self._station_keys
            self._station_keys = {}
        return out

    def _station_loop(self) -> None:
        """AP-side counters of the Wi-Fi card every STATION_EVERY_S (05 1.9: no ``iw``
        every second). A driver that gives none (rtl8814au) is not asked again after
        STATION_EMPTY_MAX empty answers."""
        nic = self._nic
        assert nic is not None
        last: tuple[float, dict[str, int]] | None = None
        empty = 0
        while empty < STATION_EMPTY_MAX and not self._halt.wait(STATION_EVERY_S):
            got, now = self._station_of(nic), time.monotonic()
            if not got:
                empty += 1
                last = None
                continue
            empty = 0
            if last is not None:
                span, before = now - last[0], last[1]
                keys = {key: got[src] - before[src] for key, src in (
                    (k.STATION_RETRIES, "tx_retries"), (k.STATION_FAILED, "tx_failed"),
                    (k.STATION_BEACON_LOSS, "beacon_loss"),
                    (k.STATION_RX_PACKETS, "rx_packets"),
                ) if src in got and src in before and got[src] >= before[src]}  # fmt: skip
                if keys:
                    keys[k.STATION_SPAN_S] = round(span)
                    with self._lock:
                        self._station_keys = keys
            last = (now, got)

    # ---------------------------------------------------------------- channel change

    def switch(
        self,
        ch: int,
        group: str,
        port: int,
        join_method: str = "A",
        mode: str = "manual",
        prime: dict[str, Any] | None = None,
        timeout_s: float | None = None,
        wait_s: float = 0.0,
    ) -> dict[str, Any]:
        """03 F19 receiver order: close the old analysis, stop the player, Leave, Join, a
        new analysis, the new player. RUNNING only; one change at a time (RUN-012).
        Answers after the first packet or the join failure, at most ``wait_s`` (02 4)."""
        if ch == self.cfg.ch:
            return {"ch": ch, "changed": False}
        if self.cfg.prime is not None and prime is None:
            raise ZlError("RUN-012", "Primed mode needs the new channel's prime")
        if not self._switch_lock.acquire(blocking=False):
            raise ZlError("RUN-012", "A channel change is already running")
        try:
            if self.flow.state is not State.RUNNING or self._halt.is_set() or not self.measuring:
                raise ZlError(
                    "RUN-012",
                    f"Cannot change the channel while the receiver is {self.flow.state.value}",
                )
            if timeout_s is not None:
                self._success_timeout_s = timeout_s
            zap = self._switch(ch, group, port, join_method, mode, prime)
        finally:
            self._switch_lock.release()
        end = time.monotonic() + wait_s
        while time.monotonic() < end and not self._halt.is_set():
            if zap.t_first_packet is not None or zap.join_failed or zap.end is not None:
                break
            time.sleep(0.05)
        return {"ch": ch, "changed": True, "zap": zap.to_line()}

    def _switch(
        self,
        ch: int,
        group: str,
        port: int,
        join_method: str,
        mode: str,
        prime: dict[str, Any] | None,
    ) -> Zap:
        old, old_a, old_sock = self.cfg, self.analyzer, self._sock
        assert old_a is not None and old_sock is not None
        self._close_zap("superseded")
        zap = Zap(
            self.cfg.device, old.ch, ch, join_method, mode, self._success_timeout_s,
            old.playback is not None, t_cmd=self._e_now(),
        )  # fmt: skip
        new_cfg = dataclasses.replace(old, ch=ch, group=group, port=port, prime=prime)
        watch = self._open_leave_watch(old)
        try:
            with self._tick_lock:  # 1. the old analysis ends, its last partial second too
                self._rx_stop.set()
                self._threads[0].join(2)
                self._close_second(old_a, final=True)
                self._lost_carry += old_a.lost_total
                self._switching = True
            self._stop_player()  # 2. player, then Leave (05 4.2.2: both sockets together)
            self.player = None
            if old.prime is not None:  # the old unicast flow stops now, not after 30 s
                with contextlib.suppress(OSError):  # else it stops by itself (iptv 4.5)
                    self._send_primes(stop=True)
            zap.t_leave = self._e_now()
            leave_ns = clock_ns()
            self._close_socket()
            self._start_leave_watch(zap, watch, leave_ns)
            self._buf.clear()
            join_ns = clock_ns()  # 3. Join and a new analysis
            zap.t_join = self._e_of_ns(join_ns)
            analyzer = StreamAnalyzer(
                ch, join_ns, playback=new_cfg.playback is not None,
                warmup_max_s=new_cfg.warmup_max_s, switch=True,
            )  # fmt: skip
            self.cfg = new_cfg
            if self._evidence is not None:  # the old channel's open window first
                self._keep_evidence(self._evidence.flush())
                ev = self._evidence
                ev.ch, ev.group, ev.port = new_cfg.ch, new_cfg.group, new_cfg.port
            try:
                sock = self._factory(new_cfg)
            except OSError as e:
                self._event("ZL-RCV-017", "error", f"Cannot join {group}: {e}")
                self._fail_switch(zap, e)
                raise ZlError(
                    "RCV-017", f"Cannot join {group}:{port}", str(e),
                    "Check the receive network card and the channel address.",
                ) from e  # fmt: skip
            self._sock = sock
            self._open_m2u()
            self.analyzer = analyzer
            self._zap = zap
            zap.joined.set()
            if prime is not None:
                try:
                    self._send_primes()
                except OSError as e:
                    self._event("ZL-RCV-024", "error", f"CH{ch} cannot send the prime: {e}")
                    self._fail_switch(zap, e)
                    raise ZlError("RCV-024", "Cannot send the prime for the player", str(e)) from e
            self._rx_stop = threading.Event()
            rx = threading.Thread(
                target=self._receive_loop, args=(sock, self._rx_stop), name="iptv-rx",
                daemon=True,
            )  # fmt: skip
            self._threads[0] = rx
            rx.start()
            self._start_new_player(ch)  # 4. the new player (its start is part of the zap time)
        except ZlError:
            raise
        except Exception as e:
            self._crash(e)
            raise
        finally:
            with self._tick_lock:
                self._switching = False
        self._event("ZL-INFO", "info", f"CH{old.ch} -> CH{ch} (join {join_method}, {mode})",
                    {"from": old.ch, "to": ch})  # fmt: skip
        return zap

    def _start_new_player(self, ch: int) -> None:
        if self.cfg.playback is None:
            return
        if self._desktop is not None:
            job = f"{self.cfg.run_id}/{self.cfg.device}/ch{ch}/player"  # G-13
            self.player = RemotePlayer(self._player_config(), job, self._desktop,
                                       self._player_event)  # fmt: skip
        else:
            self.player = make_player(self._player_config(), self._player_event)
        try:
            self.player.start()
        except ZlError as e:  # measurement goes on; the zap just has no normal play
            self._event("ZL-RCV-019", "error", f"Player did not start on CH{ch}: {e.what}")

    def _fail_switch(self, zap: Zap, e: Exception) -> None:
        zap.join_failed = True
        zap.joined.set()
        self._zap = zap
        self._crash(e, "ZL-RCV-017", f"Channel change failed: {e}")

    def _open_leave_watch(self, old: ReceiverConfig) -> LeaveWatch | None:
        """Before the Leave, so the first packets after it are seen. None: not checked."""
        if old.prime is not None or self._held_elsewhere(old.group, old.port):
            return None  # unicast: no group; or another receiver keeps the group joined
        watch = LeaveWatch(old.group, old.port, nic_of(old.interface_ip), self._leave_opener)
        if watch.open():
            return watch
        if not self._told_leave_unchecked:
            self._told_leave_unchecked = True
            self._event("ZL-INFO", "info", f"Leave check is off: {watch.reason}")
        return None

    def _start_leave_watch(self, zap: Zap, watch: LeaveWatch | None, leave_ns: int) -> None:
        if watch is None:
            zap.leave_check = "not_checked"
            return

        def run() -> None:
            res: LeaveResult = watch.watch(leave_ns, self._halt)
            if res.last_old_packet_ns is not None:
                zap.t_last_old_packet = self._e_of_ns(res.last_old_packet_ns)
            if res.state == "not_processed":
                self._event(
                    "ZL-NET-005", "warn", "Leave not processed by router",
                    {"group": watch.group, "port": watch.port},
                )  # fmt: skip
            zap.leave_check = res.state

        t = threading.Thread(target=run, name="iptv-leave", daemon=True)
        self._watchers = [w for w in self._watchers if w.is_alive()] + [t]
        t.start()

    # -- zap bookkeeping (times on the ``e`` clock)

    def _e_now(self) -> float:
        e0 = self._e0 if self._e0 is not None else 1
        return time.monotonic() - self._mono0 - (e0 - 1)

    def _e_of_ns(self, ns: int) -> float:
        return self._e_now() - (clock_ns() - ns) / NS_PER_S

    def _advance_zap(self, a: StreamAnalyzer, pb: str | None) -> None:
        z = self._zap
        if z is None:
            return
        now_e = self._e_now()
        fp = a.startup.first_packet_ns
        if z.t_first_packet is None and fp is not None:
            z.t_first_packet = self._e_of_ns(fp)
        p = self.player
        if z.t_first_frame is None and p is not None and p.first_frame:
            z.t_first_frame = now_e
        if z.t_normal_play is None and pb == "ok" and z.playback:
            z.t_normal_play = now_e
        if a.join_failed and not z.join_failed:
            z.join_failed = True
            self._event(
                "ZL-NET-003", "error", f"CH{z.to_ch}: no packet 5 s after the join (Join failed)",
                {"ch": z.to_ch, "group": self.cfg.group},
            )  # fmt: skip
        if z.ready_to_end(now_e):
            self._close_zap("done")

    def _close_zap(self, end: str) -> None:
        z = self._zap
        if z is None:
            return
        self._zap = None
        z.end = end
        if z.leave_check is None:
            z.leave_check = "not_checked"
        try:
            append_jsonl(self._zap_path, z.to_line())
        except OSError as e:
            self._event("ZL-RCV-009", "warn", f"Zap record not written: {e}")
        if end == "done":
            self._pending_keys = z.keys()

    # ---------------------------------------------------------------- helpers

    def _shape(self, piece: dict[str, Any]) -> dict[str, Any]:
        sock = self._sock
        return shape(
            piece,
            sock.rcvbuf_applied if sock is not None else None,
            bool(sock and sock.kernel_timestamps),
            playback=self.cfg.playback is not None,
            success_timeout_s=self._success_timeout_s,
        )

    def _player_config(self) -> PlayerConfig:
        pb = self.cfg.playback or {}
        player_port = self.cfg.flow_port(1)
        primed = self.cfg.prime is not None and player_port is not None
        return PlayerConfig(  # primed: unicast to its own port (iptv 4.5)
            url=f"rtp://@:{player_port}" if primed else f"rtp://@{self.cfg.group}:{self.cfg.port}",
            work_dir=self._player_dir,
            nic=None if primed else nic_of(self.cfg.interface_ip),
            buffer_ms=int(pb.get("buffer_ms", DEFAULTS["S-25"])),
            window=str(pb.get("window", DEFAULTS["S-50"])),
            headless=os.environ.get("ZEROLOSS_PLAYER_HEADLESS") == "1",  # tests only
            procs_dir=self._procs,
            kind=str(pb.get("player", DEFAULTS["S-26"])),
        )

    def _protect_measurement(self) -> None:
        """05 4.2-1 RCV-016: CPU >= 90% for more than 10 s -> the player gives way to
        the analyzer (once per session, recorded)."""
        if self.player is None or self.protected_at is not None:
            return
        cpu = self._cpu_of()
        self._busy_s = self._busy_s + 1 if cpu is not None and cpu >= CPU_BUSY_PCT else 0
        if self._busy_s > CPU_BUSY_S:
            done = self.player.lower_priority()
            self.protected_at = time.monotonic()
            text = ("Receiver PC CPU busy — measurement protected" if done else
                    "Receiver PC CPU busy — the player could not be made to give way")  # fmt: skip
            self._event("ZL-RCV-016", "warn", text, {"ch": self.cfg.ch, "cpu_pct": cpu})

    # ---------------------------------------------------------------- primed mode

    def _send_primes(self, stop: bool = False) -> None:
        """One prime per flow to P (opens the NAT filter) and Q (the sender learns the
        address): the analyzer from its own socket, the player through a raw socket
        from the player's port (nothing else binds it). ``stop``: the channel is left,
        to Q only (iptv 4.5). Raises OSError."""
        pr = self.cfg.prime
        assert pr is not None and self._sock is not None
        dst = str(pr["sender_ip"])
        make = stop_payload if stop else prime_payload
        for x in pr.get("flows") or []:
            payload = make(str(pr["run_id"]), self.cfg.ch, int(x["f"]))
            for port in (int(x["q"]),) if stop else (int(x["p"]), int(x["q"])):
                if x["f"] == 0:
                    self._sock.send_to(payload, dst, port)
                elif self.cfg.playback is not None:
                    send_from_port(self.cfg.interface_ip, int(x["r"]), dst, port, payload)

    def _prime_loop(self) -> None:
        """3 more at 1 s, then every 10 s, until the receive threads stop (iptv 4.5)."""
        waits = [1.0, 1.0, 1.0]
        while not self._halt.wait(waits.pop(0) if waits else PRIME_EVERY_S):
            try:
                with self._tick_lock:
                    if not self._switching and self._sock is not None:
                        self._send_primes()
            except OSError as e:  # keep trying; the sender stops after 30 s (SND-021)
                self._event("ZL-RCV-024", "warn", f"CH{self.cfg.ch} prime not sent: {e}")

    def _player_event(self, code: str, level: str, text: str) -> None:
        lv: Level = "error" if level == "error" else "warn" if level == "warn" else "info"
        self._event(code, lv, text, {"ch": self.cfg.ch})

    def _stop_player(self) -> None:
        p = self.player
        if p is None or p.flow.state not in (State.STARTING, State.RUNNING, State.FAULT):
            return
        try:
            p.stop()
        except ZlError as e:  # never leave the files or socket open because of the player
            self._event("ZL-RCV-003", "error", f"Player stop refused: {e.what}")

    def _shutdown(self) -> None:
        """Stop both threads, then close the last second on this thread."""
        self._halt.set()
        me = threading.current_thread()
        for t in [*self._threads, *self._watchers]:
            if t is not me:
                t.join(5)
        a = self.analyzer
        if a is None or self._sock is None or not a.measuring or self._faulted:
            return  # after a crash the analyzer is not trusted again
        try:
            self._one_second(final=True)
        except Exception as e:  # still close files and the socket below
            self._event("ZL-RCV-018", "error", f"Last second not closed: {e!r}")

    def _finish_files(self, reason: str) -> None:
        self._close_zap("stopped")
        if self._evidence is not None:
            self._keep_evidence(self._evidence.flush())
        closed = self._summarizer.flush()
        with self._lock:
            self.summaries += [self._shape(p) for p in closed]
        if self._recorder is not None:
            self._recorder.close(reason)
            self._recorder = None

    def _close_socket(self) -> None:
        if self._m2u is not None:  # its view belongs to this socket's group
            self._m2u.close()
            self._m2u = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def _rollback(self) -> None:
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(5)
        try:
            self.flow.move(State.STOPPING, reason="warm-up failed", expect=State.STARTING)
        except ZlError:
            return  # a Stop command is already cleaning up (it closes the socket)
        self._stop_player()
        self._close_socket()
        self.flow.move(State.IDLE, reason="warm-up failed")

    def _crash(self, e: Exception, code: str = "ZL-RCV-018", text: str = "") -> None:
        if self._halt.is_set():
            return
        self._halt.set()
        self._faulted = True
        self._event(code, "error", text or f"Receiver stopped unexpectedly: {e!r}")
        try:
            self._finish_files("fault")
            self._stop_player()
        finally:
            self.flow.move(State.FAULT, reason=repr(e))

    def _event(
        self, code: str, level: Level, text: str, data: dict[str, Any] | None = None
    ) -> None:
        payload = {"module": "iptv", **(data or {})}
        self.bus.publish(Event(code, level, text, device=self.cfg.device, data=payload))

    def _changed(self, name: str, old: State, new: State, reason: str) -> None:
        self._event("ZL-INFO", "info", f"{name}: {old} -> {new}", {"reason": reason})


SETTLE_OK_S = 2  # 05 2.1-2: seconds of steady playback before measuring
SETTLE_MAX_S = 5  # ... at the latest this long after the first picture
STATION_EVERY_S = 10.0  # 05 1.9 full Wi-Fi read
STATION_EMPTY_MAX = 3


def _open_receiver(cfg: ReceiverConfig) -> UdpReceiver:
    size = int(DEFAULTS["S-27"])
    port = cfg.flow_port(0)
    if cfg.prime is not None and port is not None:  # primed: unicast, no group (iptv 4.5)
        return UdpReceiver(port, None, cfg.interface_ip, rcvbuf_bytes=size)
    return UdpReceiver(cfg.port, cfg.group, cfg.interface_ip, rcvbuf_bytes=size)
