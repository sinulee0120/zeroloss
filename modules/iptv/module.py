"""IPTV module on a node (docs/12-architecture.md 2, docs/02-data.md 4).

Routes under /v1/modules/iptv/ start and stop the sender and the PC receiver; the
heartbeat carries their 1-second values; 5-minute summaries go to the node's store.
The node is RUNNING while any session runs and IDLE when all have stopped. Safe stop
closes receivers first, then senders (stop order, 05 2.1-2 rule 5).
"""

from __future__ import annotations

import functools
import threading
from pathlib import Path
from typing import Any

from core.blocks.tablets import TabletError
from core.blocks.unicode_path import find_file
from core.events.bus import Event
from core.lifecycle.state_flow import CONTROLLER_RESTARTED, State
from core.protocol.envelope import Request, ZlError
from core.protocol.http_server import Query
from core.records.recorder import recover
from modules.iptv.defaults import DEFAULTS
from modules.iptv.probe import (
    LISTEN_S_MAX,
    SEND_S_MAX,
    Listener,
    probe_command,
    send_probe,
    wait_prime,
)
from modules.iptv.receiver.perf import PerfCheck
from modules.iptv.receiver.session import ReceiverConfig, ReceiverSession
from modules.iptv.receiver.tablet import HeldWifi, TabletConfig, TabletReceiver
from modules.iptv.sender.commands import DEFAULT_TTL, QUALITIES
from modules.iptv.sender.prepare import Preparer
from modules.iptv.sender.prime import PrimedSender, PrimeFlow, prime_payload
from modules.iptv.sender.session import SenderConfig, SenderSession
from modules.iptv.sender.sources import digest, legacy_digest, list_sources, safe_name
from modules.iptv.warmup import JOIN_WAIT_S, START_MAX_S

PREFIX = "/v1/modules/iptv"
ACTIVE = (State.STARTING, State.RUNNING, State.FAULT)


def _need(params: dict[str, Any], name: str, kind: type) -> Any:
    value = params.get(name)
    if not isinstance(value, kind) or isinstance(value, bool):
        raise ZlError(
            "DSC-012", "Request rejected", detail=f"params.{name} must be {kind.__name__}"
        )
    return value


class IptvModule:
    def __init__(self) -> None:
        self.node: Any = None
        # (ch, flow): flow -1 = multicast, 0/1 = primed analyzer/player copy (iptv 4.5)
        self.senders: dict[tuple[int, int], SenderSession | PrimedSender] = {}
        self.receivers: dict[int, ReceiverSession] = {}
        # tablets on this PC's USB (01-overview D3): one to three, by device name, on the
        # same channel or on different ones; the app on each tablet receives and plays
        self.tablet_rx: dict[str, TabletReceiver] = {}
        self._lock = threading.Lock()  # one change at a time (single queue rule)
        self._digests: dict[tuple[str, int, int], set[str]] = {}  # per file version
        # Summaries of stopped receivers (the last partial cell is made at stop) wait
        # here until the node collects them; dropping them would lose the final cell.
        self._pending: list[dict[str, Any]] = []
        self.preparer: Preparer | None = None
        self.listener = Listener()  # F17 step 5 multicast test
        self.perf: PerfCheck | None = None  # F17 step 4 PC speed (RCV-015)

    # ---------------------------------------------------------------- contract

    def attach(self, node: Any) -> None:
        self.node = node
        self._recover_records()
        self.preparer = Preparer(node.module_dir("iptv", "cache"), node.bus, node.procs_dir)
        self.perf = PerfCheck(node.module_dir("iptv", "cache"), node.procs_dir,
                              idle_cpu=lambda: node.resources.read().cpu_pct)  # fmt: skip
        node.api.command(f"{PREFIX}/sender/start", self.sender_start)
        node.api.command(f"{PREFIX}/sender/stop", self.sender_stop)
        node.api.command(f"{PREFIX}/sender/prepare", self.prepare)
        node.api.command(f"{PREFIX}/sender/prepare/cancel", self.prepare_cancel)
        node.api.command(f"{PREFIX}/receiver/start", self.receiver_start)
        node.api.command(f"{PREFIX}/receiver/stop", self.receiver_stop)
        node.api.command(f"{PREFIX}/receiver/channel", self.receiver_channel)
        node.api.command(f"{PREFIX}/tablet/start", self.tablet_start)
        node.api.command(f"{PREFIX}/tablet/stop", self.tablet_stop)
        node.api.command(f"{PREFIX}/sender/probe", self.sender_probe)
        node.api.command(f"{PREFIX}/receiver/perfcheck", self.perfcheck)
        node.api.get(f"{PREFIX}/sender/sources", self.sources)
        node.api.command(f"{PREFIX}/receiver/perfcheck/cancel", self.perfcheck_cancel)
        node.api.command(f"{PREFIX}/receiver/desktop", self.desktop_check)
        node.api.command(f"{PREFIX}/receiver/probe", self.receiver_probe)
        node.api.command(f"{PREFIX}/receiver/probe/result", self.receiver_probe_result)
        node.module_data["iptv"] = self.status
        node.summary_sources.append(self._take_summaries)
        # a crash or power loss during a tablet test: put back what it had paused (TAB-018)
        tablets = getattr(node, "tablets", None)
        if tablets is not None:
            threading.Thread(target=tablets.resume_timer_scans, name="iptv-scans-back",
                             daemon=True).start()  # fmt: skip
            threading.Thread(target=tablets.release_wifi, name="iptv-wifi-back",
                             daemon=True).start()  # TAB-019 as well  # fmt: skip

    def safe_stop(self, reason: str) -> None:
        # a video being made stops too (iptv 1-1-2) — except when only the Controller came
        # back while nothing sends or receives: closing and opening the window (which ends
        # an idle service) must not throw away a movie half made (GAP-124)
        idle_restart = reason == CONTROLLER_RESTARTED and not self.senders and not self.receivers
        if self.preparer is not None and not idle_restart:
            self.preparer.cancel()
        if self.perf is not None:
            self.perf.cancel()  # a PC speed test too (RCV-015)
        with self._lock:
            order: list[SenderSession | PrimedSender | ReceiverSession] = [
                *self.receivers.values(),  # receivers close first (stop order)
                *self.senders.values(),
            ]
            for t in self.tablet_rx.values():  # tablets are receivers too: first
                if t.state == "RUNNING":
                    t.stop()
                self._keep_summaries(t)
            self.tablet_rx.clear()
            for session in order:
                if session.flow.state in ACTIVE:
                    session.stop()
            for r in self.receivers.values():
                self._keep_summaries(r)
            self.receivers.clear()
            self.senders.clear()

    def restore_settings(self) -> list[dict[str, Any]]:
        """Reset All (node /v1/reset): what the tablets had paused for a test is put
        back (TAB-018), also after a crash left it so."""
        tablets = getattr(self.node, "tablets", None)
        if tablets is None:
            return []
        return [*tablets.resume_timer_scans(), *tablets.release_wifi()]  # TAB-018, TAB-019

    def clean_check(self) -> list[str]:
        """F11 Reset All (02 4 ``/v1/reset``): what is still running after the stop."""
        left = (
            [f"CH{ch} sender" for ch, _ in self.senders]
            + [f"CH{ch} receiver" for ch in self.receivers]
            + [f"{device} receiver" for device in self.tablet_rx]
        )
        if self.preparer is not None and self.preparer.status().get("busy"):
            left.append("a video is still being made")
        if self.listener.busy():
            left.append("a multicast test is still listening")
        if self.perf is not None and self.perf.busy():
            left.append("a PC speed test is still running")
        return left

    # ---------------------------------------------------------------- commands

    def sender_start(self, req: Request) -> dict[str, Any]:
        p = req.params
        ch, muxrate = _need(p, "ch", int), _need(p, "muxrate_bps", int)
        prime = _prime_flow(p.get("prime"), req.run_id)
        file, source = self._source(p, ch, muxrate)
        cfg = SenderConfig(
            ch=ch,
            file=file,
            source=source,
            dest_ip="0.0.0.0" if prime else _need(p, "dest_ip", str),  # primed: from the prime
            port=0 if prime else _need(p, "port", int),
            muxrate_bps=muxrate,
            ttl=int(p.get("ttl", DEFAULT_TTL)),
            local_address=p.get("local_address"),
        )
        key = (ch, prime.flow if prime else -1)
        with self._lock:
            if key in self.senders and self.senders[key].flow.state in ACTIVE:
                raise ZlError("RUN-012", f"CH{cfg.ch} is already sending")
            run_dir = self._run_dir(req)
            session: SenderSession | PrimedSender = (
                PrimedSender(cfg, prime, run_dir, self.node.bus, self.node.procs_dir)
                if prime else SenderSession(cfg, run_dir, self.node.bus, self.node.procs_dir)
            )  # fmt: skip
            self._node_starting()
            try:
                session.start()  # primed: returns at once, sends when the prime comes
            except ZlError:
                self._node_settle()
                raise
            self.senders[key] = session
            self._node_settle()
            return session.status() | {"ssrc": session.ssrc}

    def prepare(self, req: Request) -> dict[str, Any]:
        """Make the generated videos that are missing (iptv 1-1-2); returns at once."""
        p = req.params
        quality = _need(p, "quality", str)
        if quality not in QUALITIES:
            raise ZlError("DSC-012", "Request rejected", detail="params.quality must be UHD or HD")
        muxrate = _need(p, "muxrate_bps", int)
        chans = p.get("channels")
        if not isinstance(chans, list) or not all(isinstance(c, dict) for c in chans):
            raise ZlError("DSC-012", "Request rejected", detail="params.channels must be a list")
        out, todo, sources = [], [], {}
        for c in chans:
            ch = _need(c, "ch", int)
            file = c.get("file")
            if isinstance(file, str) and file and find_file(Path(file)) is not None:
                out.append({"ch": ch, "state": "ready", "source": "file"})
                continue
            todo.append(ch)
            media = self._media_file(ch, c.get("source"))
            if media is not None:
                sources[ch] = media
        assert self.preparer is not None
        only = bool(p.get("only", False))  # the latest choice: other making stops (GAP-116)
        whole = p.get("length") == "whole"  # S-55: the whole movie
        for r in self.preparer.request(quality, muxrate, todo, sources, only=only, whole=whole):
            out.append(r | {"source": "media" if r["ch"] in sources else "generated"})
        return {"channels": sorted(out, key=lambda r: r["ch"])}

    def sources(self, _: Query) -> dict[str, Any]:
        """02 4 ``GET sender/sources``: the S-48 media folder (iptv 1-1), each file with the
        made videos it has (``made``: start at once) and ``generated_made`` for the
        generated one (2026-10-17)."""
        out = list_sources(self.media_dir)
        made = self.preparer.made() if self.preparer is not None else []
        for f in out["files"]:
            tags = self._tags(self.media_dir / str(f.get("name")))
            f["made"] = [{k: v for k, v in m.items() if k != "digest"}
                         for m in made if m["digest"] in tags]  # fmt: skip
        out["generated_made"] = [
            {k: v for k, v in m.items() if k != "digest"} for m in made if m["digest"] is None
        ]
        return out

    def _tags(self, path: Path) -> set[str]:
        """The digests a made file of this original may carry (content, and the name +
        change time of files made before 2026-10-17); read once per file version."""
        try:
            st = path.stat()
        except OSError:
            return set()
        k = (str(path), st.st_size, st.st_mtime_ns)
        if k not in self._digests:
            try:
                self._digests[k] = {f"f{digest(path)}", f"f{legacy_digest(path)}"}
            except OSError:
                return set()
        return self._digests[k]

    @property
    def media_dir(self) -> Path:
        """Where users put their videos (iptv 1-1): kept on removal (user data)."""
        path = Path(self.node.data_dir) / "media"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _media_file(self, ch: int, name: Any, tell: bool = True) -> Path | None:
        """The S-48 file of a channel, or None (Generated). A missing one falls back to
        the generated video with SND-001 (iptv 1-1), never stops the start."""
        if name in (None, "", "Generated"):
            return None
        found = find_file(self.media_dir / str(name)) if safe_name(name) else None
        if found is None and tell:
            self.node.bus.publish(Event(
                "ZL-SND-001", "warn", f"CH{ch} source {name} missing, using the generated video",
                data={"file": str(name), "why": "file missing", "ch": ch},
            ))  # fmt: skip
        return found

    def prepare_cancel(self, _: Request) -> dict[str, Any]:
        assert self.preparer is not None
        return {"cancelled": self.preparer.cancel()}

    def sender_stop(self, req: Request) -> dict[str, Any]:
        ch = _need(req.params, "ch", int)
        with self._lock:
            gone = [self.senders.pop(k) for k in sorted(self.senders) if k[0] == ch]
            for session in gone:  # every flow of the channel (primed: two)
                if session.flow.state in ACTIVE:
                    session.stop()
            self._node_settle()
            return {"ch": ch, "stopped": bool(gone)}

    def receiver_start(self, req: Request) -> dict[str, Any]:
        p = req.params
        cfg = ReceiverConfig(
            run_id=req.run_id or "R-unknown",
            device=str(p.get("device", "pc")),
            ch=_need(p, "ch", int),
            group=_need(p, "group", str),
            port=_need(p, "port", int),
            interface_ip=str(p.get("interface_ip", "0.0.0.0")),
            warmup_max_s=float(p.get("warmup_max_s", START_MAX_S)),
            cell_s=int(p.get("cell_s", 300)),
            build=self.node.build,
            playback=dict(p["playback"]) if isinstance(p.get("playback"), dict) else None,
            prime=_receiver_prime(p.get("prime"), req.run_id),
            evidence=p.get("evidence") is True,
        )
        with self._lock:
            if cfg.ch in self.receivers and self.receivers[cfg.ch].flow.state in ACTIVE:
                raise ZlError("RUN-012", f"CH{cfg.ch} is already being received")
            session = ReceiverSession(
                cfg, self._run_dir(req), self.node.bus, procs_dir=self.node.procs_dir,
                cpu_of=lambda: self.node.resources.read().cpu_pct,
                desktop=self.node.desktop if self.node.player_host == "desktop" else None,
                held_elsewhere=functools.partial(self._held_by_other, cfg.ch),
                wifi_of=self._wifi_of,
            )  # fmt: skip
            self._node_starting()
            sender_ip = (cfg.prime or {}).get("sender_ip")
            if sender_ip and cfg.interface_ip not in ("", "0.0.0.0"):
                self.node.reach_sender(str(sender_ip), cfg.interface_ip)  # GAP-158
            try:
                session.start()  # returns after join; warm-up continues on its thread
            except ZlError:
                self._node_settle()
                raise
            self.receivers[cfg.ch] = session
            self._node_settle()
            return {"ch": cfg.ch, "state": session.flow.state.value}

    def receiver_stop(self, req: Request) -> dict[str, Any]:
        ch = _need(req.params, "ch", int)
        with self._lock:
            session = self.receivers.pop(ch, None)
            if session is not None and session.flow.state in ACTIVE:
                session.stop()
            if session is not None:
                self._keep_summaries(session)
            startup = session.analyzer.startup.to_dict() if session and session.analyzer else None
            self._node_settle()
            return {"ch": ch, "stopped": session is not None, "startup": startup}

    def tablet_start(self, req: Request) -> dict[str, Any]:
        """A tablet on this PC's USB receives a channel (02 4, iptv 3.5)."""
        p = req.params
        buffer_ms = int(p.get("buffer_ms", DEFAULTS["S-25"]))
        cfg = TabletConfig(
            run_id=req.run_id or "R-unknown",
            device=_need(p, "device", str),
            serial=_need(p, "serial", str),
            ch=_need(p, "ch", int),
            group=_need(p, "group", str),
            port=_need(p, "port", int),
            build=self.node.build,
            cell_s=int(p.get("cell_s", 300)),
            play=p.get("play", True) is not False,
            buffer_ms=buffer_ms,
        )
        with self._lock:
            old = self.tablet_rx.get(cfg.device)
            if old is not None and old.state == "RUNNING":
                raise ZlError("RUN-012", f"{cfg.device} is already receiving")
            held = self._join_wifi(cfg.device, cfg.serial, p.get("bands"))
            t = TabletReceiver(cfg, self._run_dir(req), self.node.bus, self.node.tablets,
                               held=held)  # fmt: skip
            self._node_starting()
            try:
                t.start()
            except ZlError:
                self._node_settle()
                raise
            self.tablet_rx[cfg.device] = t
            self._node_settle()
            return {"device": cfg.device, "ch": cfg.ch, "state": t.state}

    def _join_wifi(self, device: str, serial: str, bands: Any) -> HeldWifi | None:
        """S-51 / TAB-014: the tablet onto the Wi-Fi picked for it (its password added by
        the Controller, kept in memory for this test only); a tablet already on it is left
        as it is. Returns the network it stays on for the test (TAB-019): that one, or
        the one it is on now when none was picked ("Wi-Fi: keep")."""
        band = bands[0] if isinstance(bands, list) and bands else None
        if not isinstance(band, dict) or not band.get("ssid"):
            try:  # none picked: the tablet stays on the Wi-Fi it is on
                on = self.node.tablets.wifi_status(serial).get("ssid")
            except TabletError:
                return None
            if not on:
                return None
            # an open network (the router's TV SSID) is joined again with no password
            # (receiverPC 2026-10-31: Android will not go back to it by itself)
            try:
                kind = self.node.tablets.saved_security(serial, str(on))
            except TabletError:
                kind = None
            return HeldWifi(str(on), "Open" if kind == "Open" else None)
        try:
            got = self.node.tablets.join_wifi(serial, str(band["ssid"]),
                                              str(band.get("security") or "Open"),
                                              str(band.get("password") or ""))  # fmt: skip
        except TabletError as e:
            raise ZlError("TAB-014", f"{device}: could not join {band['ssid']!r}", e.why,
                          e.fix) from e  # fmt: skip
        if got.get("joined"):
            self.node.bus.publish(Event("ZL-INFO", "info",
                                        f"{device}: joined Wi-Fi {got['ssid']} ({got.get('ip')})",
                                        data={"module": "iptv", "device": device}))  # fmt: skip
        return HeldWifi(str(band["ssid"]), str(band.get("security") or "Open"),
                        str(band.get("password") or ""))  # fmt: skip

    def tablet_stop(self, req: Request) -> dict[str, Any]:
        device = _need(req.params, "device", str)
        with self._lock:
            t = self.tablet_rx.pop(device, None)
            if t is not None:
                if t.state == "RUNNING":
                    t.stop()
                self._keep_summaries(t)
            self._node_settle()
            return {"device": device, "stopped": t is not None}

    def receiver_channel(self, req: Request) -> dict[str, Any]:
        """03 F19: one receiver to another channel (02 4). The answer comes at the first
        packet or the join failure; normal playback and the Leave check go to the zaps file."""
        p = req.params
        device = str(p.get("device", "pc"))
        with self._lock:
            mine = [
                r for r in self.receivers.values()
                if r.cfg.device == device and r.flow.state in ACTIVE
            ]  # fmt: skip
            if p.get("from_ch") is not None:
                from_ch = _need(p, "from_ch", int)
                mine = [r for r in mine if r.cfg.ch == from_ch]
            if len(mine) != 1:
                raise ZlError(
                    "RUN-012", f"{device}: {len(mine)} receivers match",
                    "A channel change needs exactly one running receiver of the device.",
                    "Give from_ch (the channel it receives now).",
                )  # fmt: skip
            session = mine[0]
        method = str(p.get("join_method", "A"))
        mode = str(p.get("mode", "manual"))
        if method not in ("A", "B") or mode not in ("manual", "auto"):
            raise ZlError("DSC-012", "Request rejected", detail="join_method A|B, mode manual|auto")
        timeout = p.get("success_timeout_s")
        return session.switch(
            _need(p, "ch", int), _need(p, "group", str), _need(p, "port", int),
            join_method=method, mode=mode, prime=_receiver_prime(p.get("prime"), req.run_id),
            timeout_s=float(timeout) if timeout is not None else None,
            wait_s=JOIN_WAIT_S + 2.0,
        )  # fmt: skip

    def _wifi_of(self, ip: str) -> dict[str, Any] | None:
        """The node's Wi-Fi reading of the card with this address (02 5 ``links``);
        None for a wired card or an unspecified address (GAP-104 3)."""
        for x in list(getattr(self.node, "links", []) or []):
            if x.ip == ip and x.wifi is not None:
                return dict(x.to_dict().get("wifi") or {})
        return None

    def _held_by_other(self, slot: int, group: str, port: int) -> bool:
        """Another receiver of this node still has the group joined: the router keeps
        sending it after our Leave, so the Leave check would be wrong (03 F19)."""
        return any(
            s != slot and r.flow.state in ACTIVE and (r.cfg.group, r.cfg.port) == (group, port)
            for s, r in list(self.receivers.items())
        )

    # ---------------------------------------------------------------- multicast test (F17 5)

    def sender_probe(self, req: Request) -> dict[str, Any]:
        p = req.params
        ch, seconds = _need(p, "ch", int), _seconds(p, SEND_S_MAX, 2)
        self._idle_only()
        pr = _prime_flow(p.get("prime"), req.run_id)
        if pr is not None:  # primed test: send to where the prime came from (iptv 4.5)
            wait = float(p.get("prime_wait_s", 5))
            dest, port = wait_prime(p.get("local_address"), pr.q, pr.run_id, ch, wait)
        else:
            dest, port = _need(p, "dest_ip", str), _need(p, "port", int)
        argv = probe_command(
            SenderConfig.tsp, dest, port, seconds, int(p.get("ttl", DEFAULT_TTL)),
            p.get("local_address"), pr.p if pr else None,
        )  # fmt: skip
        log = Path(self.node.data_dir) / "logs" / f"tsp_probe_ch{ch}.log"
        return send_probe(argv, self.node.procs_dir, log, seconds, ch)

    def receiver_probe(self, req: Request) -> dict[str, Any]:
        p = req.params
        ch = _need(p, "ch", int)
        self._idle_only()
        probe_id = self.listener.start(
            _need(p, "group", str), _need(p, "port", int),
            str(p.get("interface_ip", "0.0.0.0")), _seconds(p, LISTEN_S_MAX, 4),
            m2u=p.get("m2u") is True, prime=_probe_prime(p.get("prime"), req.run_id, ch),
        )  # fmt: skip
        return {"probe_id": probe_id}

    def perfcheck(self, req: Request) -> dict[str, Any]:
        """F17 step 4 (05 4.2-1, RCV-015): can this PC play the quality? Only warns."""
        p = req.params
        quality = _need(p, "quality", str)
        if quality not in QUALITIES:
            raise ZlError("DSC-012", "Request rejected", detail="params.quality must be UHD or HD")
        assert self.perf is not None
        return self.perf.check(quality, _need(p, "muxrate_bps", int), p.get("measure") is True)

    def desktop_check(self, req: Request) -> dict[str, Any]:
        """F17 step 4 (03 F20, RCV-010): with playback on, Agent Desktop must be there."""
        if getattr(self.node, "player_host", "self") != "desktop":
            return {"needed": False, "connected": None}
        wait_s = min(max(float(req.params.get("wait_s") or 0.0), 0.0), 10.0)
        if not self.node.desktop.wait_connected(wait_s):
            raise ZlError(
                "RCV-010", "Agent Desktop is not running on this PC",
                "The player needs the screen of a logged-in user (03 F20).",
                "Log in on this PC (automatic login is best), or turn PC playback off (S-49).",
            )  # fmt: skip
        return {"needed": True, "connected": True, "clears": ["ZL-RCV-010"]}

    def perfcheck_cancel(self, _: Request) -> dict[str, Any]:
        """Stop order / rolled-back start: a sample being made stops too (like prepare)."""
        assert self.perf is not None
        was = self.perf.busy()
        self.perf.cancel()
        return {"cancelled": was}

    def receiver_probe_result(self, req: Request) -> dict[str, Any]:
        return self.listener.result(_need(req.params, "probe_id", str))

    def _idle_only(self) -> None:
        with self._lock:
            sessions: list[SenderSession | PrimedSender | ReceiverSession] = [
                *self.senders.values(),
                *self.receivers.values(),
            ]
            if any(s.flow.state in ACTIVE for s in sessions):
                raise ZlError(
                    "RUN-012", "A test is running", "The multicast test runs only between tests."
                )

    # ---------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        """Heartbeat ``module_data.iptv`` (02 5)."""
        return {
            "sender": {"channels": [s.status() for s in self.senders.values()]},
            "prepare": self.preparer.status() if self.preparer else {"busy": False, "channels": []},
            "receivers": [
                {
                    "id": r.cfg.device,
                    "ch": r.cfg.ch,
                    "slot": slot,
                    "state": r.flow.state.value,
                    "measuring": r.measuring,
                    "lost_total": r.lost_total,
                    "last_1s": r.last_1s,
                }
                for slot, r in self.receivers.items()
            ],
            "tablets": [t.status() for t in self.tablet_rx.values()],
        }

    # ---------------------------------------------------------------- helpers

    def _recover_records(self) -> None:
        """F12 / 20 GAP-47: files left open by a crash get their last partial line cut and
        a "recovered" footer, before anything new is written."""
        repaired = []
        for folder in sorted(Path(self.node.data_dir).glob("runs/*/raw/*")):
            if folder.is_dir():
                repaired += recover(folder)
        if repaired:
            self.node.bus.publish(
                Event(
                    "ZL-INFO",
                    "info",
                    f"{len(repaired)} record file(s) closed after an unexpected stop",
                    data={"module": "iptv", "files": [str(p) for p in repaired]},
                )
            )

    def _take_summaries(self) -> list[dict[str, Any]]:
        every: list[ReceiverSession | TabletReceiver] = [
            *self.receivers.values(),
            *self.tablet_rx.values(),
        ]
        for r in every:
            self._keep_summaries(r)
        out, self._pending = self._pending, []
        return out

    def _keep_summaries(self, r: ReceiverSession | TabletReceiver) -> None:
        for s in r.take_summaries():
            self._pending.append({"run_id": r.cfg.run_id, "device": r.cfg.device, **s})  # 02 6

    def _source(self, p: dict[str, Any], ch: int, muxrate: int) -> tuple[Path, str]:
        """The file to send: the plan's prepared file, else the S-48 user file made for
        sending, else the generated video. A missing or unmade file falls back to the
        generated one with one SND-001 (iptv 1-1)."""
        given = p.get("file")
        if isinstance(given, str) and given:
            found = find_file(Path(given))
            if found is not None:
                return found, "file"
        quality = str(p.get("quality", "UHD"))
        wanted = p.get("source") not in (None, "", "Generated")
        media = self._media_file(ch, p.get("source"), tell=False) if wanted else None
        if media is not None and self.preparer is not None:
            made = self.preparer.ready(quality, ch, muxrate, media, p.get("length") == "whole")
            if made is not None:
                return made, "media"
        generated = self.preparer.ready(quality, ch, muxrate) if self.preparer else None
        if generated is None:
            raise ZlError(
                "SND-001",
                f"Video for CH{ch} is not prepared",
                str(given or p.get("source") or f"generated {quality}"),
                "Prepare the videos first (zeroloss prepare) or start again.",
            )
        if (isinstance(given, str) and given) or wanted:
            what = given or p.get("source")
            why = "file missing" if media is None else "not made for sending"
            self.node.bus.publish(Event(
                "ZL-SND-001", "warn", f"CH{ch} source {what} {why}, using the generated video",
                data={"file": what, "why": why, "ch": ch},
            ))  # fmt: skip
            return generated, "generated_fallback"
        return generated, "generated"

    def _run_dir(self, req: Request) -> Path:
        return Path(self.node.data_dir) / "runs" / (req.run_id or "no-run")

    def _node_starting(self) -> None:
        if self.node.flow.state is State.IDLE:
            self.node.flow.move(State.STARTING, reason="iptv session starting")

    def _node_settle(self) -> None:
        """Node state follows the sessions: any running -> RUNNING, none -> IDLE."""
        sessions: list[SenderSession | PrimedSender | ReceiverSession] = [
            *self.senders.values(),
            *self.receivers.values(),
        ]
        running = any(s.flow.state in ACTIVE for s in sessions) or any(
            t.state == "RUNNING" for t in self.tablet_rx.values()
        )
        flow = self.node.flow
        if running and flow.state is State.STARTING:
            flow.move(State.RUNNING, reason="iptv session running")
        elif not running and flow.state in (State.STARTING, State.RUNNING):
            flow.move(State.STOPPING, reason="iptv sessions stopped")
            flow.move(State.IDLE, reason="iptv sessions stopped")


def _receiver_prime(value: Any, run_id: str | None) -> dict[str, Any] | None:
    """``prime`` of receiver/start (iptv 4.5): {"sender_ip", "flows": [{"f","p","q","r"}]}."""
    if value is None:
        return None
    flows = value.get("flows") if isinstance(value, dict) else None
    if not isinstance(flows, list) or not flows or not isinstance(value.get("sender_ip"), str):
        raise ZlError("DSC-012", "Request rejected", detail="params.prime needs sender_ip, flows")
    out = []
    for x in flows:
        if not isinstance(x, dict) or x.get("f") not in (0, 1):
            raise ZlError("DSC-012", "Request rejected", detail="params.prime.flows[].f is 0/1")
        out.append({k: _need(x, k, int) for k in ("f", "p", "q", "r")})
    return {"sender_ip": value["sender_ip"], "run_id": run_id or "no-run", "flows": out}


def _probe_prime(value: Any, run_id: str | None, ch: int) -> tuple[str, int, int, bytes] | None:
    """``prime`` of receiver/probe: {"sender_ip", "p", "q"} (analyzer flow only)."""
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("sender_ip"), str):
        raise ZlError("DSC-012", "Request rejected", detail="params.prime needs sender_ip")
    payload = prime_payload(run_id or "no-run", ch, 0)
    return value["sender_ip"], _need(value, "p", int), _need(value, "q", int), payload


def _prime_flow(value: Any, run_id: str | None) -> PrimeFlow | None:
    """``prime`` of sender/start (iptv 4.5): {"flow", "p", "q"}; None = multicast."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ZlError("DSC-012", "Request rejected", detail="params.prime must be an object")
    flow, p, q = _need(value, "flow", int), _need(value, "p", int), _need(value, "q", int)
    if flow not in (0, 1) or not (1024 <= p <= 65535 and 1024 <= q <= 65535) or p == q:
        raise ZlError("DSC-012", "Request rejected", detail="params.prime flow 0/1, ports P != Q")
    return PrimeFlow(run_id or "no-run", flow, p, q)


def _seconds(params: dict[str, Any], most: int, default: int) -> float:
    value = params.get("seconds", default)
    if not isinstance(value, int | float) or isinstance(value, bool) or not 0 < value <= most:
        raise ZlError("DSC-012", "Request rejected", detail=f"params.seconds must be 0..{most}")
    return float(value)


def create() -> IptvModule:
    return IptvModule()
