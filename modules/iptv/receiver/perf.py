"""Can this receiver PC play the quality under test? (05 4.2-1 implementation rules, RCV-015)

Only for a PC receiver with playback on. A 10 s sample with the sender's encoding
values is made here once and kept in the module cache (video never goes through the
Controller); then ffmpeg decodes it with hardware decoding as the player would and
the speed is read. Plus: hardware decoding support (``vainfo``) and idle CPU. A
result is kept 24 h. It only warns: the start is never blocked.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.blocks.process_runner import ProcessRunner, ProcState, StartError, StopError
from core.events.bus import utc_now
from modules.iptv.sender.commands import QUALITIES, prepare_command

SAMPLE_S = 10.0
MIN_SPEED = 1.5  # 05 4.2-1: decoding at least 1.5x real time
IDLE_CPU_MAX = 50.0  # 05 4.2-1 default (to be tuned with real PCs)
KEEP_S = 24 * 3600
DECODE_LIMIT_S = 60.0
MAKE_LIMIT_S = 1800.0
VA_PROFILE = {"UHD": "VAProfileHEVCMain", "HD": "VAProfileH264High"}  # as vainfo prints them


def fix_text(quality: str) -> str:
    """RCV-015 advice (09): testing in HD is offered only when the plan is not HD."""
    if quality == "HD":
        return "Turn PC playback off (S-49) or use a small player window (S-50)."
    return "Turn PC playback off (S-49), use a small player window (S-50) or test in HD."


def parse_speed(log: str) -> float | None:
    """The last ``speed=2.37x`` in an ffmpeg log."""
    found = re.findall(r"speed=\s*([\d.]+)x", log)
    return float(found[-1]) if found else None


def hw_decode(quality: str, run: Callable[[list[str]], str | None] | None = None) -> bool | None:
    """True/False from ``vainfo`` (Linux, the codec's VLD entry point), None = unknown."""
    if not sys.platform.startswith("linux"):
        return None  # Windows: DXVA check comes with V18
    out = (run or _vainfo)(["vainfo"])
    if out is None:
        return None
    profile = VA_PROFILE[quality]
    return bool(re.search(rf"{profile}\s*:\s*VAEntrypointVLD", out))


def _vainfo(argv: list[str]) -> str | None:
    if shutil.which(argv[0]) is None:
        return None
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False,
                              stdin=subprocess.DEVNULL, errors="replace")  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout + done.stderr


class PerfCheck:
    def __init__(
        self,
        cache_dir: Path,
        procs_dir: Path,
        ffmpeg: str = "ffmpeg",
        sample_s: float = SAMPLE_S,
        vainfo: Callable[[list[str]], str | None] | None = None,
        idle_cpu: Callable[[], float | None] = lambda: None,
    ) -> None:
        self.dir = cache_dir / "perf"
        self.procs_dir, self.ffmpeg, self.sample_s = procs_dir, ffmpeg, sample_s
        self.vainfo, self.idle_cpu = vainfo, idle_cpu
        self._results: dict[str, dict[str, Any]] = {}
        self._making: set[str] = set()
        self._lock = threading.Lock()
        self._running: set[ProcessRunner] = set()  # stopped by cancel() (safe stop, 12 4-3)
        self._cancelled = threading.Event()

    def busy(self) -> bool:
        with self._lock:
            return bool(self._running or self._making)

    def cancel(self) -> None:
        """Node safe stop / shutdown: stop a sample being made or a decode test now."""
        self._cancelled.set()
        with self._lock:
            running = list(self._running)
        for r in running:
            with contextlib.suppress(StopError, RuntimeError):
                r.stop()

    def sample(self, quality: str, muxrate_bps: int) -> Path:
        return self.dir / f"sample_{quality}_{muxrate_bps // 1_000_000}M.ts"

    def check(self, quality: str, muxrate_bps: int, measure: bool) -> dict[str, Any]:
        """02 4 ``receiver/perfcheck``. ``measure`` False = only what is kept."""
        key = f"{quality}_{muxrate_bps}"
        self._cancelled.clear()
        with self._lock:
            kept = self._results.get(key)
            fresh = kept is not None and time.time() - kept["_at"] < KEEP_S
            if fresh or not measure:
                return self._judge(quality, kept) if kept else _unknown("unavailable")
            path = self.sample(quality, muxrate_bps)
            if not path.exists():
                if key not in self._making:
                    self._making.add(key)
                    threading.Thread(target=self._make, args=(key, quality, muxrate_bps),
                                     name="iptv-perf", daemon=True).start()  # fmt: skip
                return _unknown("making_sample")
        # 2026-10-16: read before the decode test -- read after it, the PC was "already
        # busy" with the test itself (RCV-015 on a T460 that decodes at 1.84x)
        cpu = self.idle_cpu()
        result = self._measure(quality, path)
        with self._lock:
            self._results[key] = result
        return self._judge(quality, result, cpu)

    # ---------------------------------------------------------------- steps

    def _make(self, key: str, quality: str, muxrate_bps: int) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            final = self.sample(quality, muxrate_bps)
            part = final.with_name(final.name + ".part")
            argv = prepare_command(self.ffmpeg, QUALITIES[quality], 1, part,
                                   seconds=self.sample_s, muxrate_bps=muxrate_bps)  # fmt: skip
            code = self._run(f"perf_sample_{key}", argv, self.dir / f"make_{key}.log",
                             MAKE_LIMIT_S)  # fmt: skip
            if code == 0 and part.exists():
                part.replace(final)
            else:
                part.unlink(missing_ok=True)
        finally:
            with self._lock:
                self._making.discard(key)

    def _measure(self, quality: str, sample: Path) -> dict[str, Any]:
        """What is kept for KEEP_S: the decoding speed and hardware decoding."""
        log = self.dir / f"decode_{quality}.log"
        # the decoder the player uses (mpv --hwdec=vaapi): "auto" picked cuda on the T460,
        # failed and decoded in software (2026-10-16)
        accel = "vaapi" if sys.platform.startswith("linux") else "auto"
        speed = None
        # the node's own user may not open the GPU (2026-10-16 T460: zeroloss not in
        # "render"; mpv decodes in hardware in the person's session): then the CPU's speed
        for accel_args in (["-hwaccel", accel], []):
            argv = [self.ffmpeg, "-hide_banner", "-nostdin", *accel_args, "-i", str(sample),
                    "-map", "0:v:0", "-f", "null", "-"]  # fmt: skip
            code = self._run(f"perf_decode_{quality}", argv, log, DECODE_LIMIT_S)
            text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
            speed = parse_speed(text) if code == 0 else None
            if speed is not None or self._cancelled.is_set():
                break
        hw = hw_decode(quality, self.vainfo)
        return {"state": "ready", "hw_decode": hw, "decode_speed": speed,
                "measured_utc": utc_now(), "_at": time.time()}  # fmt: skip

    def _judge(self, quality: str, kept: dict[str, Any],
               cpu: float | None = None) -> dict[str, Any]:  # fmt: skip
        """05 4.2-1 (GAP-113): how busy the PC is now is read on every check, never
        kept; the warning is made again from it and the kept decoding result. ``cpu``:
        read just before a decode test this check ran."""
        cpu = self.idle_cpu() if cpu is None else cpu
        speed, hw = kept.get("decode_speed"), kept.get("hw_decode")
        why = []
        if speed is None:
            why.append("the decoding test did not finish")
        elif speed < MIN_SPEED:
            why.append(f"decoding runs at {speed:g}x real time (needs {MIN_SPEED:g}x)")
        if hw is False and (speed is None or speed < MIN_SPEED):
            # 2026-10-16 (GAP-161): vainfo said no while the decode test ran fast enough
            # and mpv decodes in hardware — the speed is the answer, not vainfo's list
            why.append(f"no hardware decoding for {quality}")
        if cpu is not None and cpu > IDLE_CPU_MAX:
            why.append(f"the PC is already {cpu:.0f}% busy")
        warn = None
        if why:
            what = f"Receiver PC may be too slow for {quality} playback"
            warn = {"code": "ZL-RCV-015", "what": what, "why": "; ".join(why),
                    "fix": fix_text(quality)}  # fmt: skip
        return {**_public(kept), "idle_cpu_pct": cpu, "warn": warn}

    def _run(self, name: str, argv: list[str], log: Path, limit_s: float) -> int | None:
        """Run to the end under the node's process rules (12 4-3); None = cut off or
        cancelled."""
        runner = ProcessRunner(name, argv, self.procs_dir, log)
        try:
            runner.start()
        except StartError:
            return None
        with self._lock:
            self._running.add(runner)
        try:
            end = time.monotonic() + limit_s
            while (runner.poll() is ProcState.RUNNING and time.monotonic() < end
                   and not self._cancelled.is_set()):  # fmt: skip
                time.sleep(0.1)
            finished = runner.state is ProcState.FAULT
            if runner.state in (ProcState.RUNNING, ProcState.FAULT):
                runner.stop()
        except (StopError, RuntimeError):
            return None
        finally:
            with self._lock:
                self._running.discard(runner)
        return runner.exit_code if finished and not self._cancelled.is_set() else None


def _unknown(state: str) -> dict[str, Any]:
    warn = None
    if state == "making_sample":
        warn = {"code": "ZL-RCV-015", "what": "Measuring the receiver PC",
                "why": "The decoding test video is being made on the receiver PC (once).",
                "fix": "Nothing to do; the next check has the result."}  # fmt: skip
    return {"state": state, "hw_decode": None, "decode_speed": None, "idle_cpu_pct": None,
            "measured_utc": None, "warn": warn}  # fmt: skip


def _public(r: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in r.items() if not k.startswith("_")}
