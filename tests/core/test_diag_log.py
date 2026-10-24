"""Diagnostic log rotation (docs/02-data.md 11)."""

from __future__ import annotations

import logging
from pathlib import Path

from core import diag_log


def test_rotation_keeps_at_most_n_files_of_bounded_size(tmp_path: Path) -> None:
    h = diag_log.setup(tmp_path, "agent", max_bytes=2000, max_files=4)
    log = logging.getLogger("zeroloss.test")
    try:
        for i in range(2000):
            log.info("line %05d %s", i, "x" * 40)
    finally:
        logging.getLogger().removeHandler(h)
        h.close()
    files = sorted(tmp_path.glob("agent.log*"))
    assert len(files) == 4
    assert all(f.stat().st_size <= 2000 + 200 for f in files)
    assert "line 01999" in (tmp_path / "agent.log").read_text()  # newest kept


def test_defaults_match_the_document() -> None:
    assert diag_log.MAX_BYTES == 20 * 1024 * 1024 and diag_log.MAX_FILES == 10
