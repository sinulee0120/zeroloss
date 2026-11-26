"""Processes in the real-tool tests, on Linux and on Windows (2026-11-21: the tests asked
``pgrep`` and sent SIGKILL/SIGSTOP, which Windows does not have).

- ``pids(name, needle)``: the PIDs of programs called ``name`` whose command line has
  ``needle`` (Linux ``pgrep`` + /proc, Windows the process list).
- ``kill(pid)``: at once (SIGKILL; Windows TerminateProcess).
- ``pause(pid)``: it stays, doing nothing (SIGSTOP; Windows NtSuspendProcess).
- ``LOOPBACK``: the test card as the receiver and the player take it (Linux by name,
  elsewhere by address).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

WINDOWS = sys.platform == "win32"
LOOPBACK: dict[str, Any] = {"local_ip": "127.0.0.1"} if WINDOWS else {"nic": "lo"}


def pids(name: str, needle: str | None = None) -> set[int]:
    if WINDOWS:
        return _windows_pids(name, needle)
    out = subprocess.run(["pgrep", "-x", name], capture_output=True, text=True)
    got = set()
    for x in out.stdout.split():
        if needle is None:
            got.add(int(x))
            continue
        try:
            if needle.encode() in Path(f"/proc/{x}/cmdline").read_bytes():
                got.add(int(x))
        except OSError:
            continue  # ended meanwhile
    return got


def _windows_pids(name: str, needle: str | None) -> set[int]:
    query = (f"Get-CimInstance Win32_Process -Filter \"Name='{name}.exe'\" | "
             "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress")  # fmt: skip
    out = subprocess.run(["powershell", "-NoProfile", "-Command", query], capture_output=True,
                         text=True, timeout=60).stdout.strip()  # fmt: skip
    if not out:
        return set()
    rows = json.loads(out)
    rows = rows if isinstance(rows, list) else [rows]
    return {int(r["ProcessId"]) for r in rows
            if (needle is None or needle in str(r.get("CommandLine") or ""))
            and _running(int(r["ProcessId"]))}  # fmt: skip


def _running(pid: int) -> bool:
    """Windows lists an ended process as long as a handle to it is open (the runner's
    own); only one still running counts."""
    import ctypes

    k = getattr(ctypes, "windll").kernel32  # noqa: B009
    h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(k.GetExitCodeProcess(h, ctypes.byref(code))) and code.value == 259
    finally:
        k.CloseHandle(h)


def fake_program(folder: Path, name: str, sh: str, cmd: str) -> Path:
    """A stand-in program: a shell script, or on Windows a .cmd file (a shell script
    cannot be started there, WinError 193)."""
    if WINDOWS:
        fake = folder / f"{name}.cmd"
        fake.write_text("@echo off\r\n" + cmd + "\r\n", encoding="ascii")
        return fake
    fake = folder / name
    fake.write_text("#!/bin/sh\n" + sh + "\n", encoding="ascii")
    fake.chmod(0o755)
    return fake


def lowered(pid: int) -> bool:
    """It gives way to normal programs (nice >= 10; Windows: below normal or idle)."""
    if not WINDOWS:
        return bool(os.getpriority(getattr(os, "PRIO_PROCESS"), pid) >= 10)  # noqa: B009
    import ctypes

    k = getattr(ctypes, "windll").kernel32  # noqa: B009
    h = k.OpenProcess(0x1000, False, pid)
    try:
        return k.GetPriorityClass(h) in (0x4000, 0x40)  # BELOW_NORMAL, IDLE
    finally:
        k.CloseHandle(h)


def kill(pid: int) -> None:
    os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))  # Windows: TerminateProcess


def pause(pid: int) -> None:
    if not WINDOWS:
        os.kill(pid, getattr(signal, "SIGSTOP"))  # noqa: B009 (not on Windows)
        return
    import ctypes

    k, nt = getattr(ctypes, "windll").kernel32, getattr(ctypes, "windll").ntdll  # noqa: B009
    h = k.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
    if not h:
        raise OSError(f"cannot open {pid}")
    try:
        if nt.NtSuspendProcess(h) != 0:
            raise OSError(f"cannot pause {pid}")
    finally:
        k.CloseHandle(h)
