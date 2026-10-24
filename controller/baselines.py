"""The last baseline test of each receiver and player (docs/05-standards.md 4.6.3, 02 2-13).

One job: keep, per ``<device>|<player>``, the result of the last baseline run that ran to
its planned end, in one JSON file only this Controller writes (atomically).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any


class BaselineStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def all(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        rows = data.get("baselines") if isinstance(data, dict) else None
        return {str(k): dict(v) for k, v in (rows or {}).items() if isinstance(v, dict)}

    def get(self, key: str) -> dict[str, Any] | None:
        return self.all().get(key)

    def put(self, rows: dict[str, dict[str, Any]]) -> None:
        with self._lock:
            merged = {**self.all(), **rows}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".baselines-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"schema_version": 1, "baselines": merged}, f, indent=1)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
