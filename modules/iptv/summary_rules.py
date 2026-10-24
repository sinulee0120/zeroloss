"""IPTV 5-minute summary from 1-second records (docs/02-data.md 6 and 7).

``RULES`` says how each value is combined from the 1-second keys; ``shape`` turns the
flat result into the exact 02 6 form. Every documented key is always present; values
this phase does not measure are ``null`` (20 G-02, the phase table under 02 6).
"""

from __future__ import annotations

from typing import Any

from core.blocks.summarizer import Agg, Summarizer
from modules.iptv import record_keys as k
from modules.iptv.defaults import DEFAULTS

RULES: dict[str, tuple[str, Agg]] = {
    "wifi_rssi_min": (k.RSSI_DBM, Agg.MIN),  # 02 6 ``wifi`` (GAP-104 3)
    "wifi_mbps_min": (k.WIFI_MBPS, Agg.MIN),
    "wifi_band": (k.WIFI_BAND, Agg.LAST),
    "wifi_channel": (k.WIFI_CH, Agg.LAST),
    "wifi_width": (k.WIFI_BW, Agg.LAST),
    "wifi_channel_changes": (k.WIFI_CH_CHANGED, Agg.SUM),
    "wifi_bssid_changes": (k.WIFI_BSSID_CHANGED, Agg.SUM),
    "received": (k.RECEIVED, Agg.SUM),
    "lost": (k.LOST, Agg.SUM),
    "duplicates": (k.DUPLICATES, Agg.SUM),
    "reordered_recovered": (k.REORDERED, Agg.SUM),
    "loss_events": (k.LOSS_EVENTS, Agg.SUM),
    "max_burst": (k.MAX_BURST, Agg.MAX),
    "late_discarded": (k.LATE_DISCARDED, Agg.SUM),
    "jitter_avg_ms": (k.JITTER_MS, Agg.AVG),
    "jitter_max_ms": (k.JITTER_MS, Agg.MAX),
    "delay_low_ms": (k.DELAY_LOW_MS, Agg.MIN),
    "delay_high_ms": (k.DELAY_HIGH_MS, Agg.MAX),
    "delay_window_ms": (k.DELAY_WINDOW_MS, Agg.MAX),
    "bitrate_avg_bps": (k.BITRATE_BPS, Agg.AVG),
    "bitrate_min_bps": (k.BITRATE_BPS, Agg.MIN),
    "bitrate_max_bps": (k.BITRATE_BPS, Agg.MAX),
    "host_drops": (k.HOST_DROPS, Agg.SUM),
    "p1_errors": (k.P1_ERRORS, Agg.SUM),
    "sync_loss": (k.SYNC_LOSS, Agg.SUM),
    "sync_byte_err": (k.SYNC_BYTE_ERR, Agg.SUM),
    "pat_err": (k.PAT_ERR, Agg.SUM),
    "pmt_err": (k.PMT_ERR, Agg.SUM),
    "cc_err": (k.CC_ERRORS, Agg.SUM),
    "pid_err": (k.PID_ERR, Agg.SUM),
    "tei_err": (k.TEI_ERR, Agg.SUM),  # TR 101 290 2.1, counted only
    "crc_err": (k.CRC_ERR, Agg.SUM),  # 2.2
    "skew_ppm": (k.SKEW_PPM, Agg.AVG),  # absent until estimated: null (02 6)
    "frame_drops": (k.PLAY_DROPPED, Agg.SUM),  # absent = not measured (02 7)
    "decoder_frame_drops": (k.PLAY_DECODER_DROPPED, Agg.SUM),  # absent = not measured
    "audio_underruns": (k.PLAY_AUDIO_LOST, Agg.SUM),
    "freezes": (k.PLAY_FREEZE_STARTED, Agg.SUM),
    "player_restarts": (k.PLAY_RESTARTED, Agg.SUM),
    "decoder_errors": (k.PLAY_DECODE_ERRORS, Agg.SUM),
    "zaps": (k.ZAP_COUNT, Agg.SUM),  # channel changes finished (02 7, F19)
    "zap_ok": (k.ZAP_OK, Agg.SUM),
    "join_failures": (k.ZAP_JOIN_FAILED, Agg.SUM),
    "leave_not_processed": (k.ZAP_LEAVE_NOT_PROCESSED, Agg.SUM),
    "zap_total_avg_ms": (k.ZAP_TOTAL_MS, Agg.AVG),
    "zap_total_max_ms": (k.ZAP_TOTAL_MS, Agg.MAX),
    "zap_join_max_ms": (k.ZAP_JOIN_MS, Agg.MAX),
    "zap_play_max_ms": (k.ZAP_PLAY_MS, Agg.MAX),
}
# Detail keys are left out of a second when zero (02 7): a missing value counts as 0.
ZERO_WHEN_ABSENT = (
    "loss_events",
    "max_burst",
    "late_discarded",
    "sync_loss",
    "sync_byte_err",
    "pat_err",
    "pmt_err",
    "pid_err",
    "tei_err",
    "crc_err",
    "zaps",
    "zap_ok",
    "join_failures",
    "leave_not_processed",
)


def make_summarizer(cell_s: int = 300) -> Summarizer:
    """A new join segment starts whenever the channel changes."""
    return Summarizer(RULES, cell_s=cell_s, segment_key=k.CHANNEL)


def _wifi(v: dict[str, Any]) -> dict[str, Any] | None:
    """02 6 ``wifi`` from the 1-second Wi-Fi keys; None for a wired receiver (no keys)."""
    if v.get("wifi_rssi_min") is None and v.get("wifi_band") is None:
        return None
    return {
        "rssi_dbm_min": v.get("wifi_rssi_min"),
        "link_mbps_min": v.get("wifi_mbps_min"),
        "band": v.get("wifi_band"),
        "channel": v.get("wifi_channel"),
        "width_mhz": v.get("wifi_width"),
        "channel_changes": int(v.get("wifi_channel_changes") or 0),
        "bssid_changes": int(v.get("wifi_bssid_changes") or 0),
    }


def _r(x: float | None, nd: int = 3) -> float | None:
    return round(x, nd) if x is not None else None


def shape(
    piece: dict[str, Any],
    rcvbuf_bytes: int | None,
    kernel_timestamps: bool,
    playback: bool = False,
    success_timeout_s: float = float(DEFAULTS["S-47"]),
) -> dict[str, Any]:
    """Summarizer piece -> 02 6 form (without ``run_id``/``device``, added by the module)."""
    v = dict(piece["values"])
    for name in ZERO_WHEN_ABSENT:
        if v.get(name) is None:
            v[name] = 0
    received, lost = int(v["received"] or 0), int(v["lost"] or 0)
    expected = received + lost
    host_drops = v["host_drops"]
    avg = v["bitrate_avg_bps"]
    return {
        "index": piece["index"],
        "start_elapsed_s": piece["start_elapsed_s"],
        "duration_s": piece["duration_s"],
        "partial": piece["partial"],
        "ch": piece.get(k.CHANNEL),
        "join_segment": piece["join_segment"],
        "loss": {
            "expected": expected,
            "received": received,
            "lost": lost,
            "loss_rate": lost / expected if expected else 0.0,
            "loss_events": v["loss_events"],
            "max_burst": v["max_burst"],
            "duplicates": v["duplicates"],
            "reordered_recovered": v["reordered_recovered"],
            "late_discarded": v["late_discarded"],
            "host_drop_loss": min(lost, host_drops) if host_drops is not None else None,
        },
        "jitter": {
            "avg_ms": _r(v["jitter_avg_ms"]),
            "max_ms": v["jitter_max_ms"],
            "delay_low_ms": v["delay_low_ms"],
            "delay_high_ms": v["delay_high_ms"],
            "pdv_window_ms": v["delay_window_ms"],
            "skew_ppm": _r(v["skew_ppm"], 1),
            "ipdv_max_ms": None,  # P3 (RFC 3393/5481)
            "ipdv_p99_ms": None,
        },
        "mdi": {"df_max_ms": None, "mlr_max_pps": None},  # P3 (RFC 4445)
        "ts": {
            "p1_errors": v["p1_errors"],
            "sync_loss": v["sync_loss"],
            "sync_byte_err": v["sync_byte_err"],
            "pat_err": v["pat_err"],
            "pmt_err": v["pmt_err"],
            "cc_err": v["cc_err"],
            "pid_err": v["pid_err"],
            "tei_err": v["tei_err"],
            "crc_err": v["crc_err"],
            "pcr_repetition_err": None,  # TR 101 290 priority 2: phase 2
            "pcr_accuracy_err": None,
            "pts_err": None,
        },
        "bitrate_bps": {
            "avg": round(avg) if avg is not None else None,
            "min": v["bitrate_min_bps"],
            "max": v["bitrate_max_bps"],
        },
        "receiver": {
            "host_drops": host_drops,
            "rcvbuf_bytes": rcvbuf_bytes,
            "locks_ok": None,  # tablets only (P4)
            "non_video_traffic_bps": None,  # P3
        },
        "wifi": _wifi(v),
        "playback": {
            "freezes": int(v["freezes"] or 0),
            "frame_drops": v["frame_drops"],  # None: the player's statistics did not move
            "decoder_frame_drops": v["decoder_frame_drops"],  # of frame_drops (GAP-180)
            "audio_underruns": v["audio_underruns"],
            "decoder_errors": int(v["decoder_errors"] or 0),  # player log (GAP-152)
            "player_restarts": int(v["player_restarts"] or 0),
        }
        if playback
        else None,  # S-49 Off (K-29)
        "channel": {
            "changes": v["zaps"],
            "success": v["zap_ok"],
            "success_timeout_s": success_timeout_s,
            "join_failures": v["join_failures"],
            "leave_not_processed": v["leave_not_processed"],
            "zap_ms": {
                "total_avg": _r(v["zap_total_avg_ms"], 1),
                "total_max": v["zap_total_max_ms"],
                "join_to_first_packet_max": v["zap_join_max_ms"],
                "first_packet_to_play_max": v["zap_play_max_ms"],
            },
        },
        "measure_method": {
            "loss": "rtp_seq",
            "jitter": "rfc3550",
            "timestamps": "kernel" if kernel_timestamps else "user",
        },
        "device_color": None,  # tablet app display value (P4)
    }
