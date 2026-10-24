"""Coding rules that a test can check (docs/20-spec-gaps.md 1-1).

G-14: one node request is built in one helper only.
G-16: values written in the spec tables equal the code constants (no eye comparison).
"""

from __future__ import annotations

import re
from pathlib import Path

from core import node
from modules.iptv.defaults import DEFAULTS
from modules.iptv.marker import CELLS, cell_px
from modules.iptv.sender import prepare
from modules.iptv.sender.commands import QUALITIES, TONE_HZ, prepare_command

ROOT = Path(__file__).resolve().parents[1]
IPTV = (ROOT / "docs/modules/iptv.md").read_text(encoding="utf-8")
EXC = (ROOT / "docs/09-exceptions.md").read_text(encoding="utf-8")


def row(doc: str, head: str) -> list[str]:
    line = next(x for x in doc.splitlines() if x.startswith(f"| {head}"))
    return [c.strip() for c in line.strip("|").split("|")]


def test_g14_heartbeat_request_has_one_helper() -> None:
    uses = [
        f"{p.relative_to(ROOT)}:{i}"
        for p in sorted((ROOT / "controller").rglob("*.py"))
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if '"/v1/heartbeat"' in line
    ]
    assert len(uses) == 1, f"build heartbeat requests with get_heartbeat() only: {uses}"


def test_g16_channel_table_matches_the_code() -> None:
    """iptv 2: groups and port, service ID per channel."""
    groups = row(IPTV, "멀티캐스트 그룹:포트")[1:4]
    want = [f"{g}:{DEFAULTS['S-14']['port']}" for g in DEFAULTS["S-14"]["groups"]]
    assert groups == want
    ids = [int(x, 16) for x in row(IPTV, "서비스 ID(TS)")[1:4]]
    for ch, sid in zip((1, 2, 3), ids, strict=True):
        cmd = prepare_command("ffmpeg", QUALITIES["HD"], ch, Path("x.ts"))
        assert cmd[cmd.index("-mpegts_service_id") + 1] == str(sid)


def test_g16_generated_video_values_match_the_code() -> None:
    """iptv 1-1 tones and length, 1-1-2 stall and disk margin, 3.4 marker cells."""
    tones = re.search(r"CH1 ([\d.]+) kHz, CH2 ([\d.]+) kHz, CH3 ([\d.]+) kHz", IPTV)
    assert tones and [float(t) * 1000 for t in tones.groups()] == [TONE_HZ[c] for c in (1, 2, 3)]
    assert "길이 10분" in IPTV and prepare.GENERATED_S == 600
    stall = re.search(r"\*\*(\d+)초 동안 진행이 없으면\*\*", IPTV)
    assert stall and stall.group(1) == str(int(prepare.STALL_S))
    assert "(예상 크기 + 1 GB)" in IPTV and prepare.DISK_MARGIN_MB == 1024
    cells = re.search(r"UHD (\d+) px, HD (\d+) px\)\. \*\*왼쪽 아래\*\*.*?한 줄 (\d+)칸", IPTV)
    assert cells and [int(x) for x in cells.groups()] == [cell_px(2160), cell_px(1080), CELLS]


def test_g16_disk_limits_match_the_code() -> None:
    """09 RCV-008: warn below 1 GB, stop below 200 MB."""
    text = row(EXC, "RCV-008")[3]
    assert "여유 1 GB 미만 경고" in text and node.DISK_WARN_MB == 1024
    assert "200 MB 미만이면" in text and node.DISK_STOP_MB == 200


def test_id_pattern_sees_ids_before_hangul() -> None:
    """20 G-18: the checker's own pattern must see "S-47과" (a \\b pattern did not)."""
    assert re.findall(r"(?<![A-Za-z0-9-])S-\d{2}(?![0-9])", "S-47과 S-10, XS-11 S-123") == [
        "S-47",
        "S-10",
    ]


def test_setting_and_gap_ids_never_mix() -> None:
    """20 GAP-53: settings are S-nn (16), spec gaps are GAP-nn (20). Every S-nn written
    anywhere is a real setting and every GAP-nn a real gap, so one ID never means two."""
    settings = set(
        re.findall(
            r"^\| (S-\d+) \|", (ROOT / "docs/16-settings.md").read_text(encoding="utf-8"), re.M
        )
    )
    gaps = set(
        re.findall(
            r"^\| (GAP-\d+) \|", (ROOT / "docs/20-spec-gaps.md").read_text(encoding="utf-8"), re.M
        )
    )
    assert not re.search(
        r"^\| S-\d+ \|", (ROOT / "docs/20-spec-gaps.md").read_text(encoding="utf-8"), re.M
    )
    files = [*(ROOT / "docs").rglob("*.md"), ROOT / "CLAUDE.md"]
    files += [p for top in ("core", "controller", "agent", "modules", "tests", "tools")
              for p in (ROOT / top).rglob("*.py")]  # fmt: skip
    wrong = []
    for p in files:
        text = p.read_text(encoding="utf-8")
        # not \b: Hangul counts as a word character, so "S-47과" has no word boundary
        for sid in set(re.findall(r"(?<![A-Za-z0-9-])S-\d{2}(?![0-9])", text)) - settings:
            wrong.append(f"{p.relative_to(ROOT)}: {sid} is not a setting")
        for gid in set(re.findall(r"(?<![A-Za-z0-9-])GAP-\d{2}(?![0-9])", text)) - gaps:
            wrong.append(f"{p.relative_to(ROOT)}: {gid} is not a gap")
    assert not wrong, wrong


REPLACED = [  # (old citation, what must stand beside it), 20 GAP-41 / GAP-53
    (r"(?<![0-9])3376(?![0-9])", "9776"),
    (r"(?<![0-9])3810(?![0-9])", "9777"),
    (r"TR 101 290[^|]*V1\.2\.1|V1\.2\.1", "V1.4.1"),
    (r"V1\.5\.2", "V2.1.1"),
]


def test_g19_replaced_standards_are_not_cited_alone() -> None:
    """20 G-19: a replaced standard may be named only on a line that also names its
    replacement (history in 20 is exempt)."""
    wrong = []
    for p in [*(ROOT / "docs").rglob("*.md"), ROOT / "CLAUDE.md"]:
        if p.name == "20-spec-gaps.md":
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            for old, new in REPLACED:
                if re.search(old, line) and new not in line:
                    wrong.append(f"{p.relative_to(ROOT)}:{i}: {old} without {new}")
    assert not wrong, wrong


def test_g16_package_depends_match_the_code() -> None:
    """06 4.3 Depends / Recommends rows = tools/build.py (20 GAP-73 2: libxcb-cursor0)."""
    from tools.build import DEPENDS, RECOMMENDS

    doc = (ROOT / "docs/06-install.md").read_text(encoding="utf-8")
    for label, expected in (("Depends", DEPENDS), ("Recommends", RECOMMENDS)):
        word = "의존" if label == "Depends" else "권장"
        row = re.search(rf"^\| 패키지 {word}\({label}\) \| `([^`]+)` \|", doc, re.M)
        assert row and tuple(x.strip() for x in row.group(1).split(",")) == expected, label


def test_controller_reads_files_as_utf8() -> None:
    """GAP-129: the Windows Controller read run-state.json with the system code page
    (cp949); a Korean movie name made the read fail and every clock time vanished from
    the report. Code that runs on the Controller names its encoding."""
    import re

    bad = []
    for f in sorted((ROOT / "controller").rglob("*.py")):
        text = f.read_text(encoding="utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            if (
                re.search(r"\.read_text\(\)|[^_]open\([^)]*\)\s*as", line)
                and "encoding" not in line
            ):
                if "open(" in line and (
                    "'rb'" in line
                    or '"rb"' in line
                    or '"wb"' in line
                    or "'ab'" in line
                    or '"ab"' in line
                ):
                    continue
                bad.append(f"{f.relative_to(ROOT)}:{n}: {line.strip()}")
    assert bad == [], bad
