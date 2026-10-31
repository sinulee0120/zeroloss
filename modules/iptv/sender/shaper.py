"""A traffic shaper on the sender's own test card (09 SND-026).

2026-10-31 (R20261031-205951): a 3 % netem loss put on the sender PC's card, after the
even-spacing sender, was judged red as if the router had lost it; user: "지금은 라우터 문제가
아닌데". The sender reads its card's queueing disciplines every second, only reads them
(never removes anything it did not set up), and says so while one that shapes or drops
is there: those seconds are the sender's (a purple window), not the router's.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Callable

# what a card has by itself; anything else (netem, tbf, htb, ...) changes the traffic
PLAIN = frozenset({"fq_codel", "mq", "noqueue", "pfifo_fast", "fq", "cake", "noop"})
TIMEOUT_S = 2.0

Run = Callable[[list[str]], str]


def _run(argv: list[str]) -> str:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_S,
                              check=False)  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout


def card_of(address: str | None, run: Run = _run) -> str | None:
    """The card that has ``address`` (``ip -o -4 addr``), None when not known."""
    if not address or not sys.platform.startswith("linux"):
        return None
    for line in run(["ip", "-o", "-4", "addr", "show"]).splitlines():
        m = re.match(r"\d+:\s+(\S+)\s+inet\s+([\d.]+)/", line)
        if m and m.group(2) == address:
            return m.group(1)
    return None


def shaping(card: str | None, run: Run = _run) -> str | None:
    """The first qdisc on ``card`` that is not a plain one ("netem 8003: root ... loss
    3%"), None when the card is as it comes (or tc is not here)."""
    if not card or shutil.which("tc") is None:
        return None
    for line in run(["tc", "qdisc", "show", "dev", card]).splitlines():
        parts = line.split()
        if len(parts) > 1 and parts[0] == "qdisc" and parts[1] not in PLAIN:
            return " ".join(parts[1:])
    return None
