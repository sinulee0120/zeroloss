"""The product's own checks (02 2-13 ``verify``, 13 ``zeroloss verify``, 12 2
``self_checks``, 20 GAP-81): every known-answer row runs through the real blocks, a
broken check is one failed line, and the loopback check is one short real test."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from controller.cli import main
from controller.run import RunState
from controller.service import ControllerService
from core import self_checks
from core.protocol.client import NodeClient
from core.protocol.envelope import ZlError
from core.self_check import CheckFailed
from modules.iptv import self_checks as iptv_checks

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def svc(tmp_path: Path) -> Iterator[ControllerService]:
    s = ControllerService(tmp_path / "ctl", tmp_path / "reports", REPO / "modules",
                          host="127.0.0.1", port=0, local_port=0)  # fmt: skip
    s.start()
    yield s
    s.stop()


def verify(svc: ControllerService, capsys: pytest.CaptureFixture[str], *argv: str) -> Any:
    code = main(["--json", "--port", str(svc.node.local.port), "verify", *argv])
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_default_checks_all_pass_and_cover_every_table_row(
    svc: ControllerService, capsys: pytest.CaptureFixture[str]
) -> None:
    code, doc = verify(svc, capsys)
    d = doc["data"]
    assert code == 0 and d["failed"] == 0, [c for c in d["checks"] if not c["ok"]]
    assert d["suites"] == ["accuracy", "judgement"] and d["run_id"] is None
    names = {c["name"] for c in d["checks"]}
    assert {f"loss count: {r[0]}" for r in self_checks.SEQ_ROWS} <= names
    assert {f"IPTV analyzer: {r[0]}" for r in iptv_checks.STREAM_ROWS} <= names
    assert {f"5-minute color: {r[0]}" for r in iptv_checks.CELL_ROWS} <= names
    verdicts = [c for c in d["checks"] if c["name"].startswith("verdict: ")]
    assert len(verdicts) == len(self_checks.VERDICT_ROWS)
    assert d["passed"] == len(d["checks"]) and all(c["ms"] >= 0 for c in d["checks"])
    code, doc = verify(svc, capsys, "--suite", "judgement")
    assert code == 0 and {c["suite"] for c in doc["data"]["checks"]} == {"judgement"}


def test_a_wrong_answer_is_one_failed_line_and_exit_1(
    svc: ControllerService, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    real = svc._self_checks

    def wrong() -> str:
        raise CheckFailed("lost: got 56, expected 57")

    def broken() -> str:
        raise KeyError("x")

    monkeypatch.setattr(svc, "_self_checks", lambda: [
        ("accuracy", "wrong on purpose", wrong), ("accuracy", "broken on purpose", broken),
        *real()])  # fmt: skip
    code, doc = verify(svc, capsys)
    d = doc["data"]
    assert code == 1 and d["failed"] == 2 and d["passed"] == len(real())
    bad = {c["name"]: c["detail"] for c in d["checks"] if not c["ok"]}
    assert bad == {"wrong on purpose": "lost: got 56, expected 57",
                   "broken on purpose": "KeyError: 'x'"}  # fmt: skip


def test_unknown_suite_and_loopback_during_a_test_are_refused(svc: ControllerService) -> None:
    c = NodeClient("127.0.0.1", svc.node.local.port, "cli")
    try:
        for params in ({"suites": ["speed"]}, {"suites": "accuracy"}, {"seconds": 5}):
            with pytest.raises(ZlError) as e:
                c.command("/v1/controller/verify", 0, timeout_s=30, **params)
            assert e.value.code == "ZL-DSC-012", params
        svc.run.state = RunState.RUNNING
        with pytest.raises(ZlError) as e:
            c.command("/v1/controller/verify", 0, timeout_s=30, suites=["loopback"])
        assert e.value.code == "ZL-RUN-012"
        done = c.command("/v1/controller/verify", 0, timeout_s=30, suites=["judgement"])
        assert done["failed"] == 0  # the checks alone may run during a test
    finally:
        svc.run.state = RunState.IDLE
        c.close()


class FakeRun:
    """Stands in for one real test: start, run in the background, report."""

    def __init__(self, svc: ControllerService, verdict: str, colors: dict[str, int]) -> None:
        self.svc, self.verdict, self.colors = svc, verdict, colors
        self.plans: list[dict[str, Any]] = []

    def start(self, plan: dict[str, Any]) -> str:
        self.plans.append(plan)
        run = self.svc.run
        run.state, run.run_id = RunState.RUNNING, "R-verify"

        def finish() -> None:
            run.result = {"run_id": "R-verify", "verdict": self.verdict, "devices": [
                {"device": "rx1", "colors": {"green": 1, **self.colors}}]}  # fmt: skip
            run.state = RunState.IDLE

        threading.Timer(0.3, finish).start()
        return "R-verify"


LOOPBACK_CASES = [("PASS", {}, True), ("FAIL", {"red": 1}, False),
                  ("INCOMPLETE", {"grey": 1}, False)]  # fmt: skip


@pytest.mark.parametrize(("verdict", "colors", "ok"), LOOPBACK_CASES)
def test_loopback_is_one_short_test_with_one_line_per_receiver(
    svc: ControllerService, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
    verdict: str, colors: dict[str, int], ok: bool,
) -> None:  # fmt: skip
    fake = FakeRun(svc, verdict, colors)
    monkeypatch.setattr(svc.run, "start", fake.start)
    monkeypatch.setattr(
        svc, "_setup_plan", lambda: {"plan": {"module": "iptv", "duration_s": 3600}}
    )
    code, doc = verify(svc, capsys, "--suite", "loopback", "--seconds", "10")
    d = doc["data"]
    assert fake.plans == [{"module": "iptv", "duration_s": 10}]  # the choice, shortened
    assert d["run_id"] == "R-verify" and [c["suite"] for c in d["checks"]] == ["loopback"]
    (row,) = d["checks"]
    assert row["name"] == "short real test: rx1" and row["ok"] is ok and verdict in row["detail"]
    assert code == (0 if ok else 1)
