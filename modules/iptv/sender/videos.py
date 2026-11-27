"""The sender PC's video folder (docs/modules/iptv.md 1-1, 20 GAP-195).

2026-11-27 user ("35mbs파일 위치는 고정이야? 새로 설치하면 사용자가 못바꿔?" -> "바꾸고"):
the user's movies sit at the top of one folder and the videos ZeroLoss makes for sending
in ``generated/`` under it. It is apart from the module cache, so a module removal or
update never deletes hours of making (UHD: hours per channel), and it is settable from
the Controller (S-70, e.g. a bigger disk); this PC remembers the choice.

Defaults: Ubuntu ``<data>/media`` (``/var/lib/zeroloss/media``, as before), Windows
``C:\\ZeroLoss\\Media`` (the spec's place; it was ``<data>\\media`` in the code). The
first start moves the made videos out of the old cache folder, and on Windows the
movies out of the old ``<data>\\media``.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from core.protocol.envelope import ZlError

WINDOWS_DEFAULT = Path(r"C:\ZeroLoss\Media")
SAVED = "videos.json"


def default_folder(data_dir: Path, platform: str = sys.platform) -> Path:
    return WINDOWS_DEFAULT if platform == "win32" else Path(data_dir) / "media"


def move_files(src: Path, dst: Path) -> tuple[int, int]:
    """Every file of ``src`` (not its sub-folders) into ``dst`` unless one of that name is
    there. A rename on the same disk; another disk is left as it is (hours of copying
    must not hold a command). Returns (moved, left)."""
    moved = left = 0
    if not src.is_dir() or src.resolve() == dst.resolve():
        return 0, 0
    dst.mkdir(parents=True, exist_ok=True)
    for f in sorted(src.iterdir()):
        if not f.is_file() or (dst / f.name).exists():
            continue
        try:
            os.replace(f, dst / f.name)  # the same disk: instant, never half a file
            moved += 1
        except OSError:
            left += 1
    return moved, left


class VideoFolder:
    def __init__(self, data_dir: Path, settings_dir: Path, old_generated: Path,
                 platform: str = sys.platform) -> None:  # fmt: skip
        self.default = default_folder(data_dir, platform)
        self._file = Path(settings_dir) / SAVED
        self.folder = self._saved() or self.default
        try:
            self._ready(self.folder)
        except ZlError:
            self.folder = self.default  # a saved folder gone (a disk out): the default
            self._ready(self.folder)
        move_files(Path(old_generated), self.generated)  # made before 2026-11-27
        if platform == "win32" and self.folder == WINDOWS_DEFAULT:
            move_files(Path(data_dir) / "media", self.folder)  # movies put in the old place

    @property
    def generated(self) -> Path:
        return self.folder / "generated"

    def _saved(self) -> Path | None:
        try:
            got = json.loads(self._file.read_text(encoding="utf-8")).get("folder")
        except (OSError, ValueError, AttributeError):
            return None
        return Path(got) if isinstance(got, str) and got else None

    def set(self, raw: Any) -> dict[str, Any]:
        """S-70 from the Controller: an absolute folder this PC can write, or "" for the
        default. The made videos move with it when it is on the same disk."""
        new = self.target(raw)
        if new == self.folder:
            return {"folder": str(self.folder), "moved": 0, "left": 0}
        self._ready(new)
        old = self.folder
        moved, left = move_files(old / "generated", new / "generated")
        self.folder = new
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            self._file.write_text(json.dumps({"folder": str(new)}), encoding="utf-8")
        except OSError:
            pass  # used now; the next start goes back to the default
        return {"folder": str(new), "moved": moved, "left": left,
                "old": str(old) if left else None}  # fmt: skip

    def target(self, raw: Any) -> Path:
        """The folder S-70 names: absolute, or "" for the default."""
        if raw is not None and not isinstance(raw, str):
            raise ZlError("DSC-012", "Request rejected", detail="videos must be a folder path")
        new = Path(raw.strip()) if raw and raw.strip() else self.default
        if not new.is_absolute():
            raise ZlError("RUN-014", "Video folder must be a full path",
                          f"{raw!r} is not one.",
                          "Write the whole path, e.g. /data/zeroloss or D:\\ZeroLoss.")  # fmt: skip
        return new

    @staticmethod
    def _ready(folder: Path) -> None:
        """Made and writable by this program (the node account on Ubuntu)."""
        try:
            (folder / "generated").mkdir(parents=True, exist_ok=True)
            probe = folder / f".zl-write-{uuid.uuid4().hex[:8]}"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as e:
            raise ZlError(
                "RUN-014", f"ZeroLoss cannot write the video folder {folder}",
                str(e),
                "Pick a folder on that PC that ZeroLoss can write (Ubuntu: "
                "sudo chown zeroloss: <folder>), or leave the setting empty.",
            ) from e  # fmt: skip

    def free_mb(self) -> int | None:
        try:
            return shutil.disk_usage(self.folder).free // (1 << 20)
        except OSError:
            return None
