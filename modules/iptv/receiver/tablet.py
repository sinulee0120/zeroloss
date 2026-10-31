"""A tablet as an IPTV receiver (01-overview D3, iptv 3.5): the app on the tablet joins,
measures and plays; this side starts and stops it over adb and pulls its 1-second records.

The records have the same short keys as a PC receiver's (02 7), so they are written,
summarized and judged by the same code. The app keeps the last hour of records itself:
when the USB cable is out for a while (TAB-001), the pull catches up when it is back and
nothing is lost. One tablet = one channel; up to three tablets, on the same channel or on
different ones, each its own TabletReceiver keyed by its device name.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.blocks.tablets import TabletError, Tablets, width_from_rate
from core.events.bus import Event, EventBus, Level
from core.records.recorder import Recorder
from modules.iptv import record_keys as k
from modules.iptv.summary_rules import make_summarizer, shape

PULL_EVERY_S = 1.0
APP_WAIT_S = 20.0  # a just started app opens its API within a few seconds
SETTLED_OK_S = 2  # 05 2.1-2: playback "ok" this many seconds in a row ends warm-up
SETTLE_MAX_S = 5  # ... or this long after the picture began, whatever it shows
FRONT_EVERY_S = 10  # how often the app is asked whether its screen is in front (TAB-016)
APP_KEYS_DROPPED = ("type", "seq", "e")  # the app's own numbering; ours starts at measuring
# iptv R-25: the tablet's receive counters (hd nd ne ue ub, as on a PC) read through adb
# with each pull; their rise goes on the newest second of that pull (at most a second off,
# the cell sums them)
COUNTER_KEYS = ("hd", "nd", "ne", "ue", "ub")
WIFI_EVERY_S = 10  # the standard and width (wst wbw) are read this often, and at start


@dataclass(frozen=True)
class TabletConfig:
    run_id: str
    device: str  # "tablet-1".."tablet-3"
    serial: str
    ch: int
    group: str
    port: int
    build: str
    cell_s: int = 300
    play: bool = True  # S-49
    buffer_ms: int = 1000  # S-25 or this tablet's own (02 plan devices.receivers[].buffer_ms)


@dataclass(frozen=True)
class HeldWifi:
    """The network the tablet stays on for the test (TAB-019): the one picked for it, or
    the one it was on at Start. With its security and password when ZeroLoss joined it,
    so it can be joined again; none known for a network the person joined by hand."""

    ssid: str
    security: str | None = None
    password: str = ""

    def __repr__(self) -> str:  # never the password in a log
        return f"HeldWifi({self.ssid!r}, {self.security!r})"


class TabletReceiver:
    def __init__(
        self,
        cfg: TabletConfig,
        data_dir: Path,
        bus: EventBus,
        tablets: Tablets,
        wait: Callable[[float], bool] | None = None,
        held: HeldWifi | None = None,
    ) -> None:
        self.cfg = cfg
        self.held = held
        self.bus = bus
        self.tablets = tablets
        self.records_dir = data_dir / "raw" / cfg.device
        self.summaries: list[dict[str, Any]] = []
        self.last_1s: dict[str, Any] | None = None
        self.lost_total = 0
        self.measuring = False
        self.state = "IDLE"
        self._after = 0  # the app's record seq already taken
        self._e = 0  # our elapsed second, from the first measured record
        self._recorder: Recorder | None = None
        self._summarizer = make_summarizer(cfg.cell_s)
        self._halt = threading.Event()
        self._wait = wait or self._halt.wait
        self._sleep = time.sleep
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._away = False  # the tablet does not answer (TAB-001), said once per absence
        self._behind = False  # the app's screen is not in front (TAB-016), said once
        self._ticks = 0
        self._ok_run = 0  # warm-up: seconds of playback "ok" in a row
        self._showing_s = 0  # warm-up: seconds since the player showed a picture
        self._uids: set[int] = set()
        self._counted: dict[str, int] | None = None  # the last read, cumulative
        self._rise: dict[str, int] = {}  # risen since, not yet on a second
        self._wifi: dict[str, Any] = {}  # wst / wbw, put on every second until read again
        self._off: str | None = None  # the network Android moved it to (TAB-019), said once

    # ---------------------------------------------------------------- start / stop

    def start(self) -> None:
        c = self.cfg
        params = {"group": c.group, "port": c.port, "ch": c.ch, "play": c.play,
                  "buffer_ms": c.buffer_ms}  # fmt: skip
        try:
            # the picture needs the app in front (2026-10-23: started from the home screen,
            # it measured but showed nothing, pb "none"); measuring goes on behind anyway
            self.tablets.ensure_app(c.serial)  # this build's app (Update PCs brought it)
            self._pause_scans()
            self._hold_wifi()
            self.tablets.start_app(c.serial)
            self._wait_app()
            self.tablets.call(c.serial, "POST", "/v1/receiver/start", params)
            status = self.tablets.call(c.serial, "GET", "/v1/records?after=999999999")
        except TabletError as e:
            raise _start_error(c, e) from e
        self._after = int(status.get("last_seq") or 0)  # records from before this start stay
        try:
            self._uids = self.tablets.app_uids(c.serial)
        except TabletError:
            self._uids = set()  # no socket drops then; the other counters still come
        self.state = "RUNNING"
        self._thread = threading.Thread(target=self._pull_loop, name=f"iptv-{c.device}",
                                        daemon=True)  # fmt: skip
        self._thread.start()
        self._event("ZL-INFO", "info", f"{c.device}: receiving CH{c.ch} on the tablet")

    def _pause_scans(self) -> None:
        """TAB-018 (2026-10-24 user: "시험 중에만 끄고 끝나면 복구"): the apps that scan for
        Wi-Fi on a timer are paused for the test; a tablet that refuses still runs."""
        try:
            paused = self.tablets.pause_timer_scans(self.cfg.serial)
        except TabletError as e:
            self._event("ZL-TAB-018", "warn", f"{self.cfg.device}: {e.what}: {e.why}. {e.fix}")
            return
        if paused:
            self._event("ZL-INFO", "info", f"{self.cfg.device}: timer Wi-Fi scan paused for "
                        f"the test ({', '.join(paused)})")  # fmt: skip

    def _hold_wifi(self) -> None:
        """TAB-019 (2026-10-31 user: "설정에서 내가 일부러 IPTV로 설정 바꿨으면 그대로 테스트동안
        유지하도록 adb로 고쳐"): Android does not leave the held network by itself during the
        test; a tablet that refuses still runs, watched every WIFI_EVERY_S."""
        if self.held is None:
            return
        try:
            changed = self.tablets.hold_wifi(self.cfg.serial, self.held.ssid)
        except TabletError as e:
            self._event("ZL-TAB-019", "warn", f"{self.cfg.device}: {e.what}: {e.why}. It is "
                        f"watched and said if it leaves {self.held.ssid}")  # fmt: skip
            return
        text = f"{self.cfg.device}: kept on Wi-Fi {self.held.ssid} for the test"
        self._event("ZL-INFO", "info", text + (f" ({', '.join(changed)})" if changed else ""))
        if self.held.security is None:  # joined by hand with a password ZeroLoss lacks
            again = f"pick {self.held.ssid} in its row with its password"
            self._event("ZL-INFO", "info", f"{self.cfg.device}: to have it joined again "
                        f"if it leaves, {again}")  # fmt: skip

    def _release_wifi(self) -> None:
        for r in self.tablets.release_wifi(self.cfg.serial):
            if r["ok"]:
                self._event("ZL-INFO", "info", f"{self.cfg.device}: Wi-Fi setting put back "
                            f"({r['what'].split(' ', 1)[-1]})")  # fmt: skip
            else:
                self._event("ZL-TAB-019", "warn", f"{self.cfg.device}: {r['what']} not put "
                            f"back ({r['detail']}); Reset All tries again")  # fmt: skip

    def _resume_scans(self) -> None:
        back = self.tablets.resume_timer_scans(self.cfg.serial)
        done = [r["what"].split(" ", 1)[-1] for r in back if r["ok"]]
        if done:  # the report shows the tablet was left as it was (receiverPC 2026-10-24)
            text = f"{self.cfg.device}: timer Wi-Fi scan put back ({', '.join(done)})"
            self._event("ZL-INFO", "info", text)
        for r in back:
            if not r["ok"]:
                self._event("ZL-TAB-018", "warn", f"{self.cfg.device}: {r['what']} not put "
                            f"back ({r['detail']}); Reset All tries again")  # fmt: skip

    def _wait_app(self) -> None:
        """2026-10-24 first tablet Start: the app had just been updated and launched, its
        API was not listening yet and the start gave up (TAB-017). It is asked until it
        answers, up to ``APP_WAIT_S``."""
        deadline = time.monotonic() + APP_WAIT_S
        while True:
            try:
                self.tablets.call(self.cfg.serial, "GET", "/v1/health")
                return
            except TabletError:
                if time.monotonic() >= deadline:
                    raise
                self._sleep(1.0)

    def stop(self) -> None:
        self._halt.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        try:
            self._pull()  # the last seconds the app closed
            self.tablets.call(self.cfg.serial, "POST", "/v1/receiver/stop")
        except TabletError as e:
            self._event("ZL-TAB-017", "warn", f"{self.cfg.device}: stop on the tablet: {e.why}")
        self._resume_scans()
        self._release_wifi()
        with self._lock:
            self.summaries += [self._shape(p) for p in self._summarizer.flush()]
        if self._recorder is not None:
            self._recorder.close("stop")
        self.state = "IDLE"

    def take_summaries(self) -> list[dict[str, Any]]:
        with self._lock:
            out, self.summaries = self.summaries, []
            return out

    # ---------------------------------------------------------------- records

    def _pull_loop(self) -> None:
        while not self._wait(PULL_EVERY_S):
            try:
                self._pull()
            except TabletError as e:
                if not self._away:
                    self._away = True
                    text = f"{self.cfg.device}: the tablet does not answer ({e.why})"
                    self._event("ZL-TAB-001", "error", text)
                continue
            if self._away:
                self._away = False
                self._event("ZL-INFO", "info", f"{self.cfg.device}: the tablet answers again")
            if self._ticks % WIFI_EVERY_S == 0:
                self._read_wifi()
            self._ticks += 1
            if self.cfg.play and self._ticks % FRONT_EVERY_S == 0:
                self._keep_in_front()

    def _keep_in_front(self) -> None:
        """TAB-016: behind another screen the app measures on but shows no picture (pb
        "freeze"/"none"); it is brought back once per time it went behind, and said."""
        try:
            front = self.tablets.call(self.cfg.serial, "GET", "/v1/status").get("in_front")
            if front is False and not self._behind:
                self._behind = True
                text = f"{self.cfg.device}: the app went behind another screen, brought back"
                self._event("ZL-TAB-016", "warn", text)
                self.tablets.start_app(self.cfg.serial)
            elif front:
                self._behind = False
        except TabletError:
            pass  # not answering is TAB-001, said by the pull

    def _pull(self) -> None:
        got = self.tablets.call(self.cfg.serial, "GET", f"/v1/records?after={self._after}")
        self._count()
        records = got.get("records") or []
        for i, r in enumerate(records):
            self._after = max(self._after, int(r.get("seq") or 0))
            if i == len(records) - 1 and self.measuring:
                r = {**r, **{key: int(r.get(key) or 0) + n for key, n in self._rise.items()}}
                self._rise = {}
            self._take(r)

    def _read_wifi(self) -> None:
        """2026-10-24 user ("11ax로 붙었는지 bandwidth는 80Mhz인지 … 정보가 하나도 없네")."""
        try:
            got = self.tablets.wifi_status(self.cfg.serial)
        except TabletError:
            return
        width = width_from_rate(got.get("std"), got.get("rx_mbps"))
        self._wifi = {key: v for key, v in ((k.WIFI_STD, got.get("std")), (k.WIFI_BW, width))
                      if v is not None}  # fmt: skip
        self._watch_wifi(got.get("ssid"))

    def _watch_wifi(self, on: str | None) -> None:
        """TAB-019: off the held network is said once, and it is joined again when ZeroLoss
        knows how (it joined it); back on it is said too."""
        held = self.held
        if held is None:
            return
        if on == held.ssid:
            if self._off is not None:
                self._off = None
                self._event("ZL-INFO", "info", f"{self.cfg.device}: back on Wi-Fi {held.ssid}")
            return
        if self._off is None:
            self._off = on or "no network"
            how = "joining it again" if held.security else (
                f"put it back on {held.ssid} on the tablet (Settings, Wi-Fi)")  # fmt: skip
            self._event("ZL-TAB-019", "warn", f"{self.cfg.device}: left Wi-Fi {held.ssid}, now "
                        f"on {self._off}; {how}")  # fmt: skip
        if held.security:
            with contextlib.suppress(TabletError):  # said above; tried again at the next read
                self.tablets.rejoin(self.cfg.serial, held.ssid, held.security, held.password)

    def _count(self) -> None:
        """Read the counters and keep their rise since the last read (a counter that went
        down, as when the app's socket was made again, starts over)."""
        try:
            now = self.tablets.net_counters(self.cfg.serial, self.cfg.port, self._uids)
        except TabletError:
            return
        if self._counted is not None and self.measuring:
            for key in COUNTER_KEYS:  # written even when 0: measured, not missing (02 7)
                if key in now and key in self._counted:
                    up = max(0, now[key] - self._counted[key])
                    self._rise[key] = self._rise.get(key, 0) + up
        self._counted = now

    def _take(self, r: dict[str, Any]) -> None:
        """One second from the app. Measuring starts at the first second with packets and
        the PAT seen, and with playback on once it has settled as on a PC (05 2.1-2: "ok"
        two seconds in a row, at the latest 5 s after the picture began). 2026-10-24: both
        tablets' first 3-4 s were the player filling its buffer ("freeze") and coloured
        the first cell as a playback problem."""
        if not self.measuring:
            if int(r.get(k.RECEIVED, 0) or 0) <= 0 or not r.get(k.PAT_SEEN):
                return
            if self.cfg.play and not self._settled(r.get(k.PLAYBACK)):
                return
            self.measuring = True
            c = self.cfg
            header = {"ch": c.ch, "group": f"{c.group}:{c.port}", "serial": c.serial}
            self._recorder = Recorder(self.records_dir, c.run_id, c.device, c.build, header)
            self._event("ZL-INFO", "info", f"{self.cfg.device}: warm-up done, measuring")
        rec = {key: v for key, v in r.items() if key not in APP_KEYS_DROPPED}
        rec = {"e": self._e, **self._wifi, **rec}
        if int(rec.get(k.WIFI_SCANS) or 0) and int(rec.get(k.LOST) or 0):  # R-25
            rec[k.SCAN_LOST] = int(rec[k.LOST])
        self._e += 1
        self.lost_total += int(rec.get(k.LOST, 0) or 0)
        assert self._recorder is not None
        self._recorder.write_second(rec["e"], {key: v for key, v in rec.items() if key != "e"})
        closed = self._summarizer.add(rec)
        with self._lock:
            self.last_1s = rec
            self.summaries += [self._shape(p) for p in closed]

    def _settled(self, pb: Any) -> bool:
        if pb not in (None, "none"):
            self._showing_s += 1  # the player has shown a picture
        self._ok_run = self._ok_run + 1 if pb == "ok" else 0
        return self._ok_run >= SETTLED_OK_S or self._showing_s > SETTLE_MAX_S

    def _shape(self, piece: dict[str, Any]) -> dict[str, Any]:
        return shape(piece, None, False, playback=self.cfg.play)

    def status(self) -> dict[str, Any]:
        return {"id": self.cfg.device, "serial": self.cfg.serial, "ch": self.cfg.ch,
                "state": self.state, "measuring": self.measuring, "away": self._away,
                "lost_total": self.lost_total, "last_1s": self.last_1s}  # fmt: skip

    def _event(self, code: str, level: Level, text: str) -> None:
        self.bus.publish(Event(code, level, text, data={"module": "iptv",
                                                         "device": self.cfg.device}))  # fmt: skip


def _start_error(c: TabletConfig, e: TabletError) -> Exception:
    from core.protocol.envelope import ZlError

    return ZlError("TAB-017", f"{c.device}: could not start receiving on the tablet", e.why, e.fix)
