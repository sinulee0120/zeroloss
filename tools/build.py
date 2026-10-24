"""Build entry point (docs/06-install.md 8, 11).

Usage:
  python -m tools.build version              stamp VERSION (YYYYMMDD.HHMM)
  python -m tools.build module <id> [--out DIR]
                                             module package .zlm (docs/02-data.md 13)
  python -m tools.build deb [--out DIR]      Linux node package .deb (06 4.3), on Linux,
                                             the same .deb as a .zlp (06 11.5) and every
                                             module .zlm — one VERSION stamp for all
  python -m tools.build win [--out DIR]      Windows Controller, portable (06 4.1-1), on
                                             Windows: ZeroLoss-Controller-<build>.zip with
                                             the VERSION of the matching node build (no
                                             new stamp: the Controller and the nodes must
                                             carry the same build, F17 step 1)
  python -m tools.build tablet [--out DIR]   tablet package .zlt, on Windows with the
                                             Android tools (C:/Android): the receiver app
                                             (gradle, versionName = VERSION) and Google's
                                             Linux adb (platform-tools, tools-manifest
                                             "adb"); the receiving PC unpacks both
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from agent.deployer.package import build_package
from tools.versioning import (
    android_version_code,
    deb_version,
    read_version,
    windows_file_version,
    write_version,
)

ROOT = Path(__file__).resolve().parent.parent
# 06 4.3: what dpkg must install first. Recommends only: libxcb-cursor0 is for the Agent
# Desktop window (Qt 6.5+ on X11); an offline ``dpkg -i`` must still install the node, and
# the SSH install adds it with apt (20 GAP-73 2)
DEPENDS = ("adduser", "systemd")
RECOMMENDS = ("libxcb-cursor0",)


def main(argv: list[str], version_file: Path | None = None) -> int:
    args = argv[1:]
    stamp = version_file or ROOT / "VERSION"  # 06 8 (20 GAP-73 3): each build, a new stamp
    if args == ["version"]:
        version = write_version(datetime.now(), stamp)
        print(f"VERSION        {version}")
        print(f"Windows file   {windows_file_version(version)}")
        print(f"Android code   {android_version_code(version)}")
        print(f"Debian         {deb_version(version)}")
        return 0
    if args[:1] == ["deb"] and (len(args) == 1 or (len(args) == 3 and args[1] == "--out")):
        print(f"VERSION        {write_version(datetime.now(), stamp)}")  # 06 8: every build
        out = Path(args[2]) if len(args) == 3 else ROOT / "dist"
        deb = build_deb(out)
        print(deb)
        print(build_node_zlp(deb))
        for mid in sorted(p.name for p in (ROOT / "modules").iterdir()
                          if (p / "module.json").is_file()):  # fmt: skip
            print(build_module(mid, out))  # the same stamp as the node (INS-015, GAP-73 ③)
        return 0
    if args[:1] == ["win"] and (len(args) == 1 or (len(args) == 3 and args[1] == "--out")):
        print(f"VERSION        {read_version()}")  # the node build's stamp, not a new one
        print(build_win(Path(args[2]) if len(args) == 3 else ROOT / "dist"))
        return 0
    if args[:1] == ["tablet"] and (len(args) == 1 or (len(args) == 3 and args[1] == "--out")):
        print(f"VERSION        {read_version()}")  # the node build's stamp, not a new one
        print(build_tablet(Path(args[2]) if len(args) == 3 else ROOT / "dist"))
        return 0
    if len(args) in (2, 4) and args[0] == "module" and (len(args) == 2 or args[2] == "--out"):
        out = Path(args[3]) if len(args) == 4 else ROOT / "dist"
        print(f"VERSION        {write_version(datetime.now(), stamp)}")  # 06 8: every build
        print(build_module(args[1], out))
        return 0
    print(__doc__, file=sys.stderr)
    return 6


def build_module(module_id: str, out: Path) -> Path:
    """``zeroloss-module-<id>-<build>.zlm`` from ``modules/<id>`` and the VERSION stamp."""
    src = ROOT / "modules" / module_id
    meta = json.loads((src / "module.json").read_text(encoding="utf-8"))
    build = read_version()
    info = {
        "schema_version": 1, "kind": "module", "id": meta["id"], "build": build, "os": "any",
        "core_api_min": meta["core_api_min"], "core_api_max": meta["core_api_max"],
    }  # fmt: skip
    return build_package(src, out / f"zeroloss-module-{module_id}-{build}.zlm", info)


# ---------------------------------------------------------------- Linux node package

DATA_FILES = [  # read at run time next to the code (Path(__file__) / _MEIPASS)
    ("VERSION", "."),
    ("core/settings/defaults.json", "core/settings"),
    ("controller/problems/catalog.json", "controller/problems"),
    ("controller/actions/catalog.json", "controller/actions"),
]


def module_stdlib_imports(modules_dir: Path = ROOT / "modules") -> list[str]:
    """Standard-library modules that module code imports. Modules are not frozen into
    the node program (they come as .zlm), so these must be put in (06 4.3-1)."""
    found: set[str] = set()
    for py in modules_dir.rglob("*.py"):
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in sys.stdlib_module_names:
                    found.add(name)
    return sorted(found)


def freeze(name: str, entry: str, out: Path, extra: list[str]) -> Path:
    """One PyInstaller onedir; returns its folder."""
    work = out / "pyinstaller"
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
        "--log-level", "WARN", "--name", name, "--distpath", str(work / "dist"),
        "--workpath", str(work / "build"), "--specpath", str(work), *extra,
        str(ROOT / "tools" / "entry" / entry),
    ]  # fmt: skip
    subprocess.run(cmd, check=True, cwd=ROOT, timeout=1800)
    return work / "dist" / name


def build_executables(out: Path) -> tuple[Path, Path, Path]:
    data = [f"--add-data={ROOT / src}:{dest}" for src, dest in DATA_FILES]
    hidden = [f"--hidden-import={m}" for m in module_stdlib_imports()]
    node = freeze("zeroloss", "node.py", out, [
        *data, *hidden, "--collect-submodules=core", "--exclude-module=PySide6",
        "--exclude-module=modules", "--exclude-module=tests", "--exclude-module=controller.ui",
        "--exclude-module=core.ui_qt", "--exclude-module=agent.desktop",
    ])  # fmt: skip
    deployer = freeze("zeroloss-deployer", "deployer.py", out, [
        f"--add-data={ROOT / 'VERSION'}:.", "--exclude-module=core", "--exclude-module=controller",
        "--exclude-module=modules", "--exclude-module=PySide6", "--exclude-module=asyncssh",
    ])  # fmt: skip
    # 03 F20: in the user's session; PySide6 (widgets only) lives here, not in the node.
    # Modules come from /opt/zeroloss/modules at run time, like the node's.
    desktop = freeze("zeroloss-agent-desktop", "desktop.py", out, [
        *data, *hidden, "--collect-submodules=core", "--exclude-module=modules",
        "--exclude-module=tests", "--exclude-module=controller",
        "--exclude-module=PySide6.QtWebEngineCore", "--exclude-module=PySide6.QtQml",
        "--exclude-module=PySide6.QtQuick", "--exclude-module=PySide6.Qt3DCore",
    ])  # fmt: skip
    return node, deployer, desktop


def build_deb(out: Path) -> Path:
    """``zeroloss-agent_<ver>_amd64.deb``: both programs, units, config, scripts (06 4.3)."""
    if not sys.platform.startswith("linux"):
        raise SystemExit("build the Linux package on Linux (06 11.3 build location)")
    version = deb_version(read_version())
    node, deployer, desktop = build_executables(out)
    stage = out / "deb-stage"
    shutil.rmtree(stage, ignore_errors=True)
    src = ROOT / "installer" / "linux"
    shutil.copytree(node, stage / "opt/zeroloss/agent", symlinks=True)
    shutil.copytree(deployer, stage / "opt/zeroloss/deployer", symlinks=True)
    shutil.copytree(desktop, stage / "opt/zeroloss/desktop", symlinks=True)
    (stage / "etc/xdg/autostart").mkdir(parents=True)  # 06 4.3: at every user login
    shutil.copy(src / "zeroloss-agent-desktop.desktop", stage / "etc/xdg/autostart")
    for unit in ("zeroloss-agent.service", "zeroloss-deployer.service"):
        (stage / "lib/systemd/system").mkdir(parents=True, exist_ok=True)
        shutil.copy(src / unit, stage / "lib/systemd/system" / unit)
    (stage / "etc/zeroloss").mkdir(parents=True, exist_ok=True)
    shutil.copy(src / "zeroloss.conf", stage / "etc/zeroloss/zeroloss.conf")
    debian = stage / "DEBIAN"
    debian.mkdir()
    for script in ("postinst", "prerm", "postrm"):
        shutil.copy(src / script, debian / script)
        (debian / script).chmod(0o755)
    (debian / "conffiles").write_text("/etc/zeroloss/zeroloss.conf\n", encoding="utf-8")
    size_kb = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file()) // 1024
    (debian / "control").write_text(
        "Package: zeroloss-agent\n"
        f"Version: {version}\n"
        "Architecture: amd64\n"
        "Maintainer: ZeroLoss <noreply@localhost>\n"
        f"Depends: {', '.join(DEPENDS)}\n"
        f"Recommends: {', '.join(RECOMMENDS)}\n"
        f"Installed-Size: {size_kb}\n"
        "Section: net\n"
        "Priority: optional\n"
        "Description: ZeroLoss node (IPTV quality test agent and Deployer)\n"
        " Test control, measurement and install service for a ZeroLoss lab.\n",
        encoding="utf-8",
    )
    deb = out / f"zeroloss-agent_{version}_amd64.deb"
    subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(stage), str(deb)],
                   check=True, capture_output=True, timeout=600)  # fmt: skip
    return deb


# ---------------------------------------------------------------- Windows Controller


def build_win(out: Path) -> Path:
    """``ZeroLoss-Controller-<build>.zip`` (06 4.1-1): ``ZeroLoss.exe`` (window; starts the
    Controller service if none answers), ``cli\\zeroloss.exe`` (service and CLI) and the
    modules, which the service loads at run time like an installed node's."""
    if sys.platform != "win32":
        raise SystemExit("build the Windows Controller on Windows (06 11.3 build location)")
    build = read_version()
    sep = ";"  # PyInstaller --add-data on Windows: SRC;DEST (a drive letter has a colon)
    data = [f"--add-data={ROOT / src}{sep}{dest}" for src, dest in DATA_FILES]
    hidden = [f"--hidden-import={m}" for m in module_stdlib_imports()]
    # Windows names ignore case: "zeroloss" and "ZeroLoss" would be one folder, so the
    # console program is built under another name and renamed in the stage
    cli = freeze("zeroloss-cli", "node.py", out, [
        *data, *hidden, "--collect-submodules=core", "--exclude-module=PySide6",
        "--exclude-module=modules", "--exclude-module=tests", "--exclude-module=controller.ui",
        "--exclude-module=core.ui_qt", "--exclude-module=agent.desktop",
    ])  # fmt: skip
    gui = freeze("ZeroLoss", "controller_win.py", out, [
        *data, *hidden, "--windowed", "--collect-submodules=core", "--exclude-module=modules",
        "--exclude-module=tests", "--exclude-module=PySide6.QtWebEngineCore",
        "--exclude-module=PySide6.QtQml", "--exclude-module=PySide6.QtQuick",
        "--exclude-module=PySide6.Qt3DCore",
    ])  # fmt: skip
    stage = out / f"ZeroLoss-Controller-{build}"
    shutil.rmtree(stage, ignore_errors=True)
    shutil.copytree(gui, stage)
    shutil.copytree(cli, stage / "cli")
    (stage / "cli" / "zeroloss-cli.exe").rename(stage / "cli" / "zeroloss.exe")
    # iperf3 for the Controller PC as a LAN-side card (throughput, 2026-10-17)
    for f in (ROOT / "vendor" / "iperf3-win").glob("*.*"):
        if f.suffix in (".exe", ".dll"):
            shutil.copy(f, stage / "cli" / f.name)
    shutil.copytree(ROOT / "modules", stage / "modules",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))  # fmt: skip
    return Path(shutil.make_archive(str(stage), "zip", stage.parent, stage.name))


ANDROID = Path(os.environ.get("ZEROLOSS_ANDROID", r"C:\Android"))  # jdk17, sdk (06 11.3)
PLATFORM_TOOLS = "https://dl.google.com/android/repository/platform-tools-latest-linux.zip"


def build_tablet(out: Path) -> Path:
    """``zeroloss-tablet-<build>.zlt``: the receiver app built from ``android/`` (its
    versionName is VERSION, so the receiving PC tells an old app on a tablet) and
    Google's Linux platform-tools (adb), fetched once into ``out/.cache``."""
    build = read_version()
    env = {**os.environ, "JAVA_HOME": str(ANDROID / "jdk17"), "ANDROID_HOME": str(ANDROID / "sdk")}
    gradlew = ROOT / "android" / ("gradlew.bat" if sys.platform == "win32" else "gradlew")
    subprocess.run([str(gradlew), "testDebugUnitTest", "assembleDebug", "--console=plain", "-q"],
                   cwd=ROOT / "android", env=env, check=True)  # fmt: skip
    apk = ROOT / "android" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
    cache = out / ".cache"
    cache.mkdir(parents=True, exist_ok=True)
    tools_zip = cache / "platform-tools-latest-linux.zip"
    if not tools_zip.is_file():
        tmp = tools_zip.with_suffix(".part")
        with urllib.request.urlopen(PLATFORM_TOOLS, timeout=300) as r:
            tmp.write_bytes(r.read())
        tmp.replace(tools_zip)
    stage = out / "tablet-stage"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    shutil.copy(apk, stage / "zeroloss-receiver.apk")
    with zipfile.ZipFile(tools_zip) as zf:
        for name in zf.namelist():  # adb and what it needs, not the other tools
            base = name.removeprefix("platform-tools/")
            if name.endswith("/") or "/" in base or not (base == "adb" or base.startswith("lib")):
                continue
            (stage / "platform-tools").mkdir(exist_ok=True)
            (stage / "platform-tools" / base).write_bytes(zf.read(name))
    print("platform-tools sha256", hashlib.sha256(tools_zip.read_bytes()).hexdigest())
    info = {"schema_version": 1, "kind": "tablet", "id": "tablet", "build": build, "os": "linux",
            "core_api_min": None, "core_api_max": None}  # fmt: skip
    try:
        return build_package(stage, out / f"zeroloss-tablet-{build}.zlt", info)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def build_node_zlp(deb: Path) -> Path:
    """``zeroloss-node-agent-<build>.zlp``: the .deb as a node package for updates over
    10991 (02 13, 06 11.5: the Deployer installs only packages with a hash list)."""
    build = read_version()
    stage = deb.parent / "zlp-stage"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    shutil.copy(deb, stage / deb.name)
    info = {"schema_version": 1, "kind": "node", "id": "agent", "build": build, "os": "linux",
            "core_api_min": None, "core_api_max": None}  # fmt: skip
    try:
        return build_package(stage, deb.parent / f"zeroloss-node-agent-{build}.zlp", info)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
