"""1-second records after a power cut or a full disk (2026-11-21 review, F12 / GAP-47):
one damaged line made ``recover`` raise at the module's start (the node came up without
the module), and a full disk made ``close`` raise in the middle of a stop."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.records.recorder import Recorder, recover


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_a_damaged_line_is_left_out_and_the_rest_is_kept(tmp_path: Path) -> None:
    f = tmp_path / "R1_h000.jsonl"
    f.write_bytes(
        b'{"type":"header","run_id":"R1"}\n{"type":"s","e":0,"rx":1}\n'
        b"\x00\x00\x00\x00garbage\n"  # a block a power cut left damaged
        b'{"type":"s","e":1,"rx":1}\n{"type":"s","e":2,"r'  # cut in the middle of a line
    )
    assert recover(tmp_path) == [f]
    rows = _rows(f)  # every line is JSON again
    assert [r["type"] for r in rows] == ["header", "s", "s", "footer"]
    assert rows[-1]["end_reason"] == "recovered" and rows[-1]["lines"] == 2
    assert recover(tmp_path) == []  # nothing more to do


def test_a_file_that_cannot_be_read_does_not_stop_the_others(tmp_path: Path) -> None:
    (tmp_path / "R1_h000.jsonl").mkdir()  # not a file: reading it fails
    ok = tmp_path / "R1_h001.jsonl"
    ok.write_text('{"type":"header"}\n{"type":"s","e":3600}\n', encoding="utf-8")
    assert recover(tmp_path) == [ok]


def test_close_on_a_full_disk_does_not_raise(tmp_path: Path) -> None:
    r = Recorder(tmp_path, "R1", "pc-1", "20261121.0000", {})
    r.write_second(0, {"rx": 1})

    class _Full:
        def __init__(self, real: Any) -> None:
            self.real = real

        def write(self, _: str) -> int:
            raise OSError(28, "No space left on device")

        def flush(self) -> None:
            return

        def fileno(self) -> int:
            return int(self.real.fileno())

        def close(self) -> None:
            self.real.close()

    r._file = _Full(r._file)  # type: ignore[assignment]
    r.close("stop")  # no error: the stop goes on
    r.close("stop")  # and once more is nothing
    assert recover(tmp_path) == [tmp_path / "R1_h000.jsonl"]  # the footer comes next start
    assert _rows(tmp_path / "R1_h000.jsonl")[-1]["type"] == "footer"
