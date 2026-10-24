"""Program diagnostic log (docs/02-data.md 11): ``logs/<process>.log``, a new file every
20 MB, at most 10 files, oldest removed first (at most 200 MB per process).

This is the developer log only. Measurement data, ``events.log`` and reports are never
rotated away by it.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

MAX_BYTES = 20 * 1024 * 1024
MAX_FILES = 10
FORMAT = "%(asctime)s.%(msecs)03d %(levelname)s %(threadName)s %(name)s: %(message)s"


def setup(
    logs_dir: Path, process: str, max_bytes: int = MAX_BYTES, max_files: int = MAX_FILES
) -> logging.Handler:
    """Attach the rotating file to the root logger. Returns the handler (to remove it)."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        logs_dir / f"{process}.log",
        maxBytes=max_bytes,
        backupCount=max_files - 1,  # the current file plus the kept old ones
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(FORMAT, "%Y-%m-%dT%H:%M:%S"))
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    return handler
