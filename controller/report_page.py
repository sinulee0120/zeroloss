# ruff: noqa: E501  (the inline page script below is kept as one readable block)
"""F16 time range view inside report.html (docs/03-features.md F16, implementation
rules; 20 GAP-86).

One job: the data the page needs (5-minute cells with their loss and jitter on the run
clock, 1-minute summaries and 1-second data files once the records are here) and the
script that shows a chosen range. Everything works from file:// without a server or
the internet: the 1-second values are loaded with <script> tags, an hour at a time.
The overall verdict is shown unchanged whatever range is chosen.
"""

from __future__ import annotations

import itertools
import json
import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from core.records.jsonl import read_jsonl
from core.ui_schema.design import COLORS, ICONS

DATA_DIR = "data"
# the test in the email table's title (the screen's names)
TEST_NAMES = {
    "all": "Throughput · All",
    "wired": "Throughput · Wired",
    "wifi": "Throughput · Wi-Fi",
    "pc": "IPTV test",
    "tablet": "IPTV test",
    "iptv": "IPTV test",
}


def _offsets(run_dir: Path) -> dict[str, float]:
    try:
        got = json.loads((run_dir / "timeline.json").read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in got.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def write_data_files(
    run_dir: Path,
) -> tuple[dict[str, list[list[Any]]], list[int], dict[str, dict[str, Any]]]:
    """1-second records (``raw/``) → ``data/data_hNNN.js`` per run-clock hour and the
    1-minute summaries for the page. Nothing when the records are not here yet."""
    offsets = _offsets(run_dir)
    hours: dict[int, dict[str, list[list[Any]]]] = {}
    minutes: dict[str, dict[int, list[Any]]] = {}
    bursts: dict[
        str, list[list[Any]]
    ] = {}  # [start, seconds, lost, seconds with no packet, last e]
    raw = run_dir / "raw"
    for f in sorted(raw.glob("*/*.jsonl")) if raw.is_dir() else []:
        for x in read_jsonl(f):
            if x.get("type") != "s" or not isinstance(x.get("e"), int):
                continue
            key = f"{f.parent.name}|{int(x.get('ch') or 0)}"
            t = float(x["e"]) + offsets.get(key, 0.0)
            lost, jit = int(x.get("lost") or 0), x.get("jit")
            row = [round(t, 1), lost, jit, x.get("rx"), x.get("br"), x.get("pb")]
            hours.setdefault(int(t // 3600), {}).setdefault(key, []).append(row)
            # [start, lost, jitter max, bitrate min, packets min] per minute (GAP-139)
            m = minutes.setdefault(key, {}).setdefault(
                int(t // 60), [int(t // 60) * 60, 0, None, None, None]
            )
            m[1] += lost
            if isinstance(jit, int | float):
                m[2] = jit if m[2] is None else max(m[2], jit)
            for i, v in ((3, x.get("br")), (4, x.get("rx"))):
                if isinstance(v, int | float):
                    m[i] = v if m[i] is None else min(m[i], v)
            # a loss burst (GAP-147, the reference board's "Loss bursts"): consecutive
            # seconds that lost packets or got none; a good second ends it
            if lost > 0 or x.get("rx") == 0:
                runs = bursts.setdefault(key, [])
                if (
                    runs and int(x["e"]) - runs[-1][4] <= BURST_GAP_S
                ):  # the lost count comes a second after the stream is back
                    runs[-1][1] += 1
                    runs[-1][2] += lost
                    runs[-1][3] += 1 if x.get("rx") == 0 else 0
                    runs[-1][4] = int(x["e"])
                else:
                    runs.append([round(t, 1), 1, lost, 1 if x.get("rx") == 0 else 0, int(x["e"]),
                                 {}])  # fmt: skip
                # what this PC counted in the burst (GAP-148/149 keys); absent = not read
                ev = runs[-1][5]
                for name in EVIDENCE_KEYS:
                    v = x.get(name)
                    if isinstance(v, int) and not isinstance(v, bool):
                        ev[name] = int(ev.get(name, 0)) + v
    folder = run_dir / DATA_DIR
    if hours:
        folder.mkdir(exist_ok=True)
    for h, by_key in hours.items():
        body = json.dumps(by_key, separators=(",", ":"))
        tmp = folder / f"data_h{h:03d}.js.tmp"
        tmp.write_text(f"ZL_DATA({h},{body});\n", encoding="utf-8")
        tmp.replace(folder / f"data_h{h:03d}.js")
    flat = {k: [v[m] for m in sorted(v)] for k, v in minutes.items()}
    for k in minutes:  # records here and nothing went wrong: 0 bursts, not "not here"
        bursts.setdefault(k, [])
    return flat, sorted(hours), {k: burst_view(v) for k, v in bursts.items()}


BURST_LIST_LIMIT = 25  # the heaviest are listed, the count dropped is always stated
EVIDENCE_KEYS = ("nd", "ne", "ub", "fm", "fu")  # card drops/errors, UDP buffer, stream frames
BURST_GAP_S = 2  # seconds this close are one burst, as the live list (GAP-138)
PERIOD_TOLERANCE = 0.02  # a gap within 2 % of a whole multiple of the base fits
HEAVY_SHARE = 0.01  # the heaviest 1 % of bursts (at least 4, 3 gaps) are tested for a period


def burst_view(runs: list[list[Any]]) -> dict[str, Any]:
    """Loss bursts of one receiver for the report (GAP-147, from the RealTest reference
    board): count, packets, gaps between starts (median/min/max) and whether the heaviest
    ones recur at a steady period. Descriptive only: no verdict changes here."""
    starts = [float(r[0]) for r in runs]
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    heavy = sorted(runs, key=lambda r: -int(r[2]))[: max(4, int(len(runs) * HEAVY_SHARE))]
    hs = sorted(float(r[0]) for r in heavy if int(r[2]) > 0)
    hgaps = [b - a for a, b in itertools.pairwise(hs) if b - a > 0]
    period: dict[str, Any] = {"tested": len(hgaps), "fit": 0, "seconds": None}
    if len(hgaps) >= 3:
        base = min(hgaps)
        fit = [
            g for g in hgaps if abs(g / base - round(g / base)) * base <= PERIOD_TOLERANCE * base
        ]
        period = {"tested": len(hgaps), "fit": len(fit), "seconds": round(base, 1)}
    listed = sorted(sorted(runs, key=lambda r: -int(r[2]))[:BURST_LIST_LIMIT], key=lambda r: r[0])
    return {
        "count": len(runs),
        "lost": sum(int(r[2]) for r in runs),
        "list": [[*r[:4], r[5] if len(r) > 5 else {}] for r in listed],
        "gaps": {
            "median": round(statistics.median(gaps), 1) if gaps else None,
            "min": round(min(gaps), 1) if gaps else None,
            "max": round(max(gaps), 1) if gaps else None,
        },
        "period": period,
    }


def run_clock_zero(run_dir: Path | None) -> str | None:
    """The UTC moment of run time 0, from the run's own events (``ts_utc`` - ``run_s``).
    2026-10-09: the page added run time to the moment the run reached RUNNING, 13 s
    after run time 0, so every date and time it showed was 13 s late."""
    if run_dir is None:
        return None
    for e in read_jsonl(run_dir / "events.log")[:50]:
        try:
            ts = datetime.strptime(str(e["ts_utc"]), "%Y-%m-%dT%H:%M:%S.%fZ")
            zero = ts.replace(tzinfo=UTC) - timedelta(seconds=float(e["run_s"]))
        except (KeyError, TypeError, ValueError):
            continue
        return zero.strftime("%Y-%m-%dT%H:%M:%S.") + f"{zero.microsecond // 1000:03d}Z"
    return None


def page_data(run_dir: Path | None, r: dict[str, Any]) -> dict[str, Any]:
    """What the range view draws: cells on the run clock with their values."""
    offsets = _offsets(run_dir) if run_dir else {}
    colors = {(d.get("device"), c.get("index")): c for d in r.get("devices") or []
              for c in d.get("cells") or []}  # fmt: skip
    devices: dict[str, dict[str, Any]] = {}
    rows = read_jsonl(run_dir / "summaries.jsonl") if run_dir else []
    for s in rows:
        dev, ch = str(s.get("device")), int(s.get("ch") or 0)
        key = f"{dev}|{ch}"
        off = offsets.get(key, 0.0)
        t0 = float(s.get("start_elapsed_s") or 0) + off
        judged = colors.get((dev, s.get("index"))) or {}
        if colors and not judged:  # 2026-10-09: a 1 s piece after the planned end was never
            continue  # judged and showed as a grey box with "no reason recorded"
        loss, jit = s.get("loss") or {}, s.get("jitter") or {}
        devices.setdefault(key, {"name": dev, "ch": ch, "cells": []})["cells"].append({
            "t0": round(t0, 1), "t1": round(t0 + float(s.get("duration_s") or 0), 1),
            "color": judged.get("color", "grey"), "reasons": judged.get("reasons") or [],
            "lost": int(loss.get("lost") or 0), "jit_max": jit.get("max_ms"),
            "jit_avg": jit.get("avg_ms"), "n": int(loss.get("expected") or 0),
            "br_avg": (s.get("bitrate_bps") or {}).get("avg"),
            "values": judged.get("values") or [],  # GAP-146: the box's own numbers
            # reordered / duplicates / late / gaps / TS CC errors (the reference "Seq R/D/L/G/E")
            "seq": [loss.get("reordered_recovered"), loss.get("duplicates"),
                    loss.get("late_discarded"), loss.get("loss_events"),
                    (s.get("ts") or {}).get("cc_err")],
        })  # fmt: skip
    if not devices:  # no summaries file (old runs): the judged cells alone
        for d in r.get("devices") or []:
            key = f"{d.get('device')}|{d.get('ch')}"
            devices[key] = {"name": d.get("device"), "ch": d.get("ch"), "cells": [
                {"t0": c.get("start_elapsed_s") or 0,
                 "t1": (c.get("start_elapsed_s") or 0) + (c.get("duration_s") or 0),
                 "color": c.get("color"), "reasons": c.get("reasons") or [], "lost": None,
                 "jit_max": None, "jit_avg": None, "n": 0}
                for c in d.get("cells") or []]}  # fmt: skip
    minutes, hours, bursts = write_data_files(run_dir) if run_dir else ({}, [], {})
    evidence = evidence_list(run_dir, offsets) if run_dir else []
    ends = [c["t1"] for d in devices.values() for c in d["cells"]]
    try:
        state = (
            json.loads((run_dir / "run-state.json").read_text(encoding="utf-8")) if run_dir else {}
        )
    except (OSError, ValueError):
        state = {}
    return {
        "verdict": r.get("verdict"), "planned_s": r.get("planned_s"),
        "run_id": r.get("run_id"), "test": TEST_NAMES.get(str(r.get("test_type")), ""),
        "end_s": max(ends, default=0.0),
        "started_utc": run_clock_zero(run_dir) or state.get("started_utc"),
        "devices": [devices[k] | {"key": k} for k in sorted(devices)],
        "minutes": minutes, "hours": hours, "analysis": r.get("analysis") or [],
        "bursts": bursts, "evidence": evidence,
        "data_dir": DATA_DIR,
    }  # fmt: skip


def evidence_list(run_dir: Path, offsets: dict[str, float]) -> list[dict[str, Any]]:
    """03 F23 (GAP-154): each trouble second's evidence the receivers kept, on the run
    clock, with the pictures and packet clips as paths next to this page."""
    out = []
    for f in sorted((run_dir / "raw").glob("*/*_ev_e*_ch*.json")):
        try:
            info = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        dev, ch = f.parent.name, int(info.get("ch") or 0)
        rel = f"raw/{dev}/"
        out.append({
            "device": dev, "ch": ch, "t": float(info.get("e") or 0) + offsets.get(f"{dev}|{ch}", 0.0),
            "why": list(info.get("why") or []), "packets": info.get("packets"),
            "picture": rel + info["picture"] if info.get("picture") and (f.parent / info["picture"]).is_file() else None,
            "pcap": rel + info["pcap"] if info.get("pcap") else None,
            "kept_less": info.get("kept_less"), "from_s": info.get("from_s"), "to_s": info.get("to_s"),
            "reading": str((info.get("reading") or {}).get("text") or ""),
        })  # fmt: skip
    return sorted(out, key=lambda x: x["t"])


# 2026-10-30 user: the copyable results table goes at the very end of the report
MAIL_SECTION = """<h2>Results table (for email)</h2>
<p class="cap">The whole test in one table. Copy table puts it on the clipboard with its lines, to paste into an email or a document.</p>
<button id="copy">Copy table</button> <span id="copied"></span>
<div id="mail" class="wide"></div>
<script>if(window.zlMail)window.zlMail();</script>
"""


def section(data: dict[str, Any]) -> str:
    """The range view: controls, grid, summary, graph, 1-second panel and causes."""
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    return f"""<section id="range" class="range">
<h2>Time range</h2>
<p id="shown" class="shown"></p>
<div class="controls">
<label>From <input id="from" size="16" placeholder="0h 00m or 2026-10-09 14:30" title="Run time (1h 20m) or the date and time on the clock (2026-10-09 14:30, 10-09 14:30 or 14:30)"></label>
<label>To <input id="to" size="16" placeholder="{_hm(data.get("end_s") or 0)}" title="Run time or the date and time on the clock"></label>
<button data-q="all">All</button><button data-q="first">First 1h</button>
<button data-q="last">Last 1h</button><button data-q="red1">Around first red</button>
<button data-q="red">Red only</button><button data-q="bad" title="Every box that is not green: red, yellow, purple and grey">Problems only</button>
<span id="devs"></span>
<button id="csv">Export CSV</button><button id="pdf">Export PDF</button>
</div>
<div id="cards" class="cards"></div>
<div id="grid" class="grid"></div>
<div class="wide"><table id="list" class="sum"></table></div>
<h3>Loss bursts</h3><div id="bursts"></div>
<h3>Evidence</h3><div id="evidence" class="evidence"></div>
<h3>Not measured</h3><div id="gaps"></div>
<div class="wide"><table id="sum" class="sum"></table></div>
<div id="graph" class="graphs"></div>
<h3>10-second view</h3><div id="fine" class="fine"></div>
<div id="second" class="second"></div>
<h3>Possible causes in this range</h3><ul id="causes"></ul>
</section>
<script>var ZL_REPORT={blob};</script>
<script>{SCRIPT}</script>"""


def _hm(s: float) -> str:
    return f"{int(s // 3600)}h {int(s % 3600 // 60):02d}m"


STYLE = """
.range .controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.range button { padding: 4px 10px; }
.shown { font-weight: 600; }
.evidence .ev { margin: 8px 0 16px; }
.evidence img { max-width: 100%; border: 1px solid #ccc; }
.evidence .cap { font-size: 12px; color: #666; }
.fine .strip { display: flex; flex-wrap: wrap; gap: 1px; margin: 6px 0; }
.fine .strip span { width: 14px; height: 14px; font-size: 9px; line-height: 14px; text-align: center;
  color: #fff; border-radius: 2px; cursor: default; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; margin: 10px 0; }
.card { border: 1px solid #ddd; background: #fff; padding: 8px 10px; border-radius: 4px; }
.card .l { font-size: 12px; color: #5f6368; } .card .v { font-size: 20px; font-weight: 700; }
.card.bad .v { color: #c62828; } .card.warn .v { color: #b26a00; } .card.side .v { color: __SIDE__; }
.badge { display: inline-block; color: #fff; border-radius: 4px; padding: 1px 8px; white-space: nowrap; }
.grid .row { margin: 4px 0; } .grid .name { display: inline-block; min-width: 120px; }
.grid .c { display: inline-block; width: 12px; height: 12px; margin: 1px; cursor: pointer;
  border-radius: 2px; color: #fff; font-size: 9px; line-height: 12px; text-align: center; }
.graphs .chart { position: relative; background: #fff; border: 1px solid #ddd; margin-top: 8px; }
.graphs svg { display: block; width: 100%; }
.graphs .tip { position: absolute; top: 2px; background: #1b1b1b; color: #fff; font-size: 12px;
  padding: 2px 6px; border-radius: 3px; pointer-events: none; white-space: nowrap; display: none; }
.second svg { width: 100%; height: 140px; background: #fff; border: 1px solid #ddd; }
@media print { .controls, #second { display: none; } body { background: #fff; } }
""".replace("__SIDE__", COLORS["purple"])  # sender or receiver side, never the router's red

SCRIPT = r"""
(function(){
var R=ZL_REPORT, C=__CELL_COLORS__;
/* 2026-10-31 user ("Peak loss 0.0704 % 이게 공유기 잘못인가? 리포트에 빨갛게 나와"): red is the
   router's only. Trouble inside a box judged sender or receiver side shows in that box's
   color, inside a yellow box amber; red when a box of it is red or not judged */
function boxAt(cells,t){for(var i=0;i<cells.length;i++){var c=cells[i];if(t>=c.t0&&t<c.t1)return c.color;}return null;}
function judged(colors){if(!colors.length||colors.some(function(x){return x===null||x===undefined||x==="red"||x==="grey";}))return "red";
 return colors.indexOf("purple")>=0?"purple":"yellow";}
function lossInk(color){return color==="purple"?C.purple:color==="yellow"||color==="green"?"#b26a00":"#c62828";}
function lossStyle(color){return " style='color:"+lossInk(color)+";font-weight:700'";}
function cardCls(color){return color==="purple"?"side":color==="red"?"bad":"warn";}
/* the box as a colored badge with what the color means (2026-10-09 user: "only the words
   yellow and grey, no color") */
var NAME={green:"OK",yellow:"Warning",red:"Problem",purple:"Sender or receiver side",grey:"No data"};
var I=__ICONS__;
var st={from:0,to:R.end_s,dev:R.devices.map(function(d){return d.key;}),red:false,bad:false};
function $(id){return document.getElementById(id);}
function hm(s){s=Math.max(0,s);var h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h+"h "+(m<10?"0":"")+m+"m";}
function parse(t,dflt){if(!t)return dflt;var m=/^\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*$/.exec(t);
 if(m&&(m[1]||m[2]))return (parseInt(m[1]||"0")*3600)+(parseInt(m[2]||"0")*60);
 var w=clock(t);return w===null?dflt:w;}
/* GAP-129: a date and time as the clock shows it — "2026-10-09 14:30", "10-09 14:30" or
   "14:30" (the first such time from the start) — becomes run seconds */
function clock(t){if(!R.started_utc)return null;var t0=Date.parse(R.started_utc);
 var m=/^\s*(?:(?:(\d{4})-)?(\d{1,2})-(\d{1,2})\s+)?(\d{1,2}):(\d{2})\s*$/.exec(t);if(!m)return null;
 var s=new Date(t0),d=new Date(m[1]?+m[1]:s.getFullYear(),m[2]?+m[2]-1:s.getMonth(),m[3]?+m[3]:s.getDate(),+m[4],+m[5]);
 if(!m[2])while(d.getTime()<t0-60000)d.setDate(d.getDate()+1);
 return Math.max(0,Math.ceil((d.getTime()-t0)/1000));}
function real(s){if(!R.started_utc)return "";var d=new Date(Date.parse(R.started_utc)+s*1000);
 return " ("+d.toLocaleString()+")";}
function esc(t){return String(t).replace(/[&<>"']/g,function(x){return "&#"+x.charCodeAt(0)+";";});}
/* GAP-129: the boxes of the range that are not green, with the date and time they began
   and why — the list a person reads instead of hovering every box */
/* GAP-129/146: every 5-minute box of the range as a row with its values (the reference
   board's "each 5 minutes: max jitter and loss"), the date and time it began and why;
   "Problems only" keeps the boxes that are not green */
function num2(v){return v===null||v===undefined?"":String(Math.round(v*100)/100);}
var SEQ=["reordered","duplicates","late","gaps","cc_errors"];
/* GAP-129/146: every 5-minute box of the range as a row with its values, as the reference
   board's five-minute row (loss count and %, MLR, jitter, bitrate avg/min, packets, Seq
   R/D/L/G/E), the date and time it began and why; "Problems only" keeps the boxes that
   are not green. "—" is not measured, never 0. Older reports without values keep the
   few columns they have. */
function num2(v){return v===null||v===undefined?"":String(Math.round(v*10000)/10000);}
/* 2026-10-30 user ("NaN은 지저분하다 지워라"): a value in words (a card, a case, an iperf3
   command) is shown as words; "—" is not measured or not for this case, never 0. A test
   without TV channels (throughput) writes no "CH0" */
function chs(c){return c?" CH"+c:"";}
function vcell(v,bad,color){if(v===null||v===undefined||(typeof v==="number"&&isNaN(v)))return "<td title='not measured or not for this case'>—</td>";
 if(typeof v!=="number")return "<td>"+esc(String(v))+"</td>";
 return "<td"+(bad&&v>0?lossStyle(color):"")+">"+num2(v)+"</td>";}
function troubleList(){var out=[];devs().forEach(function(d){d.cells.filter(inRange).forEach(function(c){
  out.push([c.t0,d,c]);});});
 if(!out.length)return "<tr><td>No box in this range.</td></tr>";
 out.sort(function(a,b){return a[0]-b[0];});
 var first=(out[0][2].values||[]),cols=first.filter(function(v){return SEQ.indexOf(v.key)<0;}),
  hasSeq=first.some(function(v){return SEQ.indexOf(v.key)>=0;});
 if(!first.length){cols=[{key:"_lost",label:"Lost",unit:"",bad:true},{key:"_jm",label:"Jitter max",unit:"ms"},
  {key:"_ja",label:"Jitter avg",unit:"ms"},{key:"_br",label:"Bitrate avg",unit:"Mbps"}];}
 function val(c,k){if(k==="_lost")return c.lost;if(k==="_jm")return c.jit_max;if(k==="_ja")return c.jit_avg;
  if(k==="_br")return c.br_avg===null||c.br_avg===undefined?null:c.br_avg/1e6;
  return ((c.values||[]).filter(function(x){return x.key===k;})[0]||{}).value;}
 return "<tr><th>Began</th><th>Run time</th><th>Device</th><th>Box</th>"+
  cols.map(function(v){return "<th>"+esc(v.label+(v.unit?" ("+v.unit+")":""))+"</th>";}).join("")+
  (hasSeq?"<th title='reordered / duplicates / late / gaps / TS CC errors'>Seq R/D/L/G/E</th>":"")+"<th>Why</th></tr>"+
  out.map(function(x){var c=x[2];var sv=SEQ.map(function(k){var v=val(c,k);return v===null||v===undefined?"—":v;});
   return "<tr><td>"+esc(real(c.t0).replace(/^ \(|\)$/g,""))+"</td><td>"+hm(c.t0)+"</td><td>"+
   esc(x[1].name)+chs(x[1].ch)+"</td><td><span class='badge' style='background:"+C[c.color]+"'>"+I[c.color]+" "+
   (NAME[c.color]||c.color)+"</span></td>"+cols.map(function(v){return vcell(val(c,v.key),v.bad,c.color);}).join("")+
   (hasSeq?"<td"+(sv.some(function(v){return v>0;})?" style='color:#b26a00;font-weight:700'":"")+">"+sv.join("/")+"</td>":"")+
   "<td>"+esc((c.reasons||[]).join("; ")||(c.color==="green"?"":"no reason recorded"))+"</td></tr>";}).join("");}
/* GAP-147: the reference board's summary cards, for the chosen range */
function cardsHtml(){var n={green:0,yellow:0,red:0,purple:0,grey:0},jm=null,lp=null,lpc=null,br=[],lost=0,cnt=0,bc=[];
 devs().forEach(function(d){d.cells.filter(inRange).forEach(function(c){n[c.color]=(n[c.color]||0)+1;
  var v=function(k){return ((c.values||[]).filter(function(x){return x.key===k;})[0]||{}).value;};
  var j=v("jitter_max");if(j===undefined)j=c.jit_max;if(j!==null&&j!==undefined&&(jm===null||j>jm))jm=j;
  var p=v("loss_pct");if(p!==null&&p!==undefined&&(lp===null||p>lp)){lp=p;lpc=c.color;}
  var b=v("bitrate_avg");if(b===undefined&&c.br_avg)b=c.br_avg/1e6;if(b!==null&&b!==undefined)br.push(b);});
  var bu=(R.bursts||{})[d.key];if(bu){cnt+=bu.count;lost+=bu.lost;
   (bu.list||[]).forEach(function(r){if(r[2]>0||r[3]>0)bc.push(boxAt(d.cells,r[0]));});}});
 function card(l,v,cls){return "<div class='card "+(cls||"")+"'><div class='l'>"+l+"</div><div class='v'>"+v+"</div></div>";}
 var dash="<span title='not measured'>—</span>";
 return card("Problem boxes (red)",n.red,n.red?"bad":"")+card("Warning boxes",n.yellow,n.yellow?"warn":"")+
  card("Sender or receiver side",n.purple,n.purple?"warn":"")+card("No data",n.grey)+card("OK boxes",n.green)+
  card("Loss bursts",R.bursts&&Object.keys(R.bursts).length?cnt:dash,cnt?cardCls(judged(bc)):"")+
  card("Peak jitter",jm===null?dash:num2(jm)+" ms")+card("Peak loss",lp===null?dash:num2(lp)+" %",lp?cardCls(judged([lpc])):"")+
  card("Avg bitrate",br.length?num2(br.reduce(function(a,b){return a+b;},0)/br.length)+" Mbps":dash);}
/* GAP-147: loss bursts = consecutive seconds that lost packets or got none (a good
   second ends one), the times between them, and whether the heaviest recur at a period */
/* GAP-149: what the receiving PC counted during a burst (the reference's host-path
   bracket): its card's drops and errors, UDP buffer overflow, and the stream frames that
   reached the card. A counter the PC did not report stays "not measured", never 0. */
function evidence(ev){ev=ev||{};var out=[],has=function(k){return ev[k]!==undefined;};
 if(!has("nd")&&!has("ne")&&!has("ub")&&!has("fm"))return "not measured (older build)";
 if(ev.nd>0)out.push("card dropped "+ev.nd);if(ev.ne>0)out.push("card errors "+ev.ne);if(ev.ub>0)out.push("UDP buffer overflow "+ev.ub);
 if(!out.length&&(has("nd")||has("ne")))out.push("this PC dropped nothing: the packets were lost before it (air, router or sender)");
 if(has("fm"))out.push("stream frames: "+ev.fm+" multicast, "+(ev.fu||0)+" unicast to this PC"+((ev.fu||0)>(ev.fm||0)?" (M2U)":""));
 return out.join(" · ");}
/* GAP-147 (the reference's "Measurement gaps"): a value no box in the range measured is
   listed here, so an empty column never reads as 0 */
function gapsHtml(){var seen={},names={};devs().forEach(function(d){d.cells.filter(inRange).forEach(function(c){
  (c.values||[]).forEach(function(v){names[v.key]=v.label;if(v.value!==null&&v.value!==undefined)seen[v.key]=1;});});});
 var miss=Object.keys(names).filter(function(k){return !seen[k];});
 var b=devs().filter(function(d){return !(R.bursts||{})[d.key];});
 var out=miss.map(function(k){return "<li>"+esc(names[k])+": no box in this range measured it (shown as —, not 0)</li>";});
 if(b.length)out.push("<li>Loss bursts: the 1-second records of "+b.map(function(d){return esc(d.name);}).join(", ")+" are not here yet</li>");
 return out.length?"<ul>"+out.join("")+"</ul>":"<p>Everything shown was measured.</p>";}
/* 03 F23 (GAP-154): what the receiving PC kept around each trouble second in the range:
   the picture rendered from the packets it received (S-30 On) and the packets themselves
   (pcap: RTP sequence numbers and TS CC in Wireshark). */
function evidenceHtml(){var ev=(R.evidence||[]).filter(function(x){return x.t>=st.from&&x.t<st.to;});
 if(!(R.evidence||[]).length)return "<p>No trouble second kept evidence in this test.</p>";
 if(!ev.length)return "<p>None in this range ("+R.evidence.length+" in the whole test).</p>";
 return ev.slice(0,100).map(function(x){var head="<b>"+esc(real(x.t).replace(/^ \(|\)$/g,"")||clk(x.t))+"</b> (run "+hm(x.t)+", second "+Math.round(x.t)+") · "+esc(x.device)+chs(x.ch)+" · "+esc(x.why.join(", "));
  var pic=x.picture?"<a href='"+esc(x.picture)+"' target='_blank'><img src='"+esc(x.picture)+"' loading='lazy' alt='frames around the trouble'></a><div class='cap'>Frames from 1 s before to 3 s after, 4 a second, rendered from the packets this PC received.</div>":"<div class='cap'>No picture (Evidence capture S-30 was Off, or the picture could not be made).</div>";
  var pk=x.pcap?"<a href='"+esc(x.pcap)+"'>Packets (pcap, "+(x.packets||0)+" packets, "+(x.from_s)+" s to +"+(x.to_s)+" s)</a>":"No packet clip"+(x.kept_less?": "+esc(x.kept_less):"");
  var rd=x.reading?"<p>"+esc(x.reading)+"</p>":"";
  return "<div class='ev'><p>"+head+"</p>"+rd+pic+"<p>"+pk+"</p></div>";}).join("")+(ev.length>100?"<p>"+(ev.length-100)+" more: choose a shorter range.</p>":"");}
function burstsHtml(){var h="";devs().forEach(function(d){var b=(R.bursts||{})[d.key];
  if(!b){h+="<p>"+esc(d.name)+chs(d.ch)+": the 1-second records are not here yet.</p>";return;}
  var p=b.period,g=b.gaps;
  h+="<p><b>"+esc(d.name)+chs(d.ch)+"</b>: "+b.count+" burst(s), "+b.lost+" packet(s) lost in them."+
   (g.median!==null?" Time between bursts: median "+g.median+" s (min "+g.min+", max "+g.max+").":"")+
   (p.tested>=3?(p.fit>=Math.ceil(p.tested*0.8)?" The heaviest bursts recur about every "+p.seconds+" s ("+p.fit+"/"+p.tested+
    " gaps fit) — look for something on that period (IGMP query, channel scan, a timer).":
    " The heaviest bursts do not recur at a steady period ("+p.fit+"/"+p.tested+" gaps fit)."):"")+
   (b.count>b.list.length?" Listed: the "+b.list.length+" heaviest of "+b.count+".":"")+
   " Grouping changes no verdict.</p>";
  if(b.list.length){h+="<div class='wide'><table class='sum'><tr><th>Began</th><th>Run time</th><th>Seconds</th><th>Lost</th><th>Seconds with no packet</th><th>What this PC counted</th></tr>"+
   b.list.map(function(r){return "<tr><td>"+esc(real(r[0]).replace(/^ \(|\)$/g,""))+"</td><td>"+hm(r[0])+"</td><td>"+r[1]+
    "</td><td"+(r[2]>0?lossStyle(judged([boxAt(d.cells,r[0])])):"")+">"+r[2]+"</td><td>"+r[3]+"</td><td>"+esc(evidence(r[4]))+"</td></tr>";}).join("")+"</table></div>";}});
 return h;}
function inRange(c){return c.t1>st.from&&c.t0<st.to&&(!st.red||c.color==="red")&&(!st.bad||c.color!=="green");}
function devs(){return R.devices.filter(function(d){return st.dev.indexOf(d.key)>=0;});}
function hashSave(){location.hash="from="+Math.round(st.from)+"&to="+Math.round(st.to)+"&dev="+
 st.dev.map(encodeURIComponent).join(",")+(st.red?"&red=1":"")+(st.bad?"&bad=1":"");}
function hashLoad(){var h=location.hash.replace(/^#/,"").split("&"),i,kv;
 for(i=0;i<h.length;i++){kv=h[i].split("=");if(kv[0]==="from")st.from=+kv[1]||0;
 if(kv[0]==="to")st.to=+kv[1]||R.end_s;if(kv[0]==="red")st.red=kv[1]==="1";if(kv[0]==="bad")st.bad=kv[1]==="1";
 if(kv[0]==="dev"&&kv[1])st.dev=kv[1].split(",").map(decodeURIComponent);}}
function firstRed(){var t=null;R.devices.forEach(function(d){d.cells.forEach(function(c){
 if(c.color==="red"&&(t===null||c.t0<t))t=c.t0;});});return t;}
function quick(q){st.red=false;st.bad=false;if(q==="all"){st.from=0;st.to=R.end_s;}
 if(q==="first"){st.from=0;st.to=Math.min(3600,R.end_s);}
 if(q==="last"){st.from=Math.max(0,R.end_s-3600);st.to=R.end_s;}
 if(q==="red1"){var t=firstRed();if(t===null){st.from=0;st.to=R.end_s;}
  else{st.from=Math.max(0,t-1800);st.to=Math.min(R.end_s,t+1800);}}
 if(q==="red"){st.from=0;st.to=R.end_s;st.red=true;}
 if(q==="bad"){st.bad=true;}draw();}
/* 2026-10-30 user ("표로 깔끔하게 복사 붙여넣기로 이메일에 보고서 쓸때 쓸 결과표만 딱 나오면
   좋겠는데 보고서 마지막에"): the whole test, one clean table with its own lines (an email
   keeps inline styles only). A test of cases: a row per case with its main values; a
   test of 5-minute boxes: a row per receiver. */
function mailTable(){var td="border:1px solid #999;padding:4px 8px;",th=td+"background:#eee;text-align:left;";
 function cell(v,t){return "<"+t+" style='"+(t==="th"?th:td)+"'>"+esc(v===null||v===undefined?"—":String(v))+"</"+t+">";}
 function word(c){return (NAME[c]||c);}
 var cases=R.devices.some(function(d){return d.cells.some(function(c){return (c.values||[]).some(function(v){return v.key==="case";});});});
 var head,body=[];
 if(cases){var keys=null;
  R.devices.forEach(function(d){d.cells.forEach(function(c){var main=(c.values||[]).filter(function(v){return !v.detail;});
   if(!keys)keys=main.map(function(v){return [v.key,v.label+(v.unit?" ("+v.unit+")":"")];});
   var byk={};main.forEach(function(v){byk[v.key]=v.value;});
   body.push([d.name].concat(keys.map(function(k){return byk[k[0]];})).concat([word(c.color),(c.reasons||[]).join("; ")]));});});
  head=["Device"].concat((keys||[]).map(function(k){return k[1];})).concat(["Result","Why"]);}
 else{head=["Device","Boxes","OK","Warning","Problem","Sender or receiver side","No data","Lost","Jitter max (ms)"];
  R.devices.forEach(function(d){var n={},lost=0,jm=null;d.cells.forEach(function(c){n[c.color]=(n[c.color]||0)+1;lost+=c.lost||0;
    if(c.jit_max!==null&&c.jit_max!==undefined&&(jm===null||c.jit_max>jm))jm=c.jit_max;});
   body.push([d.name+chs(d.ch),d.cells.length,n.green||0,n.yellow||0,n.red||0,n.purple||0,n.grey||0,lost,jm===null?null:num2(jm)]);});}
 return "<p style='margin:4px 0'><b>"+esc("ZeroLoss "+(R.test||"")+" · "+R.run_id+" · "+R.verdict)+"</b></p>"+
  "<table style='border-collapse:collapse;font:13px sans-serif'><tr>"+head.map(function(h){return cell(h,"th");}).join("")+"</tr>"+
  body.map(function(r){return "<tr>"+r.map(function(v){return cell(v,"td");}).join("")+"</tr>";}).join("")+"</table>";}
window.zlMail=function(){if($("mail")){$("mail").innerHTML=mailTable();$("copy").onclick=copyMail;}};
function copyMail(){var el=$("mail"),r=document.createRange();r.selectNodeContents(el);var s=window.getSelection();
 s.removeAllRanges();s.addRange(r);var ok=false;try{ok=document.execCommand("copy");}catch(e){ok=false;}s.removeAllRanges();
 $("copied").textContent=ok?"Copied: paste it into the email.":"Select the table and press Ctrl+C.";}
function draw(){hashSave();$("from").value=hm(st.from);$("to").value=hm(st.to);
 $("shown").textContent="Showing "+hm(st.from)+real(st.from)+" \u2013 "+hm(st.to)+real(st.to)+
  " of "+hm(R.end_s)+". Overall result: "+R.verdict+" (unchanged)";
 var g="",rows="<tr><th>Device</th><th>Lost</th><th>Jitter avg</th><th>Jitter max</th><th>Cells</th></tr>";
 devs().forEach(function(d){var cells=d.cells.filter(inRange),lost=0,jm=null,ja=0,jn=0,cnt={};
  g+="<div class='row'><span class='name'>"+d.name+chs(d.ch)+"</span>";
  cells.forEach(function(c){var i=d.cells.indexOf(c);
   g+="<span class='c' data-k='"+d.key+"' data-i='"+i+"' style='background:"+C[c.color]+"' title='"+
    hm(c.t0)+" "+c.color+(c.reasons.length?": "+c.reasons.join("; "):"")+"'>"+I[c.color]+"</span>";
   lost+=c.lost||0;if(c.jit_max!==null&&(jm===null||c.jit_max>jm))jm=c.jit_max;
   if(c.jit_avg!==null){ja+=c.jit_avg*(c.n||1);jn+=(c.n||1);}cnt[c.color]=(cnt[c.color]||0)+1;});
  g+="</div>";rows+="<tr><td>"+d.name+chs(d.ch)+"</td><td>"+lost+"</td><td>"+
   (jn?(ja/jn).toFixed(2)+" ms":"-")+"</td><td>"+(jm===null?"-":jm.toFixed(2)+" ms")+"</td><td>"+
   Object.keys(cnt).map(function(k){return I[k]+" "+cnt[k];}).join(" ")+"</td></tr>";});
 $("grid").innerHTML=g;$("sum").innerHTML=rows;if($("mail")){$("mail").innerHTML=mailTable();$("copy").onclick=copyMail;}$("list").innerHTML=troubleList();$("cards").innerHTML=cardsHtml();$("bursts").innerHTML=burstsHtml();$("evidence").innerHTML=evidenceHtml();$("gaps").innerHTML=gapsHtml();graph();causes();fine();
 Array.prototype.forEach.call(document.querySelectorAll("#grid .c"),function(el){
  el.onclick=function(){second(el.getAttribute("data-k"),+el.getAttribute("data-i"));};});}
function line(pts,color){if(!pts.length)return "";return "<polyline fill='none' stroke='"+color+
 "' stroke-width='2' points='"+pts.join(" ")+"'/>";}
/* GAP-139 (user: "the final report must show the whole test from start to end, so it is
   seen at a glance"): per device, charts over the chosen range (the whole test first)
   with grid values, the clock time under them, the 5-minute colors on top, red marks
   where a minute lost packets or got none, and the value under the mouse. 1-minute
   points from the 1-second records; the 5-minute boxes until those are here. */
function nice(v){if(!(v>0))return 1;var p=Math.pow(10,Math.floor(Math.log(v)/Math.LN10)),f=v/p;
 return (f<=1?1:f<=2?2:f<=5?5:10)*p;}
function clk(t){if(!R.started_utc)return hm(t);var d=new Date(Date.parse(R.started_utc)+t*1000),
 hh=("0"+d.getHours()).slice(-2)+":"+("0"+d.getMinutes()).slice(-2);
 return st.to-st.from>86400?(d.getMonth()+1)+"-"+d.getDate()+" "+hh:hh;}
function chart(host,o){var box=document.createElement("div");box.className="chart";host.appendChild(box);
 var W=Math.max(320,box.clientWidth||host.clientWidth-2||document.documentElement.clientWidth-40),H=150,L=52,RM=12,T=22,B=22,PW=W-L-RM,PH=H-T-B,span=Math.max(1,st.to-st.from);
 function X(t){return L+(t-st.from)/span*PW;}
 var hi=0;o.pts.forEach(function(p){if(p[1]!==null&&p[1]>hi)hi=p[1];});hi=nice(Math.max(hi*1.15,o.floor||0));
 function Y(v){return T+PH-v/hi*PH;}
 var s="<svg width='"+W+"' height='"+H+"' viewBox='0 0 "+W+" "+H+"' preserveAspectRatio='xMinYMin meet'>",k,i;
 s+="<text x='"+L+"' y='14' font-size='12' fill='#1b1b1b'>"+esc(o.title)+"</text>";
 if(o.peak)s+="<text x='"+(L+PW)+"' y='14' font-size='12' text-anchor='end' fill='#5f6368'>"+esc(o.peak)+"</text>";
 for(k=0;k<=4;k++){var gv=hi*k/4,gy=Y(gv);s+="<line x1='"+L+"' x2='"+(L+PW)+"' y1='"+gy+"' y2='"+gy+"' stroke='#e6e6e6'/>"+
  "<text x='"+(L-4)+"' y='"+(gy+4)+"' font-size='11' text-anchor='end' fill='#5f6368'>"+(+gv.toFixed(2))+"</text>";}
 o.cells.forEach(function(c){var a=Math.max(c.t0,st.from),b=Math.min(c.t1,st.to);if(b<=a)return;
  s+="<rect x='"+X(a)+"' y='"+(T-6)+"' width='"+Math.max(1,X(b)-X(a))+"' height='5' fill='"+C[c.color]+"'><title>"+
   esc(hm(c.t0)+" "+c.color+(c.reasons.length?": "+c.reasons.join("; "):""))+"</title></rect>";});
 (o.marks||[]).forEach(function(t){s+="<rect x='"+(X(t)-1)+"' y='"+(T+PH+1)+"' width='3' height='5' fill='"+lossInk(judged([boxAt(o.cells,t)]))+"'/>";});
 if(o.warn&&o.warn<=hi)s+="<line x1='"+L+"' x2='"+(L+PW)+"' y1='"+Y(o.warn)+"' y2='"+Y(o.warn)+
  "' stroke='#b26a00' stroke-dasharray='6 4'/><text x='"+(L+PW-4)+"' y='"+(Y(o.warn)-3)+"' font-size='11' text-anchor='end' fill='#b26a00'>warning "+o.warn+"</text>";
 var pts=o.pts.filter(function(p){return p[1]!==null&&p[1]!==undefined;});
 if(o.bars){var bw=Math.max(1.5,PW*(o.step||60)/span*0.8);pts.forEach(function(p){if(p[1]>0)
  s+="<rect x='"+(X(p[0])-bw/2)+"' y='"+Y(p[1])+"' width='"+bw+"' height='"+(T+PH-Y(p[1]))+"' fill='"+lossInk(judged([boxAt(o.cells,p[0])]))+"'/>";});}
 else s+=line(pts.map(function(p){return X(p[0]).toFixed(1)+","+Y(p[1]).toFixed(1);}),o.color);
 for(k=0;k<=5;k++){var tt=st.from+span*k/5;s+="<text x='"+X(tt)+"' y='"+(H-6)+"' font-size='11' fill='#5f6368' text-anchor='"+
  (k===0?"start":k===5?"end":"middle")+"'>"+esc(clk(tt))+"</text>";}
 s+="<line class='x' x1='0' x2='0' y1='"+T+"' y2='"+(T+PH)+"' stroke='#9e9e9e' style='display:none'/></svg>";
 box.innerHTML=s+"<div class='tip'></div>";
 var svg=box.firstChild,tip=box.lastChild,cross=svg.querySelector("line.x");
 box.onmousemove=function(ev){var r=svg.getBoundingClientRect(),x=ev.clientX-r.left;
  if(!pts.length||x<L||x>L+PW){tip.style.display="none";cross.style.display="none";return;}
  var t=st.from+(x-L)/PW*span,best=pts[0];for(i=1;i<pts.length;i++)if(Math.abs(pts[i][0]-t)<Math.abs(best[0]-t))best=pts[i];
  var bx=X(best[0]);cross.setAttribute("x1",bx);cross.setAttribute("x2",bx);cross.style.display="";
  tip.textContent=clk(best[0])+" ("+hm(best[0])+") · "+(+best[1].toFixed(2))+(o.unit?" "+o.unit:"");
  tip.style.display="block";tip.style.left=Math.min(Math.max(0,bx+8),W-200)+"px";};
 box.onmouseleave=function(){tip.style.display="none";cross.style.display="none";};}
var drawnW=-1;
/* drawn to the pixel: again when the page has its width or the window changes size */
window.addEventListener("resize",function(){clearTimeout(window.zlRw);window.zlRw=setTimeout(function(){
 if($("graph").clientWidth!==drawnW)graph();},200);});
function graph(){var host=$("graph");host.innerHTML="";drawnW=host.clientWidth;
 if(!drawnW){setTimeout(graph,100);return;}
 devs().forEach(function(d){var cells=d.cells.filter(function(c){return c.t1>st.from&&c.t0<st.to;});
  var mins=(R.minutes[d.key]||[]).filter(function(m){return m[0]>=st.from&&m[0]<st.to;}),step=60;
  if(!mins.length){mins=cells.map(function(c){return [c.t0,c.lost||0,c.jit_max,null,null];});step=300;}
  var name=R.devices.length>1?d.name+chs(d.ch)+" · ":"",lost=0,jm=null,per=step===60?" per minute":" per 5 minutes";
  mins.forEach(function(m){lost+=m[1]||0;if(m[2]!==null&&m[2]!==undefined&&(jm===null||m[2]>jm))jm=m[2];});
  var marks=mins.filter(function(m){return m[1]>0||m[4]===0;}).map(function(m){return m[0]+step/2;});
  chart(host,{title:name+"Jitter max (ms)"+per,unit:"ms",color:"#1565c0",floor:5,cells:cells,marks:marks,
   pts:mins.map(function(m){return [m[0],m[2]];}),warn:R.jitter_warn_ms,peak:jm===null?"":"max "+jm.toFixed(2)+" ms"});
  if(mins.some(function(m){return m[3]!==null&&m[3]!==undefined;}))
   chart(host,{title:name+"Bitrate, lowest in each minute (Mbps)",unit:"Mbps",color:C.green,floor:1,cells:cells,marks:marks,
    pts:mins.map(function(m){return [m[0],m[3]===null||m[3]===undefined?null:m[3]/1e6];})});
  chart(host,{title:name+"Lost packets"+per,unit:"packets",bars:true,step:step,floor:4,cells:cells,marks:marks,
   pts:mins.map(function(m){return [m[0],m[1]];}),peak:lost+" lost in this range"});});}
function causes(){var ul="";R.analysis.forEach(function(a){if(a.at_s===null||(a.at_s>=st.from&&a.at_s<st.to))
 ul+="<li>"+a.rule+" ("+a.category+", "+a.confidence+"): "+a.text+"</li>";});
 $("causes").innerHTML=ul||"<li>none</li>";}
var loaded={},waiting=[];
window.ZL_DATA=function(h,rows){loaded[h]=rows;waiting.splice(0).forEach(function(f){f();});};
function hour(h,then){if(loaded[h]){then();return;}if(R.hours.indexOf(h)<0){then();return;}
 waiting.push(then);var s=document.createElement("script");s.src=R.data_dir+"/data_h"+("00"+h).slice(-3)+".js";
 document.head.appendChild(s);}
/* GAP-147 (the reference's "Fine-grained view (10 s)"): the 1-second records of the range
   in 10-second buckets, so a test shorter than 5 minutes, or one bad minute, shows its
   shape. A letter per bucket (OK / Problem: lost or no packet), the buckets that are not
   OK in a table. Up to 2 hours at a time. Nothing here changes a verdict. */
var FINE_MAX_S=7200;
function fine(){var host=$("fine"),span=st.to-st.from;
 if(span>FINE_MAX_S){host.innerHTML="<p>Choose 2 hours or less (From / To, or a box) to see 10-second buckets.</p>";return;}
 var hs=[],h;for(h=Math.floor(st.from/3600);h<=Math.floor(Math.max(st.from,st.to-1)/3600);h++)if(R.hours.indexOf(h)>=0)hs.push(h);
 if(!hs.length){host.innerHTML="<p>The 1-second records are not here yet.</p>";return;}
 var left=hs.length;hs.forEach(function(x){hour(x,function(){if(--left===0)fineDraw(hs);});});}
function fineDraw(hs){var host=$("fine"),out="";devs().forEach(function(d){var b={};
  hs.forEach(function(x){((loaded[x]||{})[d.key]||[]).forEach(function(r){var t=r[0];if(t<st.from||t>=st.to)return;
   var k=Math.floor(t/10)*10,o=b[k]||(b[k]={t:k,n:0,lost:0,jit:null,br:[],rx0:0});o.n++;o.lost+=r[1]||0;
   if(r[2]!==null&&r[2]!==undefined&&(o.jit===null||r[2]>o.jit))o.jit=r[2];if(r[4])o.br.push(r[4]);if(r[3]===0)o.rx0++;});});
  var ks=Object.keys(b).map(Number).sort(function(x,y){return x-y;});
  out+="<p><b>"+esc(d.name)+chs(d.ch)+"</b>: "+ks.length+" buckets of 10 s.</p><div class='strip'>"+ks.map(function(k){var o=b[k],bad=o.lost>0||o.rx0>0;
   return "<span style='background:"+(bad?lossInk(judged([boxAt(d.cells,k)])):C.green)+"' title='"+esc(clk(k)+" ("+hm(k)+"): lost "+o.lost+", jitter max "+(o.jit===null?"—":o.jit)+" ms"+(o.rx0?", "+o.rx0+" s with no packet":""))+"'>"+(bad?"C":"G")+"</span>";}).join("")+"</div>";
  var badK=ks.filter(function(k){return b[k].lost>0||b[k].rx0>0;});
  if(badK.length)out+="<div class='wide'><table class='sum'><tr><th>Began</th><th>Run time</th><th>Seconds</th><th>Lost</th><th>Seconds with no packet</th><th>Jitter max (ms)</th><th>Bitrate min (Mbps)</th></tr>"+
   badK.slice(0,300).map(function(k){var o=b[k];return "<tr><td>"+esc(real(k).replace(/^ \(|\)$/g,""))+"</td><td>"+hm(k)+"</td><td>"+o.n+
    "</td><td"+lossStyle(judged([boxAt(d.cells,k)]))+">"+o.lost+"</td><td>"+o.rx0+"</td><td>"+(o.jit===null?"—":o.jit)+"</td><td>"+
    (o.br.length?num2(Math.min.apply(null,o.br)/1e6):"—")+"</td></tr>";}).join("")+"</table></div>";});
 host.innerHTML=out;}
function second(key,i){var d=R.devices.filter(function(x){return x.key===key;})[0],c=d.cells[i];
 var hs=[Math.floor(c.t0/3600),Math.floor((c.t1-1)/3600)];
 hour(hs[0],function(){hour(hs[1],function(){var rows=[];hs.forEach(function(h,j){if(j&&h===hs[0])return;
  ((loaded[h]||{})[key]||[]).forEach(function(r){if(r[0]>=c.t0&&r[0]<c.t1)rows.push(r);});});
  var box=$("second");if(!rows.length){box.innerHTML="<p>"+d.name+" "+hm(c.t0)+": 1-second values are "+
   "not here yet (they come after the test).</p>";return;}
  var span=Math.max(1,c.t1-c.t0),jm=1,lm=1;rows.forEach(function(r){if(r[2]>jm)jm=r[2];if(r[1]>lm)lm=r[1];});
  box.innerHTML="<p>"+d.name+chs(d.ch)+" "+hm(c.t0)+" \u2013 "+hm(c.t1)+" (1 s)</p><svg viewBox='0 0 1000 140' "+
   "preserveAspectRatio='none'>"+line(rows.map(function(r){return ((r[0]-c.t0)/span*1000).toFixed(1)+","+
   (65-r[1]/lm*60).toFixed(1);}),lossInk(judged([c.color])))+line(rows.filter(function(r){return r[2]!==null;}).map(function(r){
   return ((r[0]-c.t0)/span*1000).toFixed(1)+","+(135-r[2]/jm*60).toFixed(1);}),"#1565c0")+"</svg>";
  box.setAttribute("data-rows",rows.length);});});}
function q(s){return '"'+String(s).replace(/"/g,'""')+'"';}
function csv(){var out="device,ch,from_s,to_s,color,lost,jitter_max_ms,why\n";devs().forEach(function(d){
 d.cells.filter(inRange).forEach(function(c){out+=[d.name,d.ch,c.t0,c.t1,c.color,c.lost,c.jit_max,
  q((c.reasons||[]).join("; "))].join(",")+"\n";});});
 var sec="";Object.keys(loaded).forEach(function(h){Object.keys(loaded[h]).forEach(function(k){
  if(st.dev.indexOf(k)<0)return;loaded[h][k].forEach(function(r){if(r[0]>=st.from&&r[0]<st.to)
  sec+=[k,r[0],r[1],r[2]].join(",")+"\n";});});});
 if(sec)out+="\n1-second,run_s,lost,jitter_ms\n"+sec;
 var a=document.createElement("a");a.href=URL.createObjectURL(new Blob([out],{type:"text/csv"}));
 a.download="range.csv";document.body.appendChild(a);a.click();a.remove();window.ZL_CSV=out;}
hashLoad();
$("devs").innerHTML=R.devices.map(function(d){return "<label><input type='checkbox' value='"+d.key+"' "+
 (st.dev.indexOf(d.key)>=0?"checked":"")+"> "+d.name+chs(d.ch)+"</label>";}).join(" ");
Array.prototype.forEach.call(document.querySelectorAll("#devs input"),function(el){el.onchange=function(){
 st.dev=Array.prototype.filter.call(document.querySelectorAll("#devs input"),function(x){return x.checked;})
 .map(function(x){return x.value;});draw();};});
Array.prototype.forEach.call(document.querySelectorAll("[data-q]"),function(el){
 el.onclick=function(){quick(el.getAttribute("data-q"));};});
$("from").onchange=function(){st.from=parse(this.value,0);draw();};
$("to").onchange=function(){st.to=parse(this.value,R.end_s);draw();};
$("csv").onclick=csv;$("pdf").onclick=function(){window.print();};
draw();
})();
"""
SCRIPT = SCRIPT.replace("__ICONS__", json.dumps(ICONS, ensure_ascii=False))  # 11-ui 1.7
SCRIPT = SCRIPT.replace(
    "__CELL_COLORS__",
    json.dumps(  # the design's cell colors only
        {k: COLORS[k] for k in ("green", "yellow", "red", "purple", "grey")}
    ),
)
