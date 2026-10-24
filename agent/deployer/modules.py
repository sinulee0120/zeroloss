"""Module packages in and out (docs/06-install.md 12, docs/12-architecture.md 1-3).

Install: verify -> check_idle -> backup -> remove_old -> copy -> restart_agent -> cleanup.
Any failure after the backup restores the module as it was (code and manifest) and
restarts the node again. A journal (``journal.json``) is written before anything
changes; a Deployer that starts and finds one restores the backup (INS-013). New files
are listed in ``new-items.jsonl`` as they are written, so a crash mid-copy can remove
exactly what was written.
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.deployer.errors import DeployError
from agent.deployer.manifest import Item, Manifest, Removal, remove_items, still_there, utc_now
from agent.deployer.package import Package


def this_os() -> str:
    return "windows" if sys.platform.startswith("win") else "linux"


@dataclass(frozen=True)
class Layout:
    """Where things live on a node (06 12)."""

    data_dir: Path
    modules_dir: Path

    @property
    def deployer(self) -> Path:
        return self.data_dir / "deployer"

    @property
    def incoming(self) -> Path:
        return self.deployer / "incoming"

    @property
    def rollback(self) -> Path:
        return self.deployer / "rollback"

    @property
    def journal(self) -> Path:
        return self.deployer / "journal.json"

    def manifest(self, module_id: str) -> Path:
        return self.deployer / "manifests" / f"module-{module_id}.json"

    def code(self, module_id: str) -> Path:
        return self.modules_dir / module_id

    def module_root(self, module_id: str) -> Path:
        return self.data_dir / "modules" / module_id


class Agent:
    """What the installer needs from the node on this PC (``agent_link.AgentLink``)."""

    def busy(self) -> bool:  # a test is running
        raise NotImplementedError

    def restart(self) -> bool:  # True when it answers again
        raise NotImplementedError

    def health(self, timeout_s: float = 2.0) -> dict[str, Any] | None:  # None: no answer
        raise NotImplementedError


class ModuleStore:
    def __init__(
        self, layout: Layout, agent: Agent, os_name: str | None = None, owner: str | None = None
    ) -> None:
        self.layout = layout
        self.agent = agent
        self.os_name = os_name or this_os()
        self.owner = owner  # the node's account: it must write the module's cache (06 4.3)
        self.fail_at: str = ""  # tests: raise in this step (fault injection, 10 3)

    # ---------------------------------------------------------------- queries

    def installed(self) -> list[dict[str, Any]]:
        folder = self.layout.deployer / "manifests"
        out = []
        for path in sorted(folder.glob("module-*.json")) if folder.is_dir() else []:
            m = Manifest.load(path)
            if m is not None:
                out.append({"id": m.id, "build": m.build, "installed_utc": m.installed_utc})
        return out

    # ---------------------------------------------------------------- install

    def install(self, pkg: Package) -> dict[str, Any]:
        steps: list[dict[str, Any]] = []
        mid = pkg.id

        def step(name: str, detail: str = "") -> None:
            if self.fail_at == name:
                raise DeployError("SYS-008", f"Injected failure in {name}", detail="test")
            steps.append({"name": name, "ok": True, "detail": detail})

        if pkg.kind != "module":
            raise DeployError("DSC-012", "Request rejected", detail="not a module package")
        if pkg.info["os"] not in ("any", self.os_name):
            raise DeployError(
                "INS-005", "Package is for another OS", f"It is for {pkg.info['os']}.",
                f"Send the {self.os_name} package.",
            )  # fmt: skip
        step("verify")
        if self.agent.busy():
            raise DeployError("INS-022", "A test is running", "Installing now would disturb it.",
                              "Stop the test first.")  # fmt: skip
        step("check_idle")
        old = Manifest.load(self.layout.manifest(mid))
        result = "installed"
        if old is not None:
            if old.build > pkg.build:
                raise DeployError(
                    "INS-026", f"A newer build {old.build} is installed",
                    f"The package is {pkg.build}.", "Send the same or a newer build.",
                )  # fmt: skip
            result = "repaired" if old.build == pkg.build else "upgraded"
        elif self.layout.code(mid).exists():
            raise DeployError(
                "INS-021", "Files left from an earlier install", str(self.layout.code(mid)),
                "Remove that folder, then install again.",
            )  # fmt: skip
        backup = self.layout.rollback / mid
        self._journal({"operation": "install", "kind": "module", "id": mid, "step": "backup",
                       "backup": str(backup)})  # fmt: skip
        try:
            self._backup(mid, old, backup)
            step("backup")
            if old is not None:
                code = self.layout.code(mid)
                ours = [i for i in old.items if Path(i.path).is_relative_to(code)]
                gone = remove_items(ours, old.keep)
                if gone.left:
                    raise DeployError(
                        "INS-021", "Old files could not be removed", "; ".join(gone.left)
                    )
            step("remove_old")
            new = self._copy(pkg, backup)
            step("copy", f"{len(new.items)} items")
            new.save(self.layout.manifest(mid))
            if not self.agent.restart():
                raise DeployError("SYS-009", "The node did not come back after the install",
                                  fix="The module was put back as it was.")  # fmt: skip
            step("restart_agent")
            step("cleanup")
        except BaseException as e:
            self.restore(backup)
            self._clear_journal()
            pkg.path.unlink(missing_ok=True)  # sent again next time (06 11.5 step 9)
            if isinstance(e, DeployError):
                e.detail = (e.detail + "; " if e.detail else "") + "rolled back"
            raise
        shutil.rmtree(backup, ignore_errors=True)
        pkg.path.unlink(missing_ok=True)
        self._clear_journal()
        return {
            "operation": "install", "kind": "module", "id": mid, "build": pkg.build,
            "from_build": old.build if old else None, "result": result,
            "agent_restarted": True, "steps": steps,
        }  # fmt: skip

    def _backup(self, mid: str, old: Manifest | None, backup: Path) -> None:
        shutil.rmtree(backup, ignore_errors=True)
        backup.mkdir(parents=True)
        (backup / "new-items.jsonl").touch()
        if old is None:
            return
        old.save(backup / "manifest.json")
        code = self.layout.code(mid)
        if code.is_dir():
            shutil.copytree(code, backup / "code", symlinks=True)

    def _copy(self, pkg: Package, backup: Path) -> Manifest:
        mid = pkg.id
        root, code = self.layout.module_root(mid), self.layout.code(mid)
        m = Manifest("module", mid, pkg.build, utc_now())
        log = (backup / "new-items.jsonl").open("a", encoding="utf-8")

        def made(item: Item, into: list[Item]) -> None:
            into.append(item)
            log.write(json.dumps({"type": item.type, "path": item.path, "tree": item.tree}) + "\n")
            log.flush()

        def make_dir(path: Path, into: list[Item], tree: bool = False) -> None:
            if not path.exists():
                path.mkdir(parents=True)
                made(Item("dir", str(path), tree), into)
            elif into is m.keep or tree:
                into.append(Item("dir", str(path), tree))  # settings/cache survive reinstalls

        def code_dir(path: Path) -> None:
            if path.exists():
                return
            make_dir(path, m.items)
            # Python writes __pycache__ here when the node imports the module: its exact
            # name is listed now, so removal takes it too (06 12, found end to end)
            made(Item("dir", str(path / "__pycache__"), True), m.items)

        try:
            make_dir(root, m.keep)
            make_dir(root / "settings", m.keep, tree=True)
            self._give(root, root / "settings")
            code_dir(code)
            for name in sorted(pkg.files):
                dest = code / name
                for parent in reversed(dest.relative_to(code).parents[:-1]):
                    code_dir(code / parent)
                made(Item("file", str(dest), False, None), [])
                sha = pkg.extract(name, dest)
                m.items.append(Item("file", str(dest), False, sha))
                if self.fail_at == "copy_half" and len(m.items) > 3:
                    raise DeployError("SYS-008", "Injected failure in copy", detail="test")
            make_dir(root / "cache", m.items, tree=True)
            self._give(root / "cache")
        finally:
            log.close()
        return m

    def _give(self, *folders: Path) -> None:
        if self.owner:
            for folder in folders:
                shutil.chown(folder, self.owner, self.owner)

    # ---------------------------------------------------------------- restore

    def restore(self, backup: Path) -> None:
        """Put the module back as the backup says (also after a crash, INS-013)."""
        if not backup.is_dir():
            return
        mid = backup.name
        written = []
        log = backup / "new-items.jsonl"
        if log.exists():
            for line in log.read_text(encoding="utf-8").splitlines():
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue  # the last line may be cut by the crash
                written.append(Item(raw["type"], raw["path"], bool(raw.get("tree"))))
        old = Manifest.load(backup / "manifest.json")
        # only what this install made is in the log: removing it backwards gives the
        # state before (settings and cache that were already there are not in it)
        remove_items(written, old.keep if old else [])
        if old is None:
            self.layout.manifest(mid).unlink(missing_ok=True)
        else:
            code = self.layout.code(mid)
            if (backup / "code").is_dir():
                shutil.rmtree(code, ignore_errors=True)
                shutil.copytree(backup / "code", code, symlinks=True)
            old.save(self.layout.manifest(mid))
        self.agent.restart()
        shutil.rmtree(backup, ignore_errors=True)

    def recover(self) -> str | None:
        """At Deployer start: an operation that was cut short is rolled back (INS-013)."""
        try:
            j = json.loads(self.layout.journal.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if j.get("operation") == "install" and j.get("backup"):
            self.restore(Path(j["backup"]))
        elif j.get("operation") == "remove":  # removal is safe to run again: finish it
            m = Manifest.load(self.layout.manifest(str(j.get("id"))))
            if m is not None:
                self._remove_files(m, bool(j.get("purge")))
                self.agent.restart()
        self._clear_journal()
        return f"{j.get('operation')} of {j.get('id')} was cut short and rolled back"

    # ---------------------------------------------------------------- remove

    def remove(self, mid: str, purge: bool = False, dry_run: bool = False) -> dict[str, Any]:
        m = Manifest.load(self.layout.manifest(mid))
        if m is None:
            raise DeployError(
                "DSC-012", "Request rejected", detail=f"module {mid} is not installed"
            )
        if self.agent.busy():
            raise DeployError(
                "INS-022", "A test is running", "Removing now would disturb it.",
                "Stop the test first.",
            )  # fmt: skip
        if dry_run:
            r = self._remove_files(m, purge, dry_run=True)
            return self._result(mid, True, r.removed, r.kept, r.left, restarted=False)
        self._journal({"operation": "remove", "kind": "module", "id": mid, "step": "remove",
                       "purge": purge})  # fmt: skip
        r = self._remove_files(m, purge)
        restarted = self.agent.restart()
        self._clear_journal()
        out = self._result(mid, False, r.removed, r.kept, r.left, restarted)
        if r.left:
            raise DeployError(
                "INS-021", "Some items are left after removal", "; ".join(r.left[:5]),
                "Close programs using them and remove again.", json.dumps(out),
            )  # fmt: skip
        return out

    def _remove_files(self, m: Manifest, purge: bool, dry_run: bool = False) -> Removal:
        """Items backwards, then (purge) the kept ones; then check nothing listed is left.
        Keep items were made first, so removing them last keeps the backwards order."""
        order = m.keep + m.items if purge else m.items
        r = remove_items(order, [] if purge else m.keep, dry_run)
        if not purge:
            r.kept += [i.path for i in m.keep if Path(i.path).exists()]
        if dry_run:
            return r
        known = set(r.kept) | {x.split(": ", 1)[0] for x in r.left}
        r.left += [f"{p}: still there after removal" for p in still_there(order) if p not in known]
        if r.left:  # keep what is still there listed, so a second removal can finish it
            rest = [i for i in m.items if Path(i.path).exists()]
            Manifest(m.kind, m.id, m.build, m.installed_utc, rest, m.keep).save(
                self.layout.manifest(m.id)
            )
        else:
            self.layout.manifest(m.id).unlink(missing_ok=True)
        return r

    @staticmethod
    def _result(
        mid: str, dry: bool, removed: list[str], kept: list[str], left: list[str], restarted: bool
    ) -> dict[str, Any]:
        return {
            "operation": "remove",
            "kind": "module",
            "id": mid,
            "dry_run": dry,
            "removed": removed,
            "kept": sorted(set(kept)),
            "left": left,
            "agent_restarted": restarted,
        }

    # ---------------------------------------------------------------- journal

    def _journal(self, body: dict[str, Any]) -> None:
        self.layout.deployer.mkdir(parents=True, exist_ok=True)
        tmp = self.layout.journal.with_suffix(".tmp")
        tmp.write_text(json.dumps(body), encoding="utf-8")
        tmp.replace(self.layout.journal)

    def _clear_journal(self) -> None:
        self.layout.journal.unlink(missing_ok=True)
