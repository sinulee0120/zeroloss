"""GAP-167: Wi-Fi power save off on the test card while it receives, back after."""

from __future__ import annotations

from core.blocks import wifi_power


class FakeIw:
    def __init__(self, on: bool | None) -> None:
        self.on = on
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if argv[-1] == "power_save":  # get
            if self.on is None:
                return 237, "command failed: Operation not supported (-95)"
            return 0, f"Power save: {'on' if self.on else 'off'}"
        self.on = argv[-1] == "on"
        return 0, ""


def test_on_is_turned_off_for_the_test_and_back_after() -> None:
    iw = FakeIw(on=True)
    assert wifi_power.hold_off("wlx0", iw) == "turned off for the test" and iw.on is False
    assert wifi_power.hold_off("wlx0", iw) == "already off for the test"  # a second channel
    wifi_power.release("wlx0", iw)
    assert iw.on is False  # the other channel still receives
    wifi_power.release("wlx0", iw)
    assert iw.on is True


def test_off_or_unknown_is_left_as_it_is() -> None:
    off = FakeIw(on=False)
    assert wifi_power.hold_off("wlx1", off) == "already off"
    wifi_power.release("wlx1", off)
    assert off.calls == [["iw", "dev", "wlx1", "get", "power_save"]]
    unknown = FakeIw(on=None)
    assert wifi_power.hold_off("wlx2", unknown).startswith("not known")
    wifi_power.release("wlx2", unknown)
    assert len(unknown.calls) == 1
