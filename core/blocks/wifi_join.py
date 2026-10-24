"""Read and join the test Wi-Fi on this PC (docs/03-features.md F17 "무선 수신 PC").

One job: say which Wi-Fi NICs this PC has (SSID, IP, MAC) and connect one of them to
the test Wi-Fi. Linux only for now (NetworkManager): one profile ``zeroloss-test-wifi``
is made again on every join, with ``never-default`` so the Wi-Fi never takes the
default route (the wired control link stays). The password never goes on a command
line: ``nmcli connection up`` reads it from a 0600 file that is removed at once. It is
never logged, returned or put in an error text. Windows comes with V18.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.blocks.link_monitor import ipv4_of
from core.protocol.envelope import ZlError

PROFILE = "zeroloss-test-wifi"
KEY_MGMT = {"WPA2": "wpa-psk", "WPA3": "sae"}  # 16 S-51 security -> NetworkManager
CMD_TIMEOUT_S = 10.0
JOIN_TIMEOUT_S = 30.0  # 03 F17: wait at most 30 s for an address
SCAN_TIMEOUT_S = 25.0  # a full rescan of 2.4, 5 and 6 GHz
BSSID = re.compile(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}")


def band_of(mhz: int) -> str:
    """2.4GHz / 5GHz / 6GHz from a channel's centre frequency."""
    if mhz and mhz < 3000:
        return "2.4GHz"
    if mhz and mhz < 5925:
        return "5GHz"
    return "6GHz" if mhz else ""


Runner = Callable[[list[str], float], tuple[int, str]]


def run_command(argv: list[str], timeout_s: float) -> tuple[int, str]:
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False,
            stdin=subprocess.DEVNULL, errors="replace",
        )  # fmt: skip
    except FileNotFoundError:
        return 127, f"{argv[0]} not found"
    except (OSError, subprocess.SubprocessError) as e:
        return 1, type(e).__name__
    return done.returncode, (done.stdout + done.stderr).strip()


@dataclass(frozen=True)
class WifiNic:
    nic: str
    mac: str | None
    ssid: str | None
    ip: str | None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class WifiJoin:
    def __init__(
        self,
        run: Runner = run_command,
        sys_root: Path = Path("/sys/class/net"),
        ip_of: Callable[[str], str | None] = ipv4_of,
        tmp_dir: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rfkill_root: Path = Path("/sys/class/rfkill"),
    ) -> None:
        self.run = run
        self.rfkill_root = rfkill_root
        self.sys_root = sys_root
        self.ip_of = ip_of
        self.tmp_dir = tmp_dir
        self.sleep = sleep

    @property
    def supported(self) -> bool:
        return sys.platform.startswith("linux")

    def nics(self) -> list[WifiNic]:
        if not self.supported or not self.sys_root.is_dir():
            return []
        out = []
        for d in sorted(self.sys_root.iterdir()):
            if not (d / "wireless").exists():
                continue
            out.append(
                WifiNic(d.name, _read(d / "address"), self._ssid(d.name), self.ip_of(d.name))
            )
        return out

    def status(self) -> dict[str, Any]:
        """02 4 ``GET /v1/wifi``. With what holds a card off (2026-10-15 GAP-157: the
        sender PC's Wi-Fi would not come on and nobody could look at it from here):
        ``rfkill`` (soft = software switch, hard = the key or the BIOS), ``radio`` (the
        NetworkManager Wi-Fi switch) and each card's ``operstate`` and ``nm_state``. Read
        only: nothing is switched."""
        nics = [n.to_dict() for n in self.nics()]
        if not self.supported:
            return {"supported": False, "nics": nics, "rfkill": [], "radio": None}
        states = self._nm_states()
        for n in nics:
            n["operstate"] = _read(self.sys_root / n["nic"] / "operstate")
            n["nm_state"] = states.get(n["nic"])
        return {"supported": True, "nics": nics, "rfkill": self._rfkill(),
                "radio": self._radio()}  # fmt: skip

    def _rfkill(self) -> list[dict[str, Any]]:
        out = []
        for d in sorted(self.rfkill_root.iterdir()) if self.rfkill_root.is_dir() else []:
            kind = _read(d / "type")
            if kind not in ("wlan", "wwan", "bluetooth"):
                continue
            soft, hard = _read(d / "soft") == "1", _read(d / "hard") == "1"
            out.append({"name": _read(d / "name"), "type": kind, "soft": soft, "hard": hard})
        return out

    def _radio(self) -> str | None:
        rc, out = self.run(["nmcli", "-t", "radio", "wifi"], CMD_TIMEOUT_S)
        return out.strip() if rc == 0 and out.strip() else None

    def _nm_states(self) -> dict[str, str]:
        rc, out = self.run(["nmcli", "-t", "-f", "DEVICE,STATE,CONNECTION", "device"],
                           CMD_TIMEOUT_S)  # fmt: skip
        found = {}
        for line in out.splitlines() if rc == 0 else []:
            parts = line.split(":")
            if len(parts) >= 2:
                found[parts[0]] = ":".join(parts[1:]).rstrip(":")
        return found

    def join(
        self, ssid: str, security: str, password: str, nic: str | None = None,
        timeout_s: float = JOIN_TIMEOUT_S, bssid: str | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        """Connect and wait for an address. Raises ZlError RCV-023 (no password inside)."""
        if not self.supported:
            raise _fail("Wi-Fi connecting is not supported on this PC yet", "Windows comes later.")
        nics = self.nics()
        use = next((n for n in nics if n.nic == nic), None) if nic else (nics[0] if nics else None)
        if use is None:
            raise _fail(f"No Wi-Fi network card {nic or ''}".strip(), "This PC has none.")
        if security != "Open" and security not in KEY_MGMT:
            raise _fail("Unknown Wi-Fi security", str(security))
        clean = _Clean(password)
        self.run(["nmcli", "connection", "delete", "id", PROFILE], CMD_TIMEOUT_S)  # may be absent
        # hidden yes: ask for the name itself, so a hidden SSID (set-top box ones often are) is
        # found too; a visible one works the same (GAP-101, nothing to ask the user)
        add = ["nmcli", "connection", "add", "type", "wifi", "con-name", PROFILE,
               "ifname", use.nic, "ssid", ssid, "802-11-wireless.hidden", "yes",
               "connection.autoconnect", "yes",
               "ipv4.never-default", "yes", "ipv6.never-default", "yes"]  # fmt: skip
        if security != "Open":
            add += ["wifi-sec.key-mgmt", KEY_MGMT[security]]
        if bssid:  # one access point = one band, when the SSID is the same on all of them
            if not BSSID.fullmatch(bssid):
                raise _fail("Not an access point address", repr(bssid))
            add += ["802-11-wireless.bssid", bssid]
        rc, out = self.run(add, CMD_TIMEOUT_S)
        if rc != 0:
            raise _fail("Cannot make the Wi-Fi profile", clean(out), rc)
        up = ["nmcli", "--wait", str(int(timeout_s)), "connection", "up", "id", PROFILE,
              "ifname", use.nic]  # fmt: skip
        secret = None
        try:
            if security != "Open":
                secret = self._secret_file(password)
                up += ["passwd-file", str(secret)]
            rc, out = self.run(up, timeout_s + CMD_TIMEOUT_S)
        finally:
            if secret is not None:
                secret.unlink(missing_ok=True)
        if rc != 0:
            why, fix = self._explain(out, ssid, security, use, timeout_s)
            self._drop_profile()
            raise _fail(f"Cannot connect to Wi-Fi {ssid!r}", clean(why), rc, clean(fix))
        end = time.monotonic() + timeout_s
        while True:
            now = next((n for n in self.nics() if n.nic == use.nic), None)
            if now and now.ssid == ssid and now.ip:
                return {"nic": now.nic, "ssid": ssid, "ip": now.ip, "joined": True}
            if time.monotonic() >= end:
                self._drop_profile()
                raise _fail(f"No address on Wi-Fi {ssid!r} in {timeout_s:g} s",
                            "Connected, but the router gave no address.")  # fmt: skip
            self.sleep(0.5)

    def scan(self, nic: str) -> list[dict[str, Any]]:
        """The networks ``nic`` sees now: SSID, BSSID, band (2.4GHz / 5GHz / 6GHz),
        channel frequency, signal %, and "Open" or its security. Read only (the person
        picks what to test: 2026-10-17 user "와이파이도 카드 뭐로 할건지 사용자가 정할거야")."""
        if not self.supported:
            return []
        rc, out = self.run(["nmcli", "-t", "-f", "SSID,BSSID,FREQ,SIGNAL,SECURITY", "device",
                            "wifi", "list", "--rescan", "yes", "ifname", nic],
                           SCAN_TIMEOUT_S)  # fmt: skip
        found = []
        for line in out.splitlines() if rc == 0 else []:
            parts = [p.replace("\\:", ":") for p in re.split(r"(?<!\\):", line)]
            if len(parts) < 5 or not parts[0]:
                continue  # a hidden network has no name to pick
            ssid, bssid, freq, signal, sec = parts[0], parts[1], parts[2], parts[3], parts[4]
            mhz = int(re.sub(r"\D", "", freq) or 0)
            security = "Open" if sec.strip() in ("", "--") else sec.strip()
            found.append({"ssid": ssid, "bssid": bssid.upper(), "freq_mhz": mhz,
                          "band": band_of(mhz), "signal": int(signal or 0),
                          "security": security})  # fmt: skip
        return sorted(found, key=lambda x: (x["band"], -x["signal"]))

    def connection_of(self, nic: str) -> str | None:
        """The NetworkManager connection ``nic`` is on now (to go back to it)."""
        rc, out = self.run(["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", nic],
                           CMD_TIMEOUT_S)  # fmt: skip
        name = out.strip() if rc == 0 else ""
        return name or None

    def back(self, nic: str, connection: str | None,
             timeout_s: float = JOIN_TIMEOUT_S) -> dict[str, Any]:  # fmt: skip
        """``nic`` back on ``connection`` (as before the test); our profile goes."""
        self._drop_profile()
        if not connection or connection == PROFILE:
            return {"nic": nic, "back": False, "why": "it was on no network before"}
        rc, out = self.run(["nmcli", "--wait", str(int(timeout_s)), "connection", "up", "id",
                            connection, "ifname", nic], timeout_s + CMD_TIMEOUT_S)  # fmt: skip
        if rc != 0:
            return {"nic": nic, "back": False, "why": out.strip()[-200:]}
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            ip = self.ip_of(nic)
            if ip:
                return {"nic": nic, "back": True, "ip": ip}
            self.sleep(0.5)
        return {"nic": nic, "back": False, "why": "no address after going back"}

    def _drop_profile(self) -> None:
        """A failed join leaves nothing behind (G-11): with autoconnect on, the profile
        would keep taking the card and retrying (real case: the card left the network it
        was on and sat in "connecting" — GAP-103)."""
        self.run(["nmcli", "connection", "delete", "id", PROFILE], CMD_TIMEOUT_S)

    def _explain(
        self, out: str, ssid: str, security: str, use: WifiNic, timeout_s: float = JOIN_TIMEOUT_S
    ) -> tuple[str, str]:
        """Why a failed ``nmcli connection up`` failed, in words (03 F17, GAP-101/103)."""
        text = out.lower()
        mac = use.mac or "?"
        timed_out = "timeout expired" in text  # nmcli --wait ran out (real case: assoc reject)
        if "took too long" in text or "association" in text or timed_out:
            why = (
                f"The connection did not finish in {timeout_s:g} s: the router refused or did "
                "not answer this PC's connection request."
                if timed_out
                else "The router did not answer the connection request (association timed out)."
            )
            fix = (f"Check the router for this Wi-Fi: MAC filter or a limit on devices "
                   f"(this PC's Wi-Fi MAC is {mac}).")  # fmt: skip
            if security != "Open":
                fix = f"Check the test Wi-Fi password (zeroloss wifi set). {fix}"
            return why, fix
        if "secrets were required" in text or "no secrets" in text:
            return "The router refused the password.", _FIX
        if "could not be found" in text or "no network with ssid" in text:
            seen = self._seen_security(use.nic, ssid)
            if seen is not None and (seen == "Open") != (security == "Open"):
                return (
                    f"The router has {ssid!r} with {seen} security, the test Wi-Fi says "
                    f"{security}.",
                    "Set the same security (zeroloss wifi set).",
                )  # fmt: skip
            if seen is None:
                return (
                    f"{ssid!r} is not seen by this PC.",
                    "Check the router's Wi-Fi is on and in range and the name is exact "
                    "(a hidden name is fine).",
                )  # fmt: skip
        return out, _FIX

    def _seen_security(self, nic: str, ssid: str) -> str | None:
        """The security this PC sees for ``ssid`` ("Open" or e.g. "WPA2 WPA3"), None if
        not in the scan list (a hidden SSID may not be named there). Right after a failed
        attempt the card may still be busy: a second, fresh scan 2 s later (real case)."""
        for rescan in ("auto", "yes"):
            if rescan == "yes":
                self.sleep(2.0)
            rc, out = self.run(["nmcli", "-t", "-f", "SSID,SECURITY", "device", "wifi", "list",
                                "--rescan", rescan, "ifname", nic], CMD_TIMEOUT_S)  # fmt: skip
            if rc != 0:
                continue
            for line in out.splitlines():
                name, _, sec = line.replace("\\:", "\0").rpartition(":")
                if name.replace("\0", ":") == ssid:
                    sec = sec.replace("\0", ":").strip()
                    return "Open" if sec in ("", "--") else sec
        return None

    def _ssid(self, nic: str) -> str | None:
        rc, out = self.run(["iw", "dev", nic, "link"], 2.0)
        m = re.search(r"^\s*SSID: (.+)$", out, re.M) if rc == 0 else None
        return m.group(1) if m else None

    def _secret_file(self, password: str) -> Path:
        fd, name = tempfile.mkstemp(prefix="zl-wifi-", dir=self.tmp_dir)  # 0600
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"802-11-wireless-security.psk:{password}\n")
        return Path(name)


class _Clean:
    """Takes the password out of any text before it is shown (never trust tool output)."""

    def __init__(self, password: str) -> None:
        self.password = password

    def __call__(self, text: str) -> str:
        text = text[-300:]
        return text.replace(self.password, "***") if self.password else text


_FIX = "Check the test Wi-Fi name and password (zeroloss wifi set) or connect the PC by hand."


def _fail(what: str, why: str, rc: int | None = None, fix: str = _FIX) -> ZlError:
    return ZlError("RCV-023", what, why, fix, detail=f"exit {rc}" if rc is not None else "")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None
