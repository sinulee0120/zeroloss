"""F24: what changed since the previous run on this router (docs/03-features.md F24,
02 10 ``changes_since_previous``). The verdict is never touched.

2026-11-28 (user "계속해", after "안만든거 다 만들어"; it was ``None`` "P3"): one quiet change (a
firmware build, the router's mode, a receiver's Wi-Fi channel, a tool build, one setting)
sends the search elsewhere. The previous run is the newest finished one before this one
on the same router: the router's UPnP id when both runs have it, else the same
``dut.model``, else the same sender PC and the same WAN IP way. Only what differs is
listed; ``worse`` marks the changes that lower performance (narrower Wi-Fi, a much
weaker signal, a slower link, another tool build). Link speeds are the wired cards' at
Start (``links_at_start``, 2026-11-28); a card only one run had is not a change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.records.jsonl import read_jsonl

LOOK_BACK = 50  # run folders read at most to find the previous run
RSSI_STEP_DB = 6  # a lowest-RSSI change smaller than this is the air, not a change
SKIP_SETTINGS = ("S-48",)  # the sources have their own line per channel
SECRET_SETTINGS = ("S-51",)  # the test Wi-Fi: its password never reaches a report line
SHORT_MAX = 40


def _load(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any]] | None:
    try:
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        plan = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return (summary, plan) if isinstance(summary, dict) and isinstance(plan, dict) else None


def same_router(a: tuple[dict[str, Any], dict[str, Any]],
                b: tuple[dict[str, Any], dict[str, Any]]) -> bool:  # fmt: skip
    """(summary, plan) of two runs: the router's own UPnP id when both have it (2026-11-28,
    dut_info.py), else the model both name, else the same sender PC and WAN IP way."""
    da, db = a[0].get("dut") or {}, b[0].get("dut") or {}
    ua, ub = (da.get("upnp") or {}).get("udn"), (db.get("upnp") or {}).get("udn")
    if ua and ub:
        return bool(ua == ub)
    if da.get("model") and db.get("model"):
        return bool(da["model"] == db["model"])
    sa = (a[1].get("devices") or {}).get("sender")
    sb = (b[1].get("devices") or {}).get("sender")
    return bool(sa == sb and da.get("wan_ip_way") == db.get("wan_ip_way"))


def previous_run(reports_dir: Path, run_id: str, summary: dict[str, Any],
                 plan: dict[str, Any]) -> tuple[str, dict[str, Any], Path] | None:  # fmt: skip
    """The newest finished run before ``run_id`` on the same router, or None."""
    seen = 0
    for d in sorted(reports_dir.glob("R*-*"), reverse=True):
        if d.name >= run_id:
            continue
        seen += 1
        if seen > LOOK_BACK:
            return None
        got = _load(d)
        if got is not None and same_router(got, (summary, plan)):
            return d.name, got[0], d
    return None


def _wifi(cells: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Each wireless receiver's Wi-Fi at the start and its lowest RSSI (5-minute summaries)."""
    out = {}
    for dev, rows in cells.items():
        wifis = [c["wifi"] for c in rows if c.get("wifi")]
        if not wifis:
            continue
        # each value from the first cell that has it: the width comes with the 10 s full
        # reading, so the first cell often has none (R20261128-080420: "ch153/80 -> ch153")
        start = {k: next((w[k] for w in wifis if w.get(k) is not None), None)
                 for k in ("band", "channel", "width_mhz")}  # fmt: skip
        lows = [float(v) for w in wifis if (v := w.get("rssi_dbm_min")) is not None]
        out[dev] = {**start, "rssi_low": min(lows) if lows else None}
    return out


def _wifi_text(w: dict[str, Any]) -> str:
    width = f"/{w['width_mhz']} MHz" if w.get("width_mhz") else ""
    return f"{w.get('band') or '?'} ch{w.get('channel') or '?'}{width}"


def _hms(seconds: Any) -> str:
    s = int(float(seconds or 0))
    h, m = divmod(s // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s % 60:02d}s"


def _short(v: Any, secret: bool = False) -> str:
    if v is None or v == "" or v == {} or v == []:
        return "default"
    if secret or isinstance(v, dict | list) or len(str(v)) > SHORT_MAX:
        return "set"
    return str(v)


def _receivers(plan: dict[str, Any]) -> str:
    rx = (plan.get("devices") or {}).get("receivers") or []
    return ", ".join(sorted(f"{r.get('device')} CH{r.get('ch') or 1}" for r in rx))


def compare(before: tuple[dict[str, Any], dict[str, Any], dict[str, list[dict[str, Any]]]],
            after: tuple[dict[str, Any], dict[str, Any], dict[str, list[dict[str, Any]]]],
            ) -> list[dict[str, Any]]:  # fmt: skip
    """The F24 items between two runs: (summary, plan, 5-minute summaries by device)."""
    (s0, p0, c0), (s1, p1, c1) = before, after
    items: list[dict[str, Any]] = []

    def item(group: str, name: str, a: Any, b: Any, worse: bool = False) -> None:
        # a value one run does not know is not a change (R20261128-121753: "Router model
        # - -> …" the first time the model was read); a source a run did not have is
        if (a is None or b is None) and group != "source":
            return
        if a != b:
            items.append({"group": group, "name": name, "before": a, "after": b, "worse": worse})

    d0, d1 = s0.get("dut") or {}, s1.get("dut") or {}
    for key, name in (("model", "Router model"), ("firmware_build", "Firmware"),
                      ("mode_detected", "Router mode"), ("wan_ip_way", "WAN IP way"),
                      ("multicast_path", "Multicast path")):  # fmt: skip
        item("router", name, d0.get(key), d1.get(key))
    item("test", "Test type", s0.get("test_type"), s1.get("test_type"))
    item("test", "Quality", s0.get("quality"), s1.get("quality"))
    item("test", "Planned time", _hms(s0.get("planned_s")), _hms(s1.get("planned_s")))
    item("test", "Receivers", _receivers(p0), _receivers(p1))
    l0, l1 = s0.get("links_at_start") or {}, s1.get("links_at_start") or {}
    for dev in sorted(set(l0) & set(l1)):
        for nic in sorted(set(l0[dev]) & set(l1[dev])):
            a_m, b_m = l0[dev][nic], l1[dev][nic]
            item("link", f"{dev} {nic} link", f"{a_m} Mbps", f"{b_m} Mbps", b_m < a_m)
    w0, w1 = _wifi(c0), _wifi(c1)
    for dev in sorted(set(w0) & set(w1)):
        a, b = w0[dev], w1[dev]
        narrower = bool(a.get("width_mhz") and b.get("width_mhz")
                        and b["width_mhz"] < a["width_mhz"])  # fmt: skip
        if not (a.get("width_mhz") and b.get("width_mhz")):  # one does not say: not a change
            a, b = {**a, "width_mhz": None}, {**b, "width_mhz": None}
        item("wifi", f"{dev} Wi-Fi", _wifi_text(a), _wifi_text(b), narrower)
        ra, rb = a.get("rssi_low"), b.get("rssi_low")
        if ra is not None and rb is not None and abs(rb - ra) >= RSSI_STEP_DB:
            item("wifi", f"{dev} lowest RSSI", f"{ra:g} dBm", f"{rb:g} dBm", rb < ra)
    src0, src1 = s0.get("source") or {}, s1.get("source") or {}
    for ch in sorted(set(src0) | set(src1), key=str):
        item("source", f"CH{ch} source", src0.get(ch), src1.get(ch))
    set0, set1 = s0.get("settings_changed") or {}, s1.get("settings_changed") or {}
    for sid in sorted(set(set0) | set(set1)):
        if sid in SKIP_SETTINGS or set0.get(sid) == set1.get(sid):
            continue
        hide = sid in SECRET_SETTINGS
        was, now = _short(set0.get(sid), hide), _short(set1.get(sid), hide)
        items.append({"group": "test", "name": f"Setting {sid}", "before": was,
                      "after": now if was != now else "changed", "worse": False})  # fmt: skip
    v0, v1 = s0.get("versions") or {}, s1.get("versions") or {}
    item("tools", "Controller build", v0.get("controller"), v1.get("controller"), True)
    a0, a1 = v0.get("agents") or {}, v1.get("agents") or {}
    for dev in sorted(set(a0) & set(a1)):
        item("tools", f"{dev} build", a0[dev], a1[dev], True)
    p0, p1 = v0.get("app") or {}, v1.get("app") or {}
    if isinstance(p0, dict) and isinstance(p1, dict):
        for host in sorted(set(p0) & set(p1)):
            item("tools", f"Tablet app ({host})", p0[host], p1[host], True)
    t0, t1 = v0.get("tools") or {}, v1.get("tools") or {}
    for dev in sorted(set(t0) & set(t1)):
        for tool in sorted(set(t0[dev]) | set(t1[dev])):
            a_v, b_v = t0[dev].get(tool), t1[dev].get(tool)
            if a_v and b_v:  # a tool one run did not find is not a version change
                item("tools", f"{dev} {tool}", a_v, b_v, True)
    return items


def changes_since_previous(run_dir: Path, summary: dict[str, Any], plan: dict[str, Any],
                           cells: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:  # fmt: skip
    """02 10 ``changes_since_previous``: ``run_id`` None = the first run for this router."""
    prev = previous_run(run_dir.parent, run_dir.name, summary, plan)
    if prev is None:
        return {"run_id": None, "items": []}
    prev_id, prev_summary, prev_dir = prev
    got = _load(prev_dir)
    prev_plan = got[1] if got else {}
    prev_cells: dict[str, list[dict[str, Any]]] = {}
    for s in read_jsonl(prev_dir / "summaries.jsonl"):
        prev_cells.setdefault(str(s.get("device")), []).append(s)
    return {
        "run_id": prev_id,
        "items": compare((prev_summary, prev_plan, prev_cells), (summary, plan, cells)),
    }


def line(changes: dict[str, Any] | None) -> str | None:
    """The one line under the verdict (F24): changed items, or that nothing changed."""
    if not changes:
        return None
    if not changes.get("run_id"):
        return "First run for this router"
    items = changes.get("items") or []
    if not items:
        return f"Nothing changed since {changes['run_id']}"
    parts = [f"{x['name']} {x['before'] if x['before'] is not None else '-'} → "
             f"{x['after'] if x['after'] is not None else '-'}" + (" ⚠" if x.get("worse") else "")
             for x in items]  # fmt: skip
    return f"Changed since {changes['run_id']}: " + " · ".join(parts)
