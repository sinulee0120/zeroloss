# ZeroLoss

A personal, non-commercial tool for testing your **own** router at home or in your own lab:
it sends IPTV-like multicast streams (RTP/MPEG-TS over UDP) through the router and measures,
per second, what reaches each receiver — **packet loss, jitter**, delay variation, and
whether the picture actually played. 5-minute cells are colored green / yellow / red /
purple, and a report is written at the end (PASS / FAIL / INCOMPLETE).

> 한국어: 개인이 자기 공유기를 시험하려고 만든 도구입니다(판매하지 않음). 기획서는
> [`docs/`](docs/)에 한국어로 있습니다. 시작은 [`CLAUDE.md`](CLAUDE.md)와
> [`docs/01-overview.md`](docs/01-overview.md).

## Use it only on equipment you own

- Test only devices you own or are allowed to test, on a test network separated from the
  internet. The tool can send up to ~120 Mbps of multicast for 100 hours.
- The tool never logs in to the router under test and never changes its settings. It changes
  only the network card settings of the measuring PCs (and puts them back after a test).
- It comes with **no warranty** (see [LICENSE](LICENSE)). You use it at your own risk.

## What you need

| Role | Machine | Notes |
|------|---------|-------|
| Controller | Windows PC | Starts tests, judges results, shows the screens and the report (web page on TCP 10993). Video never goes through it. |
| Sender | Linux or Windows PC | Sends the channels (TSDuck `tsp`; movies made with ffmpeg). |
| Receiver | Linux or Windows PC | Measures each channel and plays it (mpv). Android tablets can receive too, over USB/adb from a receiving PC. |

External tools are installed separately and keep their own licenses (ffmpeg, TSDuck, mpv,
VLC, adb, iperf3) — see [`docs/15-tools.md`](docs/15-tools.md) and
[`tools/tools-manifest.json`](tools/tools-manifest.json).

## Getting it running

1. **Build** on each target OS (Python 3.12 and [uv](https://docs.astral.sh/uv/)):
   - Linux node package: `uv run python -m tools.build deb --out dist/node`
     (makes the `.deb`, the node update package and the module packages)
   - Windows Controller app: `uv run python -m tools.build win` (on Windows)
   - Android receiver app: `uv run python -m tools.build tablet` (on the Windows PC, with the
     Android SDK)
2. **Install** the Controller on Windows (one click). Linux receivers/senders are installed
   by the Controller over SSH the first time; after that every PC is updated from the
   Controller (**Update PCs**). Details: [`docs/06-install.md`](docs/06-install.md).
3. In the Controller, pick the PCs it found, the channels and the duration, then **Start**.
   How to read the results: [`docs/08-manual.md`](docs/08-manual.md).

The planning documents in `docs/` are the specification; the code follows them
(`docs/20-spec-gaps.md` records every mismatch found and how it was fixed).

## Status

Working in a home lab: one Windows Controller, Linux sender and receiver PCs, and up to
three Android tablets. Expect rough edges; this is a personal project.

## License

[Apache License 2.0](LICENSE) — free for anyone to use, copy and change, with no warranty.
Third-party parts kept in this repository are listed in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
