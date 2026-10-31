"""A Windows Wi-Fi card's channel-width cap and 802.11 mode for a measuring case, and back
as they were (docs/modules/throughput.md 1.2: the PC's own Wi-Fi settings, one at a time,
the router untouched).

2026-10-29 user: "무선 설정별 테스트 안되는거 구현했나?", "전에는 설정 변경 무선에서 안하고
그냥 1회 테스트하고 말았잖아", "그게 문제라고", "a/b/g/n/ac/be/bn설정 변경은 안되나?",
"연결된 컴퓨터들에 따라 윈도우,리눅스별로 해야할거야".

The driver's advanced properties (Intel: ``ChannelWidth24`` / ``ChannelWidth52`` /
``ChannelWidth6`` "20 MHz | Auto", ``IEEE11nMode`` "disabled | 802.11n | 802.11ac |
802.11ax", ``WirelessMode`` "802.11a | b | g | mixes"). Only what the card offers becomes
a case; the Auto/top value is the band's own case already. a/b/g alone = 11n/ac/ax off
plus the a/b/g item on that one standard. Setting goes through ``WinNicSpeed``'s elevated
helper (Windows asks once for both); the values before the first change are kept on disk
so a restart puts them back. After a change the card joins again by itself; the case is
measured only when Windows says it is joined as asked: the standard the Wi-Fi API reports
(its PHY type; ``netsh`` text answers in the PC's language), and for a width cap a receive
rate that width can have.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.blocks.win_nic_speed import WinNicSpeed

JOIN_WAIT_S = 45.0  # the card resets after a property change and joins again
WIDTH_KEYS = {"ChannelWidth24": "2.4GHz", "ChannelWidth52": "5GHz", "ChannelWidth6": "6GHz"}
MODE_KEY = "IEEE11nMode"  # 802.11n/ac/ax(/be) mode; its "disabled" leaves a/b/g only
LEGACY_KEY = "WirelessMode"  # 802.11a/b/g mode, used with 11n/ac/ax off
# the most a link of this width carries (802.11ax, 2 streams, top MCS, 0.8 us guard)
WIDTH_MAX_MBPS = {20: 286.8, 40: 573.5, 80: 1201.0, 160: 2402.0}
MODES = ("802.11n", "802.11ac", "802.11ax", "802.11be", "802.11bn")
LEGACY = {"802.11a": "5GHz", "802.11b": "2.4GHz", "802.11g": "2.4GHz"}  # alone, its band

READ = r"""
$n = [string]$d.nic
@(Get-NetAdapterAdvancedProperty -Name $n -ErrorAction Stop | ForEach-Object {
  [pscustomobject]@{ key = [string]$_.RegistryKeyword; value = [string](@($_.RegistryValue)[0])
    values = @($_.ValidRegistryValues | ForEach-Object { [string]$_ })
    names = @($_.ValidDisplayValues | ForEach-Object { [string]$_ }) } }) |
  ConvertTo-Json -Compress -Depth 3
"""

Read = Callable[[str], list[dict[str, Any]]]  # nic -> its advanced properties
Cards = Callable[[], list[dict[str, Any]]]  # the Wi-Fi API's cards (win_wifi._wlan_cards)


def _cards() -> list[dict[str, Any]]:
    from core.blocks.win_wifi import _wlan_cards

    return _wlan_cards()


def joined(cards: list[dict[str, Any]], nic: str) -> dict[str, Any]:
    """What the card is joined with: connected, standard, channel, band, receive rate."""
    c = next((x for x in cards if x.get("nic") == nic), None)
    if c is None:
        return {}
    ch = c.get("channel")
    band = None if not isinstance(ch, int) else "2.4GHz" if ch <= 14 else "5GHz"
    return {"connected": c.get("state") == "connected", "ssid": c.get("ssid"),
            "mode": c.get("mode"), "channel": ch, "band": band,
            "rx_mbps": c.get("rx_mbps")}  # fmt: skip


def _pairs(p: dict[str, Any]) -> list[tuple[str, str]]:
    names, values = list(p.get("names") or []), list(p.get("values") or [])
    if len(names) != len(values):
        return []  # the driver's lists do not pair up: nothing is guessed
    return [(unicodedata.normalize("NFKC", str(n)), str(v))
            for n, v in zip(names, values, strict=True)]  # fmt: skip


def _mode_in(name: str, modes: Any) -> str | None:
    """The one standard a value names ("3. 802.11ac" -> 802.11ac); None for a mix."""
    if re.search(r"802\.11[a-z]+/[a-z]", name):
        return None  # "802.11b/g", "802.11a/g": a mix, not one standard
    hits = [m for m in modes if re.search(rf"{re.escape(m)}(?![a-z])", name)]
    return hits[0] if len(hits) == 1 else None


def options(props: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The cases a card offers: each width cap below Auto per band, each mode below its
    top, and a/b/g alone when the card can turn 11n/ac/ax off. {"axis", "band" (only for
    one band), "set" [(key, value)], "label", "mhz" / "mode"}."""
    out: list[dict[str, Any]] = []
    by_key = {str(p.get("key")): p for p in props}
    for key, band in WIDTH_KEYS.items():
        for name, value in _pairs(by_key.get(key, {})):
            m = re.search(r"(\d+)\s*MHz", name, re.I)
            if m and int(m.group(1)) in WIDTH_MAX_MBPS:
                out.append({"axis": "width", "band": band, "set": [(key, value)],
                            "mhz": int(m.group(1)),
                            "label": f"width capped at {m.group(1)} MHz"})  # fmt: skip
    found = [(mode, value) for name, value in _pairs(by_key.get(MODE_KEY, {}))
             if (mode := _mode_in(name, MODES))]  # fmt: skip
    top = max((MODES.index(m) for m, _ in found), default=-1)
    for mode, value in found:
        if MODES.index(mode) < top:  # the top one is the band's own case
            out.append({"axis": "mode", "set": [(MODE_KEY, value)], "mode": mode,
                        "label": f"mode capped at {mode}"})  # fmt: skip
    off = next((v for n, v in _pairs(by_key.get(MODE_KEY, {}))
                if re.search(r"disable|비활성|off", n, re.I)), None)  # fmt: skip
    if off is not None:
        for name, value in _pairs(by_key.get(LEGACY_KEY, {})):
            mode = _mode_in(name, LEGACY)
            if mode:
                out.append({"axis": "mode", "band": LEGACY[mode], "mode": mode,
                            "set": [(MODE_KEY, off), (LEGACY_KEY, value)],
                            "label": f"{mode} only"})  # fmt: skip
    return out


def fits(option: dict[str, Any], band: str) -> bool:
    """IEEE 802.11 rules (throughput.md 1.2 table): a width cap or a/b/g alone is for its
    own band; 11ac is 5 GHz only."""
    if option.get("band"):
        return bool(option["band"] == band)
    return not (option.get("mode") == "802.11ac" and band != "5GHz")


def mismatch(option: dict[str, Any], got: dict[str, Any]) -> str | None:
    """Why what the card joined with is not the case asked, or None."""
    if option["axis"] == "mode":
        mode = str(got.get("mode") or "")
        if mode != option["mode"]:
            return f"it joined with {mode or 'an unknown mode'}, not {option['mode']}"
        return None
    rate = got.get("rx_mbps")
    top = WIDTH_MAX_MBPS.get(int(option.get("mhz") or 0))
    if isinstance(rate, int | float) and top is not None and rate > top + 1:
        return f"it receives at {rate:g} Mbps, more than a {option['mhz']} MHz link can"
    return None


class WinWifiCaps:
    """Width and mode cases of this PC's Wi-Fi card; custody on disk."""

    def __init__(self, custody: Path, helper: WinNicSpeed, read: Read | None = None,
                 cards: Cards = _cards, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:  # fmt: skip
        self.custody, self.helper, self.cards = custody, helper, cards
        self.read = read or self._read
        self.sleep, self.clock = sleep, clock

    def _read(self, nic: str) -> list[dict[str, Any]]:
        got = self.helper.ps(READ, {"nic": nic})
        return [x for x in (got if isinstance(got, list) else [got]) if isinstance(x, dict)]

    def options(self, nic: str, band: str) -> list[dict[str, Any]]:
        try:
            return [o for o in options(self.read(nic)) if fits(o, band)]
        except (OSError, ValueError, subprocess.SubprocessError):
            return []

    def now(self, nic: str) -> dict[str, Any]:
        return joined(self.cards(), nic)

    # ------------------------------------------------------------------ custody

    def _kept(self) -> dict[str, dict[str, str]]:
        try:
            return dict(json.loads(self.custody.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {}

    def _keep(self, data: dict[str, dict[str, str]]) -> None:
        data = {k: v for k, v in data.items() if v}
        if not data:
            self.custody.unlink(missing_ok=True)
            return
        self.custody.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.custody.with_name(self.custody.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.custody)

    def _props(self, nic: str) -> dict[str, dict[str, Any]]:
        return {str(p["key"]): p for p in self.read(nic)}

    # ------------------------------------------------------------------ changing

    def set_option(self, nic: str, option: dict[str, Any],
                   net: dict[str, Any] | None = None) -> dict[str, Any]:  # fmt: skip
        """Set one case's values, wait for the card to join again, read what it joined
        with. What the case before changed goes back first (one axis at a time)."""
        want = {str(k): str(v) for k, v in option["set"]}
        try:
            props = self._props(nic)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            return {"readback_ok": False, "why": f"cannot read {nic}: {e}"}
        kept = self._kept()
        mine = kept.setdefault(nic, {})
        for key in want:
            if key not in mine and key in props:  # the value before the first change, once
                mine[key] = str(props[key].get("value"))
        self._keep(kept)
        target = {**{k: v for k, v in mine.items() if k not in want}, **want}
        for key, value in target.items():
            if str(props.get(key, {}).get("value")) != value:
                why = self.helper._apply(nic, value, key)
                if why:
                    return {"readback_ok": False, "why": why}
        return self._joined(nic, target, option)

    def _joined(self, nic: str, want: dict[str, str],
                option: dict[str, Any] | None) -> dict[str, Any]:  # fmt: skip
        end = self.clock() + JOIN_WAIT_S
        got: dict[str, Any] = {}
        back = False
        while True:
            try:
                props = self._props(nic)
                back = all(str(props.get(k, {}).get("value")) == v for k, v in want.items())
            except (OSError, ValueError, subprocess.SubprocessError):
                back = False
            got = self.now(nic)
            if back and got.get("connected") and got.get("rx_mbps"):
                break
            if self.clock() >= end:
                break
            self.sleep(1.0)
        out: dict[str, Any] = {
            "readback_ok": back,
            "link": bool(got.get("connected")),
            "band": got.get("band"),
            "mode": got.get("mode"),
            "channel": got.get("channel"),
            "rx_mbps": got.get("rx_mbps"),
        }
        why = None
        if not back:
            why = f"the card does not read the values set ({', '.join(want)})"
        elif not out["link"]:
            why = f"the card did not join again within {JOIN_WAIT_S:g} s"
        elif option is not None:
            why = mismatch(option, got)
        if why:
            out["why"] = why
        return out

    def restore(self, nic: str) -> dict[str, Any]:
        """Every value of this card back as it was before the test."""
        kept = self._kept()
        mine = dict(kept.get(nic) or {})
        if not mine:
            return {"ok": True, "why": "nothing was changed"}
        try:
            now = self._props(nic)
        except (OSError, ValueError, subprocess.SubprocessError):
            now = {}
        changed = False
        for key, value in mine.items():
            if str(now.get(key, {}).get("value")) != value:
                why = self.helper._apply(nic, value, key)
                if why:
                    return {"ok": False, "why": why}
                changed = True
        last = self._joined(nic, mine, None) if changed else {"readback_ok": True}
        if not last.get("readback_ok"):
            return {"ok": False, "why": last.get("why") or "not back"}
        kept.pop(nic, None)
        self._keep(kept)
        return {"ok": True, **{k: last[k] for k in ("mode", "band", "rx_mbps") if k in last}}

    def restore_all(self) -> dict[str, dict[str, Any]]:
        return {f"{nic} Wi-Fi settings": self.restore(nic) for nic in list(self._kept())}
