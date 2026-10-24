"""CPU, memory and disk readings for the heartbeat (docs/02-data.md 5 ``resources``).

No third-party packages: Linux reads /proc, Windows uses kernel32. A value the OS cannot
give is None (shown as unknown, never as 0). Warning thresholds (SYS-001..004) use these.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Resources:
    cpu_pct: float | None
    mem_free_pct: float | None
    disk_free_mb: int | None
    proc_mem_mb: float | None

    def to_dict(self) -> dict[str, float | int | None]:
        return {
            "cpu_pct": self.cpu_pct,
            "mem_free_pct": self.mem_free_pct,
            "disk_free_mb": self.disk_free_mb,
            "proc_mem_mb": self.proc_mem_mb,
        }


class ResourceMonitor:
    """``read()`` gives CPU use since the previous call (call once per second)."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._last_cpu: tuple[int, int] | None = None
        self._last_pct: float | None = None
        self._lock = threading.Lock()

    def read(self) -> Resources:
        with self._lock:
            return Resources(
                cpu_pct=self._cpu_pct(),
                mem_free_pct=_mem_free_pct(),
                disk_free_mb=_disk_free_mb(self._data_dir),
                proc_mem_mb=_proc_mem_mb(),
            )

    def _cpu_pct(self) -> float | None:
        now = _cpu_times()
        if now is None:
            return None
        last = self._last_cpu
        if last is None:
            self._last_cpu = now
            return None
        busy, total = now[0] - last[0], now[1] - last[1]
        if total <= 0:  # two reads within one clock tick: keep the last value
            return self._last_pct
        self._last_cpu = now
        self._last_pct = round(100.0 * busy / total, 1)
        return self._last_pct


def _disk_free_mb(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free // (1024 * 1024)
    except OSError:
        return None


if sys.platform == "win32":  # pragma: no cover - checked on the Windows PC
    import ctypes
    from ctypes import wintypes

    class _MemStatus(ctypes.Structure):
        _fields_ = [
            ("dwLength", wintypes.DWORD),
            ("dwMemoryLoad", wintypes.DWORD),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    class _Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    def _cpu_times() -> tuple[int, int] | None:
        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None

        def val(t: wintypes.FILETIME) -> int:
            return (t.dwHighDateTime << 32) | t.dwLowDateTime

        total = val(kernel) + val(user)  # kernel time includes idle time
        return total - val(idle), total

    def _mem_free_pct() -> float | None:
        m = _MemStatus()
        m.dwLength = ctypes.sizeof(m)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
            return None
        return round(100.0 * m.ullAvailPhys / m.ullTotalPhys, 1)

    def _proc_mem_mb() -> float | None:
        c = _Counters()
        c.cb = ctypes.sizeof(c)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        if not ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(c), c.cb):
            return None
        return round(c.WorkingSetSize / (1024 * 1024), 1)

else:

    def _cpu_times() -> tuple[int, int] | None:
        try:
            with open("/proc/stat", encoding="ascii") as f:
                fields = [int(x) for x in f.readline().split()[1:]]
        except (OSError, ValueError):
            return None
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)  # idle + iowait
        total = sum(fields[:8])  # guest time is already inside user
        return total - idle, total

    def _mem_free_pct() -> float | None:
        info: dict[str, int] = {}
        try:
            with open("/proc/meminfo", encoding="ascii") as f:
                for line in f:
                    key, _, rest = line.partition(":")
                    info[key] = int(rest.split()[0])
        except (OSError, ValueError, IndexError):
            return None
        if "MemAvailable" not in info or not info.get("MemTotal"):
            return None
        return round(100.0 * info["MemAvailable"] / info["MemTotal"], 1)

    def _proc_mem_mb() -> float | None:
        try:
            with open(f"/proc/{os.getpid()}/statm", encoding="ascii") as f:
                pages = int(f.read().split()[1])
        except (OSError, ValueError, IndexError):
            return None
        return round(pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024), 1)
