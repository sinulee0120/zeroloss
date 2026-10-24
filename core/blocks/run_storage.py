"""Run folder storage (docs/03-features.md F15, 02 4 ``/v1/storage``, 20 GAP-79).

One job: say how much each run folder under one place takes, and remove one run or
only its raw data (``raw/``, ``screens/``, ``clips/``: 1-second originals, captures,
clips — the report, summaries and logs stay). Nothing outside the run folder is ever
touched and links are not followed. Nothing is removed on its own: only when asked.
"""

from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path
from typing import Any

from core.protocol.envelope import ZlError

RAW_DIRS = frozenset({"raw", "screens", "clips"})
RUN_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def check_run(run: str) -> str:
    """``run`` when it is a run folder name (never ``..``), else DSC-012."""
    if not RUN_ID.match(run) or set(run) == {"."}:
        raise ZlError("DSC-012", "Request rejected", detail="run must be a run ID")
    return run


def _files(folder: Path) -> list[tuple[Path, int, bool]]:
    """Every file under ``folder`` (links not followed): path, bytes, raw data or not."""
    out: list[tuple[Path, int, bool]] = []
    for top, dirs, names in os.walk(folder):  # links to folders are not entered
        rel = Path(top).relative_to(folder).parts
        raw = bool(rel) and rel[0] in RAW_DIRS
        links = [d for d in dirs if (Path(top) / d).is_symlink()]  # removed as a link only
        for name in names + links:
            p = Path(top) / name
            try:
                out.append((p, p.lstat().st_size, raw))
            except OSError:
                continue
    return out


def usage(folder: Path) -> dict[str, int]:
    files = _files(folder)
    return {"bytes": sum(b for _, b, _ in files), "raw_bytes": sum(b for _, b, r in files if r)}


def list_runs(root: Path) -> dict[str, dict[str, int]]:
    """Every run folder under ``root``: ``{run: {"bytes", "raw_bytes"}}``."""
    if not root.is_dir():
        return {}
    return {
        d.name: usage(d)
        for d in sorted(root.iterdir())
        if d.is_dir() and not d.is_symlink() and RUN_ID.match(d.name) and not d.name.startswith(".")
    }


def delete(root: Path, run: str, raw_only: bool) -> dict[str, Any]:
    """Remove ``root/run`` (or only its raw data). A run that is not there removes
    nothing (asking twice gives the same answer). Files that cannot be removed are
    listed in ``errors``; the rest go."""
    folder = root / check_run(run)
    if not folder.is_dir() or folder.is_symlink():
        return {"deleted_bytes": 0, "left_bytes": 0, "errors": []}
    deleted, errors = 0, []
    for p, size, raw in _files(folder):
        if raw_only and not raw:
            continue
        try:
            p.unlink()
            deleted += size
        except OSError as e:
            errors.append(f"{p.relative_to(folder).as_posix()}: {e.strerror or e}")
    tops = [folder / d for d in RAW_DIRS] if raw_only else [folder]
    for top in tops:
        if not top.is_dir() or top.is_symlink():
            continue
        inner = [Path(t) for t, _, _ in os.walk(top, topdown=False)]  # links not followed
        for d in inner:
            with contextlib.suppress(OSError):
                d.rmdir()  # empty folders only: a file that could not go keeps its folder
    left = usage(folder)["bytes"] if folder.is_dir() else 0
    return {"deleted_bytes": deleted, "left_bytes": left, "errors": errors}
