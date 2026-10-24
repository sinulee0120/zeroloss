"""IPTV in Agent Desktop (module.json ``desktop_entry``, docs/03-features.md F20, 12 2).

Two things only: the ``player`` screen job (the P2 player part itself, 20 GAP-39, run
in the logged-in user's session) and the status window's rows made from this module's
heartbeat values (02 5 ``module_data.iptv``). Colors say "running, and fine?" on site;
they are not the verdict (the Controller judges, absolute rule).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from modules.iptv.receiver.player import PlayerConfig, VlcPlayer, make_player

RUNNING = ("STARTING", "RUNNING")
LOGS_KEPT = 10  # player logs kept after their jobs (iptv 3.3, GAP-180)


class PlayerJob:
    """The player as a Desktop job part (agent/desktop/link.py contract)."""

    def __init__(self, player: VlcPlayer) -> None:
        self.player = player
        self._lowered = False

    @property
    def state(self) -> str:
        return self.player.flow.state.value

    def start(self) -> None:
        self.player.start()

    def stop(self) -> None:
        self.player.stop()
        keep_log(self.player.cfg.work_dir)

    def change(self, config: dict[str, Any]) -> None:
        """RCV-016: the node asked the player to give way (once); GAP-181: whether
        packets come (02 2-12 flowing)."""
        if isinstance(config.get("flowing"), bool):
            self.player.data_flowing(config["flowing"])
        if config.get("lowered") and not self._lowered:
            self._lowered = True
            if not self.player.lower_priority():
                raise RuntimeError("the player could not be made to give way")

    def report(self) -> dict[str, Any]:
        """02 2-12 report fields: this second of playback, first frame, lowered."""
        s = self.player.take_second()
        return {
            "second": {"playback": s.pb, "dropped": s.dropped, "audio_gaps": s.audio_lost,
                       "stall_started": s.freeze_started, "restarted": s.restarted,
                       "decode_errors": s.decode_errors,
                       "decoder_dropped": s.decoder_dropped},
            "first_frame": self.player.first_frame, "lowered": self._lowered,
        }  # fmt: skip


def keep_log(work_dir: Path) -> None:
    """The player's log outlives its job folder (which goes at the job's end, G-11):
    moved to ``player-logs/<job>.log`` next to ``jobs/``, the newest ``LOGS_KEPT`` stay."""
    log, into = work_dir / "player.log", work_dir.parent.parent / "player-logs"
    try:
        if log.is_file():
            into.mkdir(exist_ok=True)
            log.replace(into / f"{work_dir.name}.log")
        for old in sorted(into.glob("*.log"), key=lambda p: p.stat().st_mtime)[:-LOGS_KEPT]:
            old.unlink(missing_ok=True)
    except OSError:
        pass  # Windows: a log still open stays in the job folder and goes with it


def player_job(config: dict[str, Any], work_dir: Path, procs_dir: Path,
               on_event: Callable[[str, str, str], None]) -> PlayerJob:  # fmt: skip
    cfg = PlayerConfig(
        url=str(config["url"]), work_dir=work_dir, nic=config.get("nic"),
        buffer_ms=int(config.get("buffer_ms", 1000)), window=str(config.get("window", "")),
        headless=bool(config.get("headless")), procs_dir=procs_dir,
        kind=str(config.get("player", "VLC")),  # an older node does not say: VLC (S-26)
    )  # fmt: skip
    job = PlayerJob(make_player(cfg, on_event))
    job.change(config)  # already asked to give way (a Desktop that came back)
    return job


def status_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """03 F20 sender / receiver windows: ``{"title", "color", "big", "lines"}`` each."""
    rows: list[dict[str, Any]] = []
    senders = [c for c in (data.get("sender") or {}).get("channels") or []]
    if senders:
        lines, color = [], "green"
        for c in sorted(senders, key=lambda c: c.get("ch", 0)):
            ok = c.get("state") in RUNNING and c.get("tx_bitrate_bps")
            rate = _mbps(c.get("tx_bitrate_bps"))
            lines.append(f"CH{c.get('ch')} {c.get('group')}  {rate}  {_icon(bool(ok))}")
            if c.get("state") == "FAULT":
                color = "red"
            elif not ok and color == "green":
                color = "yellow"
        rows.append({"title": f"Sending {len(senders)} channel(s)", "color": color, "big": [],
                     "lines": lines, "role": "sender"})  # fmt: skip
    for r in data.get("receivers") or []:
        last = r.get("last_1s") or {}
        pb = last.get("pb")
        if r.get("state") == "FAULT" or (last.get("lost") or 0) > 0:
            color = "red"
        elif not r.get("measuring") or pb in ("freeze", "none"):
            color = "yellow"
        else:
            color = "green"
        jit = last.get("jit")
        big = [
            ["Jitter", f"{jit:.1f} ms" if isinstance(jit, int | float) else "—"],
            ["Loss", str(r["lost_total"]) if r.get("lost_total") is not None else "—"],
        ]
        lines = [f"Bitrate  {_mbps(last.get('br'))}"]
        if pb is not None:
            lines.append(f"Player   {_PLAYBACK.get(pb, pb)}")
        state = "Receiving" if r.get("measuring") else "Starting" if r.get("state") in RUNNING \
            else str(r.get("state", "")).title()  # fmt: skip
        detail = None
        if last.get("e") is not None:
            detail = {"key": f"{r.get('id')}/ch{r.get('ch')}/{last['e']}", "line": _line(last)}
        rows.append({"title": f"{state} CH{r.get('ch')}", "color": color, "big": big,
                     "lines": lines, "detail": detail, "role": "receiver"})  # fmt: skip
    return rows


_PLAYBACK = {"ok": "Playing", "freeze": "Frozen", "none": "Not playing", "drop": "Dropping frames",
             "audio_gap": "Sound gaps", "restart": "Restarted",
             "glitch": "Picture damaged (decoder)"}  # fmt: skip


def _line(s: dict[str, Any]) -> str:
    """One [Details] line from the 1-second record (02 7 keys)."""
    jit = s.get("jit")
    parts = [f"{s.get('e', ''):>6} s", f"CH{s.get('ch', '')}", f"lost {s.get('lost', 0)}",
             f"jitter {jit:.1f} ms" if isinstance(jit, int | float) else "jitter —",
             _mbps(s.get("br"))]  # fmt: skip
    if s.get("pb"):
        parts.append(f"player {_PLAYBACK.get(s['pb'], s['pb'])}")
    return "  ".join(parts)


def _mbps(bps: Any) -> str:
    return f"{bps / 1e6:.1f} Mbps" if isinstance(bps, int | float) and bps else "—"


def _icon(ok: bool) -> str:
    return "✔" if ok else "⚠"


class IptvDesktop:
    def __init__(self) -> None:
        self.jobs: dict[str, Callable[..., Any]] = {"player": player_job}

    @staticmethod
    def status_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
        return status_rows(data)


def create() -> IptvDesktop:
    return IptvDesktop()
