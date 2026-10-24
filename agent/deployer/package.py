"""ZeroLoss packages: ``.zlm`` module, ``.zlp`` node (docs/02-data.md 13).

A zip with ``package.json``, ``payload-hashes.json`` and ``payload/``. ``open_package``
checks everything **before** anything is unpacked: every payload file listed with the
right SHA-256, nothing unlisted, no unsafe names (absolute, ``..``, backslash, links).
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from agent.deployer.errors import damaged

INFO, HASHES, PAYLOAD = "package.json", "payload-hashes.json", "payload/"
KINDS = ("module", "node", "tools", "tablet")  # tablet: the app + adb (01-overview D3)
OS_VALUES = ("any", "linux", "windows")
_ID = re.compile(r"^[a-z][a-z0-9_]*$")
_BUILD = re.compile(r"^\d{8}\.\d{4}$")
SKIP_DIRS = {"__pycache__"}
SKIP_SUFFIXES = (".pyc", ".pyo")


@dataclass(frozen=True)
class Package:
    path: Path
    info: dict[str, Any]
    files: dict[str, str]  # payload-relative name -> sha256

    @property
    def kind(self) -> str:
        return str(self.info["kind"])

    @property
    def id(self) -> str:
        return str(self.info["id"])

    @property
    def build(self) -> str:
        return str(self.info["build"])

    def extract(self, name: str, dest: Path) -> str:
        """Write one payload file to ``dest``; returns its SHA-256 (checked again)."""
        digest = hashlib.sha256()
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with (
                zipfile.ZipFile(self.path) as zf,
                zf.open(PAYLOAD + name) as src,
                dest.open("wb") as out,
            ):
                while block := src.read(1 << 20):
                    digest.update(block)
                    out.write(block)
        except READ_ERRORS as e:
            dest.unlink(missing_ok=True)
            raise damaged(f"{name} cannot be unpacked: {e}") from None
        got = digest.hexdigest()
        if got != self.files[name]:
            dest.unlink(missing_ok=True)
            raise damaged(f"{name} changed while unpacking")
        return got


def payload_files(src: Path) -> list[Path]:
    """Files of a folder that go into a package (no caches), sorted."""
    out = []
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if any(part in SKIP_DIRS for part in rel.parts) or p.suffix in SKIP_SUFFIXES:
            continue
        if p.is_symlink():
            raise ValueError(f"links are not packed: {rel}")
        if p.is_file():
            out.append(p)
    return out


def build_package(src: Path, out: Path, info: dict[str, Any]) -> Path:
    """Pack ``src`` as ``payload/`` with the description and hash list."""
    check_info(info)
    files = payload_files(src)
    hashes = {}
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in files:
            name = p.relative_to(src).as_posix()
            data = p.read_bytes()
            hashes[PAYLOAD + name] = hashlib.sha256(data).hexdigest()
            zf.writestr(_entry(PAYLOAD + name), data)
        zf.writestr(_entry(INFO), json.dumps(info, indent=2) + "\n")
        body = {"schema_version": 1, "files": hashes}
        zf.writestr(_entry(HASHES), json.dumps(body, indent=2) + "\n")
    tmp.replace(out)
    return out


def _entry(name: str) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))  # same bytes every build
    zi.external_attr = (stat.S_IFREG | 0o644) << 16
    zi.compress_type = zipfile.ZIP_DEFLATED
    return zi


def check_info(info: dict[str, Any]) -> None:
    if info.get("schema_version") != 1:
        raise damaged("package.json schema_version must be 1")
    if info.get("kind") not in KINDS:
        raise damaged(f"package.json kind must be one of {KINDS}")
    if not isinstance(info.get("id"), str) or not _ID.match(info["id"]):
        raise damaged("package.json id must be lower case letters, digits, _")
    if not isinstance(info.get("build"), str) or not _BUILD.match(info["build"]):
        raise damaged("package.json build must be YYYYMMDD.HHMM")
    if info.get("os") not in OS_VALUES:
        raise damaged(f"package.json os must be one of {OS_VALUES}")
    if info["kind"] == "module":
        for key in ("core_api_min", "core_api_max"):
            if not isinstance(info.get(key), int) or isinstance(info.get(key), bool):
                raise damaged(f"package.json {key} must be a whole number")


# Whatever a damaged zip raises while it is read (found by the byte-flip monkey test).
READ_ERRORS = (OSError, EOFError, ValueError, NotImplementedError, zipfile.BadZipFile, zlib.error)


def open_package(path: Path) -> Package:
    """Read and check a package without unpacking anything (INS-006 on any fault)."""
    try:
        return _open(path)
    except READ_ERRORS as e:
        raise damaged(f"not a readable zip: {type(e).__name__}: {e}") from None


def _open(path: Path) -> Package:
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        if len(names) != len(set(names)):
            raise damaged("the same name twice in the zip")
        for zi in zf.infolist():
            _safe(zi)
        try:
            info = json.loads(zf.read(INFO))
            hashes = json.loads(zf.read(HASHES))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as e:
            raise damaged(f"package.json or payload-hashes.json missing or broken: {e}") from None
        if not isinstance(info, dict) or not isinstance(hashes, dict):
            raise damaged("package.json and payload-hashes.json must be objects")
        check_info(info)
        listed = hashes.get("files")
        if hashes.get("schema_version") != 1 or not isinstance(listed, dict):
            raise damaged("payload-hashes.json must have schema_version 1 and files")
        present = {n for n in names if not n.endswith("/")} - {INFO, HASHES}
        if present != set(listed):
            extra = sorted(present - set(listed))[:5]
            missing = sorted(set(listed) - present)[:5]
            raise damaged(f"unlisted files {extra}, missing files {missing}")
        files = {}
        for name, want in sorted(listed.items()):
            if not name.startswith(PAYLOAD) or not isinstance(want, str):
                raise damaged(f"bad entry in payload-hashes.json: {name}")
            digest = hashlib.sha256()
            with zf.open(name) as f:
                while block := f.read(1 << 20):
                    digest.update(block)
            if digest.hexdigest() != want.lower():
                raise damaged(f"hash differs: {name}")
            files[name[len(PAYLOAD) :]] = want.lower()
    return Package(path, info, files)


def _safe(zi: zipfile.ZipInfo) -> None:
    name = zi.filename
    kind = (zi.external_attr >> 16) & 0o170000
    parts = PurePosixPath(name).parts
    if (
        not name
        or name.startswith("/")
        or "\\" in name
        or ":" in name
        or ".." in parts
        or kind == stat.S_IFLNK
    ):
        raise damaged(f"unsafe name in the package: {name!r}")
