"""Generated source videos on the sender PC (docs/modules/iptv.md 1-1, 1-1-2, 4.3).

One file per channel, quality and bitrate under ``<module cache>/generated/`` with a description
``.json`` beside it. Jobs run one at a time on one worker (encoding uses every core).
A file is written as ``.part.ts`` and renamed only after the 4.3 check passed, so a
crash never leaves a half file that looks ready; left ``.part`` files are removed at
start. No progress for ``stall_s`` (60 s), too little disk, an ffmpeg error or a failed
check is SND-020. Cancel (USR-006) removes the file being made and keeps finished ones.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import unicodedata
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.blocks.process_runner import ProcessRunner, ProcState, StartError, StopError
from core.blocks.ts_analyzer import TsAnalyzer
from core.events.bus import Event, EventBus, Level
from modules.iptv.marker import FRAME_BITS, STRIP_H, STRIP_W, Marker, read_strip, strip_filter
from modules.iptv.sender.commands import FIRST_ES_PID, PMT_PID, QUALITIES, prepare_command
from modules.iptv.sender.sources import av_end_gap, digest, fits, legacy_digest, probe

GENERATED_S = 600.0  # 10 minutes, then the file loops (iptv 1-1)
STALL_S = 60.0
DISK_MARGIN_MB = 1024
MARKER_VERSION = 1  # frame marker layout (iptv 3.4)
RATE_TOLERANCE = 0.005  # 4.3: bitrate within +-0.5 %
PCR_GAP_MAX_S = 0.040  # 3: PCR interval <= 40 ms
FRAME_S = 1 / 30  # one picture at the made 30 fps (iptv 3): the most sound and picture
# may end apart (iptv 1-1, GAP-114)
TS = 188
PCR_CLOCK = 27_000_000
WAITING = ("queued", "making", "checking")


@dataclass
class Job:
    ch: int
    quality: str
    muxrate_bps: int
    source: str | None = None  # S-48 user file (absolute path), None = generated
    tag: str = ""  # "f<digest>" of the user file (+ "_whole", S-55), part of the kept name
    whole: bool = False  # S-55: the whole user file, not its first 10 minutes
    state: str = "queued"  # queued / making / checking / ready / failed / cancelled
    percent: float | None = None
    left_s: int | None = None
    error: str | None = None

    @property
    def key(self) -> tuple[str, int, int, str]:
        return (self.quality, self.ch, self.muxrate_bps, self.tag)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["mbps"] = d.pop("muxrate_bps") // 1_000_000
        src = d.pop("source")
        d.pop("tag")
        d["source"] = unicodedata.normalize("NFC", Path(src).name) if src else "generated"
        return d


def generated_path(folder: Path, quality: str, ch: int, muxrate_bps: int, tag: str = "") -> Path:
    """English names only (1-1-1); a user file adds ``_f<digest>`` (1-1 S-48)."""
    extra = f"_{tag}" if tag else ""
    return folder / f"ch{ch}_{quality}_{muxrate_bps // 1_000_000}M{extra}.ts"


def source_tag(source: Path | None, whole: bool = False, legacy: bool = False) -> str:
    """The kept name's part for a user file; the whole movie (S-55) is a file of its own,
    kept beside the 10-minute one (each made once, reused). ``legacy``: the name a file
    made before the content digest has (2026-10-17)."""
    if source is None:
        return ""
    d = legacy_digest(source) if legacy else digest(source)
    return f"f{d}" + ("_whole" if whole else "")


def end_problems(path: Path, length_s: float, ffprobe: str = "ffprobe") -> list[str]:
    """4.3 + iptv 1-1: sound and picture end within one frame of each other."""
    gap = av_end_gap(path, length_s, ffprobe)
    if gap is None:
        return ["the end of the sound or the picture could not be read"]
    if gap > FRAME_S:
        return [f"the sound ends {gap * 1000:.0f} ms away from the picture (max one frame, "
                f"{FRAME_S * 1000:.0f} ms)"]  # fmt: skip
    return []


def check_prepared(path: Path, muxrate_bps: int, ch: int) -> list[str]:
    """4.3 check. Returns what is wrong (empty = pass). Packets go to the TR 101 290
    analyzer at their ideal CBR arrival time (byte position / target bitrate)."""
    an = TsAnalyzer()
    pcr_pid: int | None = None
    first: tuple[int, int] | None = None  # (byte offset, PCR) of the first PCR
    last: tuple[int, int] | None = None
    gap_max = 0
    off = 0
    chunk = 7 * TS
    with path.open("rb") as f:
        while buf := f.read(chunk * 1024):
            view = memoryview(buf)
            for i in range(0, len(view), chunk):
                an.feed(bytes(view[i : i + chunk]), int((off + i) * 8e9 / muxrate_bps))
            for i in range(0, len(view) - TS + 1, TS):
                pcr = _pcr(view[i : i + TS])
                if pcr is None:
                    continue
                pid = ((view[i + 1] & 0x1F) << 8) | view[i + 2]
                if pcr_pid is None:
                    pcr_pid = pid
                if pid != pcr_pid:
                    continue
                if last is not None:
                    gap_max = max(gap_max, pcr - last[1])
                if first is None:
                    first = (off + i, pcr)
                last = (off + i, pcr)
            off += len(buf)
    an.check_timeouts(int(off * 8e9 / muxrate_bps))
    wrong = []
    c = an.counters
    if c.p1_errors:
        wrong.append(
            f"TR 101 290 priority 1 errors: {c.p1_errors} (CC {c.cc_err}, PAT {c.pat_err}, "
            f"PMT {c.pmt_err}, PID {c.pid_err}, sync {c.sync_loss + c.sync_byte_err})"
        )
    want = {PMT_PID: (ch, [FIRST_ES_PID, FIRST_ES_PID + 1])}
    if an.programs() != want:
        wrong.append(f"service/PIDs {an.programs()} (want {want})")
    if first is None or last is None or last[1] <= first[1]:
        wrong.append("no PCR")
    else:
        rate = (last[0] - first[0]) * 8 / ((last[1] - first[1]) / PCR_CLOCK)
        if abs(rate - muxrate_bps) > muxrate_bps * RATE_TOLERANCE:
            wrong.append(f"bitrate {rate / 1e6:.3f} Mbps (want {muxrate_bps / 1e6:g})")
        if gap_max / PCR_CLOCK > PCR_GAP_MAX_S:
            wrong.append(f"PCR interval {gap_max / PCR_CLOCK * 1000:.1f} ms (max 40)")
    return wrong


def marker_problems(strips: Path, ch: int) -> list[str]:
    """4.3 marker check (20 W-7): ``strips`` holds one ``strip_filter`` picture per
    frame; every frame must read (``ch``, 0, 1, 2, ...). Empty = pass."""
    size = STRIP_W * STRIP_H
    n = wrong = 0
    seen: list[str] = []
    with strips.open("rb") as f:
        while block := f.read(size * 256):
            view = memoryview(block)
            for i in range(0, len(view) - size + 1, size):
                m = read_strip(view[i : i + size])
                if m != Marker(ch, n % (1 << FRAME_BITS)):
                    wrong += 1
                    if len(seen) < 3:
                        seen.append(f"frame {n}: " + ("unreadable" if m is None
                                                      else f"CH{m.ch} #{m.frame}"))  # fmt: skip
                n += 1
    if n == 0:
        return ["frame marker: no frames decoded"]
    if wrong:
        return [f"frame marker wrong in {wrong} of {n} frames ({'; '.join(seen)})"]
    return []


def _pcr(pkt: memoryview) -> int | None:
    if pkt[0] != 0x47 or not pkt[3] & 0x20 or pkt[4] < 7 or not pkt[5] & 0x10:
        return None
    base = (pkt[6] << 25) | (pkt[7] << 17) | (pkt[8] << 9) | (pkt[9] << 1) | (pkt[10] >> 7)
    return base * 300 + (((pkt[10] & 1) << 8) | pkt[11])


def _free_mb(folder: Path) -> int:
    return shutil.disk_usage(folder).free // (1024 * 1024)


class Preparer:
    def __init__(
        self,
        cache_dir: Path,
        bus: EventBus,
        procs_dir: Path,
        ffmpeg: str = "ffmpeg",
        seconds: float = GENERATED_S,
        ffprobe: str = "ffprobe",
        stall_s: float = STALL_S,
        preset: str | None = None,  # None: by codec (commands.PRESET)
        free_mb: Callable[[Path], int] = _free_mb,
    ) -> None:
        self.folder = cache_dir / "generated"  # the module's cache (06 12)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.bus = bus
        self.procs_dir = procs_dir
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.seconds = seconds
        self._facts: dict[str, Any] = {}
        self.stall_s = stall_s
        self.preset = preset
        self.free_mb = free_mb
        self._jobs: list[Job] = []
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._worker: threading.Thread | None = None
        self._runner: ProcessRunner | None = None
        for part in self.folder.glob("*.part.*"):  # a crash left it (1-1-2), marker check too
            part.unlink(missing_ok=True)

    # ---------------------------------------------------------------- queries

    def ready(
        self, quality: str, ch: int, muxrate_bps: int, source: Path | None = None,
        whole: bool = False,
    ) -> Path | None:  # fmt: skip
        """The made file (generated, or from ``source``) if it exists, matches its
        description and passed 4.3. ``whole``: the whole user file (S-55)."""
        whole = whole and source is not None
        for legacy in (False, True) if source is not None else (False,):
            try:
                tag = source_tag(source, whole, legacy)
            except OSError:
                return None  # the user file is gone
            path = generated_path(self.folder, quality, ch, muxrate_bps, tag)
            try:
                desc = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
                size = path.stat().st_size
            except (OSError, ValueError):
                continue
            ok = (
                desc.get("key") == self._key(quality, ch, muxrate_bps, source, tag, whole)
                and desc.get("check") == []
                and desc.get("bytes") == size
            )
            if ok:
                return path
        return None

    def made(self) -> list[dict[str, Any]]:
        """Every finished file this build would use as it is: quality, channel, Mbps,
        whole or 10 minutes, and the original's digest (None = generated). The Source
        video list marks what starts at once (2026-10-17 user: "바로 사용가능한 파일은
        별도로 알려주던가 직접 선택하게")."""
        out = []
        for desc_path in sorted(self.folder.glob("ch*_*.json")):
            try:
                desc = json.loads(desc_path.read_text(encoding="utf-8"))
                size = desc_path.with_suffix(".ts").stat().st_size
            except (OSError, ValueError):
                continue
            key = desc.get("key") or {}
            if desc.get("check") != [] or desc.get("bytes") != size:
                continue
            try:
                quality, ch, mux = str(key["quality"]), int(key["ch"]), int(key["muxrate_bps"])
                tag = str(key.get("digest") or "")
                whole = key.get("seconds") is None
                src = Path("in") if key.get("source") == "file" else None
                if key != self._key(quality, ch, mux, src, tag, whole):
                    continue  # made by another recipe: it would be made again
            except (KeyError, TypeError, ValueError):
                continue
            out.append({"quality": quality, "ch": ch, "mbps": mux // 1_000_000,
                        "whole": whole and src is not None,
                        "digest": tag.removesuffix("_whole") or None})  # fmt: skip
        return out

    def status(self) -> dict[str, Any]:
        """Heartbeat ``module_data.iptv.prepare`` (02 5)."""
        with self._lock:
            return {
                "busy": any(j.state in WAITING for j in self._jobs),
                "channels": [j.to_dict() for j in self._jobs],
            }

    # ---------------------------------------------------------------- commands

    def request(
        self, quality: str, muxrate_bps: int, channels: list[int],
        sources: dict[int, Path] | None = None, only: bool = False, whole: bool = False,
    ) -> list[dict[str, Any]]:  # fmt: skip
        """Queue what is missing; returns each channel's state (ready or queued).
        ``sources``: channel -> S-48 user file (others are generated). ``only``: this is
        the latest choice (a test starts, or a movie was picked): making that it does not
        need stops, the rest goes on where it is (GAP-116)."""
        sources = sources or {}
        if quality not in QUALITIES:
            raise ValueError(f"unknown quality {quality}")
        if only:
            keys = {
                (quality, ch, muxrate_bps, source_tag(sources.get(ch), whole)) for ch in channels
            }
            with self._lock:
                busy = [j for j in self._jobs if j.state in ("making", "checking")]
                stale = any(j.key not in keys for j in busy)
            if stale:  # the file being made is not wanted any more: stop it, keep the others
                self.cancel()
            with self._lock:
                self._jobs = [j for j in self._jobs if j.key in keys or j.state == "ready"]
                if stale:
                    self._cancel.clear()
        out = []
        with self._lock:
            if not any(j.state in WAITING for j in self._jobs):
                self._jobs = []  # a new round: old results are dropped
                self._cancel.clear()
            for ch in channels:
                src = sources.get(ch)
                tag = source_tag(src, whole)
                key = (quality, ch, muxrate_bps, tag)
                job = next((j for j in self._jobs if j.key == key), None)
                if job is None or job.state in ("failed", "cancelled"):
                    if job is None and self.ready(quality, ch, muxrate_bps, src, whole):
                        out.append({"ch": ch, "state": "ready"})
                        continue
                    if job is not None:
                        self._jobs.remove(job)
                    job = Job(ch, quality, muxrate_bps, str(src) if src else None, tag,
                              whole=whole and src is not None)  # fmt: skip
                    self._jobs.append(job)
                out.append({"ch": ch, "state": job.state})
            if any(j.state == "queued" for j in self._jobs) and not (
                self._worker and self._worker.is_alive()
            ):
                self._worker = threading.Thread(target=self._work, name="iptv-prepare", daemon=True)
                self._worker.start()
        return out

    def cancel(self) -> list[int]:
        """Stop making: the file in progress is removed, finished ones kept. Only a node
        safe stop or a newer choice asks for it — a test's Stop or Cancel lets the making
        go on (GAP-116: every Start made an 11-minute movie again)."""
        with self._lock:
            waiting = [j.ch for j in self._jobs if j.state in WAITING]
            if waiting:
                self._cancel.set()
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(30)
        return waiting

    # ---------------------------------------------------------------- worker

    def _work(self) -> None:
        while True:
            with self._lock:
                job = next((j for j in self._jobs if j.state == "queued"), None)
                if job is None:
                    return
                if self._cancel.is_set():
                    for j in self._jobs:
                        if j.state in WAITING:
                            j.state = "cancelled"
                    return
                job.state = "making"
                job.percent = 0.0
            self._make(job)

    def _make(self, job: Job) -> None:
        q = QUALITIES[job.quality]
        final = generated_path(self.folder, job.quality, job.ch, job.muxrate_bps, job.tag)
        part = final.with_suffix(".part.ts")
        progress = final.with_suffix(".progress")
        try:
            length = self.seconds
            if job.whole and job.source:  # S-55: room for the whole movie
                length = float(probe(Path(job.source), self.ffprobe).get("duration_s") or length)
            need = int(job.muxrate_bps * length / 8 / (1024 * 1024)) + DISK_MARGIN_MB
            free = self.free_mb(self.folder)
            if free < need:
                raise _Failed(f"not enough disk: {free} MB free, {need} MB needed")
            job_s = self._encode(job, q, part, progress)
            with self._lock:
                job.state, job.percent, job.left_s = "checking", None, None
            wrong = check_prepared(part, job.muxrate_bps, job.ch)
            if not wrong:
                wrong = end_problems(part, job_s, self.ffprobe)
            if not wrong:  # only a file that passed the rest is decoded (4.3, 20 W-7)
                wrong = self._check_markers(job, q, part, job_s)
            if wrong:
                raise _Failed("check failed: " + "; ".join(wrong))
            self._clear(progress, part.with_suffix(".log"))  # before "ready" is published
            self._keep(job, part, final)
        except _Cancelled:
            self._clear(part, progress, part.with_suffix(".log"))
            with self._lock:
                job.state, job.percent, job.left_s = "cancelled", None, None
            self._event("ZL-INFO", "info", f"CH{job.ch} {job.quality} video: cancelled")
        # StartError: ffmpeg could not be started (not installed on a new PC, 2026-10-24):
        # before, the worker died and the channel said "making" for ever
        except (_Failed, OSError, StartError, StopError) as e:
            self._clear(
                part, progress, part.with_suffix(".log")
            )  # nothing left once "failed" shows
            with self._lock:
                job.state, job.percent, job.left_s, job.error = "failed", None, None, str(e)
            self._event(
                "ZL-SND-020", "error", f"Could not prepare the CH{job.ch} video: {e}", ch=job.ch
            )
        finally:
            for leftover in (part, progress, part.with_suffix(".log")):
                leftover.unlink(missing_ok=True)

    @staticmethod
    def _clear(*paths: Path) -> None:
        """Leftovers go before the new state is published, so "not busy" already means
        "nothing left behind" (a person or a test looking right then sees a clean folder)."""
        for leftover in paths:
            leftover.unlink(missing_ok=True)

    def _encode(self, job: Job, q: Any, part: Path, progress: Path) -> float:
        """Make ``part``; returns its length in seconds."""
        src = Path(job.source) if job.source else None
        facts = probe(src, self.ffprobe) if src else {}
        if src is not None and facts.get("error"):
            raise _Failed(f"{src.name}: {facts['error']}")
        whole = job.whole and src is not None  # S-55: the whole movie, else its first 10 min
        length = facts.get("duration_s") or self.seconds
        job_s = float(length) if whole else min(self.seconds, length)
        argv = prepare_command(
            self.ffmpeg, q, job.ch, part, source=src, seconds=None if whole else self.seconds,
            muxrate_bps=job.muxrate_bps, preset=self.preset, progress=progress,
            source_audio=bool(facts.get("audio", True)),
        )  # fmt: skip
        self._facts = facts
        self._run(job, f"prepare_ch{job.ch}", argv, progress, part.with_suffix(".log"), job_s)
        return job_s

    def _check_markers(self, job: Job, q: Any, part: Path, job_s: float) -> list[str]:
        """4.3 marker check (iptv 3.4, 20 W-7): decode once, every frame's marker row as a
        small grey strip, then read them all. Same progress, cancel and stall watch as
        making the file."""
        height = int(q.size.split("x")[1])
        strips = part.with_suffix(".markers")
        progress = part.with_suffix(".mprogress")
        log = part.with_suffix(".mlog")
        argv = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats", "-y",
                "-progress", str(progress), "-i", str(part), "-map", "0:v:0",
                "-vf", strip_filter(height), "-f", "rawvideo", str(strips)]  # fmt: skip
        try:
            self._run(job, f"markers_ch{job.ch}", argv, progress, log, job_s)
            return marker_problems(strips, job.ch)
        finally:
            for leftover in (strips, progress, log):
                leftover.unlink(missing_ok=True)

    def _run(
        self, job: Job, name: str, argv: list[str], progress: Path, log: Path, total_s: float
    ) -> None:
        """One ffmpeg run: progress into the job, Cancel (USR-006), no progress for
        ``stall_s`` and a bad exit fail it (SND-020); always stopped and confirmed."""
        runner = ProcessRunner(name, argv, self.procs_dir, log)
        self._runner = runner
        runner.start()
        t0 = last_change = time.monotonic()
        done_s = 0.0
        try:
            while runner.poll() is ProcState.RUNNING:
                if self._cancel.wait(0.5):
                    raise _Cancelled
                now = time.monotonic()
                got = _out_time_s(progress)
                if got > done_s:
                    done_s, last_change = got, now
                elif now - last_change > self.stall_s:
                    raise _Failed(f"no progress for {self.stall_s:g} s")
                speed = done_s / (now - t0) if now > t0 else 0.0
                with self._lock:
                    job.percent = round(min(done_s / total_s, 1.0) * 100, 1)
                    job.left_s = int((total_s - done_s) / speed) if speed > 0 else None
        finally:
            result = runner.stop()  # also after FAULT (ffmpeg finished): confirm and clean
            self._runner = None
        if result.exit_code != 0:
            tail = _last_line(log)
            raise _Failed(f"ffmpeg exited with {result.exit_code}: {tail}")

    def _keep(self, job: Job, part: Path, final: Path) -> None:
        digest = hashlib.sha256()
        with part.open("rb") as f:
            while block := f.read(1 << 20):
                digest.update(block)
        src = Path(job.source) if job.source else None
        facts = getattr(self, "_facts", {}) if src else {}
        desc = {
            "key": self._key(job.quality, job.ch, job.muxrate_bps, src, job.tag, job.whole),
            "source_name": unicodedata.normalize("NFC", src.name) if src else None,  # 1-1-1
            "source_size": [facts.get("width"), facts.get("height")] if src else None,
            "source_fps": facts.get("fps") if src else None,
            "upscaled": bool(src and fits(facts)[job.quality]["upscaled"]),  # K-28
            "bytes": part.stat().st_size,
            "sha256": digest.hexdigest(),
            "check": [],
            "made_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        part.replace(final)
        tmp = final.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(desc, indent=2), encoding="utf-8")
        tmp.replace(final.with_suffix(".json"))
        with self._lock:
            job.state, job.percent, job.left_s = "ready", None, None
        self._event("ZL-INFO", "info", f"CH{job.ch} {job.quality} video ready")

    def _key(
        self, quality: str, ch: int, muxrate_bps: int, source: Path | None = None, tag: str = "",
        whole: bool = False,
    ) -> dict[str, Any]:  # fmt: skip
        # how the file is made: any change of the ffmpeg command makes old files again
        # (GAP-114: a fix in the command must reach files made before it) -- except the
        # encoder's speed (preset): it changes how long making takes, not the stream the
        # test sends (bitrate, HRD, marker stay forced). 2026-10-24: the move to x265
        # ultrafast counted every movie made at veryfast as another recipe and started
        # making all three again (hours).
        argv = prepare_command("ffmpeg", QUALITIES[quality], ch, Path("out.ts"),
                               source=Path("in") if source is not None else None,
                               seconds=None if whole else self.seconds, muxrate_bps=muxrate_bps,
                               preset=RECIPE_PRESET)  # fmt: skip
        recipe = hashlib.sha256("\0".join(argv).encode()).hexdigest()[:16]
        return {
            "source": "file" if source is not None else "generated", "digest": tag or None,
            "quality": quality, "ch": ch, "muxrate_bps": muxrate_bps,
            "seconds": None if whole else self.seconds, "marker": MARKER_VERSION, "recipe": recipe,
        }  # fmt: skip

    def _event(self, code: str, level: Level, text: str, **data: Any) -> None:
        self.bus.publish(Event(code, level, text, data={"module": "iptv", **data}))


RECIPE_PRESET = "veryfast"  # the fixed preset in a made file's recipe (see _key)


class _Failed(Exception):
    pass


class _Cancelled(Exception):
    pass


def _out_time_s(progress: Path) -> float:
    """Last ``out_time_us`` in ffmpeg's -progress file (0 until the first block)."""
    try:
        text = progress.read_text(encoding="ascii", errors="replace")
    except OSError:
        return 0.0
    i = text.rfind("out_time_us=")
    if i < 0:
        return 0.0
    try:
        return int(text[i + 12 :].split("\n", 1)[0]) / 1e6
    except ValueError:
        return 0.0


def _last_line(log: Path) -> str:
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    return lines[-1][:200] if lines else ""
