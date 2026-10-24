"""IPTV sender for one channel (docs/modules/iptv.md 4.4, 6; docs/05-standards.md 2.1-2).

Start: prepared file must exist (SND-001) -> launch tsp -> "stable" = rate within +-1 %
of the target for 3 seconds in a row, within 10 s (SND-019); otherwise tsp is stopped
and the start is rolled back. Running: the rate is read every second from tsp's own
report; a stall is SND-016, slow sending SND-017. If tsp dies it is restarted once
(SND-005); a second death ends in FAULT. Stop: tsp is stopped and confirmed.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Any

from core.blocks import priority, udp_pacer
from core.blocks.process_runner import ProcessRunner, ProcState, StartError, StopError
from core.blocks.unicode_path import find_file
from core.events.bus import Event, EventBus, Level
from core.lifecycle.state_flow import State, StateFlow
from core.protocol.envelope import ZlError
from modules.iptv.sender.commands import DEFAULT_TTL, new_ssrc, parse_rate, send_command

STABLE_SECONDS = 3
STABLE_TOLERANCE = 0.01
START_LIMIT_S = 10.0
SLOW_TOLERANCE = 0.01  # SND-017 "slow" (yellow)
# SND-025: the even-spacing sender sent a datagram this late against real time (2026-10-22:
# the ProBook's Wi-Fi driver reset storm starved it 20 s, the stream reached the receiver
# 4.8 s late and 11 packets were lost -- judged as the network's until now)
PACE_LATE_MS = 50.0
UNSTABLE_TOLERANCE = 0.05  # SND-016 "unstable" (purple window, 05 5.2 / 5.3)


@dataclass(frozen=True)
class SenderConfig:
    ch: int
    file: Path
    dest_ip: str
    port: int
    muxrate_bps: int
    ttl: int = DEFAULT_TTL
    local_address: str | None = None
    source: str = "file"  # file / generated / generated_fallback (02 5)
    tsp: str = "tsp"
    start_limit_s: float = START_LIMIT_S
    local_port: int | None = None  # primed mode: send from the port P (iptv 4.5)
    tag: str = ""  # primed mode: "_f0" / "_f1", one run record and log per flow
    paced: bool = True  # GAP-160: one datagram every len*8/rate, not tsp's ~9 ms bursts


class _PaceLog:
    """Reads the even-spacing sender's new per-second lines ({"pace": {...}})."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._f: IO[bytes] | None = None
        self._partial = b""

    def new(self) -> list[dict[str, Any]]:
        if self._f is None:
            if not self.path.exists():
                return []
            self._f = self.path.open("rb")
        data = self._partial + self._f.read()
        *lines, self._partial = data.split(b"\n")
        out = []
        for raw in lines:
            try:
                d = json.loads(raw.decode("utf-8", errors="replace")).get("pace")
            except (ValueError, AttributeError):
                continue
            if isinstance(d, dict) and "listening" not in d:
                out.append(d)
        return out

    def close(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None


class _RateLog:
    """Reads new rate lines from the tsp log file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._f: IO[bytes] | None = None
        self._partial = b""

    def new_rates(self) -> list[int]:
        if self._f is None:
            if not self.path.exists():
                return []
            self._f = self.path.open("rb")
        data = self._partial + self._f.read()
        *lines, self._partial = data.split(b"\n")  # the last piece may be half-written
        rates = []
        for raw in lines:
            rate = parse_rate(raw.decode("utf-8", errors="replace"))
            if rate is not None:
                rates.append(rate)
        return rates

    def close(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None


class SenderSession:
    def __init__(
        self, cfg: SenderConfig, data_dir: Path, bus: EventBus, procs_dir: Path | None = None
    ) -> None:
        self._procs = procs_dir or data_dir / "sender" / "run"  # node's procs dir (12 4-3)
        self.cfg = cfg
        self.bus = bus
        self.flow = StateFlow(f"sender ch{cfg.ch}", self._changed)
        self.ssrc = 0
        self.last_rate: int | None = None
        self.restarts = 0
        self._dir = data_dir / "sender"
        self._runner: ProcessRunner | None = None
        self._pacer: ProcessRunner | None = None
        self._log: _RateLog | None = None
        self._halt = threading.Event()
        self._watch: threading.Thread | None = None

    # ---------------------------------------------------------------- commands

    def start(self) -> None:
        found = find_file(self.cfg.file)  # Korean names: NFC or NFD (iptv 1-1-1)
        if found is not None:
            self.cfg = replace(self.cfg, file=found)
        else:
            raise ZlError(
                "SND-001",
                f"Prepared file for CH{self.cfg.ch} is missing",
                str(self.cfg.file),
                "Prepare the channel file again.",
            )
        self.flow.move(State.STARTING, reason=f"send CH{self.cfg.ch}")
        try:
            self._launch()
        except ZlError:
            self.flow.move(State.STOPPING, reason="start failed")
            self.flow.move(State.IDLE, reason="start failed")
            raise
        self.flow.move(State.RUNNING, reason="rate stable")
        self._halt.clear()
        self._watch = threading.Thread(target=self._watch_loop, name="iptv-tx", daemon=True)
        self._watch.start()

    def stop(self) -> None:
        self.flow.move(State.STOPPING, reason="stop")
        self._halt.set()
        if self._watch is not None and self._watch is not threading.current_thread():
            self._watch.join(5)
        ok = self._stop_process()
        self.flow.move(State.IDLE if ok else State.FAULT, reason="stopped")

    # ---------------------------------------------------------------- helpers

    def _launch(self) -> None:
        """Start tsp and wait until the rate is stable. Raises ZlError after rollback."""
        self.ssrc = new_ssrc(self.cfg.ch)
        c = self.cfg
        self._dir.mkdir(parents=True, exist_ok=True)
        if c.paced:  # tsp sends to the pacer on loopback; the pacer to the destination
            via = udp_pacer.free_loopback_port()
            self._start_pacer(via)
            argv = send_command(c.tsp, c.file, "127.0.0.1", via, c.muxrate_bps, self.ssrc)
        else:
            argv = send_command(
                c.tsp, c.file, c.dest_ip, c.port, c.muxrate_bps, self.ssrc, c.ttl,
                c.local_address, c.local_port,
            )  # fmt: skip
        log_path = self._dir / f"tsp_ch{c.ch}{c.tag}.log"
        log_path.unlink(missing_ok=True)
        self._runner = ProcessRunner(f"tsp_ch{c.ch}{c.tag}", argv, self._procs, log_path)
        self._log = _RateLog(log_path)
        streak = 0
        seen: list[int] = []

        def stable() -> bool:
            nonlocal streak
            assert self._log is not None
            for rate in self._log.new_rates():
                seen.append(rate)
                self.last_rate = rate
                ok = abs(rate - c.muxrate_bps) <= c.muxrate_bps * STABLE_TOLERANCE
                streak = streak + 1 if ok else 0
            return streak >= STABLE_SECONDS

        try:
            self._runner.start(ready=stable, ready_timeout_s=c.start_limit_s)
            pid = self._runner.pid
            if pid:  # the source of the live stream goes first too (the pacer raises itself)
                priority.first_all(pid)
        except StartError as e:
            self._log.close()
            self._stop_pacer()
            detail = f"rates seen: {seen[-5:]}" if seen else str(e)
            self._event("ZL-SND-019", "error", f"CH{c.ch} sending not stable: {e}")
            raise ZlError(
                "SND-019",
                f"Sending did not reach {c.muxrate_bps / 1e6:g} Mbps",
                str(e),
                "Check the prepared file and the sender PC load.",
                detail=detail,
            ) from e

    def _start_pacer(self, listen: int) -> None:
        """GAP-160: even spacing to the real destination (its own process, 12 4-3)."""
        c = self.cfg
        argv = udp_pacer.command(listen, c.dest_ip, c.port, c.muxrate_bps, c.ttl,
                                 c.local_address, c.local_port)  # fmt: skip
        log = self._dir / f"pace_ch{c.ch}{c.tag}.log"
        log.unlink(missing_ok=True)
        self._pacer = ProcessRunner(f"pace_ch{c.ch}{c.tag}", argv, self._procs, log)
        self._pace_log = _PaceLog(log)

        def listening() -> bool:
            return log.exists() and b'"listening"' in log.read_bytes()

        try:
            self._pacer.start(ready=listening, ready_timeout_s=5.0)
        except StartError as e:
            self._event("ZL-SND-019", "error", f"CH{c.ch} pacer did not start: {e}")
            raise ZlError("SND-019", "The even-spacing sender did not start", str(e),
                          "Restart the node on the sender PC.") from e  # fmt: skip

    def _stop_pacer(self) -> bool:
        pacer, self._pacer = self._pacer, None
        if pacer is None:
            return True
        try:
            if pacer.state in (ProcState.RUNNING, ProcState.FAULT):
                pacer.stop()
            return True
        except StopError as e:
            self._event("ZL-SYS-006", "error", str(e))
            return False

    def _watch_loop(self) -> None:
        silent = 0
        while not self._halt.wait(1.0):
            runner, log = self._runner, self._log
            if runner is None or log is None:
                return
            rates = log.new_rates()
            # every second while it lasts: the Controller's trouble window follows it (05 5.3)
            if rates:
                silent = 0
                self.last_rate = rates[-1]
                if rates[-1] < self.cfg.muxrate_bps * (1 - UNSTABLE_TOLERANCE):
                    self._event("ZL-SND-016", "warn", f"CH{self.cfg.ch} unstable: {rates[-1]} b/s")
                elif rates[-1] < self.cfg.muxrate_bps * (1 - SLOW_TOLERANCE):
                    self._event("ZL-SND-017", "warn", f"CH{self.cfg.ch} slow: {rates[-1]} b/s")
            else:
                silent += 1
                if silent >= 2:  # the rate report comes once a second
                    self._event("ZL-SND-016", "warn", f"CH{self.cfg.ch} no rate report")
            self._watch_pace()
            pacer_died = self._pacer is not None and self._pacer.poll() is ProcState.FAULT
            if (runner.poll() is ProcState.FAULT or pacer_died) and not self._halt.is_set():
                self._process_died(runner.exit_code)
                if self.flow.state is State.FAULT:
                    return
                silent = 0

    def _watch_pace(self) -> None:
        """SND-025 every second the even-spacing sender ran late: the stream left this PC
        behind real time, so what the receivers lost then is the sender's (purple)."""
        pace = getattr(self, "_pace_log", None)
        for d in pace.new() if pace is not None else []:
            late_ms = float(d.get("late_us") or 0) / 1000
            dropped = int(d.get("dropped") or 0)
            if late_ms >= PACE_LATE_MS or dropped:
                self._event("ZL-SND-025", "warn",
                            f"CH{self.cfg.ch} sending fell behind real time: {late_ms:.0f} ms "
                            f"late, {dropped} datagrams too late for live, not sent")  # fmt: skip

    def _process_died(self, exit_code: int | None) -> None:
        self._event("ZL-SND-005", "error", f"CH{self.cfg.ch} tsp exited ({exit_code})")
        if not self._stop_process():  # leftovers must be gone before a restart
            self.flow.move(State.FAULT, reason="tsp leftovers did not stop")
            return
        if self.restarts >= 1:
            self.flow.move(State.FAULT, reason="tsp died twice")
            return
        try:
            self._launch()
        except ZlError:
            self.flow.move(State.FAULT, reason="restart failed")
            return
        self.restarts += 1  # counted once the new tsp is stable, as status shows it
        self._event("ZL-INFO", "info", f"CH{self.cfg.ch} sending again after a restart",
                    clears=["ZL-SND-005"])  # the sender trouble window ends (05 5.3)  # fmt: skip

    def _stop_process(self) -> bool:
        runner = self._runner
        if runner is None:
            return self._stop_pacer()
        try:
            if runner.state in (ProcState.RUNNING, ProcState.FAULT):
                runner.stop()
            return self._stop_pacer()
        except StopError as e:
            self._event("ZL-SYS-006", "error", str(e))
            self._stop_pacer()
            return False
        finally:
            if self._log is not None:
                self._log.close()

    def _event(self, code: str, level: Level, text: str, clears: list[str] | None = None) -> None:
        data: dict[str, Any] = {"module": "iptv", "ch": self.cfg.ch}
        if clears:
            data["clears"] = clears  # 02 8
        self.bus.publish(Event(code, level, text, data=data))

    def _changed(self, name: str, old: State, new: State, reason: str) -> None:
        self._event("ZL-INFO", "info", f"{name}: {old} -> {new} ({reason})")

    def status(self) -> dict[str, Any]:
        return {
            "ch": self.cfg.ch,
            "group": f"{self.cfg.dest_ip}:{self.cfg.port}",
            "state": self.flow.state.value,
            "tx_bitrate_bps": self.last_rate,
            "restarts": self.restarts,
            "tx_packets": None,  # P3 (02 5)
            "interval_dev_ms": None,  # P3
            "source": self.cfg.source,
        }
