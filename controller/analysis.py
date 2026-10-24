"""Second analysis pass after the 1-second records arrive (docs/modules/iptv.md 2.4,
02 2-13 ``records``, 20 GAP-85).

One job: read a finished run's folder (plan, 5-minute summaries, events, timeline and
the fetched records), let its module find cause candidates again with the records, and
rewrite only ``analysis``·``analysis_missing``·``analysis_pass`` in ``summary.json``
and the report page. The verdict is never touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from controller.report import write_report
from controller.run import analysis_fields
from core.modules.registry import load_run_entry
from core.protocol.envelope import ZlError
from core.records.jsonl import read_jsonl

KEYS = ("analysis", "analysis_missing", "analysis_pass")


def seconds_of(raw: Path) -> dict[str, list[dict[str, Any]]]:
    """``raw/<device>/*.jsonl`` as {"device|ch": [1-second lines]} (02 7)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for f in sorted(raw.glob("*/*.jsonl")) if raw.is_dir() else []:
        for line in read_jsonl(f):
            if line.get("type") == "s" and isinstance(line.get("e"), int):
                out.setdefault(f"{f.parent.name}|{int(line.get('ch') or 0)}", []).append(line)
    return out


def reanalyze(run_dir: Path, modules_dir: Path) -> bool:
    """True when the report was rewritten; False when there is nothing to do (no
    report, no records) or the module cannot be loaded (the first pass stays)."""
    try:
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        raw_plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    seconds = seconds_of(run_dir / "raw")
    if not seconds:
        return False
    try:
        rules = load_run_entry(modules_dir, str(raw_plan.get("module") or "iptv"))
    except ZlError:
        return False  # the module is gone: the first pass stays
    try:
        plan = rules.validate(raw_plan)
    except (ZlError, ValueError):
        plan = None  # the rules read the files, not the plan
    summaries: dict[str, list[dict[str, Any]]] = {}
    for s in read_jsonl(run_dir / "summaries.jsonl"):
        summaries.setdefault(str(s.get("device")), []).append(s)
    try:
        offsets = json.loads((run_dir / "timeline.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        offsets = {}
    data = {"summaries": summaries, "events": read_jsonl(run_dir / "events.log"),
            "seconds": seconds, "offsets": offsets}  # fmt: skip
    fields = analysis_fields(rules, plan, data, "records")
    before = {k: summary.get(k) for k in KEYS}
    summary.update(fields)
    if {k: summary.get(k) for k in KEYS} == before:
        return False
    write_report(run_dir, summary, raw_plan)  # the same verdict, the new candidates
    return True
