"""1-second record files (docs/02-data.md 7, docs/03-features.md F12).

- JSON Lines: header line, one line per second, footer line.
- A new file every hour of elapsed time: ``<run_id>_h<hour:03d>.jsonl``.
- One writer per file. Each line is flushed; fsync every ``fsync_every`` lines so a power
  cut loses at most that many seconds.
- ``recover()`` closes files left open by a crash: a partial last line is cut off and a
  footer with ``end_reason = "recovered"`` is added.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

FILE_SECONDS = 3600
RECORD_NAME = re.compile(r"^([A-Za-z0-9_.-]{1,64})_h(\d{3,})(?:_r\d+)?\.jsonl$")  # run, hour
ZAP_NAME = re.compile(r"^([A-Za-z0-9_.-]{1,64})_zaps\.jsonl$")  # channel changes (02 7-2)
# evidence of a trouble second (03 F23, 20 GAP-154): json, packet clip, picture
EVIDENCE_NAME = re.compile(r"^([A-Za-z0-9_.-]{1,64})_ev_e\d{6}_ch\d{1,2}\.(?:json|pcap|jpg)$")


def record_run(name: str) -> tuple[str, int | None] | None:
    """(run, hour) of a 1-second record file, (run, None) of a zaps or evidence file
    (F23), else None."""
    m = RECORD_NAME.match(name)
    if m:
        return m.group(1), int(m.group(2))
    z = ZAP_NAME.match(name) or EVIDENCE_NAME.match(name)
    return (z.group(1), None) if z else None


DEVICE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")  # the folder under raw/


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _dumps(obj: dict[str, Any]) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


class RecordWriteError(OSError):
    """The record could not be written even to a new file (09 RCV-009)."""


class Recorder:
    def __init__(
        self,
        folder: Path,
        run_id: str,
        device: str,
        build: str,
        settings: dict[str, Any],
        fsync_every: int = 10,
    ) -> None:
        self._folder = folder
        self._run_id = run_id
        self._header = {
            "type": "header",
            "run_id": run_id,
            "device": device,
            "build": build,
            "settings": settings,
        }
        self._fsync_every = fsync_every
        self._file: IO[str] | None = None
        self._hour: int | None = None
        self._lines = 0
        self._closed = False
        self._retries = 0
        self._path: Path | None = None
        folder.mkdir(parents=True, exist_ok=True)

    def path_for_hour(self, hour: int, retry: int = 0) -> Path:
        tail = f"_r{retry}" if retry else ""  # RCV-009: another file after a write error
        return self._folder / f"{self._run_id}_h{hour:03d}{tail}.jsonl"

    def write_second(self, elapsed_s: int, values: dict[str, Any]) -> str | None:
        """Write one second. A write error moves to a new file once (09 RCV-009) and
        returns why; a second failure raises ``RecordWriteError``."""
        if self._closed:
            raise RuntimeError("Recorder is closed")
        line = {"type": "s", "e": elapsed_s, **values}
        hour = elapsed_s // FILE_SECONDS
        try:
            if hour != self._hour:
                self._finish_file("rollover")
                self._open(hour)
            self._write(line)
            return None
        except OSError as e:
            why = f"{self._path}: {e}"
        self._drop_file()
        self._retries += 1
        try:
            self._open(hour, retry=self._retries)
            self._write(line)
        except OSError as e:
            self._drop_file()
            raise RecordWriteError(f"{why}; new file {self._path}: {e}") from e
        return why

    def close(self, end_reason: str) -> None:
        """Always call on stop, fault or disconnect (end_reason: stop, fault, disconnect)."""
        if self._closed:
            return
        self._finish_file(end_reason)
        self._closed = True

    # ---------------------------------------------------------------- helpers

    def _open(self, hour: int, retry: int = 0) -> None:
        self._path = self.path_for_hour(hour, retry)
        self._file = self._path.open("a", encoding="utf-8", newline="\n")
        self._hour = hour
        self._lines = 0
        self._write(self._header)

    def _write(self, obj: dict[str, Any]) -> None:
        assert self._file is not None
        self._file.write(_dumps(obj) + "\n")
        self._file.flush()
        if obj.get("type") == "s":
            self._lines += 1
            if self._lines % self._fsync_every == 0:
                os.fsync(self._file.fileno())

    def _drop_file(self) -> None:
        """A file we cannot write any more: close it without a footer (recover() adds
        one later), never raise."""
        f, self._file = self._file, None
        if f is not None:
            with contextlib.suppress(OSError):
                f.close()

    def _finish_file(self, end_reason: str) -> None:
        if self._file is None:
            return
        self._write(
            {"type": "footer", "end_reason": end_reason, "lines": self._lines, "ts_utc": _utc_now()}
        )
        os.fsync(self._file.fileno())
        self._file.close()
        self._file = None


def recover(folder: Path) -> list[Path]:
    """Add a footer to every record file that has none. Returns the repaired files."""
    repaired = []
    for path in sorted(folder.glob("*.jsonl")):
        if not RECORD_NAME.match(path.name):
            continue  # zaps file: written a line at a time, readers skip a cut line
        data = path.read_bytes()
        if not data:
            continue
        cut = data if data.endswith(b"\n") else data[: data.rfind(b"\n") + 1]
        lines = cut.decode("utf-8").splitlines()
        if lines and json.loads(lines[-1]).get("type") == "footer":
            continue
        seconds = sum(1 for line in lines if json.loads(line).get("type") == "s")
        footer = {
            "type": "footer",
            "end_reason": "recovered",
            "lines": seconds,
            "ts_utc": _utc_now(),
        }
        with path.open("wb") as f:
            f.write(cut + (_dumps(footer) + "\n").encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        repaired.append(path)
    return repaired
