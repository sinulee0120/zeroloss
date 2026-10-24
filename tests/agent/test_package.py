"""Package format (docs/02-data.md 13): everything is checked before anything is
unpacked; any changed, missing, extra or unsafe file is INS-006."""

from __future__ import annotations

import json
import random
import zipfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agent.deployer.errors import DeployError
from agent.deployer.package import open_package
from tests.agent.conftest import REPO, module_package


@pytest.fixture(scope="module")
def iptv_zlm(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return module_package(REPO / "modules" / "iptv", tmp_path_factory.mktemp("zlm"))


def rewrite(src: Path, dst: Path, change: dict[str, bytes | None]) -> Path:
    """Copy a zip, replacing (bytes) or dropping (None) entries, adding new ones."""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for zi in zin.infolist():
            if zi.filename in change:
                if change[zi.filename] is not None:
                    zout.writestr(zi, change[zi.filename])  # type: ignore[arg-type]
                continue
            zout.writestr(zi, zin.read(zi))
        for name, data in change.items():
            if data is not None and name not in zin.namelist():
                zout.writestr(name, data)
    return dst


def rejected(path: Path) -> str:
    with pytest.raises(DeployError) as e:
        open_package(path)
    assert e.value.code == "ZL-INS-006"
    return e.value.detail


def test_real_module_package_opens(iptv_zlm: Path) -> None:
    pkg = open_package(iptv_zlm)
    assert (pkg.kind, pkg.id, pkg.info["os"]) == ("module", "iptv", "any")
    assert "module.json" in pkg.files and "sender/prepare.py" in pkg.files
    assert not [n for n in pkg.files if "__pycache__" in n or n.endswith(".pyc")]


def test_same_input_same_bytes(tmp_path: Path) -> None:
    a = module_package(REPO / "modules" / "iptv", tmp_path / "a")
    b = module_package(REPO / "modules" / "iptv", tmp_path / "b")
    assert a.read_bytes() == b.read_bytes()


def test_changed_missing_extra_and_unsafe_files(iptv_zlm: Path, tmp_path: Path) -> None:
    assert "hash differs" in rejected(rewrite(iptv_zlm, tmp_path / "1.zlm",
                                              {"payload/module.json": b"{}"}))  # fmt: skip
    assert "missing" in rejected(rewrite(iptv_zlm, tmp_path / "2.zlm", {"payload/run.py": None}))
    assert "unlisted" in rejected(rewrite(iptv_zlm, tmp_path / "3.zlm", {"payload/x.py": b"1"}))
    for bad in ("../evil.py", "/abs.py", "payload\\win.py", "C:/x.py"):
        assert "unsafe" in rejected(rewrite(iptv_zlm, tmp_path / "4.zlm", {bad: b"1"}))
    assert "missing or broken" in rejected(rewrite(iptv_zlm, tmp_path / "5.zlm",
                                                   {"package.json": None}))  # fmt: skip
    info = json.loads(zipfile.ZipFile(iptv_zlm).read("package.json"))
    for key, value in (("kind", "app"), ("build", "1.0"), ("id", "IPTV"), ("os", "mac")):
        body = json.dumps(info | {key: value}).encode()
        rejected(rewrite(iptv_zlm, tmp_path / "6.zlm", {"package.json": body}))
    (tmp_path / "7.zlm").write_bytes(b"not a zip")
    assert "not a readable zip" in rejected(tmp_path / "7.zlm")


def test_a_link_in_the_zip_is_refused(iptv_zlm: Path, tmp_path: Path) -> None:
    out = tmp_path / "link.zlm"
    with zipfile.ZipFile(iptv_zlm) as zin, zipfile.ZipFile(out, "w") as zout:
        for zi in zin.infolist():
            zout.writestr(zi, zin.read(zi))
        link = zipfile.ZipInfo("payload/link")
        link.external_attr = (0o120777) << 16
        zout.writestr(link, "/etc/passwd")
    assert "unsafe" in rejected(out)


@settings(max_examples=30, deadline=None)
@given(st.integers(0, 2**32 - 1))
def test_random_byte_flips_never_pass(tmp_path_factory: pytest.TempPathFactory, seed: int) -> None:
    """Monkey (playbook): a package with any byte changed opens only if the change
    did not touch what is checked (then it is still exactly the same package)."""
    src = module_package(REPO / "modules" / "iptv", tmp_path_factory.mktemp("flip"))
    rng = random.Random(seed)
    data = bytearray(src.read_bytes())
    for _ in range(rng.randint(1, 4)):
        data[rng.randrange(len(data))] ^= 1 << rng.randrange(8)
    bad = src.with_name("flipped.zlm")
    bad.write_bytes(bytes(data))
    try:
        pkg = open_package(bad)
    except DeployError as e:
        assert e.code == "ZL-INS-006"
        return
    assert pkg.files == open_package(src).files  # zip metadata only (e.g. a timestamp)
