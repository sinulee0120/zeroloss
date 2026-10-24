"""Files a module asks for, brought from the desktop users' folders (docs/12-architecture.md
1장 ``media_import``, 20 GAP-109).

The node account cannot read a person's home folder (750), and a person should not need
sudo to give the tool a file. So the Deployer (root) does it for every installed module
whose ``module.json`` has ``media_import`` — it never knows what the files are for.

One pass (only while the node is IDLE): for each module's ask, the top-level files in
``/home/<user>/<from>`` with one of its extensions go into ``<data>/<into>``:
a hard link when others may read the file (no copy, and the owner and mode of the
person's file are never touched — a link shares them), else a copy owned by the node
account when there is room. A changed original is brought again, a gone one is removed.
A file a person put in ``<into>`` is never touched. Everything brought is listed in
``<into>/.imported.json``, so removal takes exactly that (G-11, 12 1-3).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RECORD = ".imported.json"
EVERY_S = 60.0
ROOM_BYTES = 1024 * 1024 * 1024  # a copy leaves at least 1 GB free
_PART = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_. -]{0,63}$")  # one plain folder name


@dataclass(frozen=True)
class Ask:
    module: str
    src: str  # a folder name inside each home, e.g. "Videos"
    into: str  # a folder name inside the node data folder, e.g. "media"
    ext: tuple[str, ...]  # lower case, without the dot


def asks(modules_dir: Path) -> list[Ask]:
    """What the installed modules declare; a bad declaration is skipped, never guessed."""
    out = []
    for manifest in sorted(modules_dir.glob("*/module.json")) if modules_dir.is_dir() else []:
        try:
            m = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        a = m.get("media_import")
        if not isinstance(a, dict):
            continue
        src, into, ext = a.get("from"), a.get("into"), a.get("ext")
        if not (isinstance(src, str) and _PART.match(src) and isinstance(into, str)
                and _PART.match(into) and isinstance(ext, list) and ext):  # fmt: skip
            continue
        exts = tuple(str(e).lower().lstrip(".") for e in ext if isinstance(e, str) and e.isalnum())
        if exts:
            out.append(Ask(str(m.get("id", manifest.parent.name)), src, into, exts))
    return out


class MediaImport:
    def __init__(
        self,
        root: Path,
        data_dir: Path,
        modules_dir: Path,
        chown: Callable[[Path], None],
        log: Callable[[str, str], None],
        free: Callable[[Path], int] = lambda p: shutil.disk_usage(p).free,
    ) -> None:
        self.root, self.data_dir, self.modules_dir = root, data_dir, modules_dir
        self.chown, self.log, self.free = chown, log, free

    # ---------------------------------------------------------------- one pass

    def run(self) -> list[str]:
        """Bring, refresh and drop; returns what changed (for the log)."""
        done: list[str] = []
        for ask in asks(self.modules_dir):
            target = self.data_dir / ask.into
            if not target.is_dir():
                target.mkdir(parents=True)
                self.chown(target)
            rec = _load(target)
            wanted = self._wanted(ask)
            for name, r in list(rec.items()):  # drop what is gone or changed hands
                src = wanted.get(name)
                if r.get("module") == ask.module and (src is None or str(src) != r.get("src")):
                    self._drop(target, name, r)
                    del rec[name]
                    done.append(f"removed {ask.into}/{name}")
            for name, src in wanted.items():
                got = self._bring(ask, target, name, src, rec.get(name))
                if got is not None:
                    rec[name] = got
                    done.append(f"{got['kind']} {ask.into}/{name}")
            _save(target, rec, self.chown)
        return done

    def remove(self, module: str | None = None) -> list[str]:
        """Node or module removal: exactly what was brought, nothing a person put there."""
        left: list[str] = []
        for target in self._targets():
            rec = _load(target)
            for name, r in list(rec.items()):
                if module is None or r.get("module") == module:
                    if not self._drop(target, name, r):
                        left.append(f"{target / name}: still there")
                    del rec[name]
            if rec:
                _save(target, rec, self.chown)
            else:
                (target / RECORD).unlink(missing_ok=True)
        return left

    # ---------------------------------------------------------------- helpers

    def _targets(self) -> list[Path]:
        return sorted(p.parent for p in self.data_dir.glob(f"*/{RECORD}"))

    def _wanted(self, ask: Ask) -> dict[str, Path]:
        """name -> source; the first user (by name) wins a name two people use."""
        found: dict[str, Path] = {}
        home = self.root / "home"
        for user in sorted(home.iterdir()) if home.is_dir() else []:
            folder = user / ask.src
            if not folder.is_dir() or folder.is_symlink():
                continue
            for f in sorted(folder.iterdir()):
                if (f.is_file() and not f.is_symlink() and not f.name.startswith(".")
                        and f.suffix.lower().lstrip(".") in ask.ext):  # fmt: skip
                    found.setdefault(f.name, f)
        return found

    def _bring(
        self, ask: Ask, target: Path, name: str, src: Path, old: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        st = src.stat()
        key = {"src": str(src), "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
        dst = target / name
        if old is not None and all(old.get(k) == v for k, v in key.items()) and dst.exists():
            return None  # as it was
        if old is None and (dst.exists() or dst.is_symlink()):
            return None  # a person's own file of the same name
        tmp = target / f".{name}.part"
        tmp.unlink(missing_ok=True)
        kind = None
        if st.st_mode & stat.S_IROTH:  # the node account can read it through a link
            try:
                os.link(src, tmp)
                kind = "link"
            except OSError:  # another file system
                tmp.unlink(missing_ok=True)
        if kind is None:
            if self.free(target) < st.st_size + ROOM_BYTES:
                self.log("warn", f"media_import: no room to copy {src} ({st.st_size} bytes)")
                return None
            shutil.copyfile(src, tmp)
            self.chown(tmp)
            kind = "copy"
        if old is not None:
            self._drop(target, name, old)
        tmp.replace(dst)
        return {**key, "module": ask.module, "kind": kind, "dst_ino": dst.stat().st_ino}

    def _drop(self, target: Path, name: str, r: dict[str, Any]) -> bool:
        """Only our own file: the one whose inode we recorded (a person may have put
        another one there since). True when nothing of ours is left."""
        dst = target / name
        try:
            if dst.stat().st_ino != r.get("dst_ino"):
                return True  # not ours any more: leave it
        except OSError:
            return True
        with contextlib.suppress(OSError):
            dst.unlink()
        return not dst.exists()


def _load(target: Path) -> dict[str, dict[str, Any]]:
    try:
        doc = json.loads((target / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = doc.get("files") if isinstance(doc, dict) else None
    return (
        {k: v for k, v in files.items() if isinstance(v, dict)} if isinstance(files, dict) else {}
    )


def _save(target: Path, rec: dict[str, dict[str, Any]], chown: Callable[[Path], None]) -> None:
    tmp = target / (RECORD + ".tmp")
    tmp.write_text(json.dumps({"schema_version": 1, "files": rec}, indent=1), encoding="utf-8")
    chown(tmp)
    tmp.replace(target / RECORD)
