"""Build the offline kit for another site (2026-10-16 user: nothing can be downloaded there).

Layout (the guide 설치_및_사용_안내.html walks through it)::

    ZeroLoss-Kit-<build>/
      설치_및_사용_안내.html, 2_리눅스PC_등록.bat
      1_Windows_PC/ZeroLoss/            the portable Controller + packages/ for the Linux PCs
      2_Linux_PC_USB/                   copied to a USB stick for the Linux PCs
        zeroloss-linux-setup.sh, offline/ (Ubuntu 24.04 .debs), tsduck_*.deb
        zeroloss-sample-video.sh, sample_1/sample_1_original.mp4,
        sample_1/sample_1_cache/<made UHD file + its description>

2026-10-17 user: the sample video goes with the UHD file already made from it ("이미 만든걸
또 만들필요없게", "원본 비디오와 함께", sample_1_original / sample_1_cache). The made file is
named and described for the original's content digest, and checked with the sender's own
rule (``Preparer.ready``) before the kit is written: on the other PC Start uses it at once.

Usage::

    python -m tools.kit.make_kit --app dist/v0034/ZeroLoss-Controller-20261017.0034 \\
        --packages <folder with the .deb and .zlm files> --offline <old kit>/2_Linux_PC_USB \\
        --original <movie.mp4> --made <cache>/ch1_UHD_35M_<tag>.ts --out <kit folder>
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
KIT_SRC = Path(__file__).resolve().parent
GUIDE = "설치_및_사용_안내.html"
OLD_BUILD = "20261016.0026"  # the build the guide's text names; replaced by this kit's


def _copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, dirs_exist_ok=False)


def sample(original: Path, made: Path, out: Path) -> Path:
    """sample_1/: the original as sample_1_original.mp4 and the made file renamed and
    described for that original's content digest; returns the made file in the kit."""
    from core.events.bus import EventBus
    from modules.iptv.sender.prepare import Preparer, generated_path, source_tag

    desc = json.loads(made.with_suffix(".json").read_text(encoding="utf-8"))
    key = desc["key"]
    folder = out / "sample_1"
    cache = folder / "sample_1_cache"
    cache.mkdir(parents=True)
    copy = folder / "sample_1_original.mp4"
    shutil.copy2(original, copy)
    tag = source_tag(copy)
    ts = generated_path(cache, str(key["quality"]), int(key["ch"]), int(key["muxrate_bps"]), tag)
    shutil.copy2(made, ts)
    desc["key"] = {**key, "digest": tag}
    desc["source_name"] = copy.name
    ts.with_suffix(".json").write_text(json.dumps(desc, indent=2), encoding="utf-8")
    # the sender's own rule, on the kit's files (a tmp cache folder, the kit's made file)
    with tempfile.TemporaryDirectory() as tmp:
        p = Preparer(Path(tmp), EventBus(), Path(tmp))
        p.folder = cache
        got = p.ready(str(key["quality"]), int(key["ch"]), int(key["muxrate_bps"]), copy,
                      key.get("seconds") is None)  # fmt: skip
    if got != ts:
        raise SystemExit(f"the made file would not be used on the sender: {ts.name}")
    return ts


def build(args: argparse.Namespace) -> Path:
    build_no = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    kit = Path(args.out) / f"ZeroLoss-Kit-{build_no}"
    if kit.exists():
        raise SystemExit(f"{kit} exists: choose another --out")
    win = kit / "1_Windows_PC" / "ZeroLoss"
    _copy_tree(Path(args.app), win)
    pk = win / "packages"
    (pk / "tools").mkdir(parents=True)
    src = Path(args.packages)
    for f in sorted(src.glob(f"*{build_no}*")):
        if f.suffix in (".deb", ".zlm", ".zlp"):  # .zlp: the screen's Update PCs
            shutil.copy2(f, pk / f.name)
    if not list(pk.glob("*.deb")) or len(list(pk.glob("*.zlm"))) < 2:
        raise SystemExit(f"{src} has no .deb or not both .zlm files of build {build_no}")
    usb = kit / "2_Linux_PC_USB"
    _copy_tree(Path(args.offline) / "offline", usb / "offline")
    for f in Path(args.offline).glob("tsduck_*.deb"):
        shutil.copy2(f, usb / f.name)
        shutil.copy2(f, pk / "tools" / f.name)
    for name in ("zeroloss-linux-setup.sh", "zeroloss-sample-video.sh"):
        (usb / name).write_bytes((KIT_SRC / name).read_bytes().replace(b"\r\n", b"\n"))
    made = sample(Path(args.original), Path(args.made), usb)
    for name in ("2_리눅스PC_등록.bat", GUIDE):
        text = (KIT_SRC / name).read_text(encoding="utf-8").replace(OLD_BUILD, build_no)
        (kit / name).write_text(text, encoding="utf-8", newline="\r\n" if name.endswith(".bat")
                                else "\n")  # fmt: skip
    print(f"kit: {kit}\nmade video: {made.name} (used as it is on the sender)", flush=True)
    if args.zip:
        zp = kit.parent / f"{kit.name}.zip"  # not with_suffix: ".0034" is no suffix
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
            for f in sorted(kit.rglob("*")):
                if f.is_file():
                    big = f.suffix.lower() in (".mp4", ".ts", ".deb", ".zlm", ".zip")
                    z.write(f, f.relative_to(kit.parent),
                            zipfile.ZIP_STORED if big else zipfile.ZIP_DEFLATED)  # fmt: skip
        print(f"zip: {zp} ({zp.stat().st_size / 1e9:.1f} GB)", flush=True)
    return kit


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", required=True, help="the built Controller folder")
    ap.add_argument("--packages", required=True, help="folder with the .deb and .zlm files")
    ap.add_argument("--offline", required=True, help="a 2_Linux_PC_USB folder: offline/, tsduck")
    ap.add_argument("--original", required=True, help="the sample's original video")
    ap.add_argument("--made", required=True, help="the UHD file made from it (.ts, .json beside)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--zip", action="store_true")
    build(ap.parse_args(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
