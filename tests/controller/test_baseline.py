"""Baseline test (05 4.6.3, S-54, 09 NET-020, 02 10 ``receiver_baseline``, 20 GAP-97)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from controller.baselines import BaselineStore
from controller.run import RunState
from core.protocol.envelope import ZlError
from tests.iptv.test_run_controller import hd_file, lab, plan, wait_state

__all__ = ["hd_file", "lab"]  # fixtures used by name

needs_tools = pytest.mark.skipif(
    not (shutil.which("tsp") and shutil.which("ffmpeg")), reason="needs ffmpeg and tsp"
)


def baseline(p: dict[str, Any], **settings: Any) -> dict[str, Any]:
    p["settings"] = {**p["settings"], "S-54": "Baseline (no router)", **settings}
    return p


def test_store_keeps_the_last_per_key_and_writes_whole_files(tmp_path: Path) -> None:
    s = BaselineStore(tmp_path / "b.json")
    assert s.all() == {} and s.get("pc|VLC") is None
    s.put({"pc|VLC": {"verified": False, "run_id": "R1"}})
    s.put({"pc|none": {"verified": True, "run_id": "R2"}})
    s.put({"pc|VLC": {"verified": True, "run_id": "R3"}})
    assert s.get("pc|VLC") == {"verified": True, "run_id": "R3"}
    assert set(s.all()) == {"pc|VLC", "pc|none"}
    assert not list(tmp_path.glob(".baselines-*"))  # no half-written leftovers
    (tmp_path / "b.json").write_text("{broken")
    assert s.all() == {}  # a damaged file reads as none, never a crash


@needs_tools
def test_a_baseline_run_verifies_the_receiver_and_later_reports_show_it(
    lab: Any, hd_file: Path
) -> None:
    _, rc, _ = lab
    run_id = rc.start(baseline(plan(hd_file, duration=10)))
    assert wait_state(rc, RunState.IDLE, 60)
    r = rc.result
    assert r is not None and r["verdict"] == "PASS" and r["test_network"] == "baseline"
    got = r["receiver_baseline"]["local"]
    assert got["verified"] and got["player"] == "mpv" and got["run_id"] == run_id
    assert got["loss"] == 0 and got["playback_issues"] == 0
    assert rc.baselines.get("local|mpv") == got
    html = (rc.reports_dir / run_id / "report.html").read_text()
    assert "Baseline test (no router" in html and "✔ verified" in html
    dut = json.loads((rc.reports_dir / run_id / "summary.json").read_text())["dut"]
    assert dut["multicast_path"] == "direct" and dut["mode_detected"] is None  # GAP-113
    # a normal test through the router shows that receiver's last baseline
    second = rc.start(plan(hd_file, duration=10))
    assert wait_state(rc, RunState.IDLE, 60)
    summary = json.loads((rc.reports_dir / second / "summary.json").read_text())
    assert summary["test_network"] == "router"
    assert summary["receiver_baseline"]["local"]["run_id"] == run_id
    # with playback off it is another combination: none yet
    p = plan(hd_file, duration=10)
    p["settings"] = {**p["settings"], "S-49": "Off"}
    third = rc.start(p)
    assert wait_state(rc, RunState.IDLE, 60)
    assert rc.result is not None and rc.result["receiver_baseline"]["local"] is None, third


@needs_tools
def test_a_stopped_baseline_is_not_kept(lab: Any, hd_file: Path) -> None:
    _, rc, _ = lab
    rc.start(baseline(plan(hd_file, duration=60), **{"S-49": "Off"}))
    rc.stop()
    r = rc.result
    assert r is not None and r["verdict"] != "PASS"  # stopped at once: too little data
    assert r["receiver_baseline"]["local"]["verified"] is False
    assert rc.baselines.get("local|none") is None


@needs_tools
def test_a_router_in_between_refuses_the_baseline(
    lab: Any, hd_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, rc, events = lab
    monkeypatch.setattr("controller.run.own_address", lambda host, port: "10.9.9.9")
    with pytest.raises(ZlError) as e:
        rc.start(baseline(plan(hd_file, duration=10)))
    assert e.value.code == "ZL-NET-020"
    assert rc.state is RunState.IDLE and rc.baselines.all() == {}
    assert any(x.code == "ZL-NET-020" for x in events)


@needs_tools
def test_a_baseline_is_not_told_to_connect_the_router(
    lab: Any, hd_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GAP-113 (real baseline precheck): no router, so step 3 must not say "reached
    without the router's NAT — connect it to the router WAN port", and step 5 says the
    stream comes directly."""
    _, rc, _ = lab
    seen: list[bool] = []

    def spy(*a: Any, **kw: Any) -> None:
        seen.append(kw["isp_ready"])

    monkeypatch.setattr("controller.run.other_network", spy)
    monkeypatch.setattr(rc, "_isp_ready", lambda w: True)  # the sender gives the WAN address
    rc.precheck(plan(hd_file), full=False)
    normal = list(seen)
    seen.clear()
    r = rc.precheck(baseline(plan(hd_file)), full=True)  # step 5 runs only in a full check
    assert True in normal and seen and not any(seen)
    step5 = r["steps"][4]
    assert "direct (baseline, no router)" in str(step5.get("detail")), step5
