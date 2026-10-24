"""User videos in the sender PC's media folder (docs/modules/iptv.md 1-1, S-48, K-28).

One job: list the videos a user put in ``<node data>/media/`` with what ffprobe says
(codec, size, fps, length, sound) and whether each fits UHD / HD, and give a short
digest of a file's content (size + its first and last 4 MiB) so a changed original
is made again and a copied one (another PC, another name, another change time) is not.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import unicodedata
from pathlib import Path
from typing import Any

from modules.iptv.sender.commands import QUALITIES

VIDEO_EXT = (".mp4", ".mkv", ".mov", ".m4v", ".ts")
PROBE_TIMEOUT_S = 15.0
END_TAIL_S = 5.0  # av_end_gap reads only the last seconds of a file


DIGEST_PART = 4 * 1024 * 1024  # bytes read from each end of the file


def digest(path: Path) -> str:
    """The original by what is in it (2026-10-17 user: "이미 만든걸 또 만들필요없게"): a
    UHD file made from it travels with it in the offline kit and is used at once on the
    other PC. Name, folder and change time do not count."""
    size = path.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with path.open("rb") as f:
        h.update(f.read(DIGEST_PART))
        if size > DIGEST_PART:
            f.seek(max(DIGEST_PART, size - DIGEST_PART))
            h.update(f.read(DIGEST_PART))
    return h.hexdigest()[:8]


def legacy_digest(path: Path) -> str:
    """Before 2026-10-17: name + size + change time. Files made then are kept (hours of
    work each) and still used while the original is the same file."""
    st = path.stat()
    raw = f"{unicodedata.normalize('NFC', path.name)}|{st.st_size}|{st.st_mtime_ns}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]


def safe_name(name: Any) -> bool:
    """S-48 in a plan is a file name in the media folder, never a path (RUN-014)."""
    return (
        isinstance(name, str) and bool(name) and name not in (".", "..")
        and "/" not in name and "\\" not in name and "\0" not in name
    )  # fmt: skip


def probe(path: Path, ffprobe: str = "ffprobe") -> dict[str, Any]:
    """ffprobe facts of one file; ``error`` says why when it cannot be read."""
    argv = [ffprobe, "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
            str(path)]  # fmt: skip
    try:
        done = subprocess.run(argv, capture_output=True, timeout=PROBE_TIMEOUT_S, check=False)
        info = json.loads(done.stdout.decode("utf-8", errors="replace") or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return {"error": f"ffprobe failed: {type(e).__name__}"}
    streams = info.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        return {"error": "no video stream"}
    return {
        "codec": video.get("codec_name"),
        "width": int(video.get("width") or 0) or None,
        "height": int(video.get("height") or 0) or None,
        "fps": _fps(str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "")),
        "duration_s": _float((info.get("format") or {}).get("duration")),
        "audio": any(s.get("codec_type") == "audio" for s in streams),
        "error": None,
    }


def av_end_gap(path: Path, length_s: float, ffprobe: str = "ffprobe") -> float | None:
    """How far apart the sound and the picture end, in seconds (iptv 1-1, GAP-114): the
    sender loops the file, so a longer sound leaves a still picture at every seam. Reads
    only the last seconds. None = the ends could not be read."""
    ends = []
    for stream in ("v:0", "a:0"):
        argv = [ffprobe, "-v", "error", "-read_intervals", f"{max(0.0, length_s - END_TAIL_S)}%",
                "-select_streams", stream, "-show_entries", "packet=pts_time,duration_time",
                "-of", "csv=p=0", str(path)]  # fmt: skip
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        end = None
        for line in done.stdout.split():
            pts, dur, *_ = [*line.split(","), "", ""]  # ffprobe may end a line with ","
            try:
                t = float(pts) + (float(dur) if dur and dur != "N/A" else 0.0)
            except ValueError:
                continue
            end = t if end is None else max(end, t)
        if end is None:
            return None
        ends.append(end)
    return abs(ends[0] - ends[1])


def fits(facts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """K-28 per quality: usable, and made larger than it is (``upscaled``)."""
    out = {}
    for name, q in QUALITIES.items():
        w, h = (int(v) for v in q.size.split("x"))
        if facts.get("error"):
            out[name] = {"ok": False, "upscaled": False, "why": facts["error"]}
            continue
        up = bool(facts.get("height") and facts["height"] < h)
        why = []
        if up:
            why.append(f"{facts.get('width')}x{facts.get('height')} is made larger to {w}x{h}")
        fps = facts.get("fps")
        if fps and abs(fps - 30) > 0.5:
            why.append(f"{fps:g} fps becomes 30 fps")
        out[name] = {"ok": True, "upscaled": up, "why": "; ".join(why) or None}
    return out


def list_sources(folder: Path, ffprobe: str = "ffprobe") -> dict[str, Any]:
    """02 4 ``GET /v1/modules/iptv/sender/sources``."""
    files = []
    for p in sorted(folder.iterdir()) if folder.is_dir() else []:
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXT:
            continue
        facts = probe(p, ffprobe)
        files.append({"name": unicodedata.normalize("NFC", p.name), "bytes": p.stat().st_size,
                      **{k: facts.get(k) for k in ("codec", "width", "height", "fps",
                                                   "duration_s", "audio")},
                      "fits": fits(facts), "error": facts.get("error")})  # fmt: skip
    return {"folder": str(folder), "files": files}


def _fps(text: str) -> float | None:
    num, _, den = text.partition("/")
    try:
        value = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return None
    return round(value, 3) if value > 0 else None


def _float(v: Any) -> float | None:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None
