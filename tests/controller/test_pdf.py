"""report.pdf with the standard library only (03 F16 implementation rules, 20 GAP-86):
a valid PDF (cross-reference offsets right), its text readable by a PDF reader when
one is installed, long content on more pages, and every report written with a PDF."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from controller.pdf import Pdf, plain, wrap
from controller.report import render_pdf, write_report


def result(n_cells: int = 12, **over: Any) -> dict[str, Any]:
    cells = [{"color": "red" if i == 5 else "green", "index": i} for i in range(n_cells)]
    r: dict[str, Any] = {
        "run_id": "R20260927-120000", "build": "20260927.1200", "verdict": "FAIL",
        "with_warnings": False, "verdict_reason": "planned_time_reached",
        "verdict_details": ["1 red cell(s)"], "test_type": "pc", "quality": "HD",
        "planned_s": 3600, "actual_s": 3600, "not_run": ["playback"],
        "devices": [{"device": "rx", "ch": 1, "loss_total": 3, "jitter_max_ms": 2.5,
                     "cells": cells, "colors": {"green": n_cells - 1, "red": 1}}],
        "analysis": [{"rule": "R-05", "category": "Router", "confidence": "high",
                      "text": "2 receivers started losing → shared path"}],
        "problems": [{"code": "ZL-NET-013", "device": "tx", "what": "Link 100 Mbps",
                      "level": "error", "open_at_end": False, "count": 1}],
    }  # fmt: skip
    return r | over


def check_structure(data: bytes) -> int:
    """Every xref entry points at its object; returns the page count."""
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    start = int(re.search(rb"startxref\n(\d+)", data).group(1))  # type: ignore[union-attr]
    assert data[start:].startswith(b"xref")
    entries = re.findall(rb"(\d{10}) 00000 n ", data[start:])
    for n, off in enumerate(entries, start=1):
        assert data[int(off) :].startswith(b"%d 0 obj" % n), n
    return int(re.search(rb"/Count (\d+)", data).group(1))  # type: ignore[union-attr]


def test_text_and_boxes_make_a_valid_pdf(tmp_path: Path) -> None:
    data = render_pdf(result())
    assert check_structure(data) == 1
    if shutil.which("pdftotext"):
        f = tmp_path / "r.pdf"
        f.write_bytes(data)
        text = subprocess.run(["pdftotext", str(f), "-"], capture_output=True, text=True,
                              check=True).stdout  # fmt: skip
        for want in ("ZeroLoss test report", "FAIL", "R-05", "-> shared path",
                     "ZL-NET-013", "playback: NOT RUN", "router problem"):  # fmt: skip
            assert want in text, want


def test_long_reports_go_on_to_more_pages() -> None:
    long = result(n_cells=100 * 12, problems=[
        {"code": "ZL-NET-013", "device": "tx", "what": f"Link change {i}", "count": 1,
         "level": "warn", "open_at_end": False}
        for i in range(120)])  # fmt: skip
    assert check_structure(render_pdf(long)) >= 3


def test_text_helpers() -> None:
    assert plain("✔ 한글 → ok") == "OK ?? -> ok"  # the built-in font has no Korean
    assert all(len(line) < 60 for line in wrap("word " * 50, 10, 250))
    doc = Pdf()
    doc.text(10, 10, "a (b) \\ c")
    assert "(a \\(b\\) \\\\ c) Tj" in doc.pages[0][0]


@pytest.mark.parametrize("verdict", ["PASS", "INCOMPLETE"])
def test_every_report_has_a_pdf(tmp_path: Path, verdict: str) -> None:
    write_report(tmp_path, result(verdict=verdict), {"module": "iptv"})
    assert check_structure((tmp_path / "report.pdf").read_bytes()) == 1
    assert not list(tmp_path.glob("*.tmp"))


def test_a_throughput_report_has_no_jitter_or_loss_totals(tmp_path: Path) -> None:
    """2026-10-17: the first Throughput · All stop failed with KeyError('jitter_max_ms')
    and stayed in REPORTING -- its device totals are cases and rates, not loss/jitter."""
    r = result(test_type="all")
    r["devices"] = [{"device": "t460", "ch": 1, "cases": 2, "pass": 1, "fail": 1,
                     "not_run": 0, "cells": [{"color": "green", "index": 0}],
                     "colors": {"green": 1}}]  # fmt: skip
    write_report(tmp_path, r, {"module": "throughput"})
    assert (tmp_path / "report.html").is_file() and (tmp_path / "report.pdf").is_file()
