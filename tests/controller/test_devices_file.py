"""A broken device list never keeps the Controller from starting (09 SYS-011, GAP-117)."""

from __future__ import annotations

from pathlib import Path

import pytest

from controller.devices import DeviceList
from core.protocol.envelope import ZlError


@pytest.mark.parametrize("text", ["{ broken", "[]", '{"schema_version": 1, "devices": [{"x": 1}]}'])
def test_a_broken_file_is_kept_aside_and_the_list_starts_empty(tmp_path: Path, text: str) -> None:
    f = tmp_path / "devices.json"
    f.write_text(text, encoding="utf-8")
    d = DeviceList(f)
    assert d.all() == [] and d.unreadable is not None and "devices.json" in d.unreadable
    kept = list(tmp_path.glob("devices.json.unreadable-*"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == text and not f.exists()


def test_a_newer_format_still_stops_the_start(tmp_path: Path) -> None:
    """INS-025 is a different case: a file from a newer build is never touched."""
    f = tmp_path / "devices.json"
    f.write_text('{"schema_version": 99, "devices": []}', encoding="utf-8")
    with pytest.raises(ZlError):
        DeviceList(f)
    assert f.exists()
