"""Assemble the verdict of a run (docs/05-standards.md 5.3-5.4, 03 F10).

The Controller computes the verdict itself (absolute rule); cell colors come from the
module's rules through ``run_entry``; combining colors is the shared ``color_judge``.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from core.blocks.color_judge import Color, Verdict, final_verdict


def in_plan(summary: dict[str, Any], planned_s: float) -> bool:
    """05 5.4 (GAP-105 3): a cell that starts at or after the planned end is the tail of
    the stop, not part of the test."""
    return float(summary.get("start_elapsed_s") or 0) < planned_s


def judge_run(
    rules: Any,
    plan: Any,
    summaries: dict[str, list[dict[str, Any]]],
    planned_s: int,
    measured_s: float,
    completed: bool,
    wireless: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """summary.json content (02-data 10) plus the cells for the report. ``wireless``:
    per Wi-Fi receiver PC ``wifi_auto_connected`` and ``m2u`` (03 F17 "무선 수신 PC")."""
    wireless = wireless or {}
    devices = []
    all_colors: list[Color] = []
    reasons: list[str] = []
    too_little = False
    for device, ch in plan.receivers:
        pieces = sorted(
            (s for s in summaries.get(device, []) if in_plan(s, planned_s)),
            key=lambda s: (s.get("index", 0), s.get("join_segment", 0)),
        )
        cells = []
        for s in pieces:
            color, why = rules.cell_color(s, plan.jitter_warn_ms)
            cells.append(
                {
                    "index": s.get("index"),
                    "start_elapsed_s": s.get("start_elapsed_s"),
                    "duration_s": s.get("duration_s"),
                    "partial": s.get("partial"),
                    "color": color.value,
                    "reasons": list(why),
                    "values": rules.cell_values(s) if hasattr(rules, "cell_values") else [],
                }
            )
            all_colors.append(color)
        ok, why_min = rules.receiver_minimum_met(pieces)
        if not ok:
            too_little = True
            reasons.append(f"{device}: {why_min}")
        counts = Counter(c["color"] for c in cells)
        devices.append(
            {
                "device": device,
                "ch": ch,
                "colors": {c.value: counts.get(c.value, 0) for c in Color},
                **rules.device_totals(pieces),  # module's own fields (layering rule)
                "igmp_version": None,  # P3 (V15)
                "playback": "off" if "playback" in plan.not_run else "on",  # S-49
                "wifi_auto_connected": wireless.get(device, {}).get("wifi_auto_connected"),
                "m2u": wireless.get(device, {}).get("m2u"),  # wired: None (snooping only)
                "cells": cells,
            }
        )
    verdict = final_verdict(all_colors, completed)
    final = verdict.verdict
    if too_little:
        final = Verdict.FAIL  # 03 F10: too little data is FAIL
    return {
        "verdict": final.value,
        "with_warnings": verdict.with_warnings,
        "verdict_details": list(verdict.reasons) + reasons,
        "planned_s": planned_s,
        "actual_s": round(measured_s, 1),
        "test_type": plan.test_type,
        "quality": plan.quality,
        "devices": devices,
        "not_run": list(plan.not_run),
        # how the module measured (iperf3 command, times, waits), kept in the result so
        # the report shows it without the module (12 1-3)
        "method": list(rules.method(plan)) if hasattr(rules, "method") else [],
    }
