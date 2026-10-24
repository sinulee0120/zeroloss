"""The manual's pictures come from the real screens (08 rule 7): the tool saves one
non-blank PNG per screen, offscreen."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QImage

from tools.manual_shots import shoot


def test_every_screen_is_saved_and_not_blank(tmp_path: Path) -> None:
    saved = shoot(tmp_path / "img")
    assert [p.stem for p in saved] == ["main-idle", "main-running", "main-fault", "storage"]
    for p in saved:
        img = QImage(str(p))
        assert img.width() >= 800 and not img.isNull(), p.name
        first = img.pixel(0, 0)
        assert any(img.pixel(x, y) != first for x in range(0, img.width(), 13)
                   for y in range(0, img.height(), 13)), p.name  # fmt: skip
