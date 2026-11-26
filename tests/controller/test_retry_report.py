"""09 CTL-007 [Retry report] (2026-11-22 review: the fix text promised a button that did
not exist): the failed report opens an action with the button, and the button makes the
report again from what the test kept."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import controller.run as controller_run
from controller.actions import ActionBoard
from controller.run import RunController, RunState
from core.events.bus import Event
from core.protocol.envelope import ZlError


def _controller(tmp_path: Path, state: RunState = RunState.IDLE) -> SimpleNamespace:
    said: list[tuple[str, dict[str, Any]]] = []
    rc = SimpleNamespace(
        _lock=threading.RLock(), state=state, reports_dir=tmp_path, result=None, said=said,
        _event=lambda code, level, text, device="", **data: said.append((code, data)),
        _recover=lambda run_dir, st: said.append(("recover", {"dir": run_dir.name})),
    )  # fmt: skip
    return rc


def _run(tmp_path: Path, run_id: str, summary: dict[str, Any] | None, status: str) -> Path:
    d = tmp_path / run_id
    d.mkdir()
    (d / "plan.json").write_text(json.dumps({"module": "iptv"}), encoding="utf-8")
    (d / "run-state.json").write_text(json.dumps({"status": status}), encoding="utf-8")
    if summary is not None:
        (d / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return d


def test_the_report_is_made_again_from_its_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(controller_run, "write_report",
                        lambda d, result, plan: made.append((d.name, result)))  # fmt: skip
    _run(tmp_path, "R1", {"run_id": "R1", "verdict": "PASS"}, "finished")
    rc = _controller(tmp_path)
    assert RunController.retry_report(rc, "R1") == "made"  # type: ignore[arg-type]
    assert made == [("R1", {"run_id": "R1", "verdict": "PASS"})]
    assert rc.said[-1][1]["clears"] == ["ZL-CTL-007"]


def test_a_failed_recovery_is_run_again(tmp_path: Path) -> None:
    _run(tmp_path, "R2", None, "running")
    rc = _controller(tmp_path)
    assert RunController.retry_report(rc, "R2") == "recovered"  # type: ignore[arg-type]
    assert rc.said == [("recover", {"dir": "R2"})]


@pytest.mark.parametrize("run_id", ["", "..", "R9", "../R1"])
def test_only_a_kept_test_and_only_between_tests(tmp_path: Path, run_id: str) -> None:
    _run(tmp_path, "R1", {"run_id": "R1"}, "finished")
    with pytest.raises(ZlError):
        RunController.retry_report(_controller(tmp_path), run_id)  # type: ignore[arg-type]
    with pytest.raises(ZlError) as e:
        RunController.retry_report(_controller(tmp_path, RunState.RUNNING), "R1")  # type: ignore[arg-type]
    assert e.value.code == "ZL-RUN-012"


def test_the_failed_report_opens_the_retry_button() -> None:
    board = ActionBoard(grace=False)
    data = {"run_id": "R1", "choose": "retry_report", "choices": [{"id": "R1", "label": "x"}]}
    board(Event("ZL-CTL-007", "error", "The report of R1 could not be made", data=data))
    row = board.snapshot()["actions"][0]
    assert row["action_id"] == "human.report.retry" and row["choose"] == "retry_report"
    assert [c["id"] for c in row["choices"]] == ["R1"]
    board(Event("ZL-INFO", "info", "made", data={"clears": ["ZL-CTL-007"]}))
    assert board.snapshot()["waiting"] == 0
