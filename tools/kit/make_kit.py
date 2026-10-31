"""Build the offline kit for another site (2026-10-16 user: nothing can be downloaded there).

Layout (the guide 설치_및_사용_안내.html walks through it)::

    ZeroLoss-Kit-<build>/
      설치_및_사용_안내.html
      1_Windows_PC/ZeroLoss-Controller-Setup-<build>.exe   the installer (the Controller,
                                        its packages/ for the Linux PCs and tablets, TSDuck)
      2_Linux_PC_USB/                   copied to a USB stick for the Linux PCs
        zeroloss-linux-setup.sh, offline/ (Ubuntu 24.04 .debs), tsduck_*.deb
        zeroloss-sample-video.sh, sample_N/sample_N_CH<n>_UHD.ts + .json (a finished
        10-minute video for its channel, sent as it is)

2026-10-17 user: the sample video goes with the UHD file already made from it ("이미 만든걸
또 만들필요없게", "원본 비디오와 함께", sample_1_original / sample_1_cache). The made file is
named and described for the original's content digest, and checked with the sender's own
rule (``Preparer.ready``) before the kit is written: on the other PC Start uses it at once.
2026-10-31 user ("설치가이드. Offline kit. Installer . 사용법"): the installer replaces the
loose program folder; "그냥 파일들 변환한거 10분짜리 3개 파일 같이 패키징하면 되잖아": the three
finished 10-minute videos of the three-channel test only (sample_1..3, one a channel,
fetched from the sender with ``zeroloss made``), no movies.

Usage::

    python -m tools.kit.make_kit --setup dist/ZeroLoss-Controller-Setup-<build>.exe \
        --offline <usb folder> --made <dir>/ch1_UHD_35M_<tag>.ts --made <dir>/ch2_... \
        --made <dir>/ch3_... \
        --out <kit folder> --zip
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
OLD_BUILD = "{BUILD}"  # where the guide names the build; replaced by this kit's


def _copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, dirs_exist_ok=False)


def sample(made: Path, out: Path, name: str = "sample_1") -> Path:
    """<name>/<name>_CH<ch>_<quality>.ts and its description: a finished 10-minute made
    file, put in the sender's media folder and sent as it is (2026-10-31 user: the kit
    carries "변환한거 10분짜리 3개", not the movies). Checked with the sender's own rule
    before the kit is written; returns the kit's file."""
    from core.events.bus import EventBus
    from modules.iptv.sender.prepare import Preparer

    desc = json.loads(made.with_suffix(".json").read_text(encoding="utf-8"))
    key = desc["key"]
    folder = out / name
    folder.mkdir(parents=True)
    ts = folder / f"{name}_CH{int(key['ch'])}_{key['quality']}.ts"
    shutil.copy2(made, ts)
    shutil.copy2(made.with_suffix(".json"), ts.with_suffix(".json"))
    with tempfile.TemporaryDirectory() as tmp:
        p = Preparer(Path(tmp), EventBus(), Path(tmp))
        got = p.ready(str(key["quality"]), int(key["ch"]), int(key["muxrate_bps"]), ts)
    if got != ts:
        raise SystemExit(f"the sender would not send {ts.name} as it is (recipe or size)")
    return ts


def build(args: argparse.Namespace) -> Path:
    build_no = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    kit = Path(args.out) / f"ZeroLoss-Kit-{build_no}"
    if kit.exists():
        raise SystemExit(f"{kit} exists: choose another --out")
    setup = Path(args.setup)
    if build_no not in setup.name:
        raise SystemExit(f"{setup.name} is not the installer of build {build_no}")
    win = kit / "1_Windows_PC"
    win.mkdir(parents=True)
    shutil.copy2(setup, win / setup.name)
    usb = kit / "2_Linux_PC_USB"
    _copy_tree(Path(args.offline) / "offline", usb / "offline")
    if not list((usb / "offline").glob("iperf3_*.deb")):
        print("note: offline/ has no iperf3: Linux PCs measure throughput only after "
              "'apt install iperf3' (offline_repo.py lists it)", flush=True)  # fmt: skip
    for f in Path(args.offline).glob("tsduck_*.deb"):
        shutil.copy2(f, usb / f.name)
    for name in ("zeroloss-linux-setup.sh", "zeroloss-sample-video.sh"):
        (usb / name).write_bytes((KIT_SRC / name).read_bytes().replace(b"\r\n", b"\n"))
    made = [sample(Path(m), usb, f"sample_{i}") for i, m in enumerate(args.made, 1)]
    text = (KIT_SRC / GUIDE).read_text(encoding="utf-8").replace(OLD_BUILD, build_no)
    (kit / GUIDE).write_text(text, encoding="utf-8", newline="\n")
    print(f"kit: {kit}\nmade videos: {', '.join(m.name for m in made)} (used as they are)",
          flush=True)  # fmt: skip
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
    ap.add_argument("--setup", required=True, help="the installer (tools.build installer)")
    ap.add_argument("--offline", required=True, help="a 2_Linux_PC_USB folder: offline/, tsduck")
    ap.add_argument("--made", action="append", required=True, metavar="TS",
                    help="a finished 10-minute made file, its .json beside it")  # fmt: skip
    ap.add_argument("--out", required=True)
    ap.add_argument("--zip", action="store_true")
    build(ap.parse_args(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
