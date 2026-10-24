"""Command lines for preparing and sending a channel (docs/modules/iptv.md 3, 4.1, 4.4).

Pure functions: the options here are the ones confirmed with real tools on 2026-09-24
(iptv 7, S1-S6). Values come from the quality table; nothing is typed by hand.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path

from modules.iptv.defaults import DEFAULTS
from modules.iptv.marker import filter_graph

RATE_TAG = "ZLRATE"


@dataclass(frozen=True)
class Quality:
    name: str
    codec: str
    size: str
    muxrate_bps: int
    video_share: float  # video target = muxrate x share; the rest is audio, PSI, stuffing


QUALITIES = {
    "UHD": Quality("UHD", "libx265", "3840x2160", 35_000_000, 0.88),
    "HD": Quality("HD", "libx264", "1920x1080", 10_000_000, 0.85),
}
PMT_PID = 0x1000
FIRST_ES_PID = 0x100
PAT_PERIOD_S = "0.08"
DEFAULT_TTL = int(DEFAULTS["S-15"])
TONE_HZ = {1: 1000, 2: 1500, 3: 2000}  # a different pitch per channel (iptv 1-1)


def new_ssrc(ch: int, rng: random.Random | None = None) -> int:
    """Top 8 bits = channel, low 24 bits = new random value on every start (iptv 2)."""
    if not 1 <= ch <= 255:
        raise ValueError("channel must be 1..255")
    low = (rng or random.SystemRandom()).getrandbits(24)
    return (ch << 24) | low


# 2026-10-24: a 10-minute UHD clip from a 720p movie took the ProBook about two hours with
# x265 "veryfast" (2.5 frames/s). The test needs the bitrate and the stream's structure,
# not picture quality: x265 runs "ultrafast" (CBR, HRD and the marker's AQ stay forced
# below; tests/iptv/test_marker.py reads the marker of an ultrafast UHD file)
PRESET = {"libx265": "ultrafast", "libx264": "veryfast"}


def prepare_command(
    ffmpeg: str,
    quality: Quality,
    ch: int,
    out: Path,
    source: Path | None = None,
    seconds: float | None = None,
    muxrate_bps: int | None = None,
    preset: str | None = None,
    progress: Path | None = None,
    source_audio: bool = True,
) -> list[str]:
    """ffmpeg command making the CBR prepared file with the channel title and the frame
    marker (iptv 1-1, 3.4). ``source`` None = generated video and a tone per channel.
    ``progress`` = file for ffmpeg ``-progress`` (key=value lines, iptv 1-1-2)."""
    mux = muxrate_bps or quality.muxrate_bps
    video = int(mux * quality.video_share)
    w, h = (int(v) for v in quality.size.split("x"))
    title = f"ZeroLoss CH{ch}"
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats", "-y"]
    if progress is not None:
        cmd += ["-progress", str(progress)]
    if source is None:
        cmd += ["-f", "lavfi", "-i", f"testsrc2=size={quality.size}:rate=30"]
        tone = TONE_HZ.get(ch, TONE_HZ[1])
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={tone}:sample_rate=48000"]
        audio = "1:a"
    else:
        cmd += ["-i", str(source)]
        audio = "0:a:0"
        if not source_audio:  # a silent video still gets its channel tone (4.3 needs audio)
            tone = TONE_HZ.get(ch, TONE_HZ[1])
            cmd += ["-f", "lavfi", "-i", f"sine=frequency={tone}:sample_rate=48000"]
            audio = "1:a"
    if seconds is not None:
        cmd += ["-t", f"{seconds:g}"]
    cmd += ["-filter_complex", filter_graph(w, h, ch, title), "-map", "[v]", "-map", audio]
    cmd += ["-c:v", quality.codec, "-preset", preset or PRESET.get(quality.codec, "veryfast")]
    cmd += ["-b:v", str(video), "-maxrate", str(video), "-bufsize", str(video)]
    if quality.codec == "libx265":
        # aq-mode = x265's default, stated: the marker ROI needs AQ (iptv 3.4, 20 W-7)
        cmd += ["-x265-params", "strict-cbr=1:hrd=1:aq-mode=2:log-level=error"]
    else:
        cmd += ["-minrate", str(video), "-x264-params", "nal-hrd=cbr", "-aq-mode", "1"]
    cmd += ["-g", "30", "-flags", "+cgop", "-r", "30", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]
    # sound ends with the picture: a longer sound left a still picture at every loop seam,
    # a shorter one a silent gap (iptv 1-1, GAP-114)
    cmd += ["-af", "apad", "-shortest"]
    cmd += ["-f", "mpegts", "-muxrate", str(mux), "-pat_period", PAT_PERIOD_S]
    cmd += ["-mpegts_service_id", str(ch), "-mpegts_pmt_start_pid", str(PMT_PID)]
    cmd += ["-mpegts_start_pid", str(FIRST_ES_PID), "-metadata", f"service_name={title}"]
    cmd += [str(out)]
    return cmd


def send_command(
    tsp: str,
    file: Path,
    dest_ip: str,
    port: int,
    muxrate_bps: int,
    ssrc: int,
    ttl: int = DEFAULT_TTL,
    local_address: str | None = None,
    local_port: int | None = None,
) -> list[str]:
    """tsp pipeline: loop the file, join the loop seam, pace, report rate, send RTP.
    ``local_port``: primed mode sends from a fixed port (iptv 4.5)."""
    rate = str(muxrate_bps)
    cmd = [tsp, "--realtime", "-I", "file", str(file), "--infinite"]
    cmd += ["-P", "continuity", "--fix"]
    cmd += ["-P", "pcradjust", "--bitrate", rate]
    cmd += ["-P", "regulate", "--bitrate", rate, "--packet-burst", "7"]
    cmd += ["-P", "bitrate_monitor", "--periodic-bitrate", "1", "--time-interval", "1"]
    cmd += [f"--json-line={RATE_TAG}"]
    cmd += ["-O", "ip", f"{dest_ip}:{port}", "--rtp", "--payload-type", "33"]
    cmd += ["--ssrc-identifier", str(ssrc), "--packet-burst", "7", "--enforce-burst"]
    cmd += ["--ttl", str(ttl)]
    if local_address:
        cmd += ["--local-address", local_address]
    if local_port:
        cmd += ["--local-port", str(local_port)]
    return cmd


def parse_rate(line: str) -> int | None:
    """Bitrate (b/s) from one bitrate_monitor log line, else None."""
    i = line.find(RATE_TAG + "{")
    if i < 0:
        return None
    try:
        value = json.loads(line[i + len(RATE_TAG) :])["bitrate"]
    except (ValueError, KeyError, TypeError):
        return None
    return int(value) if isinstance(value, int | float) else None
