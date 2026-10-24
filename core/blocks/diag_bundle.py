"""Diagnostic bundle (docs/02-data.md 4 ``GET /v1/logs``, 20 GAP-78).

One job: put chosen files of a data folder into one zip for a person or Claude to read,
never a secret: JSON values whose name says password, secret, token, psk, hmac,
private or ``key`` become ``***``. Measurement files (``.jsonl``) and media are never
taken. At most ``limit_bytes`` of files: the newest go in first, the rest are listed in
``manifest.json`` as ``left_out``. ``extra`` adds generated JSON files (a state
snapshot). The zip is a new file the caller removes after use.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

LIMIT_BYTES = 50 * 1024 * 1024  # per node (2026-09-26 user decision)
SECRET = re.compile(r"(?i)(password|passwd|secret|token|psk|hmac|private)|^key$")
NEVER = (".jsonl", ".ts", ".mp4", ".mkv", ".raw", ".part", ".zip", ".zlm", ".zlp", ".deb")


def redact(value: Any) -> tuple[Any, list[str]]:
    """``value`` with every secret-looking key's value replaced; the names it hid."""
    hidden: list[str] = []
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if SECRET.search(str(k)):
                out[k] = "***"
                hidden.append(str(k))
            else:
                out[k], more = redact(v)
                hidden += more
        return out, hidden
    if isinstance(value, list):
        items = [redact(v) for v in value]
        return [v for v, _ in items], [h for _, more in items for h in more]
    return value, hidden


def build(
    root: Path,
    patterns: list[str],
    extra: dict[str, Any],
    head: dict[str, Any],
    limit_bytes: int = LIMIT_BYTES,
    skip: tuple[str, ...] = (),
) -> Path:
    """A zip of the files under ``root`` matching ``patterns`` (glob, relative);
    ``skip`` = file names never taken (e.g. the Controller's ``wifi.json``)."""
    found: dict[Path, int] = {}
    for pat in patterns:
        for p in root.glob(pat):
            if p.is_file() and not p.name.endswith(NEVER) and p.name not in skip:
                try:
                    found[p] = p.stat().st_mtime_ns
                except OSError:
                    continue
    files: list[dict[str, Any]] = []
    left_out: list[dict[str, Any]] = []
    redacted: list[str] = []
    used = 0
    fd, name = tempfile.mkstemp(prefix="zeroloss-diag-", suffix=".zip")
    out = Path(name)
    with open(fd, "wb") as raw, zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(found, key=lambda x: found[x], reverse=True):  # newest first
            rel = p.relative_to(root).as_posix()
            try:
                data = p.read_bytes()
            except OSError as e:
                left_out.append({"name": rel, "why": f"unreadable: {e}"})
                continue
            if p.suffix == ".json":
                try:
                    clean, hidden = redact(json.loads(data))
                    data = json.dumps(clean, indent=1, ensure_ascii=False).encode()
                    redacted += [f"{rel}:{h}" for h in hidden]
                except ValueError:
                    pass  # not JSON after all: kept as it is
            if used + len(data) > limit_bytes:
                left_out.append({"name": rel, "bytes": len(data), "why": "over the size limit"})
                continue
            used += len(data)
            z.writestr(rel, data)
            files.append({"name": rel, "bytes": len(data)})
        for rel, value in extra.items():
            clean, hidden = redact(value)
            redacted += [f"{rel}:{h}" for h in hidden]
            z.writestr(rel, json.dumps(clean, indent=1, ensure_ascii=False, default=str))
        manifest = {**head, "made_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "limit_bytes": limit_bytes, "files": files, "left_out": left_out,
                    "redacted": redacted}  # fmt: skip
        z.writestr("manifest.json", json.dumps(manifest, indent=1, ensure_ascii=False))
    return out
