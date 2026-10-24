"""Collect an offline Ubuntu 24.04 (noble, amd64) package set for ZeroLoss PCs.

Usage: python offline_repo.py <out folder> [kbps limit]
Resolves Depends + Pre-Depends (first alternative, Provides for virtual names) of the
root packages over noble + noble-updates + noble-security (main, universe), downloads
every .deb and writes a Packages index (the archive's own stanzas) so apt can use the
folder as a local repository with no network.
"""

from __future__ import annotations

import hashlib
import lzma
import re
import sys
import time
import urllib.request
from pathlib import Path

ARCHIVE = "http://archive.ubuntu.com/ubuntu"
POCKETS = ("noble", "noble-security", "noble-updates")  # later ones override
COMPONENTS = ("main", "universe")
ROOTS = [
    "openssh-server",
    "ffmpeg",
    "mpv",
    "vlc",
    "libxcb-cursor0",
    "adduser",
    "systemd",
    # TSDuck's own dependencies (its .deb comes with the kit)
    "libssl3t64",
    "libsrt1.5-openssl",
    "librist4",
    "libedit2",
    "libusb-1.0-0",
    "libpcsclite1",
    "libcurl4t64",
    "libzstd1",
    "zlib1g",
    "libstdc++6",
]


def stanzas(text: str) -> list[dict[str, str]]:
    out = []
    for block in text.split("\n\n"):
        d: dict[str, str] = {}
        key = None
        for line in block.splitlines():
            if line[:1] in (" ", "\t") and key:
                d[key] += "\n" + line
            elif ":" in line:
                key, _, v = line.partition(":")
                d[key] = v.strip()
        if "Package" in d:
            d["_raw"] = block.strip()
            out.append(d)
    return out


def load(cache: Path) -> dict[str, dict[str, str]]:
    pkgs: dict[str, dict[str, str]] = {}
    for pocket in POCKETS:
        for comp in COMPONENTS:
            f = cache / f"{pocket}_{comp}_Packages"
            if not f.exists():
                url = f"{ARCHIVE}/dists/{pocket}/{comp}/binary-amd64/Packages.xz"
                print("index", url, flush=True)
                f.write_bytes(lzma.decompress(urllib.request.urlopen(url, timeout=120).read()))
            for d in stanzas(f.read_text(encoding="utf-8", errors="replace")):
                pkgs[d["Package"]] = d
    return pkgs


def deps(d: dict[str, str]) -> list[list[str]]:
    out = []
    for field in ("Pre-Depends", "Depends"):
        for group in filter(None, (x.strip() for x in d.get(field, "").split(","))):
            alts = [re.sub(r"[\s(:].*", "", a.strip()) for a in group.split("|")]
            out.append([a for a in alts if a])
    return out


def close(
    want: set[str], todo: list[str], pkgs: dict[str, dict[str, str]], provides: dict[str, str]
) -> None:
    while todo:
        n = todo.pop()
        real = n if n in pkgs else provides.get(n)
        if real is None:
            print("no package for", n, flush=True)
            continue
        if real in want:
            continue
        want.add(real)
        for alts in deps(pkgs[real]):
            pick = next((a for a in alts if a in want), None) or next(
                (a for a in alts if a in pkgs or a in provides), None
            )
            if pick:
                todo.append(pick)
            else:
                print("unresolved", alts, "for", real, flush=True)


MANIFESTS = (
    "ubuntu-24.04.3-desktop-amd64.manifest",
    "ubuntu-24.04.4-desktop-amd64.manifest",
    "ubuntu-24.04.5.1-desktop-amd64.manifest",
)


def preinstalled(cache: Path) -> set[str]:
    """Package names on a freshly installed Ubuntu 24.04 desktop (recent point releases)."""
    names: set[str] = set()
    for m in MANIFESTS:
        f = cache / m
        if not f.exists():
            url = f"https://releases.ubuntu.com/24.04/{m}"
            f.write_bytes(urllib.request.urlopen(url, timeout=60).read())
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                names.add(line.split()[0].split(":")[0])
    return names


def main() -> int:
    out = Path(sys.argv[1])
    limit = float(sys.argv[2]) * 1000 / 8 if len(sys.argv) > 2 else 0.0  # bytes/s
    cache = out.parent / ".index"
    out.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    pkgs = load(cache)
    provides: dict[str, str] = {}
    for name, d in pkgs.items():
        for p in d.get("Provides", "").split(","):
            p = re.sub(r"[\s(].*", "", p.strip())
            if p:
                provides.setdefault(p, name)
    desktop = preinstalled(cache)
    src = {n: d.get("Source", n).split()[0] for n, d in pkgs.items()}
    want: set[str] = set()
    todo = list(ROOTS)
    while True:
        close(want, todo, pkgs, provides)
        # an upgraded package whose brothers (same source) sit on a desktop PC brings
        # them along, so apt never meets "systemd (= old)" next to a new libsystemd0
        built = {src[n] for n in want}
        todo = sorted(n for n in desktop if n in pkgs and src[n] in built and n not in want)
        if not todo:
            break
        print("brothers", len(todo), flush=True)
    total = sum(int(pkgs[n].get("Size", 0)) for n in want)
    print(f"{len(want)} packages, {total / 1e6:.0f} MB", flush=True)
    index = []
    started, done = time.monotonic(), 0
    for n in sorted(want):
        d = pkgs[n]
        name = d["Filename"].rsplit("/", 1)[-1]
        dest = out / name
        if not (dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == d["SHA256"]):
            data = urllib.request.urlopen(f"{ARCHIVE}/{d['Filename']}", timeout=300).read()
            if hashlib.sha256(data).hexdigest() != d["SHA256"]:
                print("BAD HASH", name, flush=True)
                return 1
            dest.write_bytes(data)
            done += len(data)
            if limit:  # keep the shared air quiet for the running test
                ahead = done / limit - (time.monotonic() - started)
                if ahead > 0:
                    time.sleep(ahead)
        index.append(re.sub(r"(?m)^Filename: .*$", f"Filename: ./{name}", d["_raw"]))
    (out / "Packages").write_text("\n\n".join(index) + "\n", encoding="utf-8")
    print("done", len(index), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
