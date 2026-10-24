from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from core.blocks.rate_meter import RateMeter
from core.records.recorder import Recorder, recover


def read(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_rate_meter_bits_per_second() -> None:
    m = RateMeter()
    for _ in range(3300):
        m.on_packet(1328)  # 7 TS packets + RTP header
    assert m.take_second() == (3300, 3300 * 1328 * 8)
    assert m.take_second() == (0, 0)
    assert m.total_packets == 3300


def test_counters_do_not_overflow_past_32_bits() -> None:
    m = RateMeter()
    m.total_bytes = 2**32 - 10
    m.on_packet(1328)
    assert m.total_bytes > 2**32


def make(tmp_path: Path) -> Recorder:
    return Recorder(tmp_path, "R1", "tablet-1", "20260924.0100", {"quality": "UHD"}, fsync_every=5)


def test_header_seconds_footer(tmp_path: Path) -> None:
    r = make(tmp_path)
    for e in range(3):
        r.write_second(e, {"rx": 3300, "lost": 0})
    r.close("stop")
    rows = read(tmp_path / "R1_h000.jsonl")
    assert [row["type"] for row in rows] == ["header", "s", "s", "s", "footer"]
    assert rows[-1]["end_reason"] == "stop"
    assert rows[-1]["lines"] == 3


def test_new_file_every_hour(tmp_path: Path) -> None:
    r = make(tmp_path)
    for e in (3598, 3599, 3600, 3601):
        r.write_second(e, {"rx": 1})
    r.close("stop")
    first, second = read(tmp_path / "R1_h000.jsonl"), read(tmp_path / "R1_h001.jsonl")
    assert first[-1]["end_reason"] == "rollover"
    assert [row["e"] for row in second if row["type"] == "s"] == [3600, 3601]
    assert second[0]["type"] == "header"


def test_close_twice_is_harmless(tmp_path: Path) -> None:
    r = make(tmp_path)
    r.write_second(0, {})
    r.close("fault")
    r.close("stop")
    assert read(tmp_path / "R1_h000.jsonl")[-1]["end_reason"] == "fault"


def test_recover_cuts_partial_line_and_adds_footer(tmp_path: Path) -> None:
    r = make(tmp_path)
    for e in range(4):
        r.write_second(e, {"rx": 1})
    # Simulate a crash: file never closed, last line half written.
    path = tmp_path / "R1_h000.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write('{"type":"s","e":4,"rx"')
    assert recover(tmp_path) == [path]
    rows = read(path)
    assert rows[-1] == {**rows[-1], "type": "footer", "end_reason": "recovered", "lines": 4}
    assert recover(tmp_path) == []  # already closed


@settings(max_examples=60, deadline=None)
@given(
    steps=st.lists(st.integers(min_value=1, max_value=4000), min_size=1, max_size=30),
    crash=st.booleans(),
    partial=st.booleans(),
)
def test_random_writes_always_leave_closed_consistent_files(
    steps: list[int], crash: bool, partial: bool
) -> None:
    """Invariant: after close or crash + recover, every file is header ... footer,
    footer.lines equals its second lines, and no second is lost or duplicated."""
    with tempfile.TemporaryDirectory() as d:
        folder = Path(d)
        r = Recorder(folder, "R1", "pc-1", "20260924.0100", {}, fsync_every=3)
        written, e = [], 0
        for step in steps:
            r.write_second(e, {"rx": 1})
            written.append(e)
            e += step
        if crash:
            last = sorted(folder.glob("*.jsonl"))[-1]
            if partial:
                with last.open("a", encoding="utf-8") as f:
                    f.write('{"type":"s","e":')
            recover(folder)
        else:
            r.close("stop")
        seen = []
        for path in sorted(folder.glob("*.jsonl")):
            rows = read(path)
            assert rows[0]["type"] == "header"
            assert rows[-1]["type"] == "footer"
            secs = [row["e"] for row in rows if row["type"] == "s"]
            assert rows[-1]["lines"] == len(secs)
            hour = int(path.stem.split("_h")[1])
            assert all(int(str(s)) // 3600 == hour for s in secs)
            seen += secs
        assert seen == written


class _Broken:
    """A file whose writes fail (disk gone, 09 RCV-009)."""

    def write(self, _: str) -> int:
        raise OSError(5, "Input/output error")

    def flush(self) -> None:
        raise OSError(5, "Input/output error")

    def fileno(self) -> int:
        raise OSError(5, "Input/output error")

    def close(self) -> None:
        raise OSError(5, "Input/output error")


def test_a_write_error_moves_to_a_new_file_once(tmp_path: Path) -> None:
    from core.records.recorder import RecordWriteError

    r = Recorder(tmp_path, "R1", "rx", "b", {})
    assert r.write_second(0, {"rx": 1}) is None
    r._file = _Broken()  # type: ignore[assignment]
    why = r.write_second(1, {"rx": 1})
    assert why and "Input/output error" in why
    assert r.write_second(2, {"rx": 1}) is None  # goes on in the new file
    r.close("stop")
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["R1_h000.jsonl", "R1_h000_r1.jsonl"]
    new = [json.loads(x) for x in (tmp_path / "R1_h000_r1.jsonl").read_text().splitlines()]
    assert [x.get("e") for x in new if x["type"] == "s"] == [1, 2] and new[-1]["type"] == "footer"
    assert recover(tmp_path) == [tmp_path / "R1_h000.jsonl"]  # the cut file gets its footer

    r2 = Recorder(tmp_path / "b", "R2", "rx", "b", {})
    r2.write_second(0, {})
    r2._file = _Broken()  # type: ignore[assignment]
    real_open = Path.open

    def no_new_file(self: Path, *a: Any, **kw: Any) -> Any:
        if "_r" in self.name:
            raise OSError(28, "No space left on device")
        return real_open(self, *a, **kw)

    with pytest.MonkeyPatch.context() as m:
        m.setattr(Path, "open", no_new_file)
        with pytest.raises(RecordWriteError) as e:
            r2.write_second(1, {})
    assert "No space left" in str(e.value)
    r2.close("fault")  # still closes without raising
