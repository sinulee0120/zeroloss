"""Checklist object and helpers (03 F17, 02 2-1 ``checklist``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from controller.precheck import (
    STEPS,
    Checklist,
    DeviceError,
    clock_offset_s,
    most_hours,
    own_address,
)
from core.protocol.envelope import ZlError


def test_failed_step_skips_the_rest_and_keeps_the_texts() -> None:
    cl = Checklist("start", len(STEPS), run_id="R1")
    with cl.step(1) as s:
        s.detail = "a, b"
    with pytest.raises(ZlError), cl.step(2):
        raise DeviceError("b", ZlError("INS-029", "tsp is not installed on b", "why", "fix"))
    d = cl.to_dict()
    assert not d["ok"] and d["run_id"] == "R1"
    assert [s["state"] for s in d["steps"]] == ["ok", "failed"] + ["skipped"] * 5
    bad = d["steps"][1]
    assert (bad["device"], bad["code"], bad["what"], bad["why"], bad["fix"]) == (
        "b", "ZL-INS-029", "tsp is not installed on b", "why", "fix",
    )  # fmt: skip
    assert d["steps"][0]["code"] is None and cl.first_problem() is not None


def test_warning_does_not_stop_and_limits_are_kept() -> None:
    cl = Checklist("idle", 4)
    assert [s.state for s in cl.steps] == ["waiting"] * 4 + ["skipped"] * 3
    with cl.step(4) as s:
        cl.warn(s, ZlError("DSC-009", "clock off"), "rx")
    assert cl.ok and cl.steps[3].state == "warn" and cl.steps[3].device == "rx"
    with pytest.raises(ValueError):
        Checklist("other", 4)


def test_other_errors_fail_the_step_too() -> None:
    cl = Checklist("precheck", 5)
    with pytest.raises(KeyError), cl.step(3):
        raise KeyError("x")
    assert [s.state for s in cl.steps] == ["waiting", "waiting", "failed"] + ["skipped"] * 4


@given(st.floats(-5000, 5000), st.floats(0, 3))
def test_clock_offset_is_node_minus_the_middle_of_the_trip(off_ms: float, trip_s: float) -> None:
    before = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)
    after = before + timedelta(seconds=trip_s)
    node = before + timedelta(seconds=trip_s / 2, milliseconds=off_ms)
    ts = node.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    got = clock_offset_s(ts, before, after)
    assert got is not None and abs(got - off_ms / 1000) < 0.002


def test_clock_offset_bad_input_and_most_hours() -> None:
    now = datetime.now(UTC)
    assert clock_offset_s(None, now, now) is None and clock_offset_s("x", now, now) is None
    assert most_hours(1024 + 360, 3600, 100 * 3600) == pytest.approx(10.0)
    assert most_hours(500, 10, 3600) == 0.0
    assert most_hours(5000, 0, 3600) == float("inf")
    assert own_address("127.0.0.1", 10992) == "127.0.0.1"


def _nic(nic: str, ip: str | None, wireless: bool = False) -> dict[str, object]:
    return {"nic": nic, "ip": ip, "up": True, "wireless": wireless}


def test_other_network_the_first_real_install() -> None:
    """09 NET-018 (20 W-3): 2026-09-26 the sender PC answered over the home Wi-Fi —
    control went around the router. Each way of being outside is caught once; the
    Controller PC itself and a Wi-Fi receiver's test Wi-Fi are not."""
    from controller.precheck import other_network

    sender = [_nic("enp37s0", "10.0.0.1"), _nic("wlo1", "192.168.0.27", wireless=True)]
    err = other_network("tx-pc", sender, "192.168.0.27", "192.168.0.10")
    assert err is not None and err.code == "ZL-NET-018"
    assert err.what == "tx-pc is reached over Wi-Fi wlo1, not through the router"
    # reached through the router, its home Wi-Fi up too (its internet): fine, no warning
    # (GAP-161, 2026-10-16: it filled the Problems list with "nothing to do")
    ok = other_network("tx-pc", sender, "10.0.0.1", "192.168.1.10", isp_ready=True, via_nat=True)
    assert ok is None
    # the ISP role is ready but no NAT on the way: around the router by cable
    wired = [_nic("enp37s0", "10.0.0.1"), _nic("enp2s0", "192.168.45.58")]
    err = other_network("tx-pc", wired, "192.168.45.58", "192.168.45.10", isp_ready=True)
    assert err is not None and "without the router's NAT" in err.what
    # fine: only the test cable, through the NAT
    ok = other_network("tx-pc", wired[:1], "10.0.0.1", "192.168.1.10", isp_ready=True, via_nat=True)
    assert ok is None
    # a Wi-Fi receiver answering on its test Wi-Fi
    rx = [_nic("wlp4s0", "192.168.1.23", wireless=True)]
    assert other_network("rx-pc", rx, "192.168.1.23", "192.168.1.10", "192.168.1.23") is None
    # the Controller PC itself (a receiver on it too): its internet Wi-Fi is by design
    ctl = [_nic("enp0s31f6", "192.168.1.10"), _nic("wlp2s0", "172.30.1.5", wireless=True)]
    assert other_network("local", ctl, "192.168.1.10", "192.168.1.10") is None
    assert other_network("local", ctl, "127.0.0.1", "127.0.0.1") is None
    assert other_network("gone", [], None, None) is None
