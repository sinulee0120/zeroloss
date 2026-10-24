"""Module packages in and out (docs/06-install.md 12, 10 4-1): exact removal, settings
kept, repair/upgrade/downgrade, foreign files never deleted, rollback on failure and
after a crash (INS-013), and a random monkey whose only allowed end states are
"as before" or "fully installed"."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from agent.deployer.errors import DeployError
from agent.deployer.manifest import Manifest
from agent.deployer.modules import ModuleStore
from agent.deployer.package import open_package
from tests.agent.conftest import REPO, demo_module, module_package, snapshot


def pkg_of(src: Path, tmp: Path, build: str = "20260925.1200", **info: Any) -> Any:
    return open_package(module_package(src, tmp / f"pkg-{build}-{random.random()}", build, **info))


def roots(layout: Any) -> tuple[Path, Path]:
    return layout.data_dir, layout.modules_dir.parent


def test_install_then_remove_leaves_what_was_there(store: Any, tmp_path: Path) -> None:
    ms, agent, layout = store
    before = snapshot(*roots(layout))
    r = ms.install(pkg_of(REPO / "modules" / "iptv", tmp_path))
    assert r["result"] == "installed" and r["from_build"] is None and agent.restarts == 1
    steps = ["verify", "check_idle", "backup", "remove_old", "copy", "restart_agent", "cleanup"]
    assert [s["name"] for s in r["steps"]] == steps
    code = layout.code("iptv")
    assert (code / "module.json").exists() and (code / "sender" / "prepare.py").exists()
    m = Manifest.load(layout.manifest("iptv"))
    assert m is not None and m.items[0].path == str(code)
    assert all(i.sha256 for i in m.items if i.type == "file")
    assert not list(layout.incoming.iterdir()) and not list(layout.rollback.iterdir())
    (layout.module_root("iptv") / "cache" / "generated").mkdir()  # the module's own cache
    (layout.module_root("iptv") / "cache" / "generated" / "ch1_HD_10M.ts").write_bytes(b"x")
    out = ms.remove("iptv", purge=True)
    assert out["left"] == [] and out["agent_restarted"]
    assert snapshot(*roots(layout)) == before  # nothing left, nothing else touched


def test_settings_survive_removal_and_come_back(store: Any, tmp_path: Path) -> None:
    ms, _, layout = store
    ms.install(pkg_of(REPO / "modules" / "iptv", tmp_path))
    settings = layout.module_root("iptv") / "settings" / "iptv.json"
    settings.write_text('{"S-11": 12}')
    out = ms.remove("iptv")
    assert str(settings.parent) in out["kept"] and settings.exists()
    assert not layout.code("iptv").exists() and not (layout.module_root("iptv") / "cache").exists()
    ms.install(pkg_of(REPO / "modules" / "iptv", tmp_path))
    assert settings.read_text() == '{"S-11": 12}'


def test_dry_run_lists_and_changes_nothing(store: Any, tmp_path: Path) -> None:
    ms, agent, layout = store
    ms.install(pkg_of(REPO / "modules" / "iptv", tmp_path))
    before, restarts = snapshot(*roots(layout)), agent.restarts
    out = ms.remove("iptv", dry_run=True)
    assert out["dry_run"] and str(layout.code("iptv")) in out["removed"]
    assert str(layout.code("iptv") / "module.json") in out["removed"]
    assert snapshot(*roots(layout)) == before and agent.restarts == restarts


def test_repair_upgrade_and_downgrade(store: Any, tmp_path: Path) -> None:
    ms, _, layout = store
    demo = demo_module(tmp_path / "src", text="one")
    ms.install(pkg_of(demo, tmp_path, "20260925.1200"))
    (layout.code("demo") / "module.py").unlink()  # broken by hand
    assert ms.install(pkg_of(demo, tmp_path, "20260925.1200"))["result"] == "repaired"
    assert (layout.code("demo") / "module.py").read_text() == "TEXT = 'one'\n"
    (demo / "sub" / "data.txt").unlink()  # the new build no longer has this file
    (demo / "module.py").write_text("TEXT = 'two'\n")
    r = ms.install(pkg_of(demo, tmp_path, "20260926.0900"))
    assert (r["result"], r["from_build"]) == ("upgraded", "20260925.1200")
    assert not (layout.code("demo") / "sub").exists()  # old file and its folder are gone
    with pytest.raises(DeployError) as e:
        ms.install(pkg_of(demo, tmp_path, "20260920.0000"))
    assert e.value.code == "ZL-INS-026"
    assert (layout.code("demo") / "module.py").read_text() == "TEXT = 'two'\n"


def test_refusals(store: Any, tmp_path: Path) -> None:
    ms, agent, layout = store
    agent.running_test = True
    with pytest.raises(DeployError) as e:
        ms.install(pkg_of(REPO / "modules" / "iptv", tmp_path))
    assert e.value.code == "ZL-INS-022" and not layout.code("iptv").exists()
    agent.running_test = False
    other = "windows" if ms.os_name == "linux" else "linux"
    with pytest.raises(DeployError) as e:
        ms.install(pkg_of(REPO / "modules" / "iptv", tmp_path, os=other))
    assert e.value.code == "ZL-INS-005"
    layout.code("iptv").mkdir()  # a folder nobody installed
    with pytest.raises(DeployError) as e:
        ms.install(pkg_of(REPO / "modules" / "iptv", tmp_path))
    assert e.value.code == "ZL-INS-021"
    with pytest.raises(DeployError):
        ms.remove("nothing")


def test_foreign_file_is_never_deleted(store: Any, tmp_path: Path) -> None:
    """06 rule 2: only exact names from the manifest; a stranger stays and is reported."""
    ms, _, layout = store
    ms.install(pkg_of(demo_module(tmp_path / "src"), tmp_path))
    stranger = layout.code("demo") / "sub" / "mine.txt"
    stranger.write_text("user file")
    with pytest.raises(DeployError) as e:
        ms.remove("demo")
    assert e.value.code == "ZL-INS-021" and "mine.txt" in e.value.why
    assert stranger.read_text() == "user file"
    stranger.unlink()
    assert ms.remove("demo")["left"] == []  # the kept list lets a second removal finish
    assert not layout.code("demo").exists()


def test_other_module_untouched_over_ten_rounds(store: Any, tmp_path: Path) -> None:
    """10 4-1: remove and reinstall A ten times; B stays byte for byte the same."""
    ms, _, layout = store
    ms.install(pkg_of(demo_module(tmp_path / "srcb", "bmod", "b"), tmp_path))
    b_before = snapshot(layout.code("bmod"))
    a = demo_module(tmp_path / "srca", "amod", "a")
    for _ in range(10):
        ms.install(pkg_of(a, tmp_path))
        (layout.module_root("amod") / "settings" / "s.json").write_text("{}")
        ms.remove("amod")
        assert not layout.code("amod").exists()
    assert snapshot(layout.code("bmod")) == b_before
    assert [m["id"] for m in ms.installed()] == ["bmod"]


@pytest.mark.parametrize("where", ["backup", "remove_old", "copy", "copy_half", "restart_agent"])
def test_failure_in_any_step_rolls_back(store: Any, tmp_path: Path, where: str) -> None:
    ms, agent, layout = store
    demo = demo_module(tmp_path / "src", text="one")
    ms.install(pkg_of(demo, tmp_path, "20260925.1200"))
    before = snapshot(*roots(layout))
    (demo / "module.py").write_text("TEXT = 'two'\n")
    (demo / "extra.py").write_text("X = 1\n")
    new = pkg_of(demo, tmp_path, "20260926.0900")
    if where == "restart_agent":
        agent.comes_back = False
    else:
        ms.fail_at = where
    with pytest.raises(DeployError):
        ms.install(new)
    agent.comes_back, ms.fail_at = True, ""
    assert snapshot(*roots(layout)) == before
    assert not new.path.exists()  # the received package is removed too (06 11.5 step 9)


def test_crash_mid_install_is_rolled_back_at_next_start(store: Any, tmp_path: Path) -> None:
    """INS-013: the Deployer died during copy; the next Deployer finds the journal."""
    ms, agent, layout = store
    demo = demo_module(tmp_path / "src", text="one")
    ms.install(pkg_of(demo, tmp_path, "20260925.1200"))
    before = snapshot(*roots(layout))
    (demo / "module.py").write_text("TEXT = 'two'\n")
    new = pkg_of(demo, tmp_path, "20260926.0900")

    def crash(*_: Any) -> None:
        raise KeyboardInterrupt  # like power loss: no clean-up code of ours runs after it

    ms.restore = crash
    with pytest.raises(KeyboardInterrupt):
        ms.fail_at = "copy_half"
        ms.install(new)
    assert layout.journal.exists()
    fresh = ModuleStore(layout, agent)
    assert "rolled back" in (fresh.recover() or "")
    assert snapshot(*roots(layout)) == before and not layout.journal.exists()
    assert fresh.recover() is None


def test_crash_mid_remove_is_finished_at_next_start(store: Any, tmp_path: Path) -> None:
    ms, agent, layout = store
    before = snapshot(*roots(layout))
    ms.install(pkg_of(demo_module(tmp_path / "src"), tmp_path))
    ms._journal({"operation": "remove", "kind": "module", "id": "demo", "step": "remove",
                 "purge": True})  # fmt: skip
    (layout.code("demo") / "module.py").unlink()  # the removal had started
    ModuleStore(layout, agent).recover()
    assert snapshot(*roots(layout)) == before


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_monkey_install_remove(store: Any, tmp_path: Path, seed: int) -> None:
    """Random installs, reinstalls, upgrades, removals, purges, failures and a stranger
    file. After every step: each module is either absent or exactly as its manifest
    says, and the other module is never touched."""
    ms, _, layout = store
    rng = random.Random(seed)
    srcs = {m: demo_module(tmp_path / f"src-{m}", m, m) for m in ("amod", "bmod")}
    builds = ["20260925.1200", "20260926.0900", "20260927.0800"]
    for i in range(25):
        mid = rng.choice(list(srcs))
        op = rng.choice(["install", "install", "remove", "purge", "fail", "dry"])
        try:
            if op in ("install", "fail"):
                (srcs[mid] / "module.py").write_text(f"TEXT = {i}\n")
                ms.fail_at = rng.choice(["copy", "copy_half", "remove_old"]) if op == "fail" else ""
                ms.install(pkg_of(srcs[mid], tmp_path, rng.choice(builds)))
            elif op == "dry" and Manifest.load(layout.manifest(mid)):
                ms.remove(mid, dry_run=True)
            elif Manifest.load(layout.manifest(mid)):
                ms.remove(mid, purge=op == "purge")
        except DeployError as e:
            assert e.code in ("ZL-INS-026", "ZL-SYS-008"), e.to_dict()
        ms.fail_at = ""
        for m in srcs:
            check_consistent(layout, m)
    assert not layout.journal.exists()


def check_consistent(layout: Any, mid: str) -> None:
    m = Manifest.load(layout.manifest(mid))
    code = layout.code(mid)
    if m is None:
        assert not code.exists(), f"{mid}: code without a manifest"
        return
    listed = {i.path for i in m.items} | {i.path for i in m.keep}
    on_disk = {str(p) for p in code.rglob("*")} | {str(code)}
    assert on_disk <= listed, f"{mid}: unlisted {on_disk - listed}"
    for item in m.items:
        if item.type == "file":
            assert Path(item.path).exists(), item.path
    assert json.loads((code / "module.json").read_text())["id"] == mid
