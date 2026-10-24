"""File names with Korean and other non-English letters (09 RPT-002).

The same Hangul name can be stored as NFC (Windows, Linux) or NFD (copied from a Mac);
the bytes differ, so a plain lookup misses. ``find_file`` tries the name as given, then
NFC and NFD forms of every part. ``display_name`` gives the NFC form for reports/records.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path


def _forms(text: str) -> list[str]:
    out = [text]
    for form in ("NFC", "NFD"):
        n = unicodedata.normalize(form, text)
        if n not in out:
            out.append(n)
    return out


def find_file(path: Path) -> Path | None:
    """The existing file for ``path`` under any normalization of its parts, or None."""
    if path.is_file():
        return path
    candidates = [Path(path.anchor)] if path.anchor else [Path()]
    for part in path.parts[1:] if path.anchor else path.parts:
        nxt = []
        for base in candidates:
            for name in _forms(part):
                p = base / name
                if p.exists():
                    nxt.append(p)
            if not nxt and base.is_dir():  # last resort: compare normalized names
                want = unicodedata.normalize("NFC", part)
                nxt += [c for c in base.iterdir() if unicodedata.normalize("NFC", c.name) == want]
        if not nxt:
            return None
        candidates = nxt
    return next((c for c in candidates if c.is_file()), None)


def display_name(path: str | Path) -> str:
    """File name for reports and records: NFC, so the same name always looks the same."""
    return unicodedata.normalize("NFC", Path(path).name)
