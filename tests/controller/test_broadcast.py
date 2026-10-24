"""Always-on broadcast (03 F18, 02 2-13 ``broadcast``, 20 GAP-87) on one loopback node."""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from controller.run import RunState
from core.protocol.envelope import ZlError
from tests.iptv.test_run_controller import hd_file, lab, plan, tsp_running, wait_state

__all__ = ["hd_file", "lab"]  # fixtures used by name

pytestmark = pytest.mark.skipif(
    not (shutil.which("tsp") and shutil.which("ffmpeg")), reason="needs ffmpeg and tsp"
)


def on_air(p: dict[str, Any]) -> dict[str, Any]:
    p["settings"] = {**p["settings"], "S-13": "Always-on", "S-49": "Off"}
    return p


def sending(node: Any) -> list[int]:
    mod = next(m for m in node.modules if m.id == "iptv").instance
    return sorted(k[0] for k, s in mod.senders.items() if s.flow.state.value == "RUNNING")


def test_broadcast_outlives_the_test_and_stops_on_request(lab: Any, hd_file: Path) -> None:
    node, rc, events = lab
    node.lost_after_s = 5  # the sender's own watch (F11), short for the test
    p = on_air(plan(hd_file, duration=10))
    rc.start(p)
    bc = rc.broadcast_status()
    assert bc["on"] and bc["channels"] == [1] and bc["sender"] == "local" and bc["since_utc"]
    with pytest.raises(ZlError) as e:
        rc.stop_broadcast()  # not during a test
    assert e.value.code == "ZL-RUN-012"
    assert wait_state(rc, RunState.IDLE, 40)
    r = rc.result
    assert r is not None and r["verdict"] == "PASS" and r["sender_mode"] == "always_on"
    assert r["broadcast_since"] is None  # this test turned it on
    time.sleep(8)  # longer than the sender's watch: only our idle heartbeat keeps it
    assert sending(node) == [1] and rc.broadcast_status()["on"]  # kept alive between tests
    # the next test with the same sending skips the senders and says since when
    second = on_air(plan(hd_file, duration=10))
    second["settings"]["S-14"] = p["settings"]["S-14"]
    rc.start(second)
    assert wait_state(rc, RunState.IDLE, 40)
    r2 = rc.result
    assert r2 is not None and r2["verdict"] == "PASS", r2
    assert r2["broadcast_since"] == bc["since_utc"]
    skipped = [s for s in (rc.checklist.to_dict()["steps"] if rc.checklist else [])
               if s.get("state") == "skipped"]  # fmt: skip
    assert any("broadcast" in str(s.get("detail")) for s in skipped)
    # a different plan is refused before anything starts
    with pytest.raises(ZlError) as e:
        rc.start(on_air(plan(hd_file, duration=10)))  # other multicast groups
    assert e.value.code == "ZL-RUN-014" and "broadcast" in e.value.what
    assert sending(node) == [1]  # the refused start left the broadcast alone
    got = rc.stop_broadcast()
    assert got == {"on": False, "stopped": [1]}
    assert not rc.broadcast_status()["on"] and sending(node) == []
    assert rc.stop_broadcast() == {"on": False, "stopped": []}
    deadline = time.monotonic() + 10
    while tsp_running() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not tsp_running()
    assert any("Broadcast off" in e.text for e in events)


def test_reset_all_and_a_silent_controller_end_the_broadcast(lab: Any, hd_file: Path) -> None:
    node, rc, _ = lab
    node.lost_after_s = 5
    rc.start(on_air(plan(hd_file, duration=10)))
    assert wait_state(rc, RunState.IDLE, 40) and rc.broadcast_status()["on"]
    rc.reset()
    assert not rc.broadcast_status()["on"] and sending(node) == []
    rc.start(on_air(plan(hd_file, duration=10)))
    assert wait_state(rc, RunState.IDLE, 40) and sending(node) == [1]
    rc._closing.set()  # the Controller goes quiet: the sender stops by itself (F11)
    deadline = time.monotonic() + node.lost_after_s + 10
    while sending(node) and time.monotonic() < deadline:
        time.sleep(0.5)
    assert sending(node) == []


def test_a_slow_report_does_not_end_the_broadcast(
    lab: Any, hd_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run loop asks the sender only while RUNNING; while the report is written
    (REPORTING) our keeper must, or the sender's own watch stops the broadcast."""
    from controller.report import write_report as real

    node, rc, _ = lab
    node.lost_after_s = 5

    def slow(*a: Any, **kw: Any) -> Any:
        time.sleep(7)  # longer than the sender waits for us
        return real(*a, **kw)

    monkeypatch.setattr("controller.run.write_report", slow)
    rc.start(on_air(plan(hd_file, duration=10)))
    assert wait_state(rc, RunState.IDLE, 60)
    assert sending(node) == [1] and rc.broadcast_status()["on"]
    rc.stop_broadcast()
