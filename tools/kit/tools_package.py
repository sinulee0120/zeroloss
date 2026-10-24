"""Build a tools package (.zlt) the Deployer installs offline (iperf3 for the throughput test).

Usage: python tools/kit/tools_package.py <id> <out folder> <ubuntu package>...
The Ubuntu 24.04 packages and what they need that a fresh desktop does not have are
downloaded from the archive (offline_repo.py's resolver), checked by sha256 and packed
with package.json kind "tools" and the build from VERSION.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).parent))

import offline_repo  # type: ignore[import-not-found]  # noqa: E402

from agent.deployer.package import build_package  # noqa: E402


def main() -> int:
    tool_id, out, roots = sys.argv[1], Path(sys.argv[2]), sys.argv[3:]
    cache = out / ".index"
    cache.mkdir(parents=True, exist_ok=True)
    pkgs = offline_repo.load(cache)
    provides: dict[str, str] = {}
    for name, d in pkgs.items():
        for p in d.get("Provides", "").split(","):
            p = p.strip().split(" ")[0]
            if p:
                provides.setdefault(p, name)
    want: set[str] = set()
    offline_repo.close(want, list(roots), pkgs, provides)
    desktop = offline_repo.preinstalled(cache)
    take = sorted(n for n in want if n not in desktop or n in roots)
    print("packages:", take, flush=True)
    build = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    with tempfile.TemporaryDirectory() as tmp:
        for n in take:
            d = pkgs[n]
            url = f"{offline_repo.ARCHIVE}/{d['Filename']}"
            data = urllib.request.urlopen(url, timeout=300).read()
            if hashlib.sha256(data).hexdigest() != d["SHA256"]:
                print("BAD HASH", n)
                return 1
            (Path(tmp) / d["Filename"].rsplit("/", 1)[-1]).write_bytes(data)
        zlt = build_package(
            Path(tmp),
            out / f"zeroloss-tools-{tool_id}-{build}.zlt",
            {"schema_version": 1, "kind": "tools", "id": tool_id, "build": build, "os": "linux"},
        )
    print(zlt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
