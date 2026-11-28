"""F25: can this measurement be trusted? One line above the verdict (docs/03-features.md
F25, 02 10 ``measurement_trust``). The verdict rules (05 5.3-5.4) are never touched.

2026-11-28 (user "계속해", after "안만든거 다 만들어"; it was ``None`` "P3"): a red result
that came from the tool or a receiver sends the search to the router. Every check reads
what the run already collected (its problems with codes, the 5-minute summaries, the
cells' colors): nothing new is measured. A problem only noted (level info, e.g. a link of
a card that is not in the test) does not count.

- ``bad`` (✖ "Not trustworthy"): a receiver's own drops, a busy CPU, unsteady sending, a
  player not playing, a test link that dropped.
- ``warn`` (⚠ "Check N items"): Wi-Fi channel or access point changes (they can be the
  router's own doing), receive times from the user clock, a reduced evidence capture,
  grey (no data) cells.
"""

from __future__ import annotations

from typing import Any

# (id, level when it fails, codes, text when it fails, text when it passes)
CODE_CHECKS: tuple[tuple[str, str, tuple[str, ...], str, str], ...] = (
    ("cpu", "bad", ("ZL-RCV-016", "ZL-SYS-003"), "A PC's CPU was busy", "No PC was too busy"),
    ("sending", "bad", ("ZL-SND-005", "ZL-SND-019"), "Sending stopped or missed its rate",
     "Sending held its rate"),
    # 2026-11-28 (R20261127-222944): one slow second, cleared at once, made a 3-hour run
    # "not trustworthy"; a moment behind is a check, its loss is already purple
    ("sending_moment", "warn", ("ZL-SND-016", "ZL-SND-017", "ZL-SND-025"),
     "Sending fell behind for a moment", "Sending never fell behind"),
    ("player", "bad", ("ZL-RCV-019", "ZL-RCV-020", "ZL-TAB-015", "ZL-TAB-016"),
     "Player not playing", "The players played"),
    ("link", "bad", ("ZL-NET-013", "ZL-NET-015"), "A test link dropped", "The links held"),
    ("wifi", "warn", ("ZL-NET-016", "ZL-NET-017"), "Wi-Fi changed channel or access point",
     "No Wi-Fi change"),
    ("capture", "warn", ("ZL-RCV-021",), "Evidence capture was reduced",
     "Evidence capture in full"),
)  # fmt: skip
COUNTED = ("warn", "error")


def _who(problems: list[dict[str, Any]], codes: tuple[str, ...]) -> list[str]:
    return sorted({str(p.get("device") or "") for p in problems
                   if p.get("code") in codes and p.get("level") in COUNTED} - {""})  # fmt: skip


def _hit(problems: list[dict[str, Any]], codes: tuple[str, ...]) -> bool:
    return any(p.get("code") in codes and p.get("level") in COUNTED for p in problems)


def measurement_trust(summary: dict[str, Any], cells: dict[str, list[dict[str, Any]]],
                      tablets: set[str] | None = None) -> dict[str, Any]:  # fmt: skip
    """02 10 ``measurement_trust``: ``level`` ok / warn / bad and every check."""
    problems = [p for p in summary.get("problems") or [] if isinstance(p, dict)]
    checks: list[dict[str, Any]] = []

    def check(cid: str, ok: bool, level: str, bad: str, good: str,
              devices: list[str] | None = None) -> None:  # fmt: skip
        text = good if ok else bad + (f" ({', '.join(devices)})" if devices else "")
        checks.append({"id": cid, "ok": ok, "level": None if ok else level, "text": text})

    drops = {
        dev: n
        for dev, rows in cells.items()
        if (n := sum(int((c.get("loss") or {}).get("host_drop_loss") or 0) for c in rows))
    }
    drops_code = _who(problems, ("ZL-RCV-002",))
    check(
        "host_drops",
        not drops and not drops_code,
        "bad",
        "Receivers dropped packets themselves",
        "No receiver drops",
        sorted(set(drops) | set(drops_code)),
    )
    for cid, level, codes, bad, good in CODE_CHECKS:
        check(cid, not _hit(problems, codes), level, bad, good, _who(problems, codes))
    stamps = {str((c.get("measure_method") or {}).get("timestamps") or "")
              for rows in cells.values() for c in rows} - {""}  # fmt: skip
    # 2026-11-28 (R20261128-080420): an Android app has no kernel receive times, so every
    # tablet run would warn; the check is for a PC whose kernel times failed
    user = sorted(dev for dev, rows in cells.items() if dev not in (tablets or set())
                  and any((c.get("measure_method") or {}).get("timestamps") not in (None, "kernel")
                          for c in rows))  # fmt: skip
    if stamps:
        good = "Receive times from the kernel" + (" (tablets: the app's clock)" if tablets else "")
        check("clock", not user, "warn", "Receive times from the user clock", good, user)
    grey = sorted(str(d.get("device")) for d in summary.get("devices") or []
                  if int((d.get("colors") or {}).get("grey") or 0))  # fmt: skip
    check("gaps", not grey, "warn", "Cells with no data", "No cell without data", grey)
    failed = [c for c in checks if not c["ok"]]
    level = ("bad" if any(c["level"] == "bad" for c in failed)
             else "warn" if failed else "ok")  # fmt: skip
    return {"level": level, "checks": checks}


def line(trust: dict[str, Any] | None) -> str | None:
    """F25 above the verdict."""
    if not trust:
        return None
    failed = [c for c in trust.get("checks") or [] if not c.get("ok")]
    if trust.get("level") == "bad":
        why = "; ".join(c["text"] for c in failed if c.get("level") == "bad")
        return (
            f"Measurement: ✖ Not trustworthy — red results may come from the tool or "
            f"receivers ({why})"
        )
    if trust.get("level") == "warn":
        n = len(failed)
        said = "; ".join(c["text"] for c in failed)
        return f"Measurement: ⚠ Check {n} item{'s' if n != 1 else ''} ({said})"
    return "Measurement: ✔ Trustworthy"
