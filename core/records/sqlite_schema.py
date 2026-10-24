"""Open a SQLite file at the expected ``schema_version`` (docs/02-data.md 11, INS-025).

``PRAGMA user_version`` holds the version. Older files are upgraded step by step after a
copy ``<name>.bak-<build>`` is made; a failed step restores the copy and refuses to start.
A file newer than the program (an older build reinstalled) is never touched.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path

from core.protocol.envelope import ZlError

Migration = Callable[[sqlite3.Connection], None]


def open_db(path: Path, migrations: Sequence[Migration], build: str) -> sqlite3.Connection:
    """``migrations[i]`` upgrades version i to i+1; the target is ``len(migrations)``."""
    target = len(migrations)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists() and path.stat().st_size > 0
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > target:
            raise _error(path, version, build, "is newer than this program")
        if version < target:
            conn.close()
            backup = path.with_name(f"{path.name}.bak-{build}")
            if existed:
                shutil.copy2(path, backup)
            conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
            try:
                for step in range(version, target):
                    conn.execute("BEGIN")
                    migrations[step](conn)
                    conn.execute(f"PRAGMA user_version = {step + 1}")
                    conn.execute("COMMIT")
            except Exception as e:
                conn.close()
                if existed:
                    shutil.copy2(backup, path)
                else:
                    path.unlink(missing_ok=True)
                raise _error(path, version, build, f"could not be upgraded: {e!r}") from e
        conn.execute("PRAGMA journal_mode = WAL")  # readers never block the writer
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn
    except BaseException:
        conn.close()
        raise


def _error(path: Path, version: int, build: str, what: str) -> ZlError:
    return ZlError(
        "INS-025",
        f"{path.name} format {version} {what}",
        f"Build {build} cannot use it.",
        "Update this PC to the newest build.",
        detail=str(path),
    )
