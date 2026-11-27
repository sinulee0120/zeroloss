"""09 DSC-017: the [Turn on time sync] button closes its problem (02 8 ``clears``)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import controller.service as service
from controller.actions import ActionBoard
from controller.problems import ProblemBoard
from controller.service import ControllerService
from core.events.bus import Event, EventBus
from core.protocol.envelope import Request


class _Node:
    def __init__(self, *a: Any) -> None:
        pass

    def command(self, path: str, *a: Any, **k: Any) -> dict[str, Any]:
        assert path == "/v1/time/sync"
        return {"ok": True, "why": "time sync on; the clock is set within a minute"}

    def close(self) -> None:
        return


def test_the_time_sync_button_clears_its_problem(monkeypatch: Any) -> None:
    """2026-11-27: pressed at 11:28:53, but "t460-rx clock +14.3 s off and its time sync
    is off" stayed active on the Problems board through the 3-hour run."""
    bus, actions, problems = EventBus(), ActionBoard(), ProblemBoard()
    bus.subscribe(actions)
    bus.subscribe(problems)
    bus.publish(Event("ZL-DSC-017", "warn", "t460-rx clock +14.3 s off and its time sync is off",
                      device="t460-rx", data={"choose": "time_sync",
                                              "choices": [{"id": "t460-rx"}]}))  # fmt: skip
    key = actions.snapshot()["actions"][0]["key"]
    monkeypatch.setattr(service, "NodeClient", _Node)

    def event(code: str, level: Any, text: str, device: str = "", **data: Any) -> None:
        bus.publish(Event(code, level, text, device=device, data=data))

    run = SimpleNamespace(boot_id="b", generation=1, _event=event)
    dev = SimpleNamespace(host="127.0.0.1", port=1, name="t460-rx")
    svc: Any = SimpleNamespace(actions=actions, run=run,
                               devices=SimpleNamespace(get=lambda name: dev))  # fmt: skip
    req = Request(command_id="c", generation=0, run_id=None,
                  params={"key": key, "choice": "t460-rx"})  # fmt: skip
    got = ControllerService.action_choose(svc, req)
    assert got["time_sync"]["ok"] and actions.waiting_row(key) is None
    row = next(p for p in problems.snapshot()["problems"] if p["code"] == "ZL-DSC-017")
    assert not row["active"]
