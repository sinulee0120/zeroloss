"""Build version helpers.

The only version is the build date and time, ``YYYYMMDD.HHMM``, stored in the
repository ``VERSION`` file (docs/06-install.md section 8). Other formats are
derived from it automatically and never typed by hand.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
_PATTERN = re.compile(r"^(\d{4})(\d{2})(\d{2})\.(\d{2})(\d{2})$")


def make_version(now: datetime) -> str:
    """Return the build version for the given local build time."""
    return now.strftime("%Y%m%d.%H%M")


def parse_version(version: str) -> datetime:
    """Parse ``YYYYMMDD.HHMM``; raise ValueError on any other format."""
    match = _PATTERN.match(version.strip())
    if match is None:
        raise ValueError(f"Invalid build version: {version!r} (expected YYYYMMDD.HHMM)")
    year, month, day, hour, minute = (int(g) for g in match.groups())
    return datetime(year, month, day, hour, minute)


def windows_file_version(version: str) -> str:
    """Windows 4-part file version, e.g. 20260923.1432 -> 2026.923.1432.0."""
    t = parse_version(version)
    return f"{t.year}.{t.month * 100 + t.day}.{t.hour * 100 + t.minute}.0"


def android_version_code(version: str) -> int:
    """Always-increasing Android versionCode: YY*10^7 + day_of_year*10^4 + HHMM."""
    t = parse_version(version)
    day_of_year = t.timetuple().tm_yday
    return (t.year % 100) * 10_000_000 + day_of_year * 10_000 + t.hour * 100 + t.minute


def deb_version(version: str) -> str:
    """Debian package version is the build version itself."""
    parse_version(version)
    return version


def read_version(path: Path = VERSION_FILE) -> str:
    version = path.read_text(encoding="ascii").strip()
    parse_version(version)
    return version


def write_version(now: datetime, path: Path = VERSION_FILE) -> str:
    version = make_version(now)
    path.write_text(version + "\n", encoding="ascii")
    return version
