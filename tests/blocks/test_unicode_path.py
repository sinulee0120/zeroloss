"""Korean file names in NFC or NFD are found and shown the same way (iptv 1-1-1)."""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Literal

from core.blocks.unicode_path import display_name, find_file

NAME = "시험 영상 UHD (1) 🎬.ts"


def test_nfc_and_nfd_names_find_the_same_file(tmp_path: Path) -> None:
    forms: list[tuple[Literal["NFC", "NFD"], Literal["NFC", "NFD"]]] = [
        ("NFC", "NFD"),
        ("NFD", "NFC"),
        ("NFC", "NFC"),
    ]
    for stored, asked in forms:
        d = tmp_path / stored / asked
        folder = d / unicodedata.normalize(stored, "한글 폴더")
        folder.mkdir(parents=True)
        f = folder / unicodedata.normalize(stored, NAME)
        f.write_bytes(b"x")
        ask = d / unicodedata.normalize(asked, "한글 폴더") / unicodedata.normalize(asked, NAME)
        assert find_file(ask) == f


def test_missing_file_is_none(tmp_path: Path) -> None:
    assert find_file(tmp_path / "없음" / NAME) is None
    assert find_file(tmp_path / NAME) is None


def test_display_name_is_nfc() -> None:
    nfd = unicodedata.normalize("NFD", "/x/" + NAME)
    assert display_name(nfd) == unicodedata.normalize("NFC", NAME)
    assert display_name(Path("/a/b") / NAME) == NAME
