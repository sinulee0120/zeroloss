"""Past reports list (02 2-13 ``reports``, 03 F15 S8, 13 ``zeroloss reports``, 20 GAP-83):
newest first, start time from the run ID, a folder without a report says why, and
folders that are not runs (diagnostics) stay out."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from controller.cli import main
from controller.run import RunState
from controller.service import ControllerService

REPO = Path(__file__).resolve().parents[2]


def summary(run: str, verdict: str, reason: str = "planned_time_reached") -> dict[str, object]:
    return {"run_id": run, "verdict": verdict, "verdict_reason": reason, "test_type": "pc",
            "quality": "HD", "planned_s": 600, "actual_s": 600, "with_warnings": False}  # fmt: skip


@pytest.fixture
def svc(tmp_path: Path) -> Iterator[ControllerService]:
    rep = tmp_path / "rep"
    for run, verdict in (("R20260925-090000", "PASS"), ("R20260927-101500", "FAIL")):
        (rep / run).mkdir(parents=True)
        (rep / run / "summary.json").write_text(json.dumps(summary(run, verdict)))
    (rep / "R20260926-120000").mkdir()  # stopped by a crash before its report
    (rep / "R20260926-120000" / "plan.json").write_text("{}")
    (rep / "R20260926-130000").mkdir()  # a broken report
    (rep / "R20260926-130000" / "summary.json").write_text("{not json")
    (rep / "my-run").mkdir()  # an ID in another form: the folder's time
    (rep / "my-run" / "summary.json").write_text(json.dumps(summary("my-run", "INCOMPLETE")))
    os.utime(rep / "my-run", (1_000_000_000, 1_000_000_000))  # 2001
    (rep / "diagnostics").mkdir()  # not a run
    (rep / "diagnostics" / "diagnostics-x.zip").write_bytes(b"PK")
    s = ControllerService(tmp_path / "ctl", rep, REPO / "modules", host="127.0.0.1", port=0,
                          local_port=0)  # fmt: skip
    s.start()
    yield s
    s.run.state = RunState.IDLE
    s.stop()


def reports(svc: ControllerService, capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    code = main(["--json", "--port", str(svc.node.local.port), "reports"])
    doc = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0, doc
    return list(doc["data"]["reports"])


def test_newest_first_with_the_reason_when_there_is_no_report(
    svc: ControllerService, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = reports(svc, capsys)
    assert [r["run"] for r in rows] == [
        "R20260927-101500", "R20260926-130000", "R20260926-120000", "R20260925-090000",
        "my-run"]  # fmt: skip
    first = rows[0]
    assert first["started"] == "2026-09-27 10:15" and first["verdict"] == "FAIL"
    assert first["report"] == str(tmp_path / "rep" / "R20260927-101500" / "report.html")
    assert (first["test_type"], first["quality"], first["actual_s"]) == ("pc", "HD", 600)
    broken, crashed = rows[1], rows[2]
    assert broken["verdict"] is None and broken["why"].startswith("ZL-RPT-008")
    assert "JSONDecodeError" in str(broken["why"])
    assert crashed["verdict"] is None and crashed["planned_s"] is None
    assert rows[-1]["started"].startswith("2001-")


def test_the_test_on_now_says_running(
    svc: ControllerService, capsys: pytest.CaptureFixture[str]
) -> None:
    svc.run.run_id, svc.run.state = "R20260926-120000", RunState.RUNNING
    row = next(r for r in reports(svc, capsys) if r["run"] == "R20260926-120000")
    assert row["why"] == "Running"
