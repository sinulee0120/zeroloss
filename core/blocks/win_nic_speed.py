"""A Windows wired card's speed and duplex for a measuring case, and back as it was.

The same contract as ``nic_speed.NicSpeed`` (Linux, ethtool) for a Windows PC: the
driver's "Speed & Duplex" advanced property (``*SpeedDuplex``). Its values are the
driver's own (``ValidRegistryValues`` beside ``ValidDisplayValues``, e.g. "100 Mbps
반이중" / "100 Mbps Half Duplex" / "1.0 Gbps 전이중"); one exact value is set, read
back, and the link is waited for; the value before the first change is kept on disk so
a restart still puts it back (reference: realtest nic/windows.py).

Reading needs no rights. Setting needs an administrator: a small helper PowerShell is
started elevated once (Windows asks the person once, "Allow changes?") and only sets
``*SpeedDuplex`` on a named card to one of that card's own values. It ends by itself
when the node that started it is gone. Declined = the case says so, nothing changes.

2026-10-17 user: "controller의 LAN설정을 바꿔서도 해야할거 아냐 그래야 모든 LAN,WAN포트
확인을 하지", "참고하라고 준 코드는 제대로 확인도 안하고".
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import subprocess
import time
import unicodedata
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.blocks.nic_speed import Mode
from core.blocks.process_runner import NO_WINDOW

KEYWORD = "*SpeedDuplex"
# the only settings the elevated helper may change: the wired speed, and a Wi-Fi card's
# channel width and 802.11n/ac/ax mode (win_wifi_caps; Intel's names)
ALLOWED = (KEYWORD, "ChannelWidth24", "ChannelWidth52", "ChannelWidth6", "IEEE11nMode",
           "WirelessMode")  # fmt: skip
LINK_WAIT_S = 45.0  # reference link_timeout_seconds: a Windows driver renegotiates slowly
HELPER_WAIT_S = 60.0  # the person reads the Windows question
STEP_WAIT_S = 30.0  # one setting by the helper
PS_TIMEOUT_S = 20.0

Ps = Callable[[str, dict[str, Any]], Any]  # (script, data) -> parsed JSON

_AUTO = ("auto", "자동")
_HALF = ("half", "반이중")
_FULL = ("full", "전이중")
_SPEED = re.compile(r"(\d+(?:[.,]\d+)?)\s*(g|m)(?:bps|b)", re.I)

READ = r"""
$p = Get-NetAdapterAdvancedProperty -Name $d.nic -RegistryKeyword '*SpeedDuplex' -ErrorAction Stop
$a = Get-NetAdapter -Name $d.nic -ErrorAction Stop
[pscustomobject]@{
  value = [string](@($p.RegistryValue)[0]); shown = [string]$p.DisplayValue
  values = @($p.ValidRegistryValues | ForEach-Object { [string]$_ })
  names = @($p.ValidDisplayValues | ForEach-Object { [string]$_ })
  up = ([string]$a.MediaConnectionState -eq 'Connected'); speed = [int64]$a.Speed
  full = [bool]$a.FullDuplex } | ConvertTo-Json -Compress
"""

HELPER = r"""
param([string]$Dir, [int]$Owner)
$ErrorActionPreference = 'Stop'
while ($true) {
  if (-not (Get-Process -Id $Owner -ErrorAction SilentlyContinue)) { break }
  if (Test-Path (Join-Path $Dir 'stop')) { break }
  foreach ($f in @(Get-ChildItem -Path $Dir -Filter 'req-*.json' -ErrorAction SilentlyContinue)) {
    $out = Join-Path $Dir ($f.Name -replace '^req-', 'res-')
    try {
      $r = Get-Content -Raw -Encoding UTF8 $f.FullName | ConvertFrom-Json
      $k = [string]$r.keyword
      if ($k -eq '') { $k = '*SpeedDuplex' }
      if (@(__ALLOWED__) -notcontains $k) { throw 'not a setting this helper may change' }
      $p = Get-NetAdapterAdvancedProperty -Name ([string]$r.nic) -RegistryKeyword $k
      if (@($p.ValidRegistryValues) -notcontains [string]$r.value) {
        throw 'not one of the card''s own values' }
      Set-NetAdapterAdvancedProperty -Name ([string]$r.nic) -RegistryKeyword $k `
        -RegistryValue ([string]$r.value) -Confirm:$false
      '{"ok":true}' | Set-Content -Encoding UTF8 $out
    } catch {
      @{ ok = $false; why = [string]$_.Exception.Message } | ConvertTo-Json -Compress |
        Set-Content -Encoding UTF8 $out
    }
    Remove-Item $f.FullName -Force -ErrorAction SilentlyContinue
  }
  Start-Sleep -Milliseconds 200
}
"""


def _ps(script: str, data: dict[str, Any]) -> Any:
    """Run a read-only PowerShell with ``$d`` = data (Base64 JSON: names never become
    PowerShell source); the parsed JSON it prints."""
    payload = base64.b64encode(json.dumps(data).encode("utf-8")).decode()
    head = (
        "$ErrorActionPreference='Stop';[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        f"$d=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{payload}'))|"
        "ConvertFrom-Json;"
    )
    cmd = base64.b64encode((head + script).encode("utf-16-le")).decode()
    p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", cmd],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=PS_TIMEOUT_S,
                       creationflags=NO_WINDOW)  # fmt: skip
    if p.returncode != 0:
        raise OSError((p.stderr or p.stdout).strip().splitlines()[-1] if (p.stderr or p.stdout)
                      else f"PowerShell exit {p.returncode}")  # fmt: skip
    return json.loads(p.stdout)


def parse_option(shown: str, value: str) -> Mode | None:
    """One driver value as a speed case; None for Auto or what it does not say."""
    text = unicodedata.normalize("NFKC", shown).lower()
    if any(m in text for m in _AUTO):
        return None
    m = _SPEED.search(text.replace(" ", "")) or _SPEED.search(text)
    if m is None:
        return None
    scale = 1000 if m.group(2).lower() == "g" else 1
    mbps = round(float(m.group(1).replace(",", ".")) * scale)
    half = any(x in text for x in _HALF)
    if mbps >= 1000 and half:
        return None  # 1000 Mbps and up is one mode (nic_speed.GIGABIT)
    return Mode(mbps, "half" if half else "full", value)


class WinNicSpeed:
    """``NicSpeed`` for Windows: modes, set one, restore; custody on disk."""

    def __init__(self, custody: Path, ps: Ps = _ps,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 elevate: Callable[[Path], str | None] | None = None) -> None:  # fmt: skip
        self.custody, self.ps, self.sleep, self.clock = custody, ps, sleep, clock
        self.dir = custody.with_name("win-nic-helper")
        self.elevate = elevate or self._start_helper
        self._declined = False

    # ------------------------------------------------------------------ reading

    def read(self, nic: str) -> dict[str, Any]:
        got = self.ps(READ, {"nic": nic})
        return got if isinstance(got, dict) else {}

    def supported(self, nic: str) -> list[Mode]:
        try:
            r = self.read(nic)
        except (OSError, ValueError, subprocess.SubprocessError):
            return []
        names, values = list(r.get("names") or []), list(r.get("values") or [])
        if len(names) != len(values):
            return []  # the driver's lists do not pair up: nothing is guessed (reference)
        modes = [parse_option(str(n), str(v)) for n, v in zip(names, values, strict=True)]
        seen: dict[tuple[int, str], Mode] = {}
        for m in modes:
            if m is not None:
                seen.setdefault((m.speed, m.duplex), m)
        return sorted(seen.values(), key=lambda m: (m.speed, m.duplex != "full"))

    # ------------------------------------------------------------------ custody

    def _kept(self) -> dict[str, Any]:
        try:
            return dict(json.loads(self.custody.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {}

    def _keep(self, data: dict[str, Any]) -> None:
        self.custody.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.custody.with_name(self.custody.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.custody)

    # ------------------------------------------------------------------ changing

    def set_mode(self, nic: str, mode: Mode) -> dict[str, Any]:
        try:
            now = self.read(nic)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            return {"readback_ok": False, "link": False, "why": f"cannot read {nic}: {e}"}
        kept = self._kept()
        if nic not in kept:  # the value before the first change, once
            speed = int(now.get("speed") or 0) // 1_000_000 if now.get("up") else None
            kept[nic] = {"value": str(now.get("value")), "speed": speed}
            self._keep(kept)
        why = self._apply(nic, mode.name)
        if why:
            return {"readback_ok": False, "link": False, "why": why}
        return self._settle(nic, mode.name, mode)

    def restore(self, nic: str) -> dict[str, Any]:
        kept = self._kept()
        before = kept.get(nic)
        if before is None:
            return {"ok": True, "why": "nothing was changed"}
        try:
            now = self.read(nic)
        except (OSError, ValueError, subprocess.SubprocessError):
            now = {}
        if now.get("value") == str(before["value"]) and now.get("up"):
            # already as it was (e.g. the change itself never got the administrator's yes):
            # nothing to set, no administrator needed (2026-10-22: a false "not put back")
            del kept[nic]
            self._keep(kept)
            return {"ok": True, "why": "it was as before", "link": True,
                    "speed": int(now.get("speed") or 0) // 1_000_000 or None}  # fmt: skip
        why = self._apply(nic, str(before["value"]))
        if why:
            return {"ok": False, "why": why}
        got = self._settle(nic, str(before["value"]), None)
        was = before.get("speed")
        ok = bool(got.get("readback_ok")) and bool(got.get("link"))
        if ok and was and (got.get("speed") or 0) < was:
            ok, got["why"] = False, (f"{nic} came back at {got.get('speed')} Mbps; it was {was} "
                                     "Mbps before the test")  # fmt: skip
        if ok:
            del kept[nic]
            self._keep(kept)
        return {**got, "ok": ok}

    def restore_all(self) -> dict[str, dict[str, Any]]:
        return {nic: self.restore(nic) for nic in list(self._kept())}

    def _settle(self, nic: str, value: str, mode: Mode | None) -> dict[str, Any]:
        end = self.clock() + LINK_WAIT_S
        r: dict[str, Any] = {}
        while True:
            try:
                r = self.read(nic)
            except (OSError, ValueError, subprocess.SubprocessError):
                r = {}
            if r.get("value") == value and r.get("up") and r.get("speed"):
                break
            if self.clock() >= end:
                break
            self.sleep(1.0)
        speed = int(r.get("speed") or 0) // 1_000_000 if r.get("up") else None
        out: dict[str, Any] = {
            "readback_ok": r.get("value") == value, "link": bool(r.get("up")), "speed": speed,
            "duplex": ("full" if r.get("full") else "half") if r.get("up") else None,
        }  # fmt: skip
        if not out["readback_ok"]:
            out["why"] = f"the card reads {r.get('shown')!r}, not the value set"
        elif not out["link"]:
            out["why"] = f"the link did not come up within {LINK_WAIT_S:g} s"
        return out

    # ------------------------------------------------------------------ the helper

    def _apply(self, nic: str, value: str, keyword: str = KEYWORD) -> str | None:
        """Set one value through the elevated helper; None = done, else why not."""
        if self._declined:
            return "administrator approval was declined (Windows asked once)"
        if not self._helper_alive():
            why = self.elevate(self.dir)
            if why:
                self._declined = True
                return why
        rid = uuid.uuid4().hex
        req = self.dir / f"req-{rid}.json"
        res = self.dir / f"res-{rid}.json"
        tmp = self.dir / f"tmp-{rid}"
        tmp.write_text(json.dumps({"nic": nic, "value": value, "keyword": keyword}),
                       encoding="utf-8")  # fmt: skip
        tmp.replace(req)
        end = self.clock() + STEP_WAIT_S
        while self.clock() < end:
            if res.exists():
                try:
                    got = json.loads(res.read_text(encoding="utf-8-sig"))
                except ValueError:
                    self.sleep(0.2)
                    continue
                res.unlink(missing_ok=True)
                return None if got.get("ok") else f"the card did not take it: {got.get('why')}"
            self.sleep(0.2)
        req.unlink(missing_ok=True)
        return "the administrator helper did not answer"

    def _helper_alive(self) -> bool:
        beat = self.dir / "alive"
        try:
            return time.time() - beat.stat().st_mtime < 5.0
        except OSError:
            return False

    def _start_helper(self, folder: Path) -> str | None:
        """Windows asks the person once ("Allow changes?"); None = it runs."""
        folder.mkdir(parents=True, exist_ok=True)
        for f in folder.glob("*"):
            f.unlink(missing_ok=True)
        script = folder / "helper.ps1"
        allowed = ",".join(f"'{k}'" for k in ALLOWED)
        body = HELPER.replace("__ALLOWED__", allowed).replace("  Start-Sleep -Milliseconds 200",
                              "  (Get-Date).ToString() | Set-Content (Join-Path $Dir 'alive')\n"
                              "  Start-Sleep -Milliseconds 200")  # fmt: skip
        script.write_text(body, encoding="utf-8-sig")
        args = (f"-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"{script}\" "
                f"-Dir \"{folder}\" -Owner {os.getpid()}")  # fmt: skip
        try:
            import ctypes

            shell32 = getattr(ctypes, "windll").shell32  # Windows only  # noqa: B009
            rc = shell32.ShellExecuteW(None, "runas", "powershell.exe", args, None, 0)
        except (AttributeError, OSError) as e:
            return f"cannot ask Windows for administrator approval: {e}"
        if int(rc) <= 32:  # 5 = declined
            return "administrator approval was declined (Windows asked once)"
        end = self.clock() + HELPER_WAIT_S
        while self.clock() < end:
            if self._helper_alive():
                return None
            self.sleep(0.5)
        return "the administrator helper did not start"

    def prepare(self) -> str | None:
        """Ask Windows now, while the person who pressed Start is there (2026-10-29: asked
        10 minutes in, nobody answered for 2 minutes, Windows took it as No and every LAN
        case was skipped). None = the helper runs."""
        if self._helper_alive():
            return None
        why = self.elevate(self.dir)
        self._declined = bool(why)
        return why

    def allow_again(self) -> None:
        """The person chose to try again (Action Required): Windows asks once more."""
        self._declined = False

    def close(self) -> None:
        """The run ended: the helper stops (it also stops when this node is gone)."""
        with contextlib.suppress(OSError):
            (self.dir / "stop").write_text("", encoding="utf-8")
        self._declined = False
