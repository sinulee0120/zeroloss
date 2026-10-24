from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tools.versioning import (
    android_version_code,
    deb_version,
    make_version,
    parse_version,
    windows_file_version,
)


def test_examples_from_spec() -> None:
    v = "20260923.1432"
    assert make_version(datetime(2026, 9, 23, 14, 32)) == v
    assert windows_file_version(v) == "2026.923.1432.0"
    assert android_version_code(v) == 262661432
    assert deb_version(v) == v


@pytest.mark.parametrize("bad", ["", "2026.09.23", "20260923", "20260923.143", "x0260923.1432"])
def test_rejects_other_formats(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_version(bad)


times = st.datetimes(min_value=datetime(2026, 1, 1), max_value=datetime(2099, 12, 31, 23, 59))


@given(times)
def test_round_trip(t: datetime) -> None:
    t = t.replace(second=0, microsecond=0)
    assert parse_version(make_version(t)) == t


@given(times, times)
def test_android_code_increases_with_time(a: datetime, b: datetime) -> None:
    va, vb = make_version(a), make_version(b)
    if parse_version(va) < parse_version(vb):
        assert android_version_code(va) < android_version_code(vb)


def test_every_build_command_stamps_a_new_version(tmp_path: Path, monkeypatch: Any) -> None:
    """06 8 (20 GAP-73 3): `tools.build deb` and `module` stamp VERSION before building
    (the first real install shipped a 3-day-old stamp); a wrong command stamps nothing."""
    import tools.build as build

    stamp = tmp_path / "VERSION"
    stamp.write_text("20260923.2359\n", encoding="ascii")
    order: list[str] = []

    def fake_build(mid: str, out: Path) -> Path:
        order.append("build " + stamp.read_text().strip())  # the stamp it builds with
        return out / "x.zlm"

    monkeypatch.setattr(build, "build_module", fake_build)
    fixed = SimpleNamespace(now=lambda: datetime(2026, 9, 26, 16, 5))
    monkeypatch.setattr(build, "datetime", fixed)
    assert build.main(["b", "module", "iptv", "--out", str(tmp_path)], stamp) == 0
    assert order == ["build 20260926.1605"]
    stamp.write_text("20260923.2359\n", encoding="ascii")
    assert build.main(["b", "module"], stamp) == 6  # usage: nothing stamped, nothing built
    assert stamp.read_text().strip() == "20260923.2359" and len(order) == 1
