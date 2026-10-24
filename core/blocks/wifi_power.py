"""Wi-Fi power save off on the receiving card for the test, back as it was after.

2026-10-16 (20 GAP-167): Ubuntu's NetworkManager turns power save on for every Wi-Fi
connection (``wifi.powersave = 3``). A sleeping card has its frames held by the access
point; the receiver got nearly nothing for seconds, then the held packets at ~6x the
stream, and its buffer overflowed (41,328 packets in 95 s of a night run). A device that
plays a stream does not doze, so the test card does not either.

``iw dev <card> set power_save off`` needs CAP_NET_ADMIN (the installed node has it).
Only a card that was on is turned back on; several users of one card share one change.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Callable

Run = Callable[[list[str]], tuple[int, str]]
TIMEOUT_S = 5.0

_lock = threading.Lock()
_held: dict[str, int] = {}  # card -> users while it is held off by us


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, (p.stdout + p.stderr).strip()


def state(nic: str, run: Run = _run) -> bool | None:
    """True = power save on, False = off, None = not known (no ``iw``, not Wi-Fi)."""
    rc, out = run(["iw", "dev", nic, "get", "power_save"])
    if rc != 0:
        return None
    low = out.lower()
    return True if low.endswith(": on") else False if low.endswith(": off") else None


def hold_off(nic: str, run: Run = _run) -> str:
    """Turn it off for a test. Returns what happened, in words, for the event."""
    with _lock:
        if nic in _held:
            _held[nic] += 1
            return "already off for the test"
        was = state(nic, run)
        if was is not True:
            return "already off" if was is False else "not known (no iw answer)"
        rc, out = run(["iw", "dev", nic, "set", "power_save", "off"])
        if rc != 0:
            return f"could not turn it off: {out or rc}"
        _held[nic] = 1
        return "turned off for the test"


def release(nic: str, run: Run = _run) -> None:
    """The test is over on this card: on again when we turned it off and nobody else
    still uses it."""
    with _lock:
        users = _held.get(nic)
        if users is None:
            return
        if users > 1:
            _held[nic] = users - 1
            return
        del _held[nic]
    run(["iw", "dev", nic, "set", "power_save", "on"])
