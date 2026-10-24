"""Settings screen data (defaults.json ``ui``) = the settings table (16 1), G-16:
name, place, lock, number ranges and choices are compared, help text is English."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = [ROOT / "core" / "settings" / "defaults.json", ROOT / "modules" / "iptv" / "defaults.json"]
PLACE = {"Main": "main", "Adv": "adv", "Setup/Main": "setup", "Desktop": "desktop"}
KINDS = {"choice", "int", "float", "bool", "duration", "per_quality_int", "devices", "channels",
         "groups", "sources", "pair", "text", "wifi"}  # fmt: skip


def table() -> dict[str, list[str]]:
    rows = {}
    for line in (ROOT / "docs" / "16-settings.md").read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\| (S-\d\d) \|", line)
        if m:
            rows[m.group(1)] = [c.strip() for c in line.strip().strip("|").split("|")]
    return rows


def entries() -> dict[str, dict[str, object]]:
    out = {}
    for f in FILES:
        out.update(json.loads(f.read_text(encoding="utf-8"))["settings"])
    return out


@pytest.mark.parametrize("sid", sorted(entries()))
def test_each_setting_matches_the_table(sid: str) -> None:
    ui = entries()[sid]["ui"]
    assert isinstance(ui, dict)
    row = table()[sid]
    name, place, allowed, lock = row[1], row[2], row[4], row[5]
    assert name.startswith(str(ui["label"])), (sid, name, ui["label"])
    assert PLACE[place] == ui["where"], sid
    assert ui["lock"] == ("🔒" in lock), (sid, lock)
    assert ui["kind"] in KINDS, sid
    plain = allowed.replace(",", "")
    if ui["kind"] in ("int", "float"):
        lo, hi = ui["min"], ui["max"]
        assert f"{lo:g}~{hi:g}" in plain, (sid, allowed)
    if ui["kind"] == "choice":
        for c in ui["choices"]:
            assert str(c).split(" (")[0] in allowed, (sid, c, allowed)
    help_text = str(ui["help"])
    assert help_text and not re.search(r"[가-힣]", help_text), sid
    assert ui["available"] is True or (isinstance(ui["available"], str) and ui["available"])


def test_every_table_row_has_screen_data() -> None:
    assert set(table()) == set(entries())


def test_advanced_groups_hold_at_most_seven() -> None:
    """11-ui 1.11: at most 7 items per group on a settings screen."""
    groups: dict[str, int] = {}
    for sid, e in entries().items():
        ui = e["ui"]
        assert isinstance(ui, dict)
        if ui["where"] == "adv":
            assert ui.get("group"), sid
            groups[str(ui["group"])] = groups.get(str(ui["group"]), 0) + 1
    assert groups and max(groups.values()) <= 7, groups
