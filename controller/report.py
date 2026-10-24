"""summary.json and the minimal report.html (docs/02-data.md 10, 04-phases P2).

The report holds everything it shows, so it opens without the module (12 1-3).
Every cell shows its icon as well as its color (11-ui 1.7): green ✔, yellow ⚠,
red ✖, purple ◆, grey ○. Items not tested are listed as NOT RUN.
"""

from __future__ import annotations

import html
import json
import unicodedata
from pathlib import Path
from typing import Any

from controller.pdf import MARGIN, Pdf, width
from controller.report_page import STYLE as PAGE_STYLE
from controller.report_page import page_data, section
from core.ui_schema.design import ICONS  # one place for the shapes (11-ui 1.7)

COLORS = {  # report colors: clear green, others as in the design values
    "green": "#2e7d32",
    "yellow": "#b26a00",
    "red": "#c62828",
    "purple": "#6a1b9a",
    "grey": "#616161",
}
VERDICT_COLORS = {"PASS": "#2e7d32", "FAIL": "#c62828", "INCOMPLETE": "#b26a00"}
ICON_NAMES = {"green": "no problem", "yellow": "warning", "red": "loss or error",
              "purple": "tool or receiver side", "grey": "no data"}  # fmt: skip


def write_report(run_dir: Path, result: dict[str, Any], plan: dict[str, Any]) -> None:
    _atomic(run_dir / "summary.json", json.dumps(result, indent=2, ensure_ascii=False))
    _atomic(run_dir / "report.html", render_html(result, plan, page_data(run_dir, result)))
    tmp = run_dir / "report.pdf.tmp"  # 03 F16: the whole report as PDF (20 GAP-86)
    tmp.write_bytes(render_pdf(result))
    tmp.replace(run_dir / "report.pdf")


CELL_WHY_MAX = 20  # the first ones; the rest are counted (the grid has them all)


def cell_whys(r: dict[str, Any]) -> list[str]:
    """Each cell that is not green, with its time and reasons, in time order: a red cell
    with no loss and normal jitter must say why next to the verdict (GAP-116, the user
    saw only "1 red cell(s)")."""
    out: list[tuple[float, str]] = []
    for d in r.get("devices") or []:
        for c in d.get("cells") or []:
            color = str(c.get("color") or "grey")
            if color == "green":
                continue
            t0 = float(c.get("start_elapsed_s") or 0)
            span = f"{_hm(t0)} to {_hm(t0 + float(c.get('duration_s') or 0))}"
            why = "; ".join(str(x) for x in c.get("reasons") or []) or "no reason recorded"
            out.append((t0, f"{d.get('device')} CH{d.get('ch')} {span} {color}: {why}"))
    lines = [text for _, text in sorted(out, key=lambda x: x[0])]
    if len(lines) > CELL_WHY_MAX:
        lines = [*lines[:CELL_WHY_MAX], f"… and {len(lines) - CELL_WHY_MAX} more (see the grid)"]
    return lines


def _hm(s: float) -> str:
    return f"{int(s // 3600)}h {int(s % 3600 // 60):02d}m"


def render_pdf(r: dict[str, Any]) -> bytes:
    """``report.pdf``: verdict, summary, 5-minute cells per device, causes, problems and
    what was not tested, with the standard library only (``controller.pdf``)."""
    doc = Pdf()
    verdict = str(r.get("verdict"))
    doc.para("ZeroLoss test report", 18, bold=True)
    doc.gap(4)
    doc.room(30)
    label = verdict + (" (with warnings)" if r.get("with_warnings") else "")
    doc.rect(MARGIN, doc.y, width(label, 16) + 20, 26, VERDICT_COLORS.get(verdict, "#616161"))
    doc.text(MARGIN + 10, doc.y + 19, label, 16, bold=True, color="#ffffff")
    doc.gap(34)
    doc.para(f"Run {r.get('run_id')} - build {r.get('build')} - {r.get('test_type')} test - "
             f"Video quality {r.get('quality')} - measured {r.get('actual_s')} s of "
             f"{r.get('planned_s')} s - reason: {r.get('verdict_reason')}", 9)  # fmt: skip
    for d in r.get("verdict_details") or []:
        doc.para(f"- {d}", 9)
    for w in cell_whys(r):
        doc.para(f"  - {w}", 9)
    if r.get("method"):
        doc.gap(6)
        doc.para("How it was measured", 12, bold=True)
        for m in r["method"]:
            doc.para(f"- {m}", 9)
    doc.gap(6)
    doc.para("5-minute cells", 12, bold=True)
    x0 = MARGIN
    legend = [(c, n) for c, n in ICON_NAMES.items()]
    for color, name in legend:  # colors alone never carry the meaning (11-ui 1.7)
        doc.room(12)
        doc.rect(x0, doc.y, 9, 9, COLORS[color])
        doc.text(x0 + 12, doc.y + 8, name, 8)
        x0 += width(name, 8) + 26
    doc.gap(14)
    for d in r.get("devices") or []:
        jitter = d.get("jitter_max_ms")
        doc.para(f"{d.get('device')} - CH{d.get('ch')} - lost {d.get('loss_total')} - max jitter "
                 f"{'-' if jitter is None else f'{jitter:.2f} ms'}", 9, bold=True)  # fmt: skip
        per_row = int(doc.usable // 10)
        cells = d.get("cells") or []
        for i in range(0, len(cells), per_row):
            doc.room(11)
            for j, c in enumerate(cells[i : i + per_row]):
                doc.rect(MARGIN + j * 10, doc.y, 9, 9, COLORS.get(c.get("color"), "#616161"))
            doc.gap(11)
        if not cells:
            doc.para("no data", 9)
        doc.gap(4)
    doc.para("Possible causes (not part of the verdict)", 12, bold=True)
    for a in r.get("analysis") or []:
        doc.para(
            f"{a.get('rule')} ({a.get('category')}, {a.get('confidence')}): {a.get('text')}", 9
        )
    if not r.get("analysis"):
        doc.para("none", 9)
    doc.gap(6)
    doc.para("Problems during the test", 12, bold=True)
    for pr in r.get("problems") or []:
        doc.para(f"{pr.get('code')} {pr.get('device')}: {pr.get('what')} (x{pr.get('count')})", 9)
    if not r.get("problems"):
        doc.para("none", 9)
    doc.gap(6)
    doc.para("Not tested", 12, bold=True)
    doc.para(", ".join(f"{x}: NOT RUN" for x in r.get("not_run") or []) or "none", 9)
    return doc.save()


def render_html(r: dict[str, Any], plan: dict[str, Any], page: dict[str, Any] | None = None) -> str:
    e = html.escape
    verdict = str(r["verdict"])
    rows = []
    for d in r["devices"]:
        cells = (
            "".join(
                f'<span class="cell" style="background:{COLORS[c["color"]]}" '
                f'title="{e(_cell_title(c))}">{ICONS[c["color"]]}</span>'
                for c in d["cells"]
            )
            or "<em>no data</em>"
        )
        counts = " · ".join(f"{ICONS[k]} {v}" for k, v in d["colors"].items() if v)
        jitter = d.get("jitter_max_ms")  # a module without jitter (throughput, 2026-10-17)
        base = (r.get("receiver_baseline") or {}).get(d["device"])  # 05 4.6.3
        baseline = (
            "none"
            if not base
            else f"{'✔ verified' if base.get('verified') else '✖ not verified'} "
            f"{str(base.get('date_utc') or '')[:10]} ({base.get('player')})"
        )
        rows.append(
            f"<tr><td>{e(d['device'])}</td><td>CH{d['ch']}</td><td class='cells'>{cells}</td>"
            f"<td>{e(counts)}</td><td>{d.get('loss_total', '')}</td>"
            f"<td>{'' if jitter is None else f'{jitter:.2f} ms'}</td><td>{e(baseline)}</td></tr>"
        )
    details = "".join(f"<li>{e(x)}</li>" for x in r.get("verdict_details", []))
    whys = "".join(f"<li>{e(x)}</li>" for x in cell_whys(r))
    if whys:
        details += f"<li>Why each box is not green:<ul>{whys}</ul></li>"
    not_run = "".join(f"<li>{e(x)}: NOT RUN</li>" for x in r.get("not_run", []))
    shown = {k: v for k, v in plan.items() if k != "note"}
    # file names may come in NFD (Mac copies): reports show NFC (iptv 1-1-1)
    plan_text = unicodedata.normalize("NFC", json.dumps(shown, ensure_ascii=False))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ZeroLoss report {e(str(r.get("run_id")))}</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 16px; color: #1b1b1b; background: #fafafa; }}
.verdict {{ font-size: 28px; font-weight: 700; color: #fff; display: inline-block;
  padding: 8px 16px; border-radius: 6px; background: {VERDICT_COLORS.get(verdict, "#616161")}; }}
table {{ border-collapse: collapse; margin-top: 16px; width: 100%; }}
.wide {{ overflow-x: auto; }}  /* a wide table scrolls by itself, never the page (11-ui 7) */
td, th {{ border-bottom: 1px solid #ddd; padding: 8px; text-align: left; vertical-align: top; }}
.cells {{ max-width: 60vw; }}
.cell {{ display: inline-block; width: 20px; height: 20px; margin: 1px; color: #fff;
  text-align: center; line-height: 20px; font-size: 13px; border-radius: 3px; }}
{PAGE_STYLE}
</style></head><body>
<h1>ZeroLoss test report</h1>
<div class="verdict">{e(verdict)}{" (with warnings)" if r.get("with_warnings") else ""}</div>{
        "<p><strong>⚠ Not through the router under test: "
        + e(str((r.get("dut") or {}).get("not_through_router_why")))
        + "</strong></p>"
        if (r.get("dut") or {}).get("through_router") is False
        else ""
    }
<p>Run {e(str(r.get("run_id")))} · build {e(str(r.get("build")))} ·
{e(str(r.get("test_type")))} test · Video quality {e(str(r.get("quality")))} ·
measured {r.get("actual_s")} s of {r.get("planned_s")} s ·
reason: {e(str(r.get("verdict_reason")))}{
        " · Baseline test (no router, 05 4.6.3)" if r.get("test_network") == "baseline" else ""
    }</p>
<ul>{details}</ul>{
        "<h2>How it was measured</h2><ul>"
        + "".join(f"<li>{e(str(x))}</li>" for x in r.get("method") or [])
        + "</ul>"
        if r.get("method")
        else ""
    }
{section(page or page_data(None, r))}
<h2>Whole test</h2>
<div class='wide'><table><tr><th>Device</th><th>TV channel</th><th>5-minute cells</th><th>Count</th>
<th>Lost packets</th><th>Max jitter</th><th>Baseline</th></tr>{"".join(rows)}</table></div>
<h2>Possible causes</h2>{_analysis(r)}
<h2>Problems during the test</h2>{_problems(r)}
<h2>Not tested</h2><ul>{not_run or "<li>none</li>"}</ul>
<p><small>Plan: {e(plan_text)}</small></p>
</body></html>
"""


def _analysis(r: dict[str, Any]) -> str:
    """iptv 2 / 2.4: cause candidates, strongest first. They never change the verdict."""
    e = html.escape
    found = r.get("analysis") or []
    lack = r.get("analysis_missing") or []
    stage = ("from the 5-minute results; the 1-second records come next"
             if r.get("analysis_pass") == "report" else "with the 1-second records")  # fmt: skip
    rows = "".join(
        f"<tr><td>{e(str(a.get('rule')))}</td><td>{e(str(a.get('category')))}</td>"
        f"<td>{e(str(a.get('confidence')))}</td><td>{e(', '.join(a.get('devices') or []))}</td>"
        f"<td>{'' if a.get('at_s') is None else e(str(a['at_s'])) + ' s'}</td>"
        f"<td>{e(str(a.get('text')))}</td></tr>"
        for a in found
    )
    table = (
        "<div class='wide'><table><tr><th>Rule</th><th>Where</th><th>Confidence</th>"
        "<th>Devices</th>"
        f"<th>At</th><th>What to check</th></tr>{rows}</table></div>" if found else "<p>none</p>"
    )  # fmt: skip
    missing = "; ".join(f"{m.get('rule')}: {m.get('why')}" for m in lack)
    note = f"<p><small>Not checked: {e(missing)}</small></p>" if missing else ""
    head = f"<p><small>Suggestions only, not part of the verdict ({e(stage)}).</small></p>"
    return head + table + note


def _problems(r: dict[str, Any]) -> str:
    """03 F21: every problem of the run, and the alarm counts (not part of the verdict)."""
    e = html.escape
    rows = r.get("problems") or []
    if not rows:
        return "<p>none</p>"
    body = "".join(
        f"<tr><td>{ICONS['red' if p['level'] == 'error' else 'yellow']} {e(p['code'])}</td>"
        f"<td>{e(p['device'])}</td><td>{e(p['what'])}</td><td>{p['count']}</td>"
        f"<td>{'open at the end' if p['open_at_end'] else 'resolved'}</td></tr>"
        for p in rows
    )
    s = r.get("alarm_stats") or {}
    stats = (
        f"<p>Alarms: {s.get('total')} · per 10 min average {s.get('per_10_min_avg')}, "
        f"most {s.get('per_10_min_max')} · time in a flood "
        f"{round(100 * float(s.get('flood_time_ratio') or 0), 2)}% (not part of the verdict)</p>"
        if s
        else ""
    )
    return (
        "<div class='wide'><table><tr><th>Code</th><th>Device</th><th>What</th><th>Times</th>"
        "<th>State</th></tr>"
        f"{body}</table></div>{stats}"
    )


def _cell_title(c: dict[str, Any]) -> str:
    part = " (partial)" if c.get("partial") else ""
    reasons = "; ".join(c.get("reasons") or []) or "no problem"
    return f"{c.get('start_elapsed_s')} s, {c.get('duration_s')} s{part}: {reasons}"


def _atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
