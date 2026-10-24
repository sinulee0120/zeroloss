"""IPTV cause analysis (docs/modules/iptv.md 2, implementation rules 2.4, 20 GAP-85).

``analyze(data)`` (``run_entry.analysis``) turns the 5-minute summaries, the run's
events (``run_s``) and, once fetched, the receivers' 1-second records into cause
candidates. Rules never change a verdict or a color: they only suggest where to look
next. A rule whose data is missing is listed in ``missing`` with the reason, never
silently left out.

``data``: ``summaries`` {device: [5-min summary]}, ``events`` [event with ``run_s``],
``seconds`` {"device|ch": [1-second line]} or None (not fetched yet), ``offsets``
{"device|ch": run_s - e} (timeline.json).
"""

from __future__ import annotations

import itertools
import statistics
from dataclasses import dataclass
from typing import Any

RANK = {"high": 0, "medium": 1, "low": 2}
IGMP_PERIODS_S = (125.0, 260.0, 270.0)  # 05 1.11 (RFC 2236, 9776)
NEAR_S = 5.0
DELAY_JUMP_MS = 10.0  # R-22: a cell's delay excursion that goes with Wi-Fi retries
HELD_QUIET = 0.1  # R-23: a second the card got under this share of the usual packets
HELD_BURST = 2.0  # R-23: ... then over this many times the usual, with the PC's own drops
HELD_WITHIN_S = 10
NOT_FETCHED = "1-second records not fetched yet"
LATER = "later phase (needs two runs or the tablet app)"
RECEIVER_START = "/receiver/start"


@dataclass(frozen=True)
class Loss:
    """One loss event: lost seconds in a row on one receiver (iptv 2.4)."""

    device: str
    ch: int
    start_s: float  # run_s of the first lost second
    end_s: float
    lost: int


@dataclass
class Out:
    findings: list[dict[str, Any]]
    missing: list[dict[str, str]]

    def add(
        self,
        rule: str,
        category: str,
        confidence: str,
        text: str,
        devices: list[str],
        at_s: float | None = None,
    ) -> None:
        self.findings.append(
            {
                "rule": rule,
                "category": category,
                "confidence": confidence,
                "text": text,
                "devices": sorted(set(devices)),
                "at_s": None if at_s is None else round(at_s, 1),
            }
        )

    def lack(self, rule: str, why: str) -> None:
        self.missing.append({"rule": rule, "why": why})


# ---------------------------------------------------------------- inputs


def _cells(summaries: dict[str, list[dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    return [(dev, s) for dev, rows in summaries.items() for s in rows]


def _wireless(summaries: dict[str, list[dict[str, Any]]]) -> dict[str, bool]:
    """A receiver is wireless when its summaries carry Wi-Fi values (02 6 ``wifi``)."""
    return {dev: any(s.get("wifi") for s in rows) for dev, rows in summaries.items()}


def _lost(s: dict[str, Any]) -> int:
    return int((s.get("loss") or {}).get("lost") or 0)


def _rssi_min(cells: list[dict[str, Any]]) -> int | None:
    values = [int(v) for s in cells if (v := (s.get("wifi") or {}).get("rssi_dbm_min")) is not None]
    return min(values, default=None)


def losses(seconds: dict[str, list[dict[str, Any]]], offsets: dict[str, float]) -> list[Loss]:
    """Loss events on the run clock. A receiver without an offset is left out."""
    out: list[Loss] = []
    for key, lines in seconds.items():
        if key not in offsets:
            continue
        device, _, ch = key.rpartition("|")
        run: list[dict[str, Any]] = []
        for line in sorted((x for x in lines if x.get("type") == "s"), key=lambda x: x["e"]):
            if int(line.get("lost") or 0) <= 0:
                continue
            if run and int(line["e"]) - int(run[-1]["e"]) > 1:
                out.append(_loss(device, ch, run, offsets[key]))
                run = []
            run.append(line)
        if run:
            out.append(_loss(device, ch, run, offsets[key]))
    return sorted(out, key=lambda x: x.start_s)


def _loss(device: str, ch: str, run: list[dict[str, Any]], offset: float) -> Loss:
    return Loss(
        device,
        int(ch or 0),
        float(run[0]["e"]) + offset,
        float(run[-1]["e"]) + offset,
        sum(int(x.get("lost") or 0) for x in run),
    )


def _joins(events: list[dict[str, Any]]) -> list[tuple[str, int, float]]:
    """(device, ch, run_s) of every receiver start the Controller finished (2.4)."""
    out = []
    for e in events:
        d = e.get("data") or {}
        if str(d.get("step", "")).endswith(RECEIVER_START) and e.get("run_s") is not None:
            out.append((str(e.get("device", "")), int(d.get("ch") or 0), float(e["run_s"])))
    return out


def _at(events: list[dict[str, Any]], *codes: str) -> list[tuple[str, float]]:
    return [
        (str(e.get("device", "")), float(e["run_s"]))
        for e in events
        if e.get("code") in codes and e.get("run_s") is not None
    ]


def _near_period(x: float) -> bool:
    return any(abs(x - p) <= NEAR_S for p in IGMP_PERIODS_S) or (
        x > 0 and abs(x - 125.0 * round(x / 125.0)) <= NEAR_S and round(x / 125.0) >= 1
    )


# ---------------------------------------------------------------- rules


def _from_summaries(out: Out, summaries: dict[str, list[dict[str, Any]]]) -> None:
    wifi = _wireless(summaries)
    wired = [d for d, w in wifi.items() if not w]
    wireless = [d for d, w in wifi.items() if w]
    cells = _cells(summaries)
    # R-07: the receiver's own drops explain the loss (purple, 05 5.3)
    own = [
        d
        for d, s in cells
        if _lost(s) and int((s.get("loss") or {}).get("host_drop_loss") or 0) >= _lost(s)
    ]
    if own:
        out.add(
            "R-07",
            "Receiver",
            "high",
            "Receiver dropped the packets itself: its own drops cover the loss "
            f"({', '.join(sorted(set(own)))}). Not the router (purple). Next: check the "
            "receiver's CPU and other programs.",
            own,
        )
    # R-09: loss that falls inside the sender's trouble windows (05 5.3 split)
    sender = [d for d, s in cells if int((s.get("trouble") or {}).get("sender_lost") or 0) > 0]
    if sender:
        n = sum(int((s.get("trouble") or {}).get("sender_lost") or 0) for _, s in cells)
        out.add(
            "R-09",
            "Sender",
            "high",
            f"{n} lost packets fell while the sender was unstable. Sender-side cause "
            "(purple). Next: check the sender PC's CPU and disk records.",
            sender,
        )
    # R-16: the same sequence number arrived twice or more
    dup = [d for d, s in cells if int((s.get("loss") or {}).get("duplicates") or 0) > 0]
    if dup:
        n = sum(int((s.get("loss") or {}).get("duplicates") or 0) for _, s in cells)
        out.add(
            "R-16",
            "Router",
            "medium",
            f"{n} duplicate packets arrived. The router may send both the multicast and "
            "its M2U copy. Next: check the M2U setting.",
            dup,
        )
    _single_air_losses(out, cells, wifi)
    if not wireless:
        for rule in ("R-03", "R-04", "R-12", "R-13"):
            out.lack(rule, "no wireless receiver in this run")
        return
    lost_by = {d: sum(_lost(s) for dd, s in cells if dd == d) for d in wifi}
    rssi: dict[str, int | None] = {d: _rssi_min([s for dd, s in cells if dd == d])
                                   for d in wireless}  # fmt: skip
    if not wired:
        out.lack("R-03", "no wired receiver to compare with")
        out.lack("R-12", "no wired receiver to compare with")
    else:
        strong = [d for d in wireless if lost_by[d] > 0 and (rssi[d] or -999) >= -65]
        if strong and all(lost_by[d] == 0 for d in wired):
            out.add(
                "R-03",
                "Router",
                "high",
                "Only wireless receivers lost packets while the signal was good "
                "(RSSI at least -65 dBm) and the wired ones lost none. Router wireless multicast "
                "(M2U) suspected. Next: check M2U.",
                strong,
            )
        late = {
            d: sum(
                int((s.get("loss") or {}).get(k) or 0)
                for dd, s in cells
                if dd == d
                for k in ("reordered_recovered", "late_discarded")
            )
            for d in wifi
        }
        slow = [d for d in wireless if late[d] > 0]
        if slow and all(late[d] == 0 for d in wired):
            out.add(
                "R-12",
                "Router/Environment",
                "medium",
                "Wireless receivers got late or out-of-order packets and the wired ones did "
                "not: Wi-Fi retries or batching delay them.",
                slow,
            )
    weak = [
        d
        for d, s in cells
        if d in wireless
        and _lost(s)
        and (s.get("wifi") or {}).get("rssi_dbm_min") is not None
        and int((s.get("wifi") or {})["rssi_dbm_min"]) < -70
    ]
    if weak:
        out.add(
            "R-04",
            "Environment",
            "medium",
            "Loss came with a weak Wi-Fi signal (below -70 dBm). Distance or interference "
            "matters here; the red stays red. Next: test closer to the router.",
            weak,
        )
    jumpy = [
        d
        for d, s in cells
        if d in wireless
        and lost_by[d] == 0
        and float((s.get("jitter") or {}).get("max_ms") or 0) > 50
    ]
    if jumpy:
        out.add(
            "R-13",
            "Router",
            "medium",
            "No loss, but wireless jitter went over 50 ms: the router may hold multicast "
            "for power save (DTIM). Next: check whether M2U is used.",
            jumpy,
        )


def _single_air_losses(
    out: Out, cells: list[tuple[str, dict[str, Any]]], wifi: dict[str, bool]
) -> None:
    """R-22 (2026-10-15 GAP-156, the 100 h runs of 10-10/11: "loss now and then, no cause
    found"): a Wi-Fi receiver loses one packet at a time, it dropped none itself, its
    Wi-Fi did not change (channel, AP) and the cell's delay jumped — the frame was lost
    over the air or in the router's Wi-Fi queue after its retries. From the 5-minute
    summaries alone, so it is found even when the 1-second records were not fetched."""
    hits: list[tuple[str, dict[str, Any]]] = []
    for d, s in cells:
        loss, w, jit = s.get("loss") or {}, s.get("wifi") or {}, s.get("jitter") or {}
        lost = _lost(s)
        if not lost or not wifi.get(d):
            continue
        single = int(loss.get("max_burst") or 0) <= 1 and int(loss.get("loss_events") or 0) == lost
        clean = int(loss.get("host_drop_loss") or 0) == 0
        steady = not (w.get("channel_changes") or w.get("bssid_changes"))
        hi, lo = jit.get("delay_high_ms"), jit.get("delay_low_ms")
        jump = max(abs(float(hi or 0)), abs(float(lo or 0)))
        if single and clean and steady and jump >= DELAY_JUMP_MS:
            hits.append((d, s))
    if not hits:
        return
    n = sum(_lost(s) for _, s in hits)
    jumps = [max(abs(float((s.get("jitter") or {}).get(k) or 0))
                 for k in ("delay_high_ms", "delay_low_ms")) for _, s in hits]  # fmt: skip
    rssi = _rssi_min([s for _, s in hits])
    sig = f", signal {rssi} dBm" if rssi is not None else ""
    out.add(
        "R-22",
        "Router Wi-Fi",
        "medium",
        f"{n} packet(s) lost one at a time in {len(hits)} box(es), each with a delay jump "
        f"of {min(jumps):.0f}~{max(jumps):.0f} ms; this PC dropped none and its Wi-Fi did "
        f"not change{sig}. The frames were lost over the air or in the router's Wi-Fi queue "
        "after its retries. Next: compare a wired receiver at the same time, try another "
        "channel or band, and check the router's Wi-Fi retry and queue counters.",
        sorted({d for d, _ in hits}),
        float(hits[0][1].get("start_elapsed_s") or 0),
    )


def _from_events(out: Out, events: list[dict[str, Any]]) -> None:
    for rule, codes, text in (
        (
            "R-01",
            ("ZL-NET-003",),
            "No receiver got the test stream: multicast does not pass "
            "WAN → LAN. Next: IGMP proxy (NAT) or snooping (Bridge), the sender address and TTL.",
        ),
        (
            "R-14",
            ("ZL-NET-005",),
            "The old group kept coming after a channel change: IGMP "
            "leave is not handled. Next: check fast leave on the router.",
        ),
        (
            "R-15",
            ("ZL-NET-006",),
            "A group nobody joined arrived: snooping looks off and multicast floods every port.",
        ),
        (
            "R-18",
            ("ZL-RUN-009",),
            "A receiver never became steady at the start. Next: see "
            "which step stopped (first packet, PAT, picture) in the start-up record.",
        ),
    ):
        hits = _at(events, *codes)
        if hits:
            conf = "high"
            out.add(
                rule,
                "Router" if rule != "R-18" else "Start-up",
                conf,
                text,
                [d for d, _ in hits],
                min(t for _, t in hits),
            )


def _from_seconds(out: Out, data: dict[str, Any]) -> None:
    seconds, offsets = data.get("seconds"), data.get("offsets") or {}
    rules = ("R-02", "R-05", "R-05a", "R-06", "R-08", "R-10", "R-19", "R-20", "R-21", "R-24")
    if seconds is None:
        for rule in rules:
            out.lack(rule, NOT_FETCHED)
        return
    events = data.get("events") or []
    lost = losses(seconds, offsets)
    joins = _joins(events)
    receivers = {k.rpartition("|")[0] for k in seconds}
    # R-02: loss starts about one membership interval after the join
    if not joins:
        out.lack("R-02", "no receiver join time in the run events")
    else:
        hit = []
        for x in lost:
            last = max((t for d, ch, t in joins if d == x.device and t <= x.start_s), default=None)
            if last is not None and (
                250 <= x.start_s - last <= 275 or _near_period(x.start_s - last)
            ):
                hit.append((x, x.start_s - last))
        if hit:
            x, gap = hit[0]
            out.add(
                "R-02",
                "Router",
                "high",
                f"Loss began {gap:.0f} s after the join ({x.device}), about one IGMP "
                "membership interval (260-270 s) or a query period. Next: check that the "
                "router sends IGMP queries (capture).",
                [h.device for h, _ in hit],
                x.start_s,
            )
    # R-05 / R-05a: two or more receivers start losing within ±1 s
    if len(receivers) < 2:
        out.lack("R-05", "only one receiver")
        out.lack("R-05a", "only one receiver")
        out.lack("R-06", "only one receiver")
    else:
        together = _together(lost, 1.0)
        if together:
            devs, t = together
            out.add(
                "R-05",
                "Router",
                "high",
                f"{len(devs)} receivers started losing within 1 s of each other at "
                f"{t:.0f} s: a problem in the router's shared path.",
                sorted(devs),
                t,
            )
            down = {d for d, tt in _at(events, "ZL-NET-015") if abs(tt - t) <= NEAR_S}
            if down and down >= devs:
                out.add(
                    "R-05a",
                    "Router",
                    "high",
                    "All links dropped at the same time as the loss: the router restarted? "
                    "Next: check its log and uptime.",
                    sorted(down),
                    t,
                )
        near = [x for x in lost for d, _, t in joins if d != x.device and abs(x.start_s - t) <= 2]
        if near:
            out.add(
                "R-06",
                "Router",
                "medium",
                "Loss began when another receiver joined or changed channel: the router's "
                "snooping table update affects others.",
                [x.device for x in near],
                near[0].start_s,
            )
    _alone(out, lost, seconds, offsets)
    _igmp_wait(out, lost, events)
    _held_and_released(out, seconds, offsets, events)
    clean = _playback(out, seconds, offsets)
    _periodic(out, lost, clean)
    for rule, codes, window, text in (
        (
            "R-19",
            ("ZL-NET-013", "ZL-NET-015"),
            None,
            "Loss began after a link speed drop or "
            "link loss. Next: change the cable or port and test again (it may be the router port).",
        ),
        (
            "R-20",
            ("ZL-NET-016", "ZL-NET-017"),
            10.0,
            "Wireless loss came around a Wi-Fi channel, "
            "width or access point change. Next: fix the router's Wi-Fi channel and compare.",
        ),
    ):
        marks = _at(events, *codes)
        if rule == "R-19":  # a Wi-Fi drop is R-20/R-21's, not a cable's
            marks = [m for m in marks if m not in {(d, t) for d, t, _ in _wifi_back(events)}]
        hits = [
            x
            for x in lost
            for _, t in marks
            if (window is None and 0 <= x.start_s - t <= 300)
            or (window is not None and abs(x.start_s - t) <= window)
        ]
        if hits:
            out.add(
                rule,
                "Link" if rule == "R-19" else "Router Wi-Fi",
                "high",
                text,
                [x.device for x in hits],
                hits[0].start_s,
            )


def _alone(out: Out, lost: list[Loss], seconds: dict[str, list[dict[str, Any]]],
           offsets: dict[str, float]) -> None:  # fmt: skip
    """R-24 (2026-10-24 user: "공유기 문제가 아닌데 공유기 문제로 돌리지 않게"): a receiver
    lost packets while another receiver on the same channel got every one -> the cause is
    on that receiver's side (its Wi-Fi, the device), not the router or the sender. A
    tablet that scanned for Wi-Fi in those seconds (``wsc``, TAB-018) is named."""
    by_key = {k: {int(x["e"]) + offsets[k]: x for x in v if x.get("type") == "s"}
              for k, v in seconds.items() if k in offsets}  # fmt: skip
    hits: list[tuple[Loss, list[str], bool]] = []
    for x in lost:
        peers = []
        for key, at in by_key.items():
            dev, _, ch = key.rpartition("|")
            if dev == x.device or int(ch or 0) != x.ch:
                continue
            window = [at.get(t) for t in range(int(x.start_s), int(x.end_s) + 1)]
            if window and all(r is not None and int(r.get("lost") or 0) == 0
                              and int(r.get("rx") or 0) > 0 for r in window):  # fmt: skip
                peers.append(dev)
        if peers:
            mine = by_key.get(f"{x.device}|{x.ch}", {})
            scanned = any(int((mine.get(t) or {}).get("wsc") or 0) > 0
                          for t in range(int(x.start_s) - 1, int(x.end_s) + 2))  # fmt: skip
            hits.append((x, peers, scanned))
    if not lost:
        out.lack("R-24", "no loss")
        return
    if not hits:
        out.lack("R-24", "no loss while another receiver on the same channel got everything")
        return
    devices = sorted({x.device for x, _, _ in hits})
    scans = sum(1 for _, _, s in hits if s)
    x, peers, _ = hits[0]
    why = (f" {scans} of them in seconds the tablet scanned for Wi-Fi: its radio left the "
           "channel (TAB-018).") if scans else ""  # fmt: skip
    out.add(
        "R-24",
        "Receiver",
        "high",
        f"{len(hits)} loss burst(s) on {', '.join(devices)} only, while {', '.join(peers)} on "
        f"the same channel got every packet: the cause is on that receiver's side (its "
        f"Wi-Fi or the device), not the router or the sender.{why} Next: check that "
        "receiver (its Wi-Fi scans, power saving, place).",
        devices,
        x.start_s,
    )


def _held_and_released(
    out: Out,
    seconds: dict[str, list[dict[str, Any]]],
    offsets: dict[str, float],
    events: list[dict[str, Any]] | None = None,
) -> None:
    """R-23 (2026-10-16 wireless night run, receiverPC's reading of the seconds): the
    Wi-Fi card got almost nothing for seconds, then the held packets came at ~6x the
    stream and overflowed the receiver's buffer. The PC's own drops were the symptom;
    the cause is before the PC (the router's Wi-Fi, or the card's driver)."""
    for key, lines in seconds.items():
        rows = sorted((x for x in lines if x.get("type") == "s" and x.get("np") is not None),
                      key=lambda x: x["e"])  # fmt: skip
        if len(rows) < 30:
            continue
        usual = sorted(int(x["np"]) for x in rows)[len(rows) // 2]
        if usual <= 0:
            continue
        quiet = [int(x["e"]) for x in rows if int(x["np"]) < usual * HELD_QUIET]
        burst = [x for x in rows if int(x["np"]) > usual * HELD_BURST and int(x.get("hd") or 0)
                 and any(0 < int(x["e"]) - q <= HELD_WITHIN_S for q in quiet)]  # fmt: skip
        if not burst:
            continue
        device = key.rpartition("|")[0]
        at = float(burst[0]["e"]) + offsets.get(key, 0.0)
        # 2026-10-17 (night run): the receiver PC itself was at 100 % CPU (its own
        # evidence pictures); its 1-second sampling was starved too, which looks the
        # same. Then it is the PC's own drop (R-07), not the router's Wi-Fi.
        busy = [t for d, t in _at(events or [], "ZL-RCV-016", "ZL-SYS-003")
                if d == device and -120.0 <= at - t <= 120.0]  # fmt: skip
        if busy:
            continue
        peak = max(int(x["np"]) for x in burst) / usual
        drops = sum(int(x.get("hd") or 0) for x in burst)
        held = [q for q in quiet if any(0 < int(x["e"]) - q <= HELD_WITHIN_S for x in burst)]
        # 2026-10-17 user ("이미 검증되서 양산한 제품", SKB-certified): a hold seen only
        # from the receiving card cannot tell the router from the card's own driver (a USB
        # card stalls the same way). Not "the router" -- both, and the card first when it
        # dropped packets itself (nd).
        card_drops = sum(int(x.get("nd") or 0) for x in rows
                         if any(0 <= int(x["e"]) - q <= HELD_WITHIN_S for q in held))  # fmt: skip
        whose = ("the receiving Wi-Fi card or its driver (it dropped "
                 f"{card_drops} packets itself), or the router's Wi-Fi" if card_drops else
                 "the router's Wi-Fi or the receiving Wi-Fi card's driver")  # fmt: skip
        out.add(
            "R-23",
            "Receiver Wi-Fi card" if card_drops else "Wi-Fi link",
            "medium",
            f"The Wi-Fi card got almost nothing for {len(held)} s, then the held packets came "
            f"at up to {peak:.0f}x the stream and overflowed the receiver's buffer ({drops} "
            f"packets). Held by {whose}. Next: measure again with another Wi-Fi card (a "
            "built-in one, or the USB card on a USB 3 port); if it stays clean, the card "
            "was the cause.",
            [device],
            at,
        )


def _wifi_back(events: list[dict[str, Any]]) -> list[tuple[str, float, float]]:
    """(device, run_s down, run_s up) of each Wi-Fi drop that came back (NET-015, then the
    ``clears`` info). Wired drops are R-19's."""
    out: list[tuple[str, float, float]] = []
    down: dict[tuple[str, str], float] = {}
    for e in sorted((x for x in events if x.get("run_s") is not None), key=lambda x: x["run_s"]):
        d, key = (
            e.get("data") or {},
            (str(e.get("device", "")), str((e.get("data") or {}).get("nic"))),
        )
        if e.get("code") == "ZL-NET-015" and isinstance((d.get("before") or {}).get("wifi"), dict):
            down.setdefault(key, float(e["run_s"]))
        elif "ZL-NET-015" in (d.get("clears") or []) and key in down:
            out.append((key[0], down.pop(key), float(e["run_s"])))
    return out


def _igmp_wait(out: Out, lost: list[Loss], events: list[dict[str, Any]]) -> None:
    """R-21 (2026-10-09 first wireless run, GAP-134): the router kicked the Wi-Fi client
    to change channel; the card was back in 9.5 s but the stream 39 s later. The kernel
    sends no IGMP report on a reassociation, so the stream waits for the router's next
    query. The outage is split, the receiver is left as a real set-top box would be."""
    for device, t_down, t_up in _wifi_back(events):
        back = next((x for x in lost if x.device == device and x.start_s >= t_up), None)
        if back is None or back.start_s - t_up > 300:
            continue
        wait = back.start_s - t_up
        if wait < 3:
            continue  # came back with the link: nothing to wait for
        out.add(
            "R-21",
            "Router",
            "high",
            f"Wi-Fi was off {t_up - t_down:.0f} s, then the stream came back {wait:.0f} s "
            f"after the link ({back.lost} packets lost in all): the router forgot this "
            "receiver's channel when it left and only asked again at its next IGMP query. "
            "Next: check the router's IGMP snooping (keep or re-ask members that reconnect).",
            [device],
            t_up,
        )


def _together(lost: list[Loss], within: float) -> tuple[set[str], float] | None:
    for x in lost:
        devs = {y.device for y in lost if abs(y.start_s - x.start_s) <= within}
        if len(devs) >= 2:
            return devs, x.start_s
    return None


def _playback(
    out: Out, seconds: dict[str, list[dict[str, Any]]], offsets: dict[str, float]
) -> list[tuple[str, float]]:
    """R-08: a playback problem while the packets around it (±2 s) were clean. Returns
    those seconds (device, run time) for R-10."""
    seen_pb = False
    hits: list[tuple[str, float]] = []
    for key, lines in seconds.items():
        by_e = {int(x["e"]): x for x in lines if x.get("type") == "s"}
        for e, x in by_e.items():
            pb = x.get("pb")
            if pb is None:
                continue
            seen_pb = True
            if pb == "ok":
                continue
            around = [by_e.get(e + d) or {} for d in range(-2, 3)]
            if all(not int(y.get("lost") or 0) and not int(y.get("cc") or 0) for y in around):
                hits.append((key.rpartition("|")[0], e + offsets.get(key, 0.0)))
    if not seen_pb:
        out.lack("R-08", "playback was off or not reported")
    elif hits:
        out.add(
            "R-08",
            "Receiver",
            "high",
            f"{len(hits)} playback problem second(s) while the packets were clean: the "
            "receiver side (player or device). Next: run a baseline test (05 4.6.3).",
            [d for d, _ in hits],
            hits[0][1],
        )
    return hits


def _steady(times: list[float]) -> float | None:
    """The interval when 3+ events come at a steady one (spread < 10 %), not an IGMP
    period; else None."""
    if len(times) < 3:
        return None
    gaps = [b - a for a, b in itertools.pairwise(sorted(times))]
    mean = statistics.fmean(gaps)
    if mean > 0 and statistics.pstdev(gaps) < 0.1 * mean and not _near_period(mean):
        return mean
    return None


def _periodic(out: Out, lost: list[Loss], clean_playback: list[tuple[str, float]]) -> None:
    """R-10: the same kind of event at a steady interval that is not an IGMP period.
    Loss: a periodic router job. Playback trouble with clean packets: the receiver side
    (the player correcting its clock, GAP-105 2), like R-08."""
    kinds: list[tuple[str, str, dict[str, list[float]]]] = [
        ("Loss", "Router", {}), ("Playback trouble (packets clean)", "Receiver", {}),
    ]  # fmt: skip
    for x in lost:
        kinds[0][2].setdefault(x.device, []).append(x.start_s)
    for dev, t in clean_playback:
        kinds[1][2].setdefault(dev, []).append(t)
    for what, where, by_dev in kinds:
        for dev, times in by_dev.items():
            mean = _steady(times)
            if mean is None:
                continue
            why = ("a periodic router job (Wi-Fi scan, statistics) suspected"
                   if where == "Router" else "the receiver side suspected (the player "
                   "correcting its clock against the sender's); run a baseline test")  # fmt: skip
            out.add("R-10", where, "medium",
                    f"{what} came every {mean:.0f} s on {dev} ({len(times)} times): {why}.",
                    [dev], min(times))  # fmt: skip
            break


def analyze(data: dict[str, Any]) -> dict[str, Any]:
    """``run_entry.analysis`` (12 2): findings strongest first, earliest first."""
    out = Out([], [])
    _from_summaries(out, dict(data.get("summaries") or {}))
    _from_events(out, list(data.get("events") or []))
    _from_seconds(out, data)
    held = {d for f in out.findings if f["rule"] == "R-23" for d in f["devices"]}
    if held:  # R-23 names the cause of the PC's own drops: not the PC (2026-10-16)
        out.findings = [f for f in out.findings
                        if not (f["rule"] == "R-07" and set(f["devices"]) <= held)]  # fmt: skip
    for rule in ("R-11", "R-17"):
        out.lack(rule, LATER)
    out.findings.sort(
        key=lambda f: (
            RANK[f["confidence"]],
            f["at_s"] if f["at_s"] is not None else float("inf"),
            f["rule"],
        )
    )
    return {"findings": out.findings, "missing": out.missing}
