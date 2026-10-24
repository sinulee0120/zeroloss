"""Cell colors and final verdict frame (docs/05-standards.md 5.3, 5.4).

Modules decide the color of each 5-minute cell from their own rule table.
This block only knows how colors combine into the final verdict.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum


class Color(StrEnum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"
    PURPLE = "purple"  # tool or receiver-side cause: DUT cannot be judged
    GREY = "grey"  # no data


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True)
class VerdictResult:
    verdict: Verdict
    with_warnings: bool
    reasons: tuple[str, ...]


def final_verdict(colors: Iterable[Color], completed_planned_time: bool) -> VerdictResult:
    """PASS needs the full planned time and no red, purple or grey. Any red is FAIL."""
    counts = {c: 0 for c in Color}
    for c in colors:
        counts[c] += 1
    if counts[Color.RED]:
        return VerdictResult(Verdict.FAIL, False, (f"{counts[Color.RED]} red cell(s)",))

    reasons = []
    if not completed_planned_time:
        reasons.append("stopped before the planned time")
    if counts[Color.GREY]:
        reasons.append(f"{counts[Color.GREY]} cell(s) without data")
    if counts[Color.PURPLE]:
        reasons.append(f"{counts[Color.PURPLE]} cell(s) with tool or receiver-side problems")
    if sum(counts.values()) == 0:
        reasons.append("no results")
    if reasons:
        return VerdictResult(Verdict.INCOMPLETE, False, tuple(reasons))
    return VerdictResult(Verdict.PASS, counts[Color.YELLOW] > 0, ())
