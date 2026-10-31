"""A Linux Wi-Fi card's channel-width cap and 802.11 mode for a measuring case, and back
as it was (docs/modules/throughput.md 1.2; the Windows twin is ``win_wifi_caps``).

2026-10-29 user: "linux는 어떻게 할거야?", "a/b/g/n/ac/be/bn설정 변경은 안되나?", "연결된
컴퓨터들에 따라 윈도우,리눅스별로 해야할거야".

Linux drivers have no "channel width" or "mode" item; the joining program has: a
wpa_supplicant network may turn off 40 MHz (``disable_ht40``), HT (11n), VHT (11ac), HE
(11ax) and EHT (11be). For a case NetworkManager lets go of the card, our own
wpa_supplicant joins the same access point with those options, the card keeps the
address the router gave it, and ``iw dev <card> link`` says what it actually joined
with (``80MHz HE-MCS 11``: width and standard both). Afterwards wpa_supplicant stops
and the card goes back to NetworkManager (the caller joins its network again). Only
what the card can do becomes a case (``iw phy``: HT40, VHT, HE, EHT); 11b alone has no
switch on the receiving side and is not offered. The password goes in a 0600 file that
is removed at once, never on a command line.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.blocks.process_runner import NO_WINDOW, ProcessRunner, StartError, StopError

JOIN_WAIT_S = 30.0
CMD_TIMEOUT_S = 10.0
WIDTH_MAX_MBPS = {20: 286.8, 40: 573.5, 80: 1201.0, 160: 2402.0, 320: 5764.0}
# each case's wpa_supplicant network options; the order is the case order
MODE_OFF = {
    "802.11n": ("disable_vht=1", "disable_he=1", "disable_eht=1"),
    "802.11ac": ("disable_he=1", "disable_eht=1"),
    "802.11ax": ("disable_eht=1",),
}
LEGACY_OFF = ("disable_ht=1", "disable_vht=1", "disable_he=1", "disable_eht=1")
KEY_MGMT = {"Open": "NONE", "WPA2": "WPA-PSK", "WPA3": "SAE"}

Run = Callable[[list[str], float], tuple[int, str]]


def run_command(argv: list[str], timeout_s: float) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s,
                              check=False, stdin=subprocess.DEVNULL, errors="replace",
                              creationflags=NO_WINDOW)  # fmt: skip
    except FileNotFoundError:
        return 127, f"{argv[0]} not found"
    except (OSError, subprocess.SubprocessError) as e:
        return 1, type(e).__name__
    return done.returncode, (done.stdout + done.stderr).strip()


def phy_caps(text: str) -> set[str]:
    """``iw phy <phy> info`` -> what the card can do: ht40, vht, he, eht."""
    got = set()
    if re.search(r"HT20/HT40|HT40", text):
        got.add("ht40")
    if "VHT Capabilities" in text:
        got.add("vht")
    if re.search(r"HE (Iftypes|PHY Capabilities|MAC Capabilities)", text):
        got.add("he")
    if re.search(r"EHT (Iftypes|PHY Capabilities|MAC Capabilities)", text):
        got.add("eht")
    return got


def options(caps: set[str], band: str) -> list[dict[str, Any]]:
    """The cases this card offers on this band (throughput.md 1.2 rules: 11ac is 5 GHz
    only, 11a is 5 GHz, 11g is 2.4 GHz; the card's own top is the band's own case)."""
    out: list[dict[str, Any]] = []
    if "ht40" in caps:
        out.append({"axis": "width", "band": band, "mhz": 20, "set": ["disable_ht40=1"],
                    "label": "width capped at 20 MHz"})  # fmt: skip
    top = "802.11be" if "eht" in caps else "802.11ax" if "he" in caps else (
        "802.11ac" if "vht" in caps else "802.11n")  # fmt: skip
    order = ("802.11n", "802.11ac", "802.11ax", "802.11be")
    for mode, off in MODE_OFF.items():
        if order.index(mode) >= order.index(top) or (mode == "802.11ac" and band != "5GHz"):
            continue
        out.append({"axis": "mode", "mode": mode, "set": list(off),
                    "label": f"mode capped at {mode}"})  # fmt: skip
    legacy = {"5GHz": "802.11a", "2.4GHz": "802.11g"}.get(band)
    if legacy:
        out.append({"axis": "mode", "band": band, "mode": legacy, "set": list(LEGACY_OFF),
                    "label": f"{legacy} only"})  # fmt: skip
    return out


def link_now(text: str) -> dict[str, Any]:
    """``iw dev <card> link`` -> connected, band, standard, width, rate. A driver that says
    only the bare rate (2026-10-29, the T460's RTL8814AU: no "rx bitrate" line, "tx bitrate:
    867.0 MBit/s" with no MCS or MHz) leaves standard and width unknown (None)."""
    if not text.startswith("Connected"):
        return {"connected": False}
    freq = re.search(r"freq:\s*(\d+)", text)
    mhz = int(freq.group(1)) if freq else 0
    band = "2.4GHz" if mhz and mhz < 3000 else "5GHz" if mhz and mhz < 5925 else (
        "6GHz" if mhz else None)  # fmt: skip
    line = re.search(r"rx bitrate:\s*([\d.]+) MBit/s(.*)", text) or re.search(
        r"tx bitrate:\s*([\d.]+) MBit/s(.*)", text)  # fmt: skip
    rate = float(line.group(1)) if line else None
    rest = line.group(2) if line else ""
    width = re.search(r"(\d+)MHz", rest)
    if "EHT-MCS" in rest:
        mode = "802.11be"
    elif "HE-MCS" in rest:
        mode = "802.11ax"
    elif "VHT-MCS" in rest:
        mode = "802.11ac"
    elif re.search(r"(?<![A-Z-])MCS \d+", rest):  # HT's "MCS 15"
        mode = "802.11n"
    else:
        mode = None  # a bare rate says no standard
    told = mode is not None  # the driver named the standard (and then a width or 20 MHz)
    return {"connected": True, "band": band, "mode": mode, "rx_mbps": rate,
            "width_mhz": int(width.group(1)) if width else 20 if told else None}  # fmt: skip


# When the driver names neither standard nor width, the most a link can carry tells
# (4 streams, top MCS, short guard): above it, the setting did not hold
MODE_MAX_MBPS = {"802.11a": 54.0, "802.11g": 54.0, "802.11n": 600.0, "802.11ac": 3466.7,
                 "802.11ax": 4803.9}  # fmt: skip
WIDTH_MAX_4SS = {20: 346.7, 40: 800.0, 80: 1733.3, 160: 3466.7, 320: 11529.4}


def mismatch(option: dict[str, Any], got: dict[str, Any]) -> str | None:
    """Why what the card joined with is not the case asked, or None. By the rate alone
    when the driver says nothing more (the record then says ``checked_by``: rate)."""
    rate = got.get("rx_mbps")
    if option["axis"] == "mode":
        mode = got.get("mode")
        if mode is not None:
            return None if mode == option["mode"] else (
                f"it joined with {mode}, not {option['mode']}")  # fmt: skip
        top = MODE_MAX_MBPS.get(str(option["mode"]))
        if isinstance(rate, int | float) and top is not None and rate > top + 1:
            return f"it moves {rate:g} Mbps, more than {option['mode']} can"
        return None if isinstance(rate, int | float) else "the card says no rate or standard"
    w = got.get("width_mhz")
    if isinstance(w, int):
        return f"it joined at {w} MHz, not {option['mhz']} MHz or less" if w > int(
            option["mhz"]) else None  # fmt: skip
    top = WIDTH_MAX_4SS.get(int(option["mhz"]))
    if isinstance(rate, int | float) and top is not None and rate > top + 1:
        return f"it moves {rate:g} Mbps, more than a {option['mhz']} MHz link can"
    return None if isinstance(rate, int | float) else "the card says no rate or width"


def driver_note(nic: str) -> str:
    """The card's driver, when it is not one Linux's own Wi-Fi stack drives (2026-10-29:
    the T460's RTL8814AU on the out-of-tree 8814au joined with none of the options)."""
    try:
        name = os.path.basename(os.readlink(f"/sys/class/net/{nic}/device/driver"))
    except OSError:
        return ""
    own = ("iwlwifi", "ath9k", "ath10k", "ath11k", "ath12k", "mt76", "mt7921", "rtw88", "rtw89")
    if name.startswith(own):
        return ""
    return f" (driver {name}: likely not honouring wpa_supplicant's HT/VHT options)"


class WifiCaps:
    """Width and mode cases of a Linux Wi-Fi card (one card at a time)."""

    def __init__(self, work: Path, run: Run = run_command, binary: str = "wpa_supplicant",
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:  # fmt: skip
        self.work, self.run, self.binary = work, run, binary
        self.sleep, self.clock = sleep, clock
        self._runner: ProcessRunner | None = None
        self._nic: str | None = None

    def caps(self, nic: str) -> set[str]:
        rc, out = self.run(["iw", "dev", nic, "info"], CMD_TIMEOUT_S)
        m = re.search(r"wiphy (\d+)", out) if rc == 0 else None
        if m is None:
            return set()
        rc, out = self.run(["iw", "phy", f"phy{m.group(1)}", "info"], CMD_TIMEOUT_S)
        return phy_caps(out) if rc == 0 else set()

    def options(self, nic: str, band: str) -> list[dict[str, Any]]:
        return options(self.caps(nic), band)

    def now(self, nic: str) -> dict[str, Any]:
        rc, out = self.run(["iw", "dev", nic, "link"], CMD_TIMEOUT_S)
        return link_now(out) if rc == 0 else {"connected": False}

    def set_option(self, nic: str, option: dict[str, Any], net: dict[str, Any]) -> dict[str, Any]:
        """Join ``net`` (ssid, bssid, security, password, ip, prefix) with the case's
        options from our own wpa_supplicant; read what the card joined with."""
        self.restore(nic)  # the case before, if any
        rc, out = self.run(["nmcli", "device", "set", nic, "managed", "no"], CMD_TIMEOUT_S)
        if rc != 0:
            return {"readback_ok": False, "why": f"NetworkManager did not let go of {nic}: {out}"}
        self._nic = nic
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "wifi-case-card").write_text(nic, encoding="utf-8")  # a restart puts it back
        conf = self.work / "wpa-case.conf"
        lines = [f"ctrl_interface=DIR={self.work / 'wpa-ctrl'}", "network={",
                 f"  ssid=\"{net['ssid']}\"", "  scan_ssid=1",
                 f"  key_mgmt={KEY_MGMT.get(str(net.get('security')), 'NONE')}"]  # fmt: skip
        if net.get("bssid"):
            lines.append(f"  bssid={net['bssid']}")
        if net.get("security") in ("WPA2", "WPA3"):
            lines.append(f'  psk="{net.get("password") or ""}"')
            if net["security"] == "WPA3":
                lines.append("  ieee80211w=2")
        lines += [f"  {x}" for x in option["set"]] + ["}"]
        fd = os.open(conf, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        runner = ProcessRunner("wifi_case", [self.binary, "-i", nic, "-D", "nl80211", "-c",
                                             str(conf)], self.work)  # fmt: skip
        try:
            runner.start()
        except StartError as e:
            conf.unlink(missing_ok=True)
            return {"readback_ok": False, "why": f"wpa_supplicant did not start: {e}"}
        self._runner = runner
        end = self.clock() + JOIN_WAIT_S
        got: dict[str, Any] = {}
        while self.clock() < end:
            got = self.now(nic)
            if got.get("connected") and got.get("rx_mbps"):
                break
            self.sleep(1.0)
        conf.unlink(missing_ok=True)  # read at its start; the password is gone from disk
        if not got.get("connected"):
            return {
                "readback_ok": True,
                "link": False,
                "why": f"it did not join within {JOIN_WAIT_S:g} s with these options"
                + driver_note(nic),
            }
        if net.get("ip"):  # the address the router gave it a moment ago (lease still valid)
            addr = f"{net['ip']}/{int(net.get('prefix') or 24)}"
            self.run(["ip", "addr", "replace", addr, "dev", nic], CMD_TIMEOUT_S)
        out_: dict[str, Any] = {"readback_ok": True, "link": True, "ip": net.get("ip"), **got}
        if got.get("mode") is None:  # the driver named no standard: checked by the rate
            out_["checked_by"] = "rate"
        why = mismatch(option, got)
        if why:
            out_["why"] = why
        return out_

    def restore(self, nic: str) -> dict[str, Any]:
        """Our wpa_supplicant stops, the card goes back to NetworkManager."""
        runner, self._runner = self._runner, None
        if runner is None and self._nic is None:
            return {"ok": True, "why": "nothing was changed"}
        if runner is not None:
            with contextlib.suppress(StopError):
                runner.stop()
        card = self._nic or nic
        self.run(["ip", "addr", "flush", "dev", card], CMD_TIMEOUT_S)
        rc, out = self.run(["nmcli", "device", "set", card, "managed", "yes"], CMD_TIMEOUT_S)
        self._nic = None
        if rc == 0:
            (self.work / "wifi-case-card").unlink(missing_ok=True)
        return {"ok": rc == 0, **({} if rc == 0 else {"why": out})}

    def restore_all(self) -> dict[str, dict[str, Any]]:
        """Reset All, or a start after a crash: a card left with our wpa_supplicant goes
        back to NetworkManager (the process itself died with the node)."""
        try:
            left = (self.work / "wifi-case-card").read_text(encoding="utf-8").strip()
        except OSError:
            left = ""
        card = self._nic or left
        if not card:
            return {}
        self._nic = card
        return {f"{card} Wi-Fi settings": self.restore(card)}
