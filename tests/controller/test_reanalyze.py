"""Second analysis pass (iptv 2.4, 02 2-13 ``records``, 20 GAP-85): with the 1-second
records in the report folder, the cause candidates are found again on the run clock,
only the analysis fields and the page change, and the verdict stays."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from controller.analysis import reanalyze, seconds_of
from controller.report import write_report

REPO = Path(__file__).resolve().parents[2]
PLAN = {"schema_version": 1, "module": "iptv", "test_type": "pc", "quality": "HD",
        "duration_s": 600, "sender": "tx", "receivers": [{"device": "a", "ch": 1},
                                                        {"device": "b", "ch": 2}]}  # fmt: skip


def run_folder(tmp: Path, lost_b_at: int) -> Path:
    d = tmp / "R20260927-120000"
    (d / "raw" / "a").mkdir(parents=True)
    (d / "raw" / "b").mkdir(parents=True)
    summary: dict[str, Any] = {
        "run_id": d.name, "verdict": "FAIL", "verdict_reason": "planned_time_reached",
        "verdict_details": ["2 red cell(s)"], "with_warnings": False, "not_run": [],
        "planned_s": 600, "actual_s": 600, "test_type": "pc", "quality": "HD", "devices": [],
        "problems": [], "analysis": [], "analysis_pass": "report",
        "analysis_missing": [{"rule": "R-05", "why": "1-second records not fetched yet"}],
    }  # fmt: skip
    write_report(d, summary, PLAN)
    (d / "plan.json").write_text(json.dumps(PLAN))
    (d / "timeline.json").write_text(json.dumps({"a|1": 20.0, "b|2": 20.5}))
    (d / "events.log").write_text("")
    for dev, ch, at in (("a", 1, 300), ("b", 2, lost_b_at)):
        lines: list[dict[str, Any]] = [{"type": "header", "run_id": d.name, "device": dev}]
        lines += [{"type": "s", "e": e, "ch": ch, "lost": 4 if e == at else 0} for e in range(400)]
        (d / "raw" / dev / f"{d.name}_h000.jsonl").write_text(
            "\n".join(json.dumps(x) for x in lines) + "\n{cut"
        )  # a cut last line is skipped
    return d


def test_records_turn_into_candidates_and_the_verdict_stays(tmp_path: Path) -> None:
    d = run_folder(tmp_path, lost_b_at=300)
    assert set(seconds_of(d / "raw")) == {"a|1", "b|2"}
    assert reanalyze(d, REPO / "modules") is True
    s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    assert s["verdict"] == "FAIL" and s["verdict_details"] == ["2 red cell(s)"]  # untouched
    assert s["analysis_pass"] == "records"
    r05 = next(f for f in s["analysis"] if f["rule"] == "R-05")
    assert r05["devices"] == ["a", "b"] and r05["at_s"] == 320.0  # 300 + offset 20
    assert "R-05" not in {m["rule"] for m in s["analysis_missing"]}
    page = (d / "report.html").read_text(encoding="utf-8")
    assert "Possible causes" in page and "R-05" in page and "with the 1-second records" in page
    assert reanalyze(d, REPO / "modules") is False  # nothing new: the files stay


def test_apart_losses_are_not_together(tmp_path: Path) -> None:
    d = run_folder(tmp_path, lost_b_at=350)
    reanalyze(d, REPO / "modules")
    s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    assert "R-05" not in [f["rule"] for f in s["analysis"]]


def test_nothing_to_do_without_records_or_report(tmp_path: Path) -> None:
    d = run_folder(tmp_path, lost_b_at=300)
    for f in (d / "raw").rglob("*.jsonl"):
        f.unlink()
    assert reanalyze(d, REPO / "modules") is False
    assert reanalyze(tmp_path / "missing", REPO / "modules") is False
