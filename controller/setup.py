"""The test chosen on the screen (docs/03-features.md F14, 02 11 ``controller.json``).

One place keeps the last choice (test type, quality, duration, devices and channels,
changed Advanced values) so the next run starts with it (F14 rule 4), and turns it
into a plan (02 11-1) for Start and the checks — the same plan a file would give.
Roles are found by themselves (06 10.4): the PC on the router WAN side is the sender
(the ISP sends), the LAN / Wi-Fi side PCs receive; a LAN sender is a comparison test.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from controller.devices import DeviceList
from controller.precheck import own_address
from core.protocol.client import NodeClient
from core.protocol.envelope import ZlError

SCHEMA_VERSION = 1
# main-screen settings that are fields of the choice, not ``settings`` (02 2-11, GAP-99)
MAIN_SETTINGS = {"S-01": "test_type", "S-03": "quality", "S-05": "duration_s",
                 "S-06": "send_quality_reports"}  # fmt: skip
FIELDS = ("module", "test_type", "quality", "duration_s", "send_quality_reports", "sender",
          "receivers", "settings", "nics")  # fmt: skip
AUTO = ""  # a card left to the Controller (03 F17): not written in ``nics``
TEST_TYPE = {"Throughput · All": "all", "Throughput · Wired": "wired",  # S-01 -> plan
             "Throughput · Wi-Fi": "wifi"}  # fmt: skip
IPTV_TEST = "IPTV test"  # one menu since 2026-10-24: the rows pick a PC and/or tablets
OLD_TEST_TYPES = ("PC receiver test", "Tablet test")  # saved choices before the merge
# 2026-10-16: the throughput test types run the throughput module (iperf3)
MODULE_OF = {"Throughput · All": "throughput", "Throughput · Wired": "throughput",
             "Throughput · Wi-Fi": "throughput"}  # fmt: skip


def module_of(test_type: str) -> str:
    return MODULE_OF.get(test_type, "iptv")


class SetupStore:
    def __init__(self, path: Path, defaults: dict[str, Any]) -> None:
        self.path = path
        self.defaults = defaults  # {"S-03": "UHD", ...} of Core and the modules
        self._lock = threading.Lock()

    def get(self) -> dict[str, Any]:
        with self._lock:
            try:
                doc = json.loads(self.path.read_text(encoding="utf-8"))
                saved = doc.get("setup") if isinstance(doc, dict) else None
            except (OSError, ValueError):
                saved = None
        base = {
            "module": None, "test_type": IPTV_TEST,
            "quality": self.defaults.get("S-03", "UHD"),
            "duration_s": self.defaults.get("S-05", 3600),
            "send_quality_reports": True, "sender": None, "receivers": [], "settings": {},
            "nics": {},
        }  # fmt: skip
        out = {**base, **{k: v for k, v in (saved or {}).items() if k in FIELDS}}
        if out.get("test_type") in OLD_TEST_TYPES:
            out["test_type"] = IPTV_TEST  # its receivers, PC and tablets, stay as they were
        return out

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Merge screen changes (unknown keys refused); ``settings`` keeps only values
        that differ from the default (02 11-1: the plan lists changed ones only)."""
        bad = [k for k in changes if k not in FIELDS]
        if bad:
            raise ZlError("DSC-012", "Request rejected", detail=f"unknown setup keys {bad}")
        cur = self.get()
        cur.update(changes)
        settings = dict(cur.get("settings") or {})
        for sid, field in MAIN_SETTINGS.items():  # GAP-99: the plan reads these fields
            if sid in settings:
                value = settings.pop(sid)
                cur[field] = value == "On" if field == "send_quality_reports" else value
        cur["settings"] = {k: v for k, v in settings.items() if self.defaults.get(k) != v}
        cur["nics"] = {k: v for k, v in (cur.get("nics") or {}).items() if v and v != AUTO}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"schema_version": SCHEMA_VERSION, "setup": cur},
                                      indent=1, ensure_ascii=False), encoding="utf-8")  # fmt: skip
            tmp.replace(self.path)
        return cur


def addresses_of(dev: Any, links: list[dict[str, Any]] | None,
                 found: list[str] | None = None, live: bool = False) -> list[str]:  # fmt: skip
    """The addresses a person may pick for this PC (2026-10-15 GAP-155): what it has
    now. ``live``: its cards as it answered just now -- only those (2026-10-16 user: an
    address it no longer has was offered). Not answering: the one in use, the ones the
    search saw it at and its cards when last seen."""
    cards = [str(x.get("ip")) for x in links or [] if x.get("ip")]
    out = cards if live and cards else [dev.host, *(found or []), *cards]
    return [a for a in dict.fromkeys(out) if a and not a.startswith("127.")]


def address_labels(links: list[dict[str, Any]] | None,
                   wan_ip: str | None = None) -> dict[str, str]:  # fmt: skip
    """Each address the PC has, said the way a person thinks of it (2026-10-17 user: "wan
    ip, lan ip, wifi ip"): ``WAN · enp37s0`` (the card the router WAN gets its address
    from), ``Wi-Fi · wlx…``, ``LAN · enp0s31f6`` (any other cable)."""
    out = {}
    for c in links or []:
        ip = str(c.get("ip") or "")
        if not ip or ip.startswith("127."):
            continue
        kind = "WAN" if wan_ip and ip == wan_ip else "Wi-Fi" if "wifi" in c else "LAN"
        out[ip] = f"{kind} · {c.get('nic') or ''}".rstrip(" ·")
    return out


def _tablets(client: NodeClient) -> tuple[list[dict[str, Any]], int]:
    """The tablets on this PC's USB (02 4 ``/v1/tablets``), and how many more are on its
    USB that adb does not list; none from an older build."""
    try:
        got = client.get("/v1/tablets", timeout_s=15.0)
    except ZlError:
        return [], 0
    rows = [t for t in got.get("tablets") or [] if isinstance(t, dict) and t.get("serial")]
    return rows, int(got.get("usb_unseen") or 0)


def sides(devices: DeviceList, boot_id: str,
          found: dict[str, list[str]] | None = None,
          test_type: str = "") -> dict[str, dict[str, Any]]:  # fmt: skip
    """Each registered device: answers?, ``wan`` (behind the router's NAT or ISP role on),
    ``lan``, or ``unknown`` (06 10.4). ``answers`` only when the PC at its address is that
    PC (its device ID): 2026-10-12 t460-rx was saved at the sender's address and every
    question to it was answered by the sender (GAP-155, DSC-016). ``addresses``: what the
    screen offers to pick; ``found``: addresses the search saw each device at."""
    out: dict[str, dict[str, Any]] = {}
    names = {d.device_id: d.name for d in devices.all()}
    for dev in devices.all():
        client = NodeClient(dev.host, dev.port, boot_id, dev.name)
        known_links = devices.cards.get(dev.name)
        picks = addresses_of(dev, known_links, (found or {}).get(dev.name))
        try:
            info = client.get("/v1/info", timeout_s=2.0)
            other = str(info.get("device_id") or "")
            if other and other != dev.device_id:
                who = names.get(other, "another PC")
                out[dev.name] = {"reachable": False, "side": "unknown", "host": dev.host,
                                 "addresses": picks, "other": who,
                                 "why": f"{who} answers at {dev.host}, not {dev.name}"}  # fmt: skip
                continue
            try:
                wan = client.get("/v1/wan", timeout_s=2.0)
            except ZlError:
                wan = {}
            # the tablets on its USB: IPTV receivers, and in a Wi-Fi throughput test measured
            # like the PCs (T-4, 2026-10-29 user "구현부터 다 해")
            wants = (IPTV_TEST, *OLD_TEST_TYPES, "Throughput · All", "Throughput · Wi-Fi")
            tablets, unseen = _tablets(client) if test_type in wants else ([], 0)
        except ZlError:
            out[dev.name] = {"reachable": False, "side": "unknown", "host": dev.host,
                             "addresses": picks, "why": f"no answer at {dev.host}",
                             "labels": address_labels(known_links)}  # fmt: skip
            continue
        finally:
            client.close()
        seen, own = info.get("seen_from_ip"), own_address(dev.host, dev.port)
        behind_nat = bool(seen and own and seen != own)
        side = "wan" if behind_nat or wan.get("on") else "lan"
        # 02 2-11 (GAP-121): its cards for the Card list — from the node (0328 on), else
        # the last ones a test saw; None = not known yet
        links = info.get("links") if isinstance(info.get("links"), list) else None
        if links is not None:
            devices.note_links(dev.name, links)
        known = links if links is not None else devices.cards.get(dev.name)
        picks = addresses_of(dev, known, (found or {}).get(dev.name), live=links is not None)
        wan_ip = str(wan.get("server_ip") or "") if wan.get("on") else None
        out[dev.name] = {"reachable": True, "side": side, "isp_role": bool(wan.get("on")),
                         "links": known, "host": dev.host, "addresses": picks,
                         "labels": address_labels(known, wan_ip), "tablets": tablets,
                         "usb_unseen": unseen}  # fmt: skip
    return out


def roles(setup: dict[str, Any], found: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The sender and receivers exactly as the person chose them in the device rows
    (2026-10-17 user: "니가 판단하지말고" -- nothing is picked by the tool); ``why`` names
    what is still to choose."""
    names = set(found)
    sender = setup.get("sender") if setup.get("sender") in names else None
    why = None if sender else "Choose the sender: set its row to Sender"
    # a tablet is reached through the PC it hangs on by USB (01-overview D3)
    # the other test type's receivers stay saved for when it is picked again
    tablet = setup.get("test_type") in (IPTV_TEST, *OLD_TEST_TYPES)
    # a tablet test has its tablets and may have the PC receiver too (2026-10-23 user:
    # "수신기 pc 한대, 태블릿 두대"); a PC test has PCs only
    # 2026-10-31 user ("지금 두대 뿐이거든", "한대밖에 안골라지거든"): a tablet unplugged
    # since stays saved but is not a receiver of this test (its PC lists the ones on USB)
    on_usb = {str(t.get("serial")) for s in found.values() if isinstance(s, dict)
              for t in s.get("tablets") or [] if isinstance(t, dict)
              and t.get("state") == "device"}  # fmt: skip
    asked = any("tablets" in s for s in found.values() if isinstance(s, dict))
    receivers = [r for r in setup.get("receivers") or []
                 if (r.get("host") in names if r.get("serial") else r.get("device") in names)
                 and (tablet or not r.get("serial"))
                 and (not r.get("serial") or not asked or r.get("serial") in on_usb)]  # fmt: skip
    # 2026-10-31 user ("왜 태블릿 한개인데 이름이 tablet 3이야"): the tablets of this test are
    # tablet-1, -2, -3 in the order they were numbered before, with no gaps
    tabs = sorted((r for r in receivers if r.get("serial")), key=lambda r: str(r.get("device")))
    renamed = {id(r): f"tablet-{i}" for i, r in enumerate(tabs, start=1)}
    receivers = [{**r, "device": renamed[id(r)]} if id(r) in renamed else r for r in receivers]
    if not receivers:
        why = why or ("Choose the receivers: set a PC or tablet row to Receiver" if tablet
                      else "Choose the receiver: set its row to Receiver")  # fmt: skip
    comparison = sender is not None and found.get(sender, {}).get("side") == "lan"
    return {"sender": sender, "receivers": receivers, "why": why,
            "lan_sender": comparison}  # fmt: skip


def plan_of(setup: dict[str, Any], chosen: dict[str, Any],
            found: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:  # fmt: skip
    """02 11-1 plan from the screen choice (the same shape as a plan file)."""
    rx = chosen["receivers"]
    used = {chosen["sender"], *(r.get("host") or r["device"] for r in rx)}
    picked = {d: ip for d, ip in (setup.get("nics") or {}).items() if d in used and ip != AUTO}
    # a card the person picked (DSC-006, GAP-115), the Wi-Fi PC's test Wi-Fi (03 F17) over it
    nics = picked  # the rest: the address picked in the row (run step 1, 2026-10-17)
    # 2026-11-01 user ("PC의 RSSI는 안보이네"): a PC row set to Wireless whose address is a
    # wired card's (its Card box is hidden while it says Wireless) receives on its Wi-Fi card
    # that is on the same router network; nothing is joined (2026-10-17: the address decides)
    for r in rx:
        if r.get("serial") or r.get("network") != "Wireless":
            continue
        dev = str(r["device"])
        side = (found or {}).get(dev) or {}
        labels = {str(a): str(lb) for a, lb in (side.get("labels") or {}).items()}
        ip = str(nics.get(dev) or side.get("host") or "")
        if not ip or labels.get(ip, "").startswith("Wi-Fi"):
            continue
        net = ip.rsplit(".", 1)[0]
        wifi = sorted(a for a, lb in labels.items()
                      if lb.startswith("Wi-Fi") and a.rsplit(".", 1)[0] == net)  # fmt: skip
        if wifi:
            nics[dev] = wifi[0]
    receivers = [{"device": r["device"], "ch": int(r.get("ch") or 1),
                  **({"buffer_ms": int(r["buffer_ms"])} if r.get("buffer_ms") else {}),
                  **({"host": r["host"], "serial": r["serial"]} if r.get("serial") else {}),
                  **({"wifi": r["wifi"]} if r.get("serial") and r.get("wifi") else {})}
                 for r in rx]  # fmt: skip
    plan: dict[str, Any] = {
        "schema_version": 1, "module": module_of(str(setup.get("test_type"))),
        # the IPTV test is a tablet test when a tablet receives, else a PC test (02 11-1)
        "test_type": TEST_TYPE.get(str(setup.get("test_type")))
        or ("tablet" if any(r.get("serial") for r in rx) else "pc"),
        "devices": {"sender": chosen["sender"], "receivers": receivers},
        "quality": setup.get("quality"), "duration_s": setup.get("duration_s"),
        "send_quality_reports": bool(setup.get("send_quality_reports", True)),
        "settings": dict(setup.get("settings") or {}),
    }  # fmt: skip
    if nics:
        plan["nics"] = nics
    # 2026-11-06 user ("경고 추가해"): a Wireless PC row whose Wi-Fi card had no address on
    # the test router received on its cable with nothing said; Start warns (NET-026)
    wireless = sorted(str(r["device"]) for r in rx
                      if not r.get("serial") and r.get("network") == "Wireless")  # fmt: skip
    if wireless:
        plan["wireless_pcs"] = wireless
    return plan
