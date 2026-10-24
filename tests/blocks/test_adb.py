"""core/blocks/adb.py with a fake adb: up to three tablets, each by its own serial."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from core.blocks.adb import Adb, AdbError, check_serial, parse_devices

LIST = """List of devices attached
R00X0000001            device usb:2-1.1 product:gts9wifi model:SM_X710 device:gts9 transport_id:1
R52T1000ABC            unauthorized usb:2-1.2 transport_id:2
emulator-5554 no permissions (user in plugdev group; are your udev rules wrong?) usb:2-1.3
"""


class Fake:
    def __init__(self, answers: dict[str, tuple[int, str]]) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], timeout: float) -> tuple[int, str]:
        self.calls.append(list(argv))
        return self.answers.get(" ".join(argv[1:]), (1, "unknown command"))


def test_three_tablets_each_with_its_state() -> None:
    t = parse_devices(LIST)
    assert [(x.serial, x.state) for x in t] == [
        ("R00X0000001", "device"),
        ("R52T1000ABC", "unauthorized"),
        ("emulator-5554", "no permissions"),
    ]
    assert t[0].model == "SM X710" and t[0].usb == "2-1.1"


def test_no_adb_says_so() -> None:
    with pytest.raises(AdbError, match="not installed"):
        Adb(lambda: None, Fake({})).devices()


def test_each_tablet_gets_its_own_pc_port() -> None:
    fake = Fake({
        "-s A1 forward tcp:0 tcp:10995": (0, "41234\n"),
        "-s B2 forward tcp:0 tcp:10995": (0, "41235\n"),
    })  # fmt: skip
    adb = Adb(lambda: "/x/adb", fake)
    assert adb.forward("A1", 10995) == 41234
    assert adb.forward("B2", 10995) == 41235


def test_forward_list() -> None:
    fake = Fake({"forward --list": (0, "A1 tcp:41234 tcp:10995\nB2 tcp:41235 tcp:10995\n")})
    assert Adb(lambda: "adb", fake).forwards() == [
        ("A1", 41234, "tcp:10995"),
        ("B2", 41235, "tcp:10995"),
    ]


def test_adb_words_come_back_when_it_fails() -> None:
    fake = Fake({"-s A1 shell getprop": (1, "error: device unauthorized.")})
    with pytest.raises(AdbError, match="unauthorized"):
        Adb(lambda: "adb", fake).shell("A1", ["getprop"])


@pytest.mark.parametrize("bad", ["", "a b", "x;rm -rf /", "-s", "a" * 65])
def test_a_serial_is_checked_before_use(bad: str) -> None:
    with pytest.raises(ValueError):
        check_serial(bad)
