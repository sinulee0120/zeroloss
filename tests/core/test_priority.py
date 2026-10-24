"""The sending path first on its PC (2026-10-23 user: "Tx가 우선순위가 가장 높아야겠지")."""

from __future__ import annotations

import os
from typing import Any

import pytest

from core.blocks import priority


def test_real_time_when_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    got: list[Any] = []
    monkeypatch.setattr(os, "SCHED_RR", 2, raising=False)
    monkeypatch.setattr(os, "sched_param", lambda p: p, raising=False)
    monkeypatch.setattr(os, "sched_setscheduler", lambda pid, pol, p: got.append((pid, pol, p)),
                        raising=False)  # fmt: skip
    assert priority.first(42) == "real-time (SCHED_RR 30)" and got == [(42, 2, 30)]


def test_nice_when_real_time_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def refused(*a: Any) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "SCHED_RR", 2, raising=False)
    monkeypatch.setattr(os, "sched_param", lambda p: p, raising=False)
    monkeypatch.setattr(os, "sched_setscheduler", refused, raising=False)
    monkeypatch.setattr(os, "PRIO_PROCESS", 0, raising=False)
    monkeypatch.setattr(os, "setpriority", lambda w, pid, n: None, raising=False)
    assert priority.first() == "nice -15"
    monkeypatch.setattr(os, "setpriority", refused, raising=False)
    assert priority.first().startswith("normal (")  # never raises: it says what it got


def test_the_quietest_cpu_is_picked() -> None:
    """senderPC: the wired card's interrupts on CPU 0, the Wi-Fi's on CPU 3 (ProBook)."""
    reads = iter([
        "  CPU0 CPU1 CPU2 CPU3\n 36: 1000 0 0 0 eth\n 19: 0 0 0 500 wifi\n",
        "  CPU0 CPU1 CPU2 CPU3\n 36: 9000 3 0 0 eth\n 19: 0 0 2 900 wifi\n",
    ])  # fmt: skip
    # 2026-10-24: a CPU that handled the Wi-Fi's interrupts (CPU 2, two of them) comes
    # after one that handled the wired card's; CPU 1 has the fewest of those
    assert priority.quietest_cpu(lambda: next(reads), wait_s=0) == 1
    assert priority.quietest_cpu(lambda: "  CPU0\n 1: 5 x\n", wait_s=0) is None  # one CPU


def test_every_thread_is_raised(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """senderPC: only tsp's main thread was raised; its 8 workers stayed normal."""
    for tid in ("100", "101", "102"):
        (tmp_path / "100" / "task" / tid).mkdir(parents=True)
    raised: list[int] = []

    def first(pid: int = 0) -> str:
        raised.append(pid)
        return "real-time"

    monkeypatch.setattr(priority, "first", first)
    assert priority.first_all(100, tmp_path) == "real-time (3 threads)"
    assert sorted(raised) == [100, 101, 102]


def test_two_senders_take_two_quiet_cpus(tmp_path: Any) -> None:
    """2026-10-24: two channels' senders both took the quietest CPU and pushed each
    other late (SND-025 on every receiver at once); each now holds its own."""
    import subprocess
    import sys

    readings = iter([
        "CPU0 CPU1 CPU2 CPU3\n 1: 100 10 20 500\n",
        "CPU0 CPU1 CPU2 CPU3\n 1: 900 11 22 900\n",
    ])  # fmt: skip
    first = priority.claim_quiet_cpu(tmp_path, read=lambda: next(readings), wait_s=0)
    assert first == 1
    code = (  # another process (another channel's sender) asks the same
        "import sys; from pathlib import Path; from core.blocks import priority as p;"
        "r=iter(['CPU0 CPU1 CPU2 CPU3\\n 1: 100 10 20 500\\n',"  # escaped: one -c line
        "'CPU0 CPU1 CPU2 CPU3\\n 1: 900 11 22 900\\n']);"
        "print(p.claim_quiet_cpu(Path(sys.argv[1]), read=lambda: next(r), wait_s=0))"
    )
    got = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True,
                         text=True, check=True).stdout.strip()  # fmt: skip
    assert got == "2"  # the next quietest, not the one the first sender holds


def test_a_quiet_wifi_cards_cpu_is_not_picked() -> None:
    """senderPC 2026-10-24 (ProBook): ath9k's interrupts all on CPU 3 but only ~20/s at
    that moment, the wired card's on CPU 0; CPU 2 serves no card. CPU 3 was picked."""
    text = (
        "  CPU0 CPU1 CPU2 CPU3\n"
        " 19: 0 0 0 1780466 IO-APIC ath9k\n"
        " 36: 415222653 0 0 0 PCI-MSIX enp37s0\n"
        "LOC: 900 4300 2600 4300 Local timer interrupts\n"
    )
    later = text.replace("1780466", "1780476").replace("415222653", "415229272")
    later = later.replace("LOC: 900 4300 2600 4300", "LOC: 1800 8600 5200 8600")
    reads = iter([text, later])
    assert priority.quiet_cpus(lambda: next(reads), wait_s=0)[:2] == [2, 1]


def test_wifi_and_wired_card_cpus_come_last() -> None:
    """senderPC's ProBook totals: CPU0 timer+enp37s0, CPU1 i915, CPU2 snd only, CPU3
    usb+ath9k; ath9k's CPU last, the wired card's before it."""
    text = (
        "  CPU0 CPU1 CPU2 CPU3\n"
        " 1: 900 0 0 0 IO-APIC i8042\n"
        " 19: 0 0 0 1780466 IO-APIC ath9k\n"
        " 36: 415222653 0 0 0 PCI-MSIX-0000:25:00.0 enp37s0\n"
        " 140: 0 1622393 0 0 PCI-MSI i915\n"
        " 141: 0 0 670 0 PCI-MSI snd_hda_intel:card0\n"
    )
    reads = iter([text, text])
    assert priority.quiet_cpus(lambda: next(reads), wait_s=0) == [2, 1, 0, 3]
