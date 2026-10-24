# Third-party notices

ZeroLoss itself is under the [Apache License 2.0](LICENSE). The parts below are not ours and
keep their own licenses.

## Kept in this repository

| Part | Where | License | Source |
|------|-------|---------|--------|
| iperf 3.21 (`iperf3.exe`, Windows build) | `vendor/iperf3-win/` | BSD-3-Clause — [LICENSE-iperf3.txt](vendor/iperf3-win/LICENSE-iperf3.txt) | https://github.com/esnet/iperf/releases/tag/3.21 (Windows build: https://github.com/ar51an/iperf3-win-builds/releases/tag/3.21) |
| Cygwin DLL 3.6.7-1 (`cygwin1.dll`, needed by that `iperf3.exe`) | `vendor/iperf3-win/` | LGPL-3.0-or-later with the Cygwin linking exception — [LICENSE-cygwin-LGPL-3.0.txt](vendor/iperf3-win/LICENSE-cygwin-LGPL-3.0.txt), https://cygwin.com/licensing.html | Unmodified binary. Its source code: https://cygwin.com/git/?p=newlib-cygwin.git;a=tag;h=refs/tags/cygwin-3.6.7 (tag `cygwin-3.6.7`) |

The Gradle wrapper files under `android/` are from Gradle (Apache License 2.0).

## Used, not kept here

- **Python packages** (installed by `uv`, bundled only into built packages):
  PySide6 / Qt for Python (LGPL-3.0, used unmodified as separate libraries), asyncssh
  (EPL-2.0 or GPL-2.0-or-later), and their dependencies.
- **Android libraries**: AndroidX Media3 / ExoPlayer and AndroidX (Apache License 2.0).
- **External tools**, installed separately and never changed by us: ffmpeg/ffprobe (LGPL/GPL
  depending on the build), TSDuck (BSD-2-Clause), mpv (GPL-2.0-or-later / LGPL), VLC
  (GPL-2.0-or-later), Android platform-tools/adb (Apache License 2.0), iperf3 on Linux
  (BSD-3-Clause). See [`docs/15-tools.md`](docs/15-tools.md).

If you redistribute built packages (`.deb`, the Windows app, the APK), include this file and
the license texts of what they contain.
