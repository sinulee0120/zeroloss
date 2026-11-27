"""Words both the screens and the reports use (no Qt: the frozen node has no UI package).

2026-11-28 (receiverPC, the Linux test of the frozen node): report.py took
TEST_LABELS, hms and reason_text from controller.ui, which the frozen node leaves out
(tools/build.py --exclude-module=controller.ui), so the node exited at start with
"No module named 'controller.ui'". They live here; controller.ui.texts and
controller.ui.view keep their names for the screens.
"""

from __future__ import annotations

from typing import Any

TEST_TYPES: dict[str, str] = {
    "iptv": "IPTV test", "all": "Throughput · All",
    "wired": "Throughput · Wired", "wifi": "Throughput · Wi-Fi",
}  # fmt: skip
# a report's test_type (02 10): runs before and after the merge read the same way
TEST_LABELS: dict[str, str] = {**TEST_TYPES, "pc": "IPTV test", "tablet": "IPTV test"}

REASONS = {
    "planned_time_reached": "planned time reached",
    "all_cases_done": "all cases done",
    "took_too_long": "stopped: it took 4 times longer than planned",
    "stopped_by_user": "stopped by user",
    "controller_shutdown": "the Controller service stopped",
}
DEVICE_LOST = "a device stopped answering: {detail}"


def reason_text(reason: Any) -> str:
    """Why a test ended, in words (02 10 ``verdict_reason``)."""
    r = str(reason or "")
    if r.startswith("device_lost: "):
        return DEVICE_LOST.format(detail=r.removeprefix("device_lost: "))
    return REASONS.get(r, r.replace("_", " "))


def hms(seconds: float) -> str:
    s = int(seconds)
    h, m = divmod(s // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s % 60:02d}s"
