"""The start-step lines in the run's events (iptv 2.4) say which device and flow."""

from __future__ import annotations

from typing import Any

from controller.run import RunController
from modules.iptv.run import Step


class Seen:
    def __init__(self) -> None:
        self.got: list[tuple[str, str]] = []

    def _event(self, code: str, level: str, text: str, device: str = "", **data: Any) -> None:
        self.got.append((text, device))


def done(step: Step) -> tuple[str, str]:
    seen = Seen()
    RunController._step_done(seen, step)  # type: ignore[arg-type]
    return seen.got[0]


def test_each_start_line_can_be_told_apart() -> None:
    """2026-11-27 R20261127-011120: "t460-rx: tablet started" twice, "sender started"
    four times."""
    tab = Step("t460-rx", "/v1/iptv/tablet/start", {"device": "tablet-2", "ch": 1})
    assert done(tab) == ("tablet-2: tablet started on t460-rx (CH1)", "tablet-2")
    snd = Step("probook-sender", "/v1/iptv/sender/start",
               {"ch": 1, "prime": {"p": 1, "q": 2, "flow": 0, "slot": 2}})  # fmt: skip
    assert done(snd) == ("probook-sender: sender started (CH1, flow 0 slot 2)", "probook-sender")
    rcv = Step("t460-rx", "/v1/iptv/receiver/start", {"ch": 3})
    assert done(rcv) == ("t460-rx: receiver started (CH3)", "t460-rx")
