"""The sending path runs first on its PC (2026-10-23 user: "Tx가 우선순위가 가장 높아야겠지").

2026-10-22: a Wi-Fi driver reset storm on the sender PC held up the even-spacing sender
for 20 s. A live sender must go before anything else on its PC: ``first`` gives a process
the round-robin real-time class (SCHED_RR, a middle priority: above every normal program
and the kernel's softirq threads, below the kernel's own real-time work), else the best
"nice" it may have. The node service holds CAP_SYS_NICE for this (06 4.3). The sender's
loop sleeps between datagrams, and the kernel keeps 5 % of each second for normal
programs (sched_rt_runtime_us), so the PC stays usable. Never raises: it says what it got.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

# of 1..99: above normal programs and the desktop's own real-time threads (pipewire,
# gnome-shell KMS at RR 20 on the ProBook, senderPC 2026-10-23), below the kernel's
# interrupt and idle-injection threads (FIFO 50) and migration (99)
RR_PRIORITY = 30
NICE_BEST = -15


def first(pid: int = 0) -> str:
    """Raise ``pid`` (0 = this process); returns what it got, in words."""
    rr = getattr(os, "SCHED_RR", None)
    if rr is not None and hasattr(os, "sched_setscheduler"):
        try:
            os.sched_setscheduler(pid, rr, os.sched_param(RR_PRIORITY))
            return f"real-time (SCHED_RR {RR_PRIORITY})"
        except (PermissionError, OSError):
            pass
    if hasattr(os, "setpriority"):
        try:
            os.setpriority(os.PRIO_PROCESS, pid, NICE_BEST)
            return f"nice {NICE_BEST}"
        except (PermissionError, OSError) as e:
            return f"normal ({e.strerror or e})"
    return "normal (not on this OS)"


def first_all(pid: int, proc: Path = Path("/proc")) -> str:
    """Every thread of ``pid`` first (a program's worker threads were made before it was
    raised and keep the normal class: senderPC 2026-10-23, tsp's 8 workers)."""
    tids = [int(t.name) for t in (proc / str(pid) / "task").glob("*") if t.name.isdigit()]
    got = [first(t) for t in tids or [pid]]
    return f"{got[0]} ({len(got)} threads)" if got else "normal (no threads)"


# interrupt names of Wi-Fi cards and of wired network cards (the last word of the line)
RADIO_IRQ = re.compile(r"^(ath|iwl|rtw|rtl8|mt7|wl|brcm|mwifiex|wifi)", re.I)
WIRED_IRQ = re.compile(r"^(en|eth|igb|e1000|r8169|ixgbe|i40e|mlx|bnx)", re.I)


def _card_cpus(text: str, cpus: int) -> tuple[set[int], set[int]]:
    """CPUs that have handled a Wi-Fi card's interrupts, and a wired card's (senderPC
    2026-10-24: every ProBook CPU serves some device; a quiet ath9k CPU still came
    second by a thin margin). A sender goes there only when nothing else is left."""
    radio: set[int] = set()
    wired: set[int] = set()
    for line in text.splitlines()[1:]:
        head, _, rest = line.partition(":")
        parts = rest.split()
        if not head.strip().isdigit() or len(parts) <= cpus:
            continue
        name = parts[-1].split("-")[0]
        hit = {i for i, v in enumerate(parts[:cpus]) if v.isdigit() and int(v) > 0}
        if RADIO_IRQ.match(name):
            radio |= hit
        elif WIRED_IRQ.match(name):
            wired |= hit
    return radio, wired


def _irq_per_cpu(text: str, devices_only: bool = False) -> list[int]:
    """Interrupts so far per CPU, from /proc/interrupts; ``devices_only``: the numbered
    lines only (cards, disks), not the timer and the CPUs' own (LOC, RES, CAL…)."""
    lines = text.splitlines()
    if not lines:
        return []
    cpus = len(lines[0].split())
    totals = [0] * cpus
    for line in lines[1:]:
        if devices_only and not line.split(":", 1)[0].strip().isdigit():
            continue
        parts = line.split()[1 : 1 + cpus]
        for i, v in enumerate(parts):
            if v.isdigit():
                totals[i] += int(v)
    return totals


def quiet_cpus(read: Any = None, wait_s: float = 0.5) -> list[int]:
    """CPUs best first for a sender: those that serve no device interrupt come first
    (senderPC 2026-10-24: a quiet half-second made the Wi-Fi card's CPU look free; it
    was where its reset storms ran), then the fewest interrupts over ``wait_s``
    ([] on one CPU or no reading)."""
    read = read or (lambda: Path("/proc/interrupts").read_text(encoding="utf-8"))
    try:
        first = read()
        time.sleep(wait_s)
        last = read()
        before, after = _irq_per_cpu(first), _irq_per_cpu(last)
    except OSError:
        return []
    if len(before) < 2 or len(before) != len(after):
        return []
    delta = [a - b for a, b in zip(after, before, strict=True)]
    devices = _irq_per_cpu(last, devices_only=True)  # since boot: who serves cards
    if len(devices) != len(delta):
        devices = [0] * len(delta)
    radio, wired = _card_cpus(last, len(delta))
    return sorted(range(len(delta)), key=lambda i: (
        i in radio, i in wired, devices[i] > 0, devices[i], delta[i], i))  # fmt: skip


_claimed: list[Any] = []  # open lock files, held as long as this process runs


def claim_quiet_cpu(lock_dir: Path | None = None, read: Any = None,
                    wait_s: float = 0.5) -> int | None:  # fmt: skip
    """The quietest CPU no other sender of this PC holds (2026-10-24: two channels'
    senders both took the quietest one and pushed each other late, SND-025 on all
    receivers at once). Held by a lock file until this process ends; when every quiet
    CPU is held, the quietest is shared."""
    order = quiet_cpus(read, wait_s)
    if not order:
        return None
    try:
        import fcntl
    except ImportError:  # Windows: no pinning anyway
        return order[0]
    folder = lock_dir or Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
    for cpu in order[: max(1, len(order) - 1)]:  # never the busiest one
        try:
            f = open(folder / f"zeroloss-sender-cpu{cpu}.lock", "a")  # noqa: SIM115
        except OSError:
            continue
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()  # another sender has this one
            continue
        _claimed.append(f)
        return cpu
    return order[0]


def quietest_cpu(read: Any = None, wait_s: float = 0.5) -> int | None:
    """The CPU with the fewest interrupts over ``wait_s`` (None: one CPU or no reading).
    senderPC 2026-10-23: on the ProBook the wired card's interrupts all land on CPU 0 and
    the Wi-Fi's on CPU 3; a real-time sender spinning on CPU 0 would starve the network's
    own deferred work (ksoftirqd is not real-time)."""
    order = quiet_cpus(read, wait_s)
    return order[0] if order else None


def pin(cpu: int | None, pid: int = 0) -> str:
    """Keep ``pid`` on ``cpu``; returns where it runs, in words."""
    if cpu is None or not hasattr(os, "sched_setaffinity"):
        return "any CPU"
    try:
        os.sched_setaffinity(pid, {cpu})
        return f"CPU {cpu}"
    except OSError as e:
        return f"any CPU ({e.strerror or e})"
