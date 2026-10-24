"""Code must match the spec documents (CLAUDE.md rules), checked before anything runs.

- Every ZL-<AREA>-<NNN> code used in code exists in docs/09-exceptions.md
  ("new exceptions go into the document first").
- Every S-<nn> setting used in code exists in docs/16-settings.md ("no setting outside
  the table").
- The IPTV 1-second short keys are exactly the ones docs/02-data.md 7 lists.
- Core never names a test type (no "if IPTV..." branches in core/).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE_DIRS = ("core", "controller", "agent", "modules")
CODE = re.compile(r"ZL-([A-Z]{3})-(\d{3})")
SETTING = re.compile(r"\bS-(\d{2})\b")


def code_files() -> list[Path]:
    return [p for d in CODE_DIRS for p in (ROOT / d).rglob("*.py")]


def doc(name: str) -> str:
    return (ROOT / "docs" / name).read_text(encoding="utf-8")


def test_every_error_code_in_code_is_documented() -> None:
    documented = set(re.findall(r"^\| ([A-Z]{3}-\d{3}) \|", doc("09-exceptions.md"), re.M))
    missing: dict[str, list[str]] = {}
    for p in code_files():
        text = p.read_text(encoding="utf-8")
        # both "ZL-RCV-017" and ZlError("RCV-017", ...) forms
        used = {f"{a}-{n}" for a, n in CODE.findall(text)}
        used |= set(re.findall(r'ZlError\(\s*"([A-Z]{3}-\d{3})"', text))
        for c in used - documented:
            missing.setdefault(c, []).append(str(p.relative_to(ROOT)))
    assert not missing, f"codes not in docs/09-exceptions.md: {missing}"


def test_every_setting_in_code_is_in_the_settings_table() -> None:
    table = set(re.findall(r"^\| S-(\d{2}) \|", doc("16-settings.md"), re.M))
    missing = {
        f"S-{n}": str(p.relative_to(ROOT))
        for p in code_files()
        for n in SETTING.findall(p.read_text(encoding="utf-8"))
        if n not in table
    }
    assert not missing, f"settings not in docs/16-settings.md: {missing}"


def test_record_keys_match_the_data_document() -> None:
    from modules.iptv import record_keys

    lines = [
        x
        for x in doc("02-data.md").splitlines()
        if "짧은 키를 쓴다" in x or "채널 전환 키" in x or "받는 카드·호스트 카운터" in x
    ]
    documented = {k for x in lines for k in re.findall(r"`([a-z0-9]+)`=", x)} | {"rssi"}
    in_code = {v for k, v in vars(record_keys).items() if k.isupper() and isinstance(v, str)}
    assert in_code == documented, (in_code - documented, documented - in_code)


def test_core_does_not_name_a_test_type() -> None:
    offenders = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "core").rglob("*.py")
        if re.search(r"\biptv\b|\biperf\b", p.read_text(encoding="utf-8"), re.I)
    ]
    assert not offenders, f"core/ must stay test-type neutral: {offenders}"
