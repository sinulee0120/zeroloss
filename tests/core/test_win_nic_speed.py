"""The Windows card's Speed & Duplex for a case, and back (core/blocks/win_nic_speed).

2026-10-17 user: "controller의 LAN설정을 바꿔서도 해야할거 아냐 그래야 모든 LAN,WAN포트 확인을
하지". The driver's own values (this PC's Realtek, Korean Windows) are the cases; the
administrator helper is played by a thread that answers the request files.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from core.blocks import win_nic_speed as w
from core.blocks.nic_speed import Mode

NAMES = ["자동 교섭", "10 Mbps 반이중", "10 Mbps 전이중", "100 Mbps 반이중", "100 Mbps 전이중",
         "1.0 Gbps 전이중"]  # fmt: skip
VALUES = ["0", "1", "2", "3", "4", "6"]
SPEED = {"0": 1000, "1": 10, "2": 10, "3": 100, "4": 100, "6": 1000}


class Card:
    """The card as PowerShell reads it, and the elevated helper that sets it."""

    def __init__(self, folder: Path) -> None:
        self.value, self.folder, self.stop = "0", folder, threading.Event()

    def ps(self, script: str, data: dict[str, Any]) -> Any:
        return {"value": self.value, "shown": NAMES[VALUES.index(self.value)], "values": VALUES,
                "names": NAMES, "up": True, "speed": SPEED[self.value] * 1_000_000,
                "full": self.value not in ("1", "3")}  # fmt: skip

    def helper(self) -> None:
        while not self.stop.is_set():
            (self.folder / "alive").write_text("x", encoding="utf-8")
            for f in self.folder.glob("req-*.json"):
                r = json.loads(f.read_text(encoding="utf-8"))
                ok = r["value"] in VALUES
                if ok:
                    self.value = r["value"]
                out = f.with_name(f.name.replace("req-", "res-"))
                out.write_text(json.dumps({"ok": ok, "why": None if ok else "bad"}), "utf-8")
                f.unlink()
            self.stop.wait(0.02)


def _speed(tmp_path: Path) -> tuple[w.WinNicSpeed, Card, list[Path]]:
    card = Card(tmp_path / "win-nic-helper")
    started: list[Path] = []

    def elevate(folder: Path) -> str | None:
        folder.mkdir(parents=True, exist_ok=True)
        started.append(folder)
        threading.Thread(target=card.helper, daemon=True).start()
        return None

    ns = w.WinNicSpeed(tmp_path / "c.json", card.ps, sleep=lambda s: None, elevate=elevate)
    return ns, card, started


def test_the_drivers_own_values_are_the_cases() -> None:
    modes = [w.parse_option(n, v) for n, v in zip(NAMES, VALUES, strict=True)]
    assert [m.label if m else None for m in modes] == [
        None, "10 Mbps half", "10 Mbps full", "100 Mbps half", "100 Mbps full", "1000 Mbps"
    ]  # fmt: skip
    assert w.parse_option("100 Mbps Half Duplex", "3") == Mode(100, "half", "3")
    assert w.parse_option("Auto Negotiation", "0") is None
    assert w.parse_option("2.5 Gbps Full Duplex", "7") == Mode(2500, "full", "7")


def test_one_value_set_read_back_and_put_back(tmp_path: Path) -> None:
    ns, card, started = _speed(tmp_path)
    try:
        got = ns.set_mode("이더넷", Mode(100, "half", "3"))
        assert got["readback_ok"] and got["link"] and (got["speed"], got["duplex"]) == (100, "half")
        assert ns.set_mode("이더넷", Mode(10, "full", "2"))["speed"] == 10
        assert len(started) == 1  # Windows asked once for the whole list
        back = ns.restore("이더넷")
        assert (
            back["ok"]
            and card.value == "0"
            and ns.restore("이더넷")["why"] == "nothing was changed"
        )
    finally:
        card.stop.set()


def test_a_declined_approval_changes_nothing_and_is_asked_once(tmp_path: Path) -> None:
    asked: list[Path] = []

    def declined(folder: Path) -> str | None:
        asked.append(folder)
        return "administrator approval was declined (Windows asked once)"

    card = Card(tmp_path / "win-nic-helper")
    ns = w.WinNicSpeed(tmp_path / "c.json", card.ps, sleep=lambda s: None, elevate=declined)
    for value in ("1", "2", "3"):
        got = ns.set_mode("이더넷", Mode(10, "half", value))
        assert not got["link"] and "declined" in got["why"]
    assert len(asked) == 1 and card.value == "0"


def test_a_card_already_as_before_is_put_back_without_the_administrator(tmp_path: Path) -> None:
    """2026-10-22: the change never got the administrator's yes, and the put-back asked
    again and said "not put back" -- the card had never left Auto."""
    ns, card, started = _speed(tmp_path)
    (tmp_path / "c.json").write_text(
        json.dumps({"이더넷": {"value": "0", "speed": 1000}}), encoding="utf-8"
    )  # kept before a change that failed
    got = ns.restore("이더넷")
    assert got["ok"] and started == [] and card.value == "0"
    assert ns.restore_all() == {}  # nothing kept any more
