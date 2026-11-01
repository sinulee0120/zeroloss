"""What the main screen shows, computed from the Store only (docs/11-ui.md 3, 1.5).

Pure functions, no Qt: the widgets only copy these values. Device rows come from the
module's ``row`` (02 2-1); this file adds what every module shares: stale and idle
rows, RSSI / Wi-Fi from the heartbeat links, and the link tooltip.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from controller.precheck import IDLE_STEPS, is_wireless
from controller.ui.store import Status
from controller.ui.texts import TEST_LABELS, TEXT
from core.ui_schema.design import ICONS

STALE_S = 3.0  # 11-ui 1.5
# Weak Wi-Fi signal: the -70 dBm of cause rule R-04 (docs/modules/iptv.md 2), used as is
WEAK_RSSI_DBM = -70
MINUS = chr(0x2212)  # a real minus sign, as in the spec examples


@dataclass(frozen=True)
class Part:
    text: str
    key: bool = False  # Jitter / Loss: the boldest (D17)
    warn: str = ""  # "" | "yellow" | "red": an icon before the text
    tooltip: str = ""


@dataclass(frozen=True)
class RowView:
    name: str
    color: str  # green yellow red purple grey (05 5.3)
    icon: str
    why: str
    parts: tuple[Part, ...]  # main values
    extra: tuple[Part, ...]  # thin secondary values (RSSI, Wi-Fi, link)
    tooltip: str
    dim: bool = False  # stale or idle: grey text
    alarm: bool = False  # an open error for this device: red border (F21)
    ch: int | None = None  # the channel it receives now (F19 channel box)
    channels: tuple[int, ...] = ()  # channels on air it may change to; () = no box


@dataclass(frozen=True)
class CellView:
    color: str
    icon: str
    tooltip: str
    merged: int = 1  # 5-min results in this box (more than 1: an older stretch, worst shown)


def _num(v: Any) -> str:
    if isinstance(v, float):
        s = f"{v:.1f}" if abs(v) < 100 else f"{v:.0f}"
        return s.replace("-", MINUS)
    return str(v).replace("-", MINUS)


def _speed(mbps: int) -> str:
    return f"{mbps / 1000:g} Gbps" if mbps >= 1000 else f"{mbps} Mbps"


def _clock(ts_utc: str, fmt: str = "%H:%M") -> str:
    try:
        return datetime.fromisoformat(ts_utc.replace("Z", "+00:00")).astimezone().strftime(fmt)
    except ValueError:
        return "?"


def _wifi_parts(
    links: list[dict[str, Any]], changes: list[dict[str, Any]], test_ip: str | None = None
) -> list[Part]:
    """RSSI and Wi-Fi only for wireless receivers; wired ones get no field at all. With
    the test card known, only that card: the PC's internet Wi-Fi is not part of the test
    (GAP-105 1)."""
    out: list[Part] = []
    if test_ip and test_ip != "wifi":
        links = [x for x in links if x.get("ip") == test_ip]
    for x in links:
        wifi = x.get("wifi")
        if not isinstance(wifi, dict):
            continue
        rssi = wifi.get("rssi_dbm")
        if isinstance(rssi, int | float):
            weak = rssi < WEAK_RSSI_DBM
            tip = TEXT["rssi_weak_tip"].format(v=_num(WEAK_RSSI_DBM)) if weak else ""
            out.append(
                Part(TEXT["rssi"].format(v=_num(rssi)), warn="yellow" if weak else "", tooltip=tip)
            )
        else:
            out.append(Part(TEXT["rssi_none"], tooltip=TEXT["rssi_none_tip"]))
        mine = [c for c in changes if c.get("nic") == x.get("nic")]
        if mine:
            first, last = mine[0]["from"], mine[-1]["to"]
            text = TEXT["wifi_changed"].format(
                c0=first.get("channel"),
                c1=last.get("channel"),
                w0=first.get("width_mhz") or "?",
                w1=last.get("width_mhz") or "?",
                t=_clock(str(mine[-1].get("ts_utc", ""))),
            )
            history = "\n".join(
                f"{_clock(str(c.get('ts_utc', '')))}  Wi-Fi {c['from'].get('channel')} → "
                f"{c['to'].get('channel')}, {c['from'].get('width_mhz') or '?'} → "
                f"{c['to'].get('width_mhz') or '?'} MHz, {c['to'].get('band')}"
                for c in mine
            )
            out.append(Part(text, warn="yellow", tooltip=history))
        elif wifi.get("channel") is not None:
            w = wifi.get("width_mhz")
            mbps = max(float(wifi.get("tx_mbps") or 0), float(wifi.get("rx_mbps") or 0))
            least = width_from_rate(mbps) if w is None else None
            if least:  # 2026-10-16 user: "bandwidth가 얼마야? 80Mhz?" (the dongle said nothing)
                tip = TEXT["wifi_rate_width_tip"].format(mbps=mbps, w=least)
                out.append(Part(TEXT["wifi_rate_width"].format(ch=wifi.get("channel"), w=least),
                                tooltip=tip))  # fmt: skip
            else:
                key = "wifi" if w is not None else "wifi_no_width"
                out.append(Part(TEXT[key].format(ch=wifi.get("channel"), w=w)))
    return out


# the most 1~2 streams carry at each width (802.11ax, 0.8 us guard), Mbps
MAX_2SS_MBPS = ((20, 286.8), (40, 573.5), (80, 1201.0))


def width_from_rate(mbps: float) -> int | None:
    """The least channel width a link rate proves for a 1~2 stream card, when the driver
    does not report the width: 867 Mbps cannot be 40 MHz, so at least 80. None: no proof."""
    least = None
    for width, top in MAX_2SS_MBPS:
        if mbps > top:
            least = width * 2
    return least


def card_text(links: list[dict[str, Any]], test_ip: str | None) -> str:
    """The card the test uses on this PC, always shown (2026-10-09 GAP-127, user: "리시버가
    무선으로 받는건가? 유선인지 무선인지 어디서 알아?"): "Wired enp0s31f6 · 1 Gbps" or
    "Wi-Fi wlx… · SSID". "" when it is not known yet."""
    if not test_ip:
        return ""
    if test_ip == "wifi":
        return TEXT["card_wifi_test"]
    card = next((x for x in links if x.get("ip") == test_ip), None)
    if card is None:
        return TEXT["card_ip"].format(ip=test_ip)
    nic = str(card.get("nic") or "")
    if is_wireless(card):
        ssid = (card.get("wifi") or {}).get("ssid") if isinstance(card.get("wifi"), dict) else None
        return TEXT["card_wireless"].format(nic=nic, ssid=f" · {ssid}" if ssid else "")
    mbps = card.get("link_mbps")
    speed = f" · {_speed(mbps)}" if isinstance(mbps, int) else ""
    return TEXT["card_wired"].format(nic=nic, speed=speed)


def _link(checks: list[dict[str, Any]]) -> tuple[list[Part], str]:
    """Wired links from the Controller's judgement (02 2-1 ``link_checks``, 05 1.8):
    tooltip only while normal; below needed ✖, slower than usual ⚠, down ✖ on the row."""
    parts: list[Part] = []
    tips: list[str] = []
    for c in checks:
        label, level, mbps = str(c.get("label") or ""), c.get("level"), c.get("link_mbps")
        speed = _speed(mbps) if isinstance(mbps, int) else "?"
        need = TEXT["link_need_tip"].format(n=f"{float(c.get('needed_mbps') or 0):.0f}")
        usual = c.get("usual_mbps")
        usual_tip = TEXT["link_usual_tip"].format(s=_speed(usual)) if isinstance(usual, int) else ""
        tip = " · ".join(
            x
            for x in (
                TEXT["link"].format(label=label, speed=speed, duplex=c.get("duplex") or "").strip(),
                need,
                usual_tip,
            )
            if x
        )
        if level == "down":
            parts.append(Part(TEXT["link_down"].format(label=label), warn="red", tooltip=tip))
        elif level == "short":
            parts.append(
                Part(TEXT["link_short"].format(label=label, speed=speed), warn="red", tooltip=tip)
            )
        elif level == "slow":
            parts.append(
                Part(
                    TEXT["link_short"].format(label=label, speed=speed), warn="yellow", tooltip=tip
                )
            )
        tips.append(tip)
    return parts, "\n".join(tips)


def _link_status(status: Status) -> tuple[str, str] | None:
    """The status line names a link problem first: losses after it are likely from it."""
    worst: tuple[int, str, str] | None = None
    for dev in (status.run.get("devices") or {}).values():
        for c in (dev or {}).get("link_checks") or []:
            level, label, mbps = c.get("level"), c.get("label") or "", c.get("link_mbps")
            at = _clock(str(c.get("since_utc", "")), "%H:%M:%S")
            speed = _speed(mbps) if isinstance(mbps, int) else "?"
            if level == "short":
                cand = (2, "red", TEXT["status_link_short"].format(label=label, speed=speed, at=at))
            elif level == "down":
                cand = (2, "red", TEXT["status_link_down"].format(label=label, at=at))
            elif level == "slow":
                usual = c.get("usual_mbps")
                s = _speed(usual) if isinstance(usual, int) else "?"
                cand = (
                    1,
                    "yellow",
                    TEXT["status_link_slow"].format(label=label, speed=speed, at=at, usual=s),
                )
            else:
                continue
            if worst is None or cand[0] > worst[0]:
                worst = cand
    return (worst[1], worst[2]) if worst else None


def _alarmed(status: Status) -> set[str]:
    return {
        str(p.get("device"))
        for p in (status.problems or {}).get("problems") or []
        if p.get("active") and p.get("level") == "error"
    }


def _label(item: dict[str, Any]) -> str:
    """A row item's words ("label"; "text" from a module of build 0033 and before)."""
    return str(item.get("label") or item.get("text") or "")


THIS_PC = "controller"  # controller/service.py THIS_PC


def device_rows(status: Status) -> list[RowView]:
    rows = _device_rows(status)
    alarmed = _alarmed(status)
    return [replace(r, alarm=r.name in alarmed) for r in rows]


def _device_rows(status: Status) -> list[RowView]:
    run_devices: dict[str, Any] = status.run.get("devices") or {}
    rows: list[RowView] = []
    known = {str(d.get("name")): d for d in status.devices}
    # this PC is a row too while a test runs, in it or not (2026-10-22 user: "왜 노트북이
    # 두개만 보여" -- a Wi-Fi run left the Controller out without a word)
    cases = status.run.get("cell_s") == 0  # a test of cases (throughput): this PC can be in it
    names = set(known) | set(run_devices) | ({THIS_PC} if run_devices and cases else set())
    for name in sorted(names):
        host = str(known.get(name, {}).get("host", ""))
        dev = run_devices.get(name)
        if dev is None:
            rows.append(
                RowView(
                    name,
                    "grey",
                    ICONS["grey"],
                    "",
                    (Part(host), Part(TEXT["not_in_test"] if run_devices else TEXT["idle"])),
                    (),
                    host,
                    dim=True,
                )
            )
            continue
        row = dev.get("row") or {}
        parts = [
            Part(
                f"{_label(i)} {_num(i['value'])} {i.get('unit', '')}".strip()
                if i.get("value") is not None
                else _label(i),
                key=bool(i.get("key")),
            )
            for i in row.get("items") or []
            if isinstance(i, dict)
        ]
        links = [x for x in dev.get("links") or [] if isinstance(x, dict)]
        link_parts, link_tip = _link(list(dev.get("link_checks") or []))
        test_ip = dev.get("test_ip") if isinstance(dev.get("test_ip"), str) else None
        extra = _wifi_parts(links, list(dev.get("wifi_changes") or []), test_ip) + link_parts
        card = card_text(links, test_ip)
        if card:  # which card the test goes over, wired or Wi-Fi, always (GAP-127)
            extra = [Part(card, tooltip=TEXT["card_tip"]), *extra]
        color, why = str(row.get("color") or "grey"), str(row.get("why") or "")
        age = float(dev.get("age_s") or 0.0)
        dim = age >= STALE_S
        if dim:
            color, why = "grey", TEXT["last_update"].format(s=f"{age:.0f}")
            parts.append(Part(why))
        tooltip = "\n".join(x for x in (host, link_tip) if x)
        ch = row.get("channel")
        on_air = tuple((status.run.get("channels") or {}).get("on_air") or ())
        if isinstance(ch, int) and on_air:  # the box shows the channel: not twice
            parts = [p for p in parts if p.text != f"CH{ch}"]
        else:
            ch, on_air = None, ()
        rows.append(
            RowView(
                name,
                color,
                ICONS.get(color, ICONS["grey"]),
                why,
                tuple(parts),
                tuple(extra),
                tooltip,
                dim,
                ch=ch,
                channels=on_air,
            )
        )
    return rows


def cell_rows(status: Status) -> dict[str, list[CellView]]:
    cells: dict[str, Any] = status.run.get("cells") or {}
    out: dict[str, list[CellView]] = {}
    for name in sorted(cells):
        out[name] = []
        for c in cells[name]:
            color = str(c.get("color") or "grey")
            index = c.get("index")
            reasons = "; ".join(str(r) for r in c.get("reasons") or [])
            tip = f"5-min result {index}{' (partial)' if c.get('partial') else ''}: {color}"
            vals = value_text(c.get("values") or [])  # GAP-146: its max jitter and loss
            text = "\n".join(x for x in (tip, vals, reasons) if x)
            out[name].append(CellView(color, ICONS.get(color, ICONS["grey"]), text))
    return out


WORST = ("red", "purple", "grey", "yellow", "green")  # a merged box shows the first found
MERGE_CELLS = (12, 72, 288)  # 1 h, 6 h, 24 h of 5-min results


def fit_cells(cells: list[CellView], fit: int) -> list[CellView]:
    """2026-10-25 user ("5분 결과 아이콘이 우측으로만 늘어나서 다 보이질 않아. 9시간을
    했는데. 몇개밖에 안보여"): the whole test always fits. When the 5-min results do not,
    the oldest whole hours (then 6 h, then days) become one box each, its worst color --
    only as many as it takes (2026-11-01 user: "7시간 37분을 돌았는데 14개 마크만 표시되네":
    every hour was merged and most of the row stayed empty); the rest one box per result."""
    if len(cells) <= fit:
        return cells
    n = len(cells)
    for size in MERGE_CELLS:
        k = -(-(n - fit) // (size - 1))  # fewest merged boxes: k + (n - k * size) <= fit
        if k <= n // size:
            boxes = [_merge(cells[i : i + size], i, size) for i in range(0, k * size, size)]
            return boxes + cells[k * size :]
    size = -(-n // max(1, fit))
    return [_merge(cells[i : i + size], i, size) for i in range(0, n, size)]


def _merge(part: list[CellView], first: int, size: int) -> CellView:
    color = next(c for c in WORST if any(x.color == c for x in part))
    counts = ", ".join(f"{n} {c}" for c in WORST if (n := sum(x.color == c for x in part)))
    span = TEXT["cells_merged"].format(a=first * 5 // 60, b=(first + len(part)) * 5 // 60)
    worst = next((x.tooltip for x in part if x.color == color), "") if color != "green" else ""
    tip = "\n".join(x for x in (f"{span}: {counts}", worst) if x)
    return CellView(color, ICONS.get(color, ICONS["grey"]), tip, merged=len(part))


@dataclass(frozen=True)
class CaseRow:
    device: str
    color: str
    icon: str
    values: tuple[str, ...]
    why: str
    detail: str = ""  # the values not shown as columns, for the row's hover


def _cell(v: Any) -> str:
    """A number as the screens show numbers; text as it is (an iperf3 "-c" stays a dash)."""
    return _num(v) if isinstance(v, int | float) and not isinstance(v, bool) else str(v)


def case_table(status: Status) -> tuple[tuple[str, ...], list[CaseRow]]:
    """A test without 5-minute boxes (Throughput): one line per finished case with the
    values its module names, the verdict icon and why (2026-10-17 user: "5-min result
    화면도 지금 이상황에선 안보여도 되는데 ... 다른걸 보여주던가")."""
    cells: dict[str, Any] = status.run.get("cells") or {}
    heads: tuple[str, ...] = ()
    rows = []
    for name in sorted(cells):
        for c in cells[name]:
            every = [v for v in c.get("values") or [] if isinstance(v, dict)]
            vals = [v for v in every if not v.get("detail")]
            more = "\n".join(
                f"{v.get('label')}: {_cell(v['value'])}{' ' + v['unit'] if v.get('unit') else ''}"
                for v in every
                if v.get("detail") and v.get("value") is not None
            )
            if not heads:
                heads = tuple(
                    str(v.get("label")) + (f" ({v['unit']})" if v.get("unit") else "") for v in vals
                )
            color = str(c.get("color") or "grey")
            shown = tuple("" if v.get("value") is None else _cell(v["value"]) for v in vals)
            why = "; ".join(str(r) for r in c.get("reasons") or [])
            rows.append(CaseRow(name, color, ICONS.get(color, ICONS["grey"]), shown, why, more))
    return heads, rows


def value_text(values: list[dict[str, Any]]) -> str:
    """A box's values in one line: "Lost 0 · Jitter max 2.4 ms · …" (module order)."""
    parts = []
    for v in values:
        x = v.get("value")
        if x is None:
            continue
        unit = f" {v['unit']}" if v.get("unit") else ""
        parts.append(f"{v.get('label')} {x}{unit}")
    return " · ".join(parts)


@dataclass(frozen=True)
class ResultView:
    title: str
    color: str
    body: str
    report: str
    folder: str


# "pc" / "tablet" -> "IPTV test" (02 10 test_type)
REASONS = {
    "planned_time_reached": "planned time reached",
    "all_cases_done": "all cases done",
    "took_too_long": "stopped: it took 4 times longer than planned",
    "stopped_by_user": "stopped by user",
    "controller_shutdown": "the Controller service stopped",
}


def reason_text(reason: Any) -> str:
    """Why a test ended, in words (02 10 ``verdict_reason``)."""
    r = str(reason or "")
    if r.startswith("device_lost: "):
        return TEXT["reason_device_lost"].format(detail=r.removeprefix("device_lost: "))
    return REASONS.get(r, r.replace("_", " "))


def verdict_color(verdict: str) -> str:
    return {"PASS": "green", "FAIL": "red"}.get(verdict, "purple")


def result_view(result: dict[str, Any], reports_dir: str) -> ResultView:
    """The result popup (03 F15): verdict and why, how long it ran, red boxes, where."""
    verdict = str(result.get("verdict", "INCOMPLETE"))
    why = reason_text(result.get("verdict_reason"))
    color = verdict_color(verdict)
    reds = sum(int((d.get("colors") or {}).get("red") or 0) for d in result.get("devices") or [])
    whole = result.get("verdict_reason") in ("planned_time_reached", "all_cases_done")
    no_red = TEXT["no_red_end"] if whole else TEXT["no_red"]
    red_text = no_red if reds == 0 else TEXT["red_boxes"].format(n=reds)
    body = TEXT["ran_of"].format(
        ran=hms(float(result.get("actual_s") or 0)),
        planned=hms(float(result.get("planned_s") or 0)),
        red=red_text,
    )
    folder = str(Path(reports_dir) / str(result.get("run_id", "")))  # same PC as the service
    title = TEXT["finished_title"].format(verdict=verdict, why=why) if why else verdict
    return ResultView(title, color, body, str(Path(folder) / "report.html"), folder)


DURATION_MIN_S, DURATION_MAX_S = 10, 200 * 3600  # S-05 (16): 10 s .. 200 h


def parse_duration(text: str) -> int | None:
    """A typed test length (2026-10-09 user: "you set any time, but I cannot type one in
    the app"): "40m", "1h 30m", "1h30m", "100h", "90s", "1.5h", "2:30" (h:mm) or plain
    minutes ("40"). None when it cannot be read or is outside 10 s .. 200 h."""
    t = text.strip().lower().replace(" ", "")
    if not t:
        return None
    total = 0.0
    if re.fullmatch(r"\d+:\d{1,2}", t):
        h, m = t.split(":")
        total = int(h) * 3600 + int(m) * 60
    elif re.fullmatch(r"\d+(\.\d+)?", t):
        total = float(t) * 60  # a bare number is minutes
    else:
        parts = re.findall(r"(\d+(?:\.\d+)?)(h|m|s)", t)
        if not parts or "".join(n + u for n, u in parts) != t:
            return None
        total = sum(float(n) * {"h": 3600, "m": 60, "s": 1}[u] for n, u in parts)
    secs = round(total)
    return secs if DURATION_MIN_S <= secs <= DURATION_MAX_S else None


def hms(seconds: float) -> str:
    s = int(seconds)
    h, m = divmod(s // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s % 60:02d}s"


def progress(status: Status) -> str:
    run = status.run
    planned = run.get("planned_s")
    if run.get("state") not in ("RUNNING", "STOPPING") or not planned:
        return ""
    elapsed = float(run.get("elapsed_s") or 0.0)
    pct = min(100, int(elapsed * 100 // planned))
    return TEXT["progress"].format(elapsed=hms(elapsed), planned=hms(planned), pct=pct)


def status_line(
    status: Status, plan_ok: bool, failed: str = "", not_ready: str = ""
) -> tuple[str, str]:
    """(color, text) for the Status box: one line during a test (11-ui 3)."""
    if not status.connected:
        return "grey", TEXT["reconnecting"]
    state = str(status.run.get("state", "IDLE"))
    if failed and state == "IDLE":
        return "red", TEXT["status_failed"].format(what=failed)
    if state == "PREPARING":
        videos = status.run.get("preparing")
        if isinstance(videos, dict) and videos.get("text"):
            return "grey", TEXT["status_preparing_videos"].format(text=videos["text"])
        return "grey", TEXT["status_preparing"]
    if state in ("STOPPING", "REPORTING", "SAFE_STOP", "RESETTING"):
        return "grey", TEXT["status_stopping"]
    if state == "FAULT":
        return "red", TEXT["status_fault"]
    if state == "RUNNING":
        link = _link_status(status)
        if link:
            return link
        bad = [r for r in device_rows(status) if r.color in ("red", "purple", "yellow") or r.dim]
        order = ("grey", "yellow", "purple", "red")
        if bad:
            worst = max(bad, key=lambda r: order.index(r.color) if r.color in order else 0)
            return worst.color, TEXT["status_running_problem"].format(
                device=worst.name, why=worst.why
            )
        if status.run.get("cell_s") == 0:  # cases, not 5-minute boxes (throughput)
            return "green", TEXT["status_running_cases"]
        cell_s = int(status.run.get("cell_s") or 300)
        elapsed = int(float(status.run.get("elapsed_s") or 0.0))
        left = cell_s - elapsed % cell_s
        planned = int(status.run.get("planned_s") or 0)
        if 0 < planned - elapsed <= left:  # the test ends first: its last result then (GAP-115)
            end = planned - elapsed
            return "green", TEXT["status_running_end"].format(end=f"{end // 60}:{end % 60:02d}")
        return "green", TEXT["status_running"].format(next=f"{left // 60}:{left % 60:02d}")
    result = status.run.get("result")
    if isinstance(result, dict) and result.get("verdict"):
        return finished_line(result)
    started = status.run.get("checklist")
    if isinstance(started, dict) and started.get("kind") == "start" and started.get("ok") is False:
        stop = next((s for s in started.get("steps") or [] if s.get("state") == "failed"), None)
        if stop is not None and _cancelled(stop):  # the person's own Cancel: not a fault
            return "grey", TEXT["status_cancelled"]
        if stop is not None:  # the Start check stopped at a step (F17): never "Ready" then
            why = f"{stop.get('name')}: {stop.get('what') or 'failed'}"
            return "red", TEXT["status_not_started"].format(why=why)
    if plan_ok and not_ready:  # the light check failed (F17): the list below says more
        return "red", TEXT["status_not_ready"].format(why=not_ready)
    if not plan_ok:  # the choice is not clear yet (02 2-11 roles.why)
        return "grey", not_ready or TEXT["status_need_plan"]
    return "green", TEXT["status_ready"]


def finished_line(result: dict[str, Any]) -> tuple[str, str]:
    """(color, text) when a run has ended (11-ui 3, GAP-106): the verdict first, then
    loss and jitter, then the red reasons (FAIL) or the notes that keep a PASS."""
    verdict = str(result.get("verdict"))
    devices = [d for d in result.get("devices") or [] if isinstance(d, dict)]
    lost = sum(int(d.get("loss_total") or 0) for d in devices)
    jitters = [float(d["jitter_max_ms"]) for d in devices if d.get("jitter_max_ms") is not None]

    def reasons(color: str) -> list[str]:
        out: list[str] = []
        for d in devices:
            for c in d.get("cells") or []:
                if c.get("color") == color:
                    out += [str(r) for r in c.get("reasons") or [] if str(r) not in out]
        return out

    loss = TEXT["finished_no_loss"] if lost == 0 else TEXT["finished_lost"].format(n=lost)
    notes = reasons("yellow")
    tail = TEXT["finished_notes"].format(n=len(notes), why="; ".join(notes)) if notes else ""
    # a test that counts no packets (throughput: cases, no loss, no jitter) says its cases,
    # not "no packet lost" (2026-10-17, as the user saw "FAIL — no packet lost: WAN→LAN ...")
    if devices and all("loss_total" not in d for d in devices):
        cells = [c for d in devices for c in d.get("cells") or [] if isinstance(c, dict)]
        counts = TEXT["finished_cases"].format(
            n=len(cells), ok=sum(c.get("color") == "green" for c in cells),
            check=sum(c.get("color") == "yellow" for c in cells),
            bad=sum(c.get("color") == "red" for c in cells),
        )  # fmt: skip
        if verdict == "PASS":
            return "green", TEXT["finished_pass_cases"].format(cases=counts) + tail
        if verdict == "FAIL":
            why = "; ".join(reasons("red")) or reason_text(result.get("verdict_reason"))
            return "red", TEXT["finished_fail_cases"].format(cases=counts, why=why)
    if verdict == "PASS":
        jitter = f"{max(jitters):.1f}" if jitters else "n/a"
        return "green", TEXT["finished_pass"].format(loss=loss, jitter=jitter) + tail
    why = reason_text(result.get("verdict_reason"))
    if verdict == "FAIL":
        return "red", TEXT["finished_fail"].format(loss=loss, why="; ".join(reasons("red")) or why)
    return verdict_color(verdict), TEXT["finished_other"].format(verdict=verdict, why=why)


@dataclass(frozen=True)
class FaultDevice:
    name: str
    state: str  # a short line: Stopped / Not answering / Clean / Not clean: … / No answer — …
    color: str  # green / red / grey


@dataclass(frozen=True)
class FaultView:
    """S6 (11-ui 2, 03 F11 fault screen): what stopped, each device, the data."""

    title: str
    what: str
    data: str
    devices: tuple[FaultDevice, ...]


def _utc_short(ts: Any) -> str:
    return str(ts)[11:19] if isinstance(ts, str) and len(ts) >= 19 else "unknown"


def fault_view(status: Status) -> FaultView | None:
    """Shown only in FAULT. After a Reset All that was not complete, the device lines
    come from its answers (who is clean, who needs a hand)."""
    run = status.run
    if not status.connected or run.get("state") != "FAULT":
        return None
    raw = run.get("fault")
    fault: dict[str, Any] = raw if isinstance(raw, dict) else {}
    last_ok = _utc_short(fault.get("last_ok_utc"))
    reason = str(fault.get("reason") or "unknown")
    if fault.get("device"):
        why = reason.split(":", 1)[1].strip() if ":" in reason else reason
        what = TEXT["fault_what"].format(device=fault["device"], reason=why, last_ok=last_ok)
    else:
        what = TEXT["fault_what_other"].format(reason=reason)
    result = run.get("result") if isinstance(run.get("result"), dict) else None
    data = (
        TEXT["fault_data"].format(
            run_id=result.get("run_id"), verdict=result.get("verdict"), last_ok=last_ok
        )
        if result
        else TEXT["fault_no_report"]
    )
    reset = run.get("reset") if isinstance(run.get("reset"), dict) else None
    rows: list[FaultDevice] = []
    if reset and reset.get("devices"):
        for d in reset["devices"]:
            if d.get("clean"):
                rows.append(FaultDevice(str(d["device"]), TEXT["fault_state_clean"], "green"))
            elif d.get("answered"):
                state = TEXT["fault_state_not_clean"].format(why=d.get("why") or "")
                rows.append(FaultDevice(str(d["device"]), state, "red"))
            else:
                state = TEXT["fault_state_no_answer"].format(fix=d.get("fix") or "")
                rows.append(FaultDevice(str(d["device"]), state, "red"))
    else:
        for name, d in sorted((run.get("devices") or {}).items()):
            silent = float(d.get("age_s") or 0) >= STALE_S or name == fault.get("device")
            rows.append(FaultDevice(
                name, TEXT["fault_state_silent" if silent else "fault_state_stopped"],
                "red" if silent else "grey",
            ))  # fmt: skip
    return FaultView(TEXT["fault_title"], what, data, tuple(rows))


CHECK_MARKS = {"ok": "✔", "warn": "⚠", "failed": "✖", "running": "⏳", "waiting": "⏸",
               "skipped": "⏸"}  # fmt: skip


def _cancelled(step: dict[str, Any]) -> bool:
    """A start step that ended because the person pressed Cancel (09 USR-006, GAP-125)."""
    return str(step.get("code") or "").endswith("USR-006")


def checklist_lines(status: Status, idle_check: dict[str, Any] | None) -> list[str]:
    """Status box under the status line (11-ui 3, 03 F17): the Start checklist while
    starting; between tests the newest failed check (Start or the light check), each
    step as mark + name + one line, Why / Fix under the failed or warned step."""
    if not status.connected:
        return []
    run = status.run
    state = str(run.get("state", "IDLE"))
    start = run.get("checklist") if isinstance(run.get("checklist"), dict) else None
    if state == "PREPARING":
        shown = start
    elif state == "IDLE":
        failed = [c for c in (start, idle_check) if isinstance(c, dict) and not c.get("ok")]
        shown = max(failed, key=lambda c: str(c.get("updated_utc", ""))) if failed else None
    else:
        shown = None
    if not shown:
        return []
    lines = []
    for s in shown.get("steps") or []:
        if shown.get("kind") == "idle" and int(s.get("n", 0)) > IDLE_STEPS:
            continue  # the light check never runs 5-7: do not list them
        text = s.get("what") or s.get("detail") or ""
        where = f"{s['device']}: " if s.get("device") and s.get("what") else ""
        if _cancelled(s):  # a Cancel is shown as paused, without Why/Fix (GAP-125)
            lines.append(f"{CHECK_MARKS['waiting']} {s['name']} — {TEXT['step_cancelled']}")
            continue
        lines.append(f"{CHECK_MARKS.get(s.get('state'), '·')} {s['name']}"
                     + (f" — {where}{text}" if text else ""))  # fmt: skip
        if s.get("state") in ("failed", "warn"):
            lines += [f"    {TEXT[k]}: {s[k]}" for k in ("why", "fix") if s.get(k)]
    return lines


@dataclass(frozen=True)
class ProblemRow:
    time: str
    device: str
    code: str
    what: str
    why: str
    fix: str
    icon: str
    color: str  # red / yellow / grey (resolved)
    count: int
    active: bool
    stale: bool


@dataclass(frozen=True)
class ProblemsView:
    badge: str  # "" when nothing is open
    badge_color: str  # red / yellow / grey
    flood: str  # summary line during a flood, else ""
    rows: tuple[ProblemRow, ...]


def problems_view(status: Status) -> ProblemsView:
    """03 F21: badge with red / yellow counts; during a flood only errors are listed
    (the summary line says how many); resolved rows grey under the open ones."""
    p = status.problems or {}
    counts = p.get("counts") or {}
    err, warn = int(counts.get("error") or 0), int(counts.get("warn") or 0)
    n = err + warn
    badge = TEXT["problems_badge"].format(n=n, s="" if n == 1 else "s") if n else ""
    color = "red" if err else "yellow" if warn else "grey"
    flood_n = (p.get("flood") or {}).get("new_in_10_min")
    rows = []
    for r in p.get("problems") or []:
        active = bool(r.get("active"))
        level = str(r.get("level", "warn"))
        if flood_n and active and level != "error":
            continue  # flood: only errors stay expanded (F21, ISA-18.2)
        c = ("red" if level == "error" else "yellow") if active else "grey"
        t = str(r.get("last_utc" if active else "cleared_utc") or "")
        rows.append(
            ProblemRow(
                _clock(t, "%H:%M:%S"),
                str(r.get("device", "")),
                str(r.get("code", "")),
                str(r.get("what", "")),
                str(r.get("why", "")),
                str(r.get("fix", "")),
                ICONS["red"] if c == "red" else ICONS["yellow"] if c == "yellow" else ICONS["grey"],
                c,
                int(r.get("count") or 1),
                active,
                bool(r.get("stale")),
            )
        )
    flood = TEXT["problems_flood"].format(n=flood_n) if flood_n else ""
    return ProblemsView(badge, color, flood, tuple(rows))


@dataclass(frozen=True)
class ActionRow:
    key: str
    device: str
    what: str
    how: str
    why: str
    state: str  # text shown
    waiting: bool
    manual: bool  # [Done] works on it: every waiting row (ZeroLoss may close it first)
    choices: tuple[tuple[str, str], ...] = ()  # (id, label): one button each (20 W-1)


@dataclass(frozen=True)
class ActionsView:
    badge: str
    rows: tuple[ActionRow, ...]
    waiting_keys: frozenset[str]


def actions_view(status: Status) -> ActionsView:
    """03 F22: waiting first (⏳), done below (✔ seen by ZeroLoss / ✔ by a person)."""
    a = status.actions or {}
    n = int(a.get("waiting") or 0)
    badge = TEXT["actions_badge"].format(n=n, s="" if n == 1 else "s") if n else ""
    rows = []
    for x in a.get("actions") or []:
        waiting = x.get("state") == "waiting"
        state = (
            TEXT["actions_waiting"]
            if waiting
            else TEXT["actions_done_auto"]
            if x.get("done_by") == "auto"
            else TEXT["actions_done_human"]
        )
        rows.append(
            ActionRow(
                str(x.get("key", "")),
                str(x.get("device", "")),
                str(x.get("what", "")),
                str(x.get("how", "")),
                str(x.get("why", "")),
                state,
                waiting,
                waiting,  # a person may always say it is done (GAP-116: a row nobody could close)
                tuple(
                    (str(c.get("id", "")), str(c.get("label") or c.get("id", "")))
                    for c in (x.get("choices") or [])
                    if waiting and isinstance(c, dict) and c.get("id")
                ),
            )
        )
    keys = frozenset(r.key for r in rows if r.waiting)
    return ActionsView(badge, tuple(rows), keys)


# ---------------------------------------------------------------- F14 setup (02 2-11)


@dataclass(frozen=True)
class SetupRowView:
    """One device line while no test runs: role, TV channel and receive network."""

    name: str
    side: str  # router WAN side / router LAN side / not answering
    role: str  # sender / receiver / unused
    ch: int
    network: str  # Wired / Wireless (S-20)
    reachable: bool
    cards: tuple[tuple[str, str], ...] = ()  # (label, address) of its cards (DSC-006)
    card: str = ""  # the card picked, "" = Auto (03 F17 decides)
    cards_known: bool = True  # False: this PC's cards were not seen yet (GAP-121)
    host: str = ""  # the address the Controller uses for it (GAP-155)
    addresses: tuple[str, ...] = ()  # what the IP box offers: in use, found, its cards
    why: str = ""  # why it does not answer (no answer / another PC answers there)
    buffer_ms: int = 0  # this receiver's player buffer, 0 = the test's (S-25)
    labels: tuple[tuple[str, str], ...] = ()  # address -> "WAN · enp37s0" / "LAN …" / "Wi-Fi …"
    video: bool = True  # an IPTV test: TV channel, network, buffer and card boxes
    this_pc: bool = False  # the Controller PC: shown, its role is not picked
    cards_lines: tuple[CardLine, ...] = ()  # a test without video: one line per card
    tablet: bool = False  # a tablet on a PC's USB (Tablet test): channel and buffer only
    title: str = ""  # what the row says instead of ``name`` (a tablet: its model and PC)
    # a tablet's Wi-Fi (S-51): (label, ssid, security) of what it sees; "" ssid = keep its own
    wifi_options: tuple[tuple[str, str, str], ...] = ()
    wifi_pick: str = ""  # the SSID picked for it, "" = the Wi-Fi it is on
    wifi_pw: str = ""  # its password box: "open" / "saved" / "empty" (as the PC bands)


def card_links(status: Status) -> dict[str, list[dict[str, Any]]]:
    """Each device's network cards from its heartbeat (02 5 ``links``)."""
    devices = status.run.get("devices") or {}
    return {n: [x for x in d.get("links") or [] if isinstance(x, dict)]
            for n, d in devices.items() if isinstance(d, dict)}  # fmt: skip


def card_options(links: list[dict[str, Any]]) -> tuple[tuple[str, str], ...]:
    """(label, address) for the card list: cards with an IPv4 address, loopback left out."""
    out = []
    for x in links:
        ip = str(x.get("ip") or "")
        if not ip or ip.startswith("127."):
            continue
        wifi = " (Wi-Fi)" if is_wireless(x) else ""
        out.append((f"{x.get('nic')} · {ip}{wifi}", ip))
    return tuple(out)


WIFI_BANDS = ("2.4GHz", "5GHz", "6GHz")
TABLET_CARD = "tablet:"  # S-65: a tablet line under its PC, "tablet:<serial>" (throughput T-4)


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


@dataclass(frozen=True)
class CardLine:
    """One card of a PC in Devices (2026-10-17 user: "연결된 포트가 있으면 그만큼 row가
    늘어나야지 그래야 사용자가 고르지"): tick it to measure it."""

    nic: str
    label: str
    tickable: bool
    ticked: bool
    wifi: bool
    # (band, ((label, bssid, usable, tip), ...), picked bssid, password box: "" (nothing
    # picked) / "open" / "empty" / "saved", the picked SSID)
    bands: tuple[tuple[str, tuple[tuple[str, str, bool, str], ...], str, str, str], ...] = ()
    scanning: bool = False
    note: str = ""  # why the last scan failed (shown under the card)


def scan_note(answer: Any) -> str:
    """Why a card's last Wi-Fi scan failed, in words; "" when it did not."""
    err = _as_dict(_as_dict(answer).get("error"))
    if not err:
        return ""
    if "no such path" in str(err.get("detail") or ""):
        return TEXT["wifi_scan_old"]  # a PC on an older build has no scan yet
    why = ": ".join(str(err[k]) for k in ("what", "why") if err.get(k))
    return TEXT["wifi_scan_failed"].format(why=why or err.get("code") or "?")


def card_lines(name: str, s: dict[str, Any], is_wan: bool, lan: set[str], ticked: Any,
               scans: dict[str, Any], busy: set[str],
               saved: tuple[str, ...] = ()) -> tuple[CardLine, ...]:  # fmt: skip
    """A PC's cards as lines: the WAN card shown (not ticked), a wired card on the router
    LAN side tickable, a Wi-Fi card tickable (its bands from its last scan)."""
    mine = _as_dict(_as_dict(ticked).get(name))
    out = []
    for x in s.get("links") or []:
        if not isinstance(x, dict) or not x.get("nic"):
            continue
        nic, ip = str(x["nic"]), str(x.get("ip") or "")
        if ip.startswith("127."):
            continue
        wifi = "wifi" in x
        on_lan = bool(ip) and ".".join(ip.split(".")[:3]) in lan
        if is_wan and ip and ip == str(s.get("host") or ""):
            kind = "WAN"
        elif is_wan and not wifi:
            kind = "other network" if ip else "not connected"
        elif wifi and is_wan:
            kind = "Wi-Fi (other network)" if ip else "Wi-Fi, not connected"
        elif wifi:
            w = _as_dict(x.get("wifi"))
            ch = f"ch {w['channel']}" if w.get("channel") else ""
            kind = "Wi-Fi " + " ".join(str(p) for p in (w.get("band"), ch) if p)
        else:
            kind = "LAN" if on_lan else ("other network" if ip else "not connected")
        speed = f" · {_speed(x['link_mbps'])}" if not wifi and x.get("link_mbps") else ""
        label = f"{nic} · {ip or 'no address'} · {kind.strip()}{speed}"
        tickable = not is_wan if wifi else (not is_wan and on_lan)
        tick = nic in mine and tickable
        bands: list[tuple[str, tuple[tuple[str, str, bool, str], ...], str, str, str]] = []
        if wifi and tick:
            nets = _as_dict(scans.get(f"{name}|{nic}")).get("networks") or []
            chosen = _as_dict(_as_dict(mine.get(nic)).get("bands"))
            for band in WIFI_BANDS:
                opts = []
                for n in nets:
                    if not isinstance(n, dict) or n.get("band") != band:
                        continue
                    sec = security_of(n.get("security"))
                    text = TEXT["wifi_band_item"].format(ssid=n.get("ssid"), bssid=n.get("bssid"),
                                                         signal=n.get("signal"),
                                                         security=sec.lower())  # fmt: skip
                    opts.append((text, str(n.get("bssid")), True, ""))
                pick = _as_dict(chosen.get(band))
                ssid = str(pick.get("ssid") or "")
                bssid = str(pick.get("bssid") or "")
                # a network picked before shows before any scan (2026-10-22: a new app
                # showed "not tested" though the picks were saved); the scan, when there is
                # one, says its security now
                seen = next((n for n in nets if isinstance(n, dict) and n.get("bssid") == bssid
                             and n.get("band") == band), None)  # fmt: skip
                if ssid and seen is None:
                    opts.insert(0, (TEXT["wifi_band_kept"].format(ssid=ssid, bssid=bssid),
                                    bssid, True, ""))  # fmt: skip
                security = security_of(seen.get("security")) if seen else pick.get("security")
                if not ssid:
                    box = ""
                elif str(security or "Open") == "Open":
                    box = "open"
                else:
                    box = "saved" if ssid in saved else "empty"
                bands.append((band, tuple(opts), str(pick.get("bssid") or ""), box, ssid))
        note = scan_note(scans.get(f"{name}|{nic}")) if wifi and tick else ""
        if wifi and tick and not note and not any(ssid for *_, ssid in bands):
            note = TEXT["wifi_no_band"]  # 2026-10-22: the bands were skipped without a word
        out.append(CardLine(nic, label, tickable, tick, wifi, tuple(bands),
                            f"{name}|{nic}" in busy, note))  # fmt: skip
    if not is_wan:
        out.extend(tablet_lines(s, mine, {b.split("|", 1)[1] for b in busy
                                          if b.startswith(f"{name}|")}))  # fmt: skip
    return tuple(out)


def tablet_lines(s: dict[str, Any], mine: dict[str, Any],
                 scanning: set[str] | None = None) -> list[CardLine]:  # fmt: skip
    """2026-10-29 (throughput T-4): each tablet on this PC's USB is a line to tick, its
    Wi-Fi as it is, or a band network per band from the tablet's own scan (by SSID: the
    tablet's scan names no access point)."""
    out = []
    for t in s.get("tablets") or []:
        if not isinstance(t, dict) or t.get("state") != "device" or not t.get("serial"):
            continue
        nic = TABLET_CARD + str(t["serial"])
        on = str(t.get("wifi_ssid") or "")
        label = (f"Tablet {t.get('model') or t['serial']} · {t.get('wifi_ip') or 'no address'} · "
                 f"Wi-Fi {on or 'not connected'}")  # fmt: skip
        tick = nic in mine
        bands: list[tuple[str, tuple[tuple[str, str, bool, str], ...], str, str, str]] = []
        if tick:
            chosen = _as_dict(_as_dict(mine.get(nic)).get("bands"))
            # 2026-10-30 user ("여전히 태블릿의 scan버튼은 안눌려"): the signal changed every few
            # seconds, the line was drawn again and the Scan under the mouse went with it.
            # Only what stays: SSID and security, by name
            nets = sorted((n for n in t.get("networks") or [] if isinstance(n, dict)),
                          key=lambda n: str(n.get("ssid")))  # fmt: skip
            for band in WIFI_BANDS:
                opts = [(TEXT["wifi_band_item_tablet"].format(
                    ssid=n.get("ssid"), security=security_of(n.get("security")).lower()),
                    str(n.get("ssid")), True, "")
                    for n in nets if n.get("band") == band]  # fmt: skip
                pick = _as_dict(chosen.get(band))
                ssid = str(pick.get("ssid") or "")
                if ssid and not any(o[1] == ssid for o in opts):
                    opts.insert(0, (TEXT["wifi_band_kept"].format(ssid=ssid, bssid="-"), ssid,
                                    True, ""))  # fmt: skip
                open_ = str(pick.get("security") or "Open") == "Open"
                box = "" if not ssid else "open" if open_ else "empty"
                bands.append((band, tuple(opts), ssid, box, ssid))
        out.append(CardLine(nic, label, True, tick, True, tuple(bands),
                            nic in (scanning or set())))  # fmt: skip
    return out


def security_of(seen: Any) -> str:
    """A scan's security as a join needs it: "Open", "WPA3" (WPA3 only) or "WPA2"
    (WPA2, WPA2+WPA3, or Windows' "Protected": WPA2 is what both sides can do)."""
    text = str(seen or "Open")
    if text in ("Open", "", "--"):
        return "Open"
    return "WPA3" if "WPA3" in text and "WPA2" not in text else "WPA2"


def ticks_change(
    value: Any, device: str, nic: str, tick: bool | None = None, band: str | None = None,
    net: dict[str, Any] | None = None, drop: tuple[str, ...] = (),
) -> dict[str, Any]:  # fmt: skip
    """S-65 after one tick or one band pick (only that card of that PC changes). ``drop``:
    cards a tick takes the place of -- one Wi-Fi card per PC (2026-10-17 user: "두개 있는
    경우 하나만 선택할건데")."""
    out = {k: {n: dict(v) for n, v in _as_dict(c).items()} for k, c in _as_dict(value).items()}
    cards = out.setdefault(device, {})
    if tick is not None:
        if tick:
            for other in drop:
                cards.pop(other, None)
            cards.setdefault(nic, {})
        else:
            cards.pop(nic, None)
    elif band:
        bands = dict(_as_dict(cards.setdefault(nic, {}).get("bands")))
        if net:
            bands[band] = {"ssid": net.get("ssid"), "bssid": net.get("bssid") or "",
                           "security": security_of(net.get("security"))}  # fmt: skip
        else:
            bands.pop(band, None)
        cards[nic]["bands"] = bands
    if not cards:
        out.pop(device, None)
    return out


def setup_rows(
    setup: dict[str, Any] | None, links: dict[str, list[dict[str, Any]]] | None = None,
    ticked: Any = None, scans: dict[str, Any] | None = None, busy: set[str] | None = None,
) -> list[SetupRowView]:  # fmt: skip
    if not setup:
        return []
    roles = setup.get("roles") or {}
    rx = {r["device"]: r for r in roles.get("receivers") or []}
    settings = (setup.get("setup") or {}).get("settings") or {}
    picked = (setup.get("setup") or {}).get("nics") or {}
    direct = settings.get("S-54") == BASELINE  # no router: the sides are the cable (GAP-115)
    # 2026-10-17 user: a throughput test showed Receiver, CH1, Wired, Buffer on its rows
    video = str((setup.get("setup") or {}).get("module") or "iptv") == "iptv"
    rows = []
    sides_all = setup.get("sides") or {}
    lan = {".".join(str(x.get("host") or "").split(".")[:3]) for x in sides_all.values()
           if isinstance(x, dict) and x.get("side") == "lan" and x.get("host")}  # fmt: skip
    tablet_test = (setup.get("setup") or {}).get("test_type") == TABLET_TEST
    for name, s in sorted((setup.get("sides") or {}).items()):
        role = "sender" if roles.get("sender") == name else "receiver" if name in rx else "unused"
        r = rx.get(name) or {}
        side = s.get("side", "unknown")
        cards = (links or {}).get(name) or s.get("links")  # a test's, else the setup's
        side = "direct" if direct and side in ("wan", "lan") else side
        said = TEXT[f"side_{side}"]
        relays = sum(1 for x in (roles.get("receivers") or []) if x.get("host") == name)  # on USB
        if tablet_test and relays:  # Not used here still relays them (tablets only)
            said = f"{said} · {TEXT['relays_tablets'].format(n=relays)}"
        if s.get("usb_unseen"):  # on its USB, missing from adb (receiverPC 2026-10-31)
            said = f"{said} · {TEXT['usb_unseen'].format(n=s['usb_unseen'])}"
        rows.append(SetupRowView(
            name, said, role, int(r.get("ch") or 1),
            str(r.get("network") or "Wired"), bool(s.get("reachable")),
            card_options(cards or []), str(picked.get(name) or ""), cards is not None,
            str(s.get("host") or ""), tuple(str(a) for a in s.get("addresses") or ()),
            str(s.get("why") or ""), int(r.get("buffer_ms") or 0),
            tuple((str(k), str(v)) for k, v in (s.get("labels") or {}).items()),
            video, bool(s.get("this_pc")),
            () if video else card_lines(name, {**s, "links": cards or []},
                                        role == "sender" or s.get("side") == "wan", lan,
                                        ticked, scans or {}, busy or set(),
                                        tuple(setup.get("wifi_passwords") or ())),
        ))  # fmt: skip
    if tablet_test:
        rows += tablet_rows(setup)
    return rows


TABLET_TEST = IPTV_TEST = "IPTV test"  # tablet rows: the IPTV test (PC and tablets)
TABLET = "tab:"  # a tablet row's name: "tab:<serial>" (the device name comes when picked)
TABLETS_MAX = 3  # 01-overview 4


def tablet_rows(setup: dict[str, Any]) -> list[SetupRowView]:
    """11-ui 3 (Tablet test): a row per tablet on the PCs' USB, whatever PC it hangs on;
    the PC itself only relays and is not a receiver."""
    picked = {r.get("serial"): r for r in (setup.get("roles") or {}).get("receivers") or []
              if r.get("serial")}  # fmt: skip
    rows = []
    for host, s in sorted((setup.get("sides") or {}).items()):
        for t in s.get("tablets") or [] if isinstance(s, dict) else []:
            serial = str(t.get("serial"))
            r = picked.get(serial) or {}
            pick = (r.get("wifi") or {}) if r else {}
            options = tablet_wifi_options(t, pick)
            sec = next((x[2] for x in options if x[1] and x[1] == pick.get("ssid")), "")
            pw = (
                ""
                if not pick.get("ssid")
                else "open"
                if sec == "Open"
                else ("saved" if pick["ssid"] in (setup.get("wifi_passwords") or ()) else "empty")
            )
            rows.append(SetupRowView(
                TABLET + serial, tablet_side(t, host), "receiver" if r else "unused",
                int(r.get("ch") or 1), "Wireless", t.get("state") == "device", host=host,
                why=tablet_why(t), buffer_ms=int(r.get("buffer_ms") or 0), tablet=True,
                title=f"{r.get('device') or TEXT['tablet_new']} · {t.get('model') or serial}",
                wifi_options=options, wifi_pick=str(pick.get("ssid") or ""), wifi_pw=pw,
            ))  # fmt: skip
    return rows


def tablet_wifi_options(t: dict[str, Any], pick: dict[str, Any]
                        ) -> tuple[tuple[str, str, str], ...]:  # fmt: skip
    """The tablet row's Wi-Fi list: keep the one it is on, or a network it sees (its own
    scan, strongest first). One picked earlier but not seen now stays, said so."""
    now = t.get("wifi_ssid")
    keep = TEXT["tablet_wifi_keep"].format(ssid=now) if now else TEXT["tablet_wifi_keep_none"]
    out = [(keep, "", "")]
    seen = set()
    for n in t.get("networks") or []:
        if not isinstance(n, dict) or n.get("security") == "other":
            continue
        seen.add(n["ssid"])
        label = f"{n['ssid']} · {n.get('band')} · {n.get('security')} · {n.get('rssi_dbm')} dBm"
        out.append((label, str(n["ssid"]), str(n.get("security"))))
    if pick.get("ssid") and pick["ssid"] not in seen:
        out.append((TEXT["tablet_wifi_unseen"].format(ssid=pick["ssid"]), str(pick["ssid"]),
                    str(pick.get("security") or "WPA2")))  # fmt: skip
    return tuple(out)


def tablet_side(t: dict[str, Any], host: str) -> str:
    """What a tablet row says after its name: its PC, battery, Wi-Fi address, app."""
    bits = [TEXT["tablet_on"].format(host=host)]
    b = t.get("battery") or {}
    if b.get("level") is not None:
        bits.append(TEXT["tablet_battery"].format(
            n=b["level"], how=TEXT["tablet_charging"] if b.get("charging") else ""))  # fmt: skip
    bits.append(TEXT["tablet_wifi"].format(ip=t["wifi_ip"]) if t.get("wifi_ip")
                else TEXT["tablet_no_wifi"])  # fmt: skip
    app = t.get("app") or {}
    if app.get("installed"):
        bits.append(TEXT["tablet_app"].format(v=app.get("version")))
    return " · ".join(bits)


def tablet_why(t: dict[str, Any]) -> str:
    """What the person must do before this tablet can take part, "" when nothing."""
    if t.get("why"):
        return str(t["why"])
    if t.get("state") != "device":
        return str(t.get("state") or "")
    if not (t.get("app") or {}).get("installed"):
        return TEXT["tablet_no_app"]
    b = t.get("battery") or {}
    if b.get("level") is not None and b["level"] < 30 and not b.get("charging"):
        return TEXT["tablet_low_battery"].format(n=b["level"])
    return ""


def tablets_behind(setup: dict[str, Any] | None, build: str) -> bool:
    """A tablet on a PC's USB without this build's app (2026-10-31: the rows showed the old
    app and Update PCs was hidden, since every PC was on this build already)."""
    for side in ((setup or {}).get("sides") or {}).values():
        for t in (side or {}).get("tablets") or []:
            if not isinstance(t, dict) or t.get("state") != "device":
                continue
            app = t.get("app") or {}
            if not app.get("installed") or (build and app.get("version") != build):
                return True
    return False


def _serials(side: dict[str, Any]) -> set[str]:
    return {str(t.get("serial")) for t in side.get("tablets") or [] if isinstance(t, dict)}


def _kept_other(setup: dict[str, Any]) -> list[dict[str, Any]]:
    """Receivers the test type on screen does not use, saved as they were: a PC test
    keeps the tablets for when the IPTV test is picked again (the IPTV test uses both)."""
    if (setup.get("setup") or {}).get("test_type") == TABLET_TEST:
        return []
    saved = (setup.get("setup") or {}).get("receivers") or []
    return [dict(r) for r in saved if r.get("serial")]


def tablets_present(setup: dict[str, Any]) -> set[str]:
    """The tablets on the PCs' USB now (adb state "device")."""
    return {str(t.get("serial")) for s in (setup.get("sides") or {}).values()
            if isinstance(s, dict) for t in s.get("tablets") or []
            if isinstance(t, dict) and t.get("state") == "device"}  # fmt: skip


def tablet_role_change(setup: dict[str, Any], name: str, role: str) -> dict[str, Any]:
    """Pick or drop a tablet (Receiver / Not used). A picked tablet gets the first free
    name tablet-1..3, which stays its name in the records and reports."""
    serial = name[len(TABLET) :]
    every = [dict(r) for r in (setup.get("roles") or {}).get("receivers") or []]
    pcs = [r for r in every if not r.get("serial")]  # the PC receiver stays as it is
    mine = [r for r in every if r.get("serial") and r.get("serial") != serial]
    if role == "receiver" and len(mine) >= TABLETS_MAX:
        # 2026-10-31 user ("태블릿 첫번째꺼 선택을 못하니 왜", "reciever선택도 안되고"): a
        # tablet unplugged since still held a place; one not on USB now gives its place up
        present = tablets_present(setup)
        mine = [r for r in mine if r.get("serial") in present]
    if role == "receiver" and len(mine) < TABLETS_MAX:
        host = next((h for h, s in (setup.get("sides") or {}).items()
                     if isinstance(s, dict) and serial in _serials(s)), None)  # fmt: skip
        used = {r.get("device") for r in mine}
        free = next(f"tablet-{i}" for i in range(1, TABLETS_MAX + 1) if f"tablet-{i}" not in used)
        if host:
            mine.append({"device": free, "ch": 1, "host": host, "serial": serial})
    return {"receivers": pcs + mine + _kept_other(setup)}


def parse_address(text: str, port: int = 10992) -> tuple[str, int] | None:
    """GAP-153: "10.0.0.1" or "10.0.0.1:10992" typed by the person; None if not one."""
    m = re.fullmatch(r"\s*(\d{1,3}(?:\.\d{1,3}){3})(?::(\d{1,5}))?\s*", text or "")
    if m is None or any(int(x) > 255 for x in m.group(1).split(".")):
        return None
    p = int(m.group(2)) if m.group(2) else port
    return (m.group(1), p) if 0 < p < 65536 else None


def card_change(setup: dict[str, Any], name: str, ip: str) -> dict[str, Any]:
    """The ``changes`` (02 2-11) for one card pick; "" = back to Auto."""
    nics = dict((setup.get("setup") or {}).get("nics") or {})
    if ip:
        nics[name] = ip
    else:
        nics.pop(name, None)
    return {"nics": nics}


def role_change(setup: dict[str, Any], name: str, role: str) -> dict[str, Any]:
    """The ``changes`` (02 2-11) for one role pick. A PC receiver test has one receiver:
    picking a new one replaces the old; the sender cannot also be the receiver unless
    it is the same PC on purpose (a loopback check)."""
    roles = setup.get("roles") or {}
    sender = roles.get("sender")
    receivers = [dict(r) for r in roles.get("receivers") or []]
    if role == "sender":
        sender = name
    elif sender == name:
        sender = None
    tablets = [r for r in receivers if r.get("serial")]  # a tablet test's tablets stay
    receivers = [r for r in receivers if not r.get("serial")]
    if role == "receiver":  # one PC receiver: picking a new one replaces the old
        keep = next((r for r in receivers if r["device"] == name), None)
        receivers = [keep or {"device": name, "ch": 1, "network": "Wired"}]
    else:
        receivers = [r for r in receivers if r["device"] != name]
    return {"sender": sender, "receivers": receivers + tablets + _kept_other(setup)}


def receiver_change(setup: dict[str, Any], name: str, **fields: Any) -> dict[str, Any]:
    receivers = [dict(r) for r in (setup.get("roles") or {}).get("receivers") or []]
    tablet = name.startswith(TABLET)
    for r in receivers:
        if (r.get("serial") == name[len(TABLET) :]) if tablet else r["device"] == name:
            r.update(fields)
    return {"receivers": receivers + _kept_other(setup)}


def adv_items(
    schema: dict[str, dict[str, Any]], module: str = "iptv"
) -> list[tuple[str, dict[str, Any]]]:
    """Advanced settings in table order (16 1), the ones a screen control can edit: Core's
    and the chosen test's module's (2026-10-10: the throughput settings were not for an
    IPTV test)."""
    mine = {sid: s for sid, s in schema.items() if s.get("module") in (None, module)}
    order = {g: i for i, g in enumerate(dict.fromkeys(
        s.get("group") for s in mine.values() if s.get("where") == "adv"))}  # fmt: skip
    rows = [(sid, s) for sid, s in mine.items() if s.get("where") == "adv"]
    return sorted(rows, key=lambda r: (order.get(r[1].get("group"), 99), r[0]))  # by group


EDITABLE_KINDS = ("choice", "int", "float", "bool", "per_quality_int", "sources")
GENERATED = "Generated"  # S-48: the made-up video (iptv 1-1)
BASELINE = "Baseline (no router)"  # S-54 (05 4.6.3)


def source_channels(setup: dict[str, Any] | None) -> list[int]:
    """The channels the chosen receivers watch (one Source video list each, 11-ui 1.10)."""
    receivers = ((setup or {}).get("roles") or {}).get("receivers") or []
    return sorted({int(r.get("ch") or 1) for r in receivers if isinstance(r, dict)}) or [1]


def _made_for(made: Any, quality: str, ch: int, mbps: int | None, whole: bool) -> bool:
    return any(
        isinstance(m, dict) and m.get("quality") == quality and m.get("ch") == ch
        and (mbps is None or m.get("mbps") == mbps) and bool(m.get("whole")) == whole
        for m in made or []
    )  # fmt: skip


def _made_mark(made: Any, quality: str, ch: int | None, mbps: int | None,
               whole: bool) -> tuple[str, str]:  # fmt: skip
    """(label end, tooltip) for the channel on screen; nothing when the sender did not
    say (an older node) or no channel is given."""
    if ch is None or made is None:
        return "", ""
    if _made_for(made, quality, ch, mbps, whole):
        return TEXT["source_ready"], TEXT["source_ready_tip"]
    return TEXT["source_to_make"], TEXT["source_to_make_tip"]


def source_options(
    answer: dict[str, Any] | None, quality: str, chosen: tuple[str, ...] = (),
    ch: int | None = None, mbps: int | None = None, whole: bool = False,
) -> list[tuple[str, str, bool, str]]:  # fmt: skip
    """(label, value, usable, note) for one channel's Source video list: Generated first,
    then the sender PC's files with size and length (02 2-10, iptv 1-1, GAP-110). Not
    usable at this quality = grey; usable but changed (made larger, fps) = ⚠ (K-28);
    the note says why, as the tooltip. A chosen file not in the list (yet) stays shown
    by name (2026-10-09: right after the sender restarted the screen said "Generated"
    while the Greenland movie was chosen). With ``ch``: each says whether its video for
    that channel, quality and ``mbps`` is made already (starts at once) or is made first
    (2026-10-17 user: "바로 사용가능한 파일은 별도로 알려주던가 직접 선택하게")."""
    end, tip = _made_mark((answer or {}).get("generated_made"), quality, ch, mbps, False)
    out = [(end + TEXT["source_generated"], GENERATED, True, tip)]
    listed = {str(f.get("name")) for f in (answer or {}).get("files") or [] if isinstance(f, dict)}
    for name in dict.fromkeys(chosen):
        if name and name != GENERATED and name not in listed:
            key = "source_checking" if answer is None else "source_missing"
            out.append((TEXT[key].format(name=name), name, True, TEXT[f"{key}_tip"]))
    for f in (answer or {}).get("files") or []:
        if not isinstance(f, dict) or not f.get("name"):
            continue
        fit = (f.get("fits") or {}).get(quality) or {}
        length = (
            TEXT["source_minutes"].format(n=int(f["duration_s"] // 60))
            if f.get("duration_s")
            else ""
        )
        label = (
            TEXT["source_file"].format(
                name=f["name"], width=f["width"], height=f["height"], length=length
            )
            if f.get("width") and f.get("height")
            else f"{f['name']} · ?{length}"
        )
        usable = bool(fit.get("ok", True))
        note = str(fit.get("why") or ("" if usable else "cannot be used"))
        if usable and note:
            label = f"{label} ⚠"
        if usable:
            end, tip = _made_mark(f.get("made"), quality, ch, mbps, whole)
            label, note = end + label, "\n".join(x for x in (note, tip) if x)
        out.append((label, str(f["name"]), usable, note))
    # 2026-10-24 user: the movies made already come first (they start at once; the
    # picture does not matter for the test), then the ones that would be made first
    ready = TEXT["source_ready"]
    head, rest = out[:1], out[1:]
    return (
        head
        + [o for o in rest if ready and o[0].startswith(ready)]
        + [o for o in rest if not (ready and o[0].startswith(ready))]
    )


def ready_source(answer: dict[str, Any] | None, quality: str, ch: int, mbps: int | None,
                 whole: bool) -> str | None:  # fmt: skip
    """The sender's movie already made for this channel, quality and bitrate (the
    first by name), else None: a channel nobody picked a movie for gets it, so a test
    starts without making anything (2026-10-24 user: "추가 변환없이 바로 기존 10분 영상
    3개를 default로")."""
    for f in sorted((answer or {}).get("files") or [], key=lambda x: str(x.get("name"))):
        if isinstance(f, dict) and f.get("name") and _made_for(f.get("made"), quality, ch,
                                                                mbps, whole):  # fmt: skip
            return str(f["name"])
    return None


def source_value(current: Any, ch: int) -> str:
    return str(current.get(str(ch)) or GENERATED) if isinstance(current, dict) else GENERATED


def making_lines(
    answer: dict[str, Any] | None, quality: str, mbps: int, chosen: dict[int, str],
    whole: bool = False,
) -> list[tuple[str, str]]:  # fmt: skip
    """(color, text) per chosen channel from the sender's making state (02 2-4 GET):
    what the person waits for before Start (iptv 1-1-2, GAP-116). Only jobs of this
    quality, bitrate and file count; nothing known = no line."""
    jobs = (answer or {}).get("channels") or []
    out = []
    for ch, name in sorted(chosen.items()):
        want = "generated" if name == GENERATED else unicodedata.normalize("NFC", name)
        long = whole and name != GENERATED  # S-55: the whole movie is a file of its own
        job = next((j for j in jobs if j.get("ch") == ch and j.get("quality") == quality
                    and j.get("mbps") == mbps and j.get("source") == want
                    and bool(j.get("whole")) == long), None)  # fmt: skip
        if job is None:
            continue
        st = job.get("state")
        if st == "making":
            left = job.get("left_s")
            more = TEXT["making_left"].format(n=max(1, round(left / 60))) if left else ""
            out.append(("grey", TEXT["making_now"].format(
                ch=ch, pct=f"{float(job.get('percent') or 0):.0f}", left=more)))  # fmt: skip
        elif st in ("queued", "checking"):
            out.append(("grey", TEXT[f"making_{st}"].format(ch=ch)))
        elif st == "ready":
            out.append(("green", TEXT["making_ready"].format(ch=ch)))
        elif st == "failed":
            out.append(("red", TEXT["making_failed"].format(ch=ch, why=job.get("error") or "")))
    return out


def chosen_sources(value: Any, channels: list[int]) -> dict[int, str]:
    """S-48 as {channel: file name or Generated} for the channels in the test."""
    return {ch: source_value(value, ch) for ch in channels}


def source_change(current: Any, ch: int, value: str) -> Any:
    """S-48 after one channel's choice: only the channels with a file are written."""
    chosen = (
        {k: v for k, v in current.items() if v != GENERATED} if isinstance(current, dict) else {}
    )
    if value == GENERATED:
        chosen.pop(str(ch), None)
    else:
        chosen[str(ch)] = value
    return chosen or GENERATED


def adv_help(item: dict[str, Any]) -> str:
    """ⓘ help (F14 rule 7): what it is and the default."""
    default = item.get("value")
    if isinstance(default, dict):
        default = ", ".join(f"{k} {v}" for k, v in default.items())
    return f"{item.get('help')} Default: {default}{' ' + item['unit'] if item.get('unit') else ''}."


def adv_why(item: dict[str, Any]) -> str:
    """Why the setting cannot be changed on the screen ("" = it can)."""
    if item.get("available") is not True:
        return str(item["available"])
    if item.get("kind") not in EDITABLE_KINDS:
        return "Set with the plan file or 'zeroloss setup set' in this build"
    return ""


def adv_tip(item: dict[str, Any]) -> str:
    """ⓘ text on the label: the help, and why it is grey when it is."""
    why = adv_why(item)
    return adv_help(item) + (f" ({why})" if why else "")


def size_text(n: int | float | None) -> str:
    """Bytes as people read them (MB below 1 GB, else GB with one decimal)."""
    if n is None:
        return "—"
    mb = float(n) / (1024 * 1024)
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB" if mb >= 1 else "< 1 MB"


@dataclass(frozen=True)
class StorageRow:
    run: str
    size: str
    raw: str
    where: str
    in_use: bool
    bytes: int
    raw_bytes: int


@dataclass(frozen=True)
class StorageView:
    rows: tuple[StorageRow, ...]
    notes: tuple[str, ...]  # free space per PC, devices not read, the last deletion


def storage_view(listing: dict[str, Any] | None, note: str = "") -> StorageView:
    """S14 (02 2-13 ``storage``): runs biggest first, as the service sends them."""
    if listing is None:
        return StorageView((), (TEXT["storage_loading"],))
    rows = []
    for r in listing.get("runs") or []:
        where = ", ".join(
            f"{k} {size_text(v.get('bytes'))}" for k, v in (r.get("where") or {}).items()
        )
        run = str(r.get("run", ""))
        rows.append(StorageRow(
            run=run, size=size_text(r.get("bytes")), raw=size_text(r.get("raw_bytes")),
            where=where + (f" · {TEXT['storage_running']}" if r.get("in_use") else ""),
            in_use=bool(r.get("in_use")), bytes=int(r.get("bytes") or 0),
            raw_bytes=int(r.get("raw_bytes") or 0),
        ))  # fmt: skip
    free = ", ".join(f"{k} {size_text((v or 0) * 1024 * 1024)}"
                     for k, v in (listing.get("free_mb_of") or {}).items())  # fmt: skip
    notes = [TEXT["storage_free"].format(free=free)] if free else []
    notes += [TEXT["storage_not_from"].format(device=k, why=v)
              for k, v in (listing.get("nodes") or {}).items() if v != "ok"]  # fmt: skip
    if note:
        notes.append(note)
    if not rows:
        notes.append(TEXT["storage_empty"])
    return StorageView(tuple(rows), tuple(notes))


def deleted_note(answer: dict[str, Any]) -> str:
    """One line after a deletion (02 2-13 ``storage/delete``)."""
    text = TEXT["storage_deleted"].format(size=size_text(answer.get("deleted_bytes")),
                                          run=answer.get("run", ""))  # fmt: skip
    kept = [f"{k}: {v}" for k, v in (answer.get("nodes") or {}).items() if v != "ok"]
    kept += [str(e) for e in answer.get("errors") or []]
    return text + (" " + TEXT["storage_kept"].format(what="; ".join(kept)) if kept else "")


def export_text(ok: bool, data: dict[str, Any], error: dict[str, Any] | None) -> str:
    """The line under [Export diagnostics] after the answer (02 2-13 ``diagnostics``)."""
    if not ok:
        return TEXT["export_failed"].format(what=(error or {}).get("what", "unknown"))
    lines = [
        TEXT["export_done"].format(path=data.get("path", ""), size=size_text(data.get("bytes")))
    ]
    lines += [TEXT["export_not_from"].format(device=k, why=v)
              for k, v in (data.get("nodes") or {}).items() if v != "ok"]  # fmt: skip
    return "\n".join(lines)


@dataclass(frozen=True)
class ReportRow:
    run: str
    started: str
    kind: str  # test type · video quality
    time: str  # actual / planned
    verdict: str  # PASS / FAIL / INCOMPLETE, or the reason there is no report
    color: str  # green red purple grey (grey: no report)
    why: str
    report: str
    folder: str
    has_report: bool


@dataclass(frozen=True)
class ReportsView:
    rows: tuple[ReportRow, ...]
    notes: tuple[str, ...]


def reports_view(listing: dict[str, Any] | None, error: str = "") -> ReportsView:
    """S8 (02 2-13 ``reports``): newest first, as the service sends them."""
    if error:
        return ReportsView((), (TEXT["reports_failed"].format(what=error),))
    if listing is None:
        return ReportsView((), (TEXT["reports_loading"],))
    rows = []
    for r in listing.get("reports") or []:
        verdict = r.get("verdict")
        kind = " · ".join(str(x) for x in (TEST_LABELS.get(str(r.get("test_type")),
                                                             r.get("test_type")),
                                            r.get("quality")) if x)  # fmt: skip
        planned, actual = r.get("planned_s"), r.get("actual_s")
        time = (
            f"{hms(float(actual))} / {hms(float(planned))}"
            if planned and actual is not None
            else ""
        )
        rows.append(ReportRow(
            run=str(r.get("run", "")), started=str(r.get("started", "")), kind=kind, time=time,
            verdict=str(verdict) if verdict else str(r.get("why") or ""),
            color=verdict_color(str(verdict)) if verdict else "grey",
            why=reason_text(r.get("verdict_reason")) if verdict else "",
            report=str(r.get("report", "")), folder=str(r.get("folder", "")),
            has_report=bool(verdict),
        ))  # fmt: skip
    notes = [TEXT["reports_where"].format(path=listing.get("reports_dir", ""))]
    if not rows:
        notes.append(TEXT["reports_none"])
    return ReportsView(tuple(rows), tuple(notes))


def start_summary(plan: dict[str, Any] | None, test: str) -> str:
    """The Start question says what will run (2026-10-31: a throughput test ran without
    this PC's Wi-Fi because its tick was missed): the test, then each sender, receiver or
    measured card with its channel, video and Wi-Fi."""
    if not plan:
        return test or "the test"
    lines = [" · ".join(x for x in (test, str(plan.get("quality") or ""),
                                    hms(float(plan.get("duration_s") or 0))) if x)]  # fmt: skip
    dev = plan.get("devices") or {}
    settings = plan.get("settings") or {}
    if plan.get("module") == "throughput":
        lines = [test]  # its length is its cases', and it sends no video
        if dev.get("sender"):
            lines.append(TEXT["start_wan"].format(pc=dev["sender"]))
        for pc, cards in (settings.get("S-65") or {}).items():
            for nic, how in (cards or {}).items():
                bands = ", ".join(f"{b} {v.get('ssid')}" for b, v in
                                  ((how or {}).get("bands") or {}).items() if v)  # fmt: skip
                name = nic.replace("tablet:", "tablet ")
                where = TEXT["this_pc"] if pc == "controller" else pc
                lines.append(f"{where} · {name}" + (f" ({bands})" if bands else ""))
        return "\n".join(lines)
    if dev.get("sender"):
        lines.append(TEXT["start_sender"].format(pc=dev["sender"]))
    files = settings.get("S-48") or {}
    for r in sorted(dev.get("receivers") or [], key=lambda x: (int(x.get("ch") or 0),
                                                               str(x.get("device")))):  # fmt: skip
        ch = int(r.get("ch") or 0)
        bits = [f"CH{ch} → {r.get('device')}"]
        wifi = (r.get("wifi") or {}).get("ssid")
        if wifi:
            bits.append(TEXT["start_wifi"].format(ssid=wifi))
        movie = files.get(str(ch))
        if movie:
            bits.append(str(movie))
        lines.append(" · ".join(bits))
    return "\n".join(lines)
