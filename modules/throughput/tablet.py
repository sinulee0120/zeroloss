"""A tablet in the throughput test (docs/modules/throughput.md T-4, 2026-10-29 user:
"구현부터 다 해" — the tablets measured like the PCs, one after another).

The tablet's app measures against the same WAN-side ``iperf3 -s`` (its own iperf3 client,
over the Wi-Fi network only); the PC it hangs on by USB runs its case list with the same
``ClientJob`` as a PC's: ``run`` takes the iperf3 command the job built and has the app do
it, then hands back iperf3's own JSON shape, so judging and records are the PC's. A tablet
has no Wi-Fi setting an app may change: its case is the band it is on, both directions.
"""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Callable
from typing import Any

from core.blocks.adb import AdbError
from core.blocks.tablets import TabletError, Tablets, parse_ssid, width_from_rate

POLL_S = 1.0
APP_WAIT_S = 20.0


def iperf_args(argv: list[str]) -> dict[str, Any]:
    """The parts of an iperf3 client command the app does: server, port, time, -R."""

    def after(flag: str) -> str | None:
        i = argv.index(flag) + 1 if flag in argv else len(argv)
        return argv[i] if i < len(argv) else None

    return {"server": after("-c"), "port": int(after("-p") or 5201),
            "seconds": int(float(after("-t") or 10)), "reverse": "-R" in argv,
            "udp": "-u" in argv}  # fmt: skip


class TabletIperf:
    """``run_iperf`` and ``read_link`` of a ClientJob, done by a tablet's app over adb."""

    def __init__(self, tablets: Tablets, serial: str,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:  # fmt: skip
        self.tablets, self.serial, self.sleep, self.clock = tablets, serial, sleep, clock
        self.live: Callable[[dict[str, Any] | None], None] = lambda _: None
        self._ready = False
        self._saved: set[str] = set()  # networks the tablet kept before the test
        self._added: list[str] = []  # band networks the test added (forgotten after)

    def ready(self) -> str | None:
        """This build's app installed and started, its API answering; None = ready."""
        try:
            self.tablets.ensure_app(self.serial)
            self.tablets.start_app(self.serial)
        except TabletError as e:
            return e.why
        end = self.clock() + APP_WAIT_S
        while True:
            try:
                self.tablets.call(self.serial, "GET", "/v1/health")
                return None
            except TabletError as e:
                if self.clock() >= end:
                    return e.why
            self.sleep(1.0)

    # ------------------------------------------------------------------ Wi-Fi bands
    # (client.WifiControl: the band networks the person picked for this tablet, joined by
    # adb one after another, then back on its own network; 2026-10-29 user: "2G 5G 6G변경도
    # 해야하네"). Width and mode: no app or adb may change them; recorded as joined.

    def connection_of(self, nic: str) -> str | None:
        try:
            self._saved = set(self.tablets.saved_networks(self.serial))
            return parse_ssid(self.tablets.adb.shell(self.serial, ["cmd", "wifi", "status"]))
        except (TabletError, AdbError):
            return None

    def join_band(self, nic: str, ssid: str, bssid: str, security: str = "Open",
                  password: str = "") -> dict[str, Any]:  # fmt: skip
        try:
            got = self.tablets.join_wifi(self.serial, ssid, security, password)
        except TabletError as e:
            if "not found" not in e.why and ssid not in self._saved:
                self._added.append(ssid)  # it reached the tablet: a network may be saved
            return {"why": f"{e.what}: {e.why}"}
        if ssid not in self._saved:
            self._added.append(ssid)
        return {"ip": got.get("ip"), "ssid": ssid, **self.wifi()}

    def back(self, nic: str, connection: str | None) -> dict[str, Any]:
        if not self._added:  # nothing joined (2026-10-29: the tablet was off USB): as it was
            return {"back": True, "why": "nothing was changed"}
        got = self.tablets.back_wifi(self.serial, connection, list(self._added))
        if got.get("back"):
            self._added = []
        return got

    def read_link(self, bind: str) -> tuple[str, int | None, str | None]:
        """The tablet's Wi-Fi: its receive rate stands for the link (values only, no pass
        level on Wi-Fi)."""
        try:
            got = self.tablets.wifi_status(self.serial)
        except TabletError:
            return f"{self.serial} Wi-Fi", None, None
        rate = got.get("rx_mbps")
        return f"{self.serial} Wi-Fi", int(rate) if rate else None, None

    def wifi(self) -> dict[str, Any]:
        """What the tablet is joined with, for the case record (band, standard, width)."""
        try:
            got = self.tablets.wifi_status(self.serial)
        except TabletError:
            return {}
        freq = int(got.get("freq") or 0)
        band = "2.4GHz" if 0 < freq < 3000 else "5GHz" if 0 < freq < 5925 else (
            "6GHz" if freq else None)  # fmt: skip
        return {"band": band, "mode": got.get("std"), "rx_mbps": got.get("rx_mbps"),
                "width_mhz": width_from_rate(got.get("std"), got.get("rx_mbps"))}  # fmt: skip

    def run(self, argv: list[str], limit_s: float) -> str:
        """One direction: the app runs it; iperf3's JSON shape comes back."""
        if not self._ready:
            why = self.ready()
            if why:
                return json.dumps({"error": f"the tablet app is not ready: {why}"})
            self._ready = True
        a = iperf_args(argv)
        if a["udp"]:
            return json.dumps({"error": "UDP is not measured on a tablet (TCP only)"})
        params = {k: a[k] for k in ("server", "port", "seconds", "reverse")}
        try:
            self.tablets.call(self.serial, "POST", "/v1/throughput/start", params)
        except TabletError as e:
            return json.dumps({"error": f"the tablet did not start: {e.why}"})
        end = self.clock() + limit_s
        t0, prev = self.clock(), 0
        try:
            while self.clock() < end:
                self.sleep(POLL_S)
                try:
                    st = self.tablets.call(self.serial, "GET", "/v1/throughput/status")
                except TabletError:
                    continue  # a USB hiccup: the run goes on in the tablet
                if st.get("state") == "running":
                    moved = int(st.get("moved_bytes") or 0)
                    mbps = (moved - prev) * 8 / POLL_S / 1e6
                    prev = moved
                    self.live({"direction": "WAN→LAN" if a["reverse"] else "LAN→WAN",
                               "mbps": round(mbps, 1), "s": int(self.clock() - t0)})  # fmt: skip
                    continue
                r = st.get("result") or {}
                if not r.get("ok"):
                    return json.dumps({"error": str(r.get("error") or "no result")})
                bps = float(r.get("bits_per_second") or 0)
                side = {"bits_per_second": bps, "bytes": r.get("bytes"),
                        "seconds": r.get("seconds")}  # fmt: skip
                return json.dumps({"end": {"sum_received": side, "sum_sent": side}})
            with contextlib.suppress(TabletError):
                self.tablets.call(self.serial, "POST", "/v1/throughput/stop")
            return json.dumps({"error": f"no result within {limit_s:g} s"})
        finally:
            self.live(None)
