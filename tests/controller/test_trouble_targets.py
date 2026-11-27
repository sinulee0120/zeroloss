"""05 5.3 trouble windows: a sender flow's trouble is its receivers' (12 2 trouble_targets)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from controller.devices import DeviceList
from controller.run import RunController
from core.events.bus import EventBus

REPO = Path(__file__).resolve().parents[2]


class Rules:
    @staticmethod
    def trouble_of(p: Any, device: str, code: str, data: dict[str, Any]) -> Any:
        return ("sender", code == "ZL-SND-005") if device == "tx" else None

    @staticmethod
    def trouble_targets(p: Any, device: str, data: dict[str, Any]) -> list[str] | None:
        if device != "tx" or "slot" not in data:
            return None
        return {0: ["pc"], 1: ["tab"]}[data["slot"]]


def test_a_flows_window_is_its_receivers_and_the_pcs_is_everyones(tmp_path: Path) -> None:
    """2026-11-27 (R20261127-112905): SND-025 on t460-rx's flow counted for the tablets."""
    rc = RunController(DeviceList(tmp_path / "devices.json"), tmp_path / "rep",
                       REPO / "modules", EventBus())  # fmt: skip
    rc._rules, rc._plan = Rules(), SimpleNamespace(sender="tx")
    rc._note_trouble("tx", "ZL-SND-025", {"ch": 1, "slot": 0})
    assert "sender" in rc._windows_for("pc") and "sender" not in rc._windows_for("tab")
    rc._note_trouble("tx", "ZL-SND-005", {"ch": 1, "slot": 1})  # until cleared, tab only
    assert rc._windows_for("tab")["sender"][-1][1] == float("inf")
    assert all(b != float("inf") for _, b in rc._windows_for("pc")["sender"])
    rc._note_trouble("tx", "ZL-INFO", {"ch": 1, "slot": 1, "clears": ["ZL-SND-005"]})
    assert rc._windows_for("tab")["sender"][-1][1] != float("inf")  # closed for tab
    rc._note_trouble("tx", "ZL-SND-016", {})  # the whole sender PC: everyone's
    assert len(rc._windows_for("pc")["sender"]) == 2 and len(rc._windows_for("tab")["sender"]) == 2
