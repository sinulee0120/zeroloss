"""Controller channel change (03 F19, 02 2-13 ``channel``, 20 GAP-91) on one loopback node."""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from controller.run import RunController, RunState
from core.protocol.envelope import ZlError
from tests.iptv.test_run_controller import hd_file, lab, plan, wait_state

__all__ = ["hd_file", "lab"]  # fixtures used by name

pytestmark = pytest.mark.skipif(
    not (shutil.which("tsp") and shutil.which("ffmpeg")), reason="needs ffmpeg and tsp"
)


def channel_now(rc: RunController, device: str = "local") -> int | None:
    recv = rc.status()["devices"][device]["module_data"].get("receivers") or []
    return recv[0]["ch"] if recv else None


def wait_for(pred: Any, timeout_s: float) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


def auto_plan(hd: Path, duration: int = 40) -> dict[str, Any]:
    p = plan(hd, duration=duration, files={str(c): str(hd) for c in (1, 2, 3)})
    p["settings"] = {**p["settings"], "S-21": "Every 120 min", "S-49": "Off"}
    return p


def test_manual_change_is_refused_outside_its_rules(lab: Any, hd_file: Path) -> None:
    _, rc, _ = lab
    with pytest.raises(ZlError) as e:
        rc.channel("local", 2)
    assert e.value.code == "ZL-RUN-012"  # no test
    p = plan(hd_file, duration=30)
    p["settings"] = {**p["settings"], "S-49": "Off"}
    rc.start(p)
    try:
        st = rc.status()["channels"]
        assert st == {"on_air": [1], "auto": None, "next_in_s": {}}
        with pytest.raises(ZlError) as e:
            rc.channel("local", 2)  # only CH1 is sent (20 GAP-91)
        assert e.value.code == "ZL-RUN-014" and "not on air" in e.value.what
        with pytest.raises(ZlError) as e:
            rc.channel("nobody", 1)
        assert e.value.code == "ZL-RUN-014"
        same = rc.channel("local", 1)
        assert same["zap"] is None and same["from"] == same["to"] == 1
    finally:
        rc.stop()
    assert rc.result is not None and rc.result["channel_change"] is None
    assert rc.status()["channels"] is None


def test_auto_change_sends_all_channels_and_is_no_loss(lab: Any, hd_file: Path) -> None:
    _, rc, events = lab
    rc.start(auto_plan(hd_file))
    st = rc.status()["channels"]
    assert st["on_air"] == [1, 2, 3] and st["auto"] == {"every_s": 7200.0, "stagger_s": 10.0}
    senders = rc.status()["devices"]["local"]["module_data"]["sender"]["channels"]
    assert sorted(c["ch"] for c in senders) == [1, 2, 3]
    with pytest.raises(ZlError) as e:
        rc.channel("local", 2)  # K-11: by hand is locked while S-21 is on
    assert e.value.code == "ZL-RUN-012" and "Automatic" in e.value.what
    rc._zap_due["local"] = time.monotonic()  # the timer is due now
    assert wait_for(lambda: channel_now(rc) == 2, 15)
    assert rc.status()["channels"]["next_in_s"]["local"] > 7000
    time.sleep(3)
    got = rc.channel("local", 3, mode="auto")
    assert (got["from"], got["to"], got["join_method"]) == (2, 3, "A")
    assert got["zap"]["t_first_packet"] is not None and got["zap"]["join_failed"] is False
    assert wait_state(rc, RunState.IDLE, 60)
    r = rc.result
    assert r is not None and r["verdict"] == "PASS", r  # the change gaps are not loss
    cc = r["channel_change"]
    assert cc["changes"] == 2 and cc["join_failures"] == 0 and cc["success"] == 2
    assert cc["success_timeout_s"] == 5.0 and cc["total_ms_max"] is not None
    assert not any(e.code == "ZL-NET-003" for e in events)
    changes = [e for e in events if e.code == "ZL-INFO" and "CH1 -> CH2 (auto" in e.text]
    assert changes and changes[0].device == "local"
