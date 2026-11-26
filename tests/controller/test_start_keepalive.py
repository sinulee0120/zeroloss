"""GAP-153: while the receivers warm up, the sender is asked too.

2026-10-10 the ProBook got no request for 7.5 s after its start (the Controller asked only
the receivers during warm-up); with a slow receiver it went past 10 s and the sender
safe-stopped (CTL-008) before the test began.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from controller.run import RunController


def test_the_sender_is_asked_while_the_receivers_warm_up() -> None:
    rc: Any = object.__new__(RunController)
    asked: list[str] = []
    passes = {"n": 0}

    def heartbeat(w: Any) -> dict[str, Any]:
        asked.append(w.device.name)
        if w.device.name == "rx":
            passes["n"] += 1
        return {"module_data": {"iptv": {"measuring": passes["n"] >= 3}}}

    rc._plan = SimpleNamespace(receivers=[("rx", 1)], warmup_max_s=30)
    rc._rules = SimpleNamespace(heartbeat_key="iptv", measuring=lambda d: d.get("measuring"))
    rc._watch = {n: SimpleNamespace(device=SimpleNamespace(name=n), last_error="")
                 for n in ("tx", "rx")}  # fmt: skip
    rc._heartbeat = heartbeat
    rc._wait_measuring()
    assert asked.count("tx") == asked.count("rx") == 3  # every pass asks the sender too


def test_a_receiver_that_fails_during_warm_up_is_named() -> None:
    """Its Action Required row needs the PC (it closes when that PC answers again)."""
    import pytest

    from controller.precheck import DeviceError
    from core.protocol.envelope import ZlError

    rc: Any = object.__new__(RunController)

    def heartbeat(w: Any) -> dict[str, Any]:
        if w.device.name == "rx":
            raise ZlError("DSC-014", "Cannot reach rx")
        return {}

    # 2026-11-21 review: one missed answer no longer ends the start, but a PC that has not
    # answered for LOST_S still ends it at once (as before), long before the 30 s warm-up
    rc.lost_s = 0.6  # the Controller's own S-42 lost time
    rc._plan = SimpleNamespace(receivers=[("rx", 1)], warmup_max_s=30, sender="tx")
    rc._rules = SimpleNamespace(heartbeat_key="iptv", measuring=lambda d: False)
    rc._watch = {n: SimpleNamespace(device=SimpleNamespace(name=n), last_error="")
                 for n in ("tx", "rx")}  # fmt: skip
    rc._heartbeat = heartbeat
    rc._windows = {}
    rc.router_logger = SimpleNamespace(trigger=lambda why, dev: False)
    t0 = time.monotonic()
    with pytest.raises(DeviceError) as e:
        rc._wait_measuring()
    assert e.value.device == "rx"
    assert time.monotonic() - t0 < 5  # at once, not at the end of warm-up


def test_one_missed_answer_during_warm_up_does_not_end_the_start() -> None:
    """2026-11-21 review: a single heartbeat timeout rolled the whole start back."""
    from core.protocol.envelope import ZlError

    rc: Any = object.__new__(RunController)
    rc.lost_s = 10.0
    calls = {"rx": 0}

    def heartbeat(w: Any) -> dict[str, Any]:
        if w.device.name == "rx":
            calls["rx"] += 1
            if calls["rx"] == 1:
                raise ZlError("DSC-014", "Cannot reach rx")  # one slow answer
            return {"module_data": {"iptv": {"measuring": True}}}
        return {}

    rc._plan = SimpleNamespace(receivers=[("rx", 1)], warmup_max_s=30)
    rc._rules = SimpleNamespace(heartbeat_key="iptv", measuring=lambda d: d.get("measuring"))
    rc._watch = {n: SimpleNamespace(device=SimpleNamespace(name=n), last_error="")
                 for n in ("tx", "rx")}  # fmt: skip
    rc._heartbeat = heartbeat
    rc._wait_measuring()  # no error: the next answer came, warm-up done
    assert calls["rx"] == 2
