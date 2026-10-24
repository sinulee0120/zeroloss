"""Self checks for ``zeroloss verify`` (docs/02-data.md 2-13, 12 2 ``self_checks``, 20 GAP-81).

One job: run named checks whose right answer is known and say which pass. A check that
raises is one failed line, never the end of the run. Core's own checks are in
``core.self_checks``; a module gives its own through ``run_entry.self_checks()``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any

SUITES = ("accuracy", "judgement", "loopback")
DEFAULT_SUITES = ("accuracy", "judgement")

CheckFn = Callable[[], str]  # a short "what was seen" when right; raises when wrong
Check = tuple[str, str, CheckFn]  # (suite, name, fn)


class CheckFailed(Exception):
    """The measured answer is not the known one."""


def expect(got: Any, want: Any, what: str) -> str:
    """``what`` when ``got == want``, else CheckFailed with both values."""
    if got != want:
        raise CheckFailed(f"{what}: got {got}, expected {want}")
    return f"{what} = {want}"


def near(got: float, want: float, tol: float, what: str) -> str:
    if abs(got - want) > tol:
        raise CheckFailed(f"{what}: got {got:.3f}, expected {want:.3f} +-{tol}")
    return f"{what} = {got:.3f} (+-{tol} of {want:.3f})"


def run(checks: Iterable[Check], suites: Iterable[str]) -> dict[str, Any]:
    wanted = tuple(suites)
    rows: list[dict[str, Any]] = []
    for suite, name, fn in checks:
        if suite not in wanted:
            continue
        start = time.monotonic()
        try:
            detail, ok = fn(), True
        except CheckFailed as e:
            detail, ok = str(e), False
        except Exception as e:  # a broken check is a failed line, the rest still run
            detail, ok = f"{type(e).__name__}: {e}", False
        ms = round((time.monotonic() - start) * 1000, 1)
        rows.append({"suite": suite, "name": name, "ok": ok, "detail": detail, "ms": ms})
    passed = sum(1 for r in rows if r["ok"])
    return {"suites": list(wanted), "passed": passed, "failed": len(rows) - passed,
            "checks": rows}  # fmt: skip
