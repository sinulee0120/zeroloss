"""JSON Lines files (docs/02-data.md 7, 8): read them back, a line cut by a crash is left
out rather than failing the whole file."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any


def append_jsonl(path: Path, item: dict[str, Any]) -> None:
    """One line, flushed to disk: a crash can cut only the last line (read_jsonl skips it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as f:
        f.write((json.dumps(item, separators=(",", ":")) + "\n").encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        with contextlib.suppress(ValueError):
            item = json.loads(line)
            if isinstance(item, dict):
                out.append(item)
    return out
