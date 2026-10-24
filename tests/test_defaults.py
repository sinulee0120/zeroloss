"""Defaults come from one place per owner and match the settings table (02 11, 16 4)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from core.settings import load_defaults
from modules.iptv.sender.commands import QUALITIES

ROOT = Path(__file__).resolve().parent.parent
FILES = [
    ROOT / "core" / "settings" / "defaults.json",
    *sorted(ROOT.glob("modules/*/defaults.json")),
]


def table() -> dict[str, str]:
    rows = {}
    for line in (ROOT / "docs" / "16-settings.md").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\| (S-\d\d) \| [^|]* \| [^|]* \| ([^|]*) \|", line)
        if m:
            rows[m.group(1)] = m.group(2).strip()
    return rows


def test_every_setting_has_exactly_one_default_and_matches_the_table() -> None:
    rows = table()
    seen: dict[str, Path] = {}
    for f in FILES:
        for sid, entry in json.loads(f.read_text(encoding="utf-8"))["settings"].items():
            assert sid not in seen, f"{sid} in {seen.get(sid)} and {f}"
            seen[sid] = f
            assert entry["doc"] == rows.get(sid), f"{f.name} {sid}: {entry['doc']!r} != table"
    assert set(seen) == set(rows), (set(rows) - set(seen), set(seen) - set(rows))


def test_typed_values_agree_with_the_table_text() -> None:
    d = load_defaults(ROOT / "modules" / "iptv" / "defaults.json")
    assert d["S-15"] == 16 and d["S-23"] == 50 and d["S-24"] == 1000 and d["S-28"] == 15
    assert d["S-27"] == 16 * 1024 * 1024 and d["S-14"]["port"] == 5004
    for quality, mbps in d["S-11"].items():  # quality table follows the default bitrate
        assert QUALITIES[quality].muxrate_bps == mbps * 1_000_000
    c = load_defaults(ROOT / "core" / "settings" / "defaults.json")
    assert c["S-42"] == {"suspect_s": 3, "lost_s": 10}


def test_no_default_literals_left_in_code() -> None:
    """The values that used to be scattered (S-15 16, S-28 15, S-23 50, S-42 3/10)."""
    bad = [
        r"_num\(s, \"S-\d\d\", \d",
        r"p\.get\(\"ttl\", 16\)",
        r"warmup_max_s\", 15",
        r"^(SUSPECT_S|LOST_S|LOST_AFTER_S) = \d",
        r"^DEFAULT_(TTL|JITTER_WARN_MS) = \d",
    ]
    for p in [
        *ROOT.glob("core/**/*.py"),
        *ROOT.glob("controller/**/*.py"),
        *ROOT.glob("modules/**/*.py"),
    ]:
        text = p.read_text(encoding="utf-8")
        for pat in bad:
            assert not re.search(pat, text, re.M), f"{p.relative_to(ROOT)}: {pat}"
