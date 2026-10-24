# iperf3 for Windows (throughput test on the Controller PC)

- iperf 3.21, BSD-3-Clause (ESnet): https://github.com/esnet/iperf/releases/tag/3.21
- Windows build: https://github.com/ar51an/iperf3-win-builds/releases/tag/3.21 (`iperf-3.21-win64.zip`)
- Downloaded 2026-10-17 with the user's OK ("다운받아"); the hashes equal the ones pinned by
  the reference (RealTest `vendor/iperf3/SHA256SUMS.txt`):

```
9B73B7E0E0326347B5F4AC4F6A1FC34FE60A5966E5FD172C7BFCD0E1CC93E709  iperf-3.21-win64.zip
08D35A41132930B3F2657B99CB81AE651EBBC9AC42E387084BB8D76547DBB389  iperf3.exe
0AB76B4724499DF54B75B7FA701788F1E77425CE65C8BCA0A9F2120598BB8A70  cygwin1.dll
```

The Windows build copies these into `cli/` next to `zeroloss.exe`.
