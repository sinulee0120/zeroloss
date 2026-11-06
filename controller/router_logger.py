"""Router Logger (16 S-66..S-69, 2026-11-05 user): on a problem the router is to blame
for, press Start in the person's router firmware log program, let it log for the record
time, press Stop, and press nothing more until it has saved everything.

The program is a Windows (Tk) window. Start and stop are the Windows commands its own
buttons send: ``WM_COMMAND`` 34 (start) and 35 (stop), posted straight to the window (user:
"메뉴로 하지 말라고" — nothing is opened or clicked; it works in the background, minimized
too). 33 opens a settings dialog and is never sent. Whether it is logging is the state of
those two commands (start enabled and stop greyed = idle; start greyed and stop enabled =
logging), read by their IDs. User, 2026-11-05: "stop을
누르고 ... stop이 정확히 마무리될때까지는 start를 눌러선 안돼. 오류나", "어떨땐 몇분을
기다려야" — after Stop the save is done only when the menu is idle again and, for
``STABLE_S`` in a row, its log files have not changed, the program has written nothing
to disk (Windows' own count of its writes) and has been nearly idle (under ``BUSY_CPU``
of a core). 2026-11-06 user, "stop후 saving동안 다시 start가 안눌린다는것만 확실하면":
the menu turns idle at once after Stop, before the save is done. The split files (their
size is the program's own setting, so how many varies) all count. Problems meanwhile
are not logged again.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

WM_COMMAND = 0x0111
START, STOP = 34, 35  # the commands its start and stop send (33 = a settings dialog: never)
MF_BYCOMMAND = 0x0
LOG_GLOB = "*.bin"  # the files it writes next to itself
STABLE_S = 10.0  # the newest log file unchanged this long after Stop: saved
BUSY_CPU = 0.2  # the program using more than this share of a CPU core: still working
SHOW_FILES = 3  # more files than this (a small split size, S-69 program's own setting): counted
SLOW_SAVE_S = 1200.0  # told once: still saving (it is still not pressed)
POLL_S = 1.0

Emit = Callable[[str, str, str, str], None]  # code, level, text, device


class Window(Protocol):
    def state(self) -> tuple[bool, bool] | None:
        """(start enabled, stop enabled); None when the program's window is not there."""

    def press(self, command: int) -> bool:
        """Post the command (START or STOP) to the window; False if it is not there."""

    def newest(self) -> tuple[str, int] | None:
        """The newest log file next to the program: (name, size)."""

    def writes(self) -> int | None:
        """Bytes the program has written to disk so far (any file); None = not known."""

    def files(self) -> list[tuple[str, int]]:
        """Its log files, oldest first by name (fw_log_<date>_<time>_<part>): (name, size)."""

    def cpu(self) -> float | None:
        """CPU seconds the program has used so far; None = not known."""


class WinWindow:
    """The program's main window, found by its .exe path (Windows only)."""

    def __init__(self, program: str) -> None:
        self.program = Path(program)
        self._hwnd = 0

    def _window(self) -> int:
        import ctypes

        u = getattr(ctypes, "windll").user32  # Windows only  # noqa: B009
        if not (self._hwnd and u.IsWindow(self._hwnd)):
            self._hwnd = self._find()
        return self._hwnd

    def _find(self) -> int:
        import ctypes
        from ctypes import wintypes

        dll = getattr(ctypes, "windll")  # Windows only  # noqa: B009
        u, k = dll.user32, dll.kernel32
        want = os.path.normcase(str(self.program))
        found: list[int] = []

        def image(pid: int) -> str:
            h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return ""
            try:
                buf, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
                ok = k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
                return os.path.normcase(buf.value) if ok else ""
            finally:
                k.CloseHandle(h)

        def each(hwnd: int, _: int) -> bool:
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if u.IsWindowVisible(hwnd) and u.GetMenu(hwnd) and image(pid.value) == want:
                found.append(int(hwnd))
            return True

        winfunc = getattr(ctypes, "WINFUNCTYPE")  # Windows only  # noqa: B009
        proto = winfunc(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        u.EnumWindows(proto(each), 0)
        return found[0] if found else 0

    def state(self) -> tuple[bool, bool] | None:
        import ctypes

        hwnd = self._window()
        if not hwnd:
            return None
        u = getattr(ctypes, "windll").user32  # Windows only  # noqa: B009
        u.GetMenu.restype = ctypes.c_void_p
        u.GetMenuState.argtypes = (ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint)
        u.GetMenuState.restype = ctypes.c_uint
        bar = u.GetMenu(hwnd)  # the states of the two commands, by their IDs
        st = [u.GetMenuState(bar, c, MF_BYCOMMAND) for c in (START, STOP)]
        if any(x == 0xFFFFFFFF for x in st):
            return None  # not this program's window (no such commands)
        return not (st[0] & 0x3), not (st[1] & 0x3)

    def press(self, command: int) -> bool:
        import ctypes

        if command not in (START, STOP):
            return False  # never anything else (33 opens a settings dialog)
        hwnd = self._window()
        u = getattr(ctypes, "windll").user32  # Windows only  # noqa: B009
        return bool(hwnd and u.PostMessageW(hwnd, WM_COMMAND, command, 0))

    def writes(self) -> int | None:
        import ctypes
        from ctypes import wintypes

        hwnd = self._window()
        if not hwnd:
            return None
        dll = getattr(ctypes, "windll")  # Windows only  # noqa: B009
        pid = wintypes.DWORD()
        dll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = dll.kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            io = (ctypes.c_ulonglong * 6)()  # IO_COUNTERS: reads, writes, others, then bytes
            ok = dll.kernel32.GetProcessIoCounters(h, ctypes.byref(io))
            return int(io[4]) if ok else None  # WriteTransferCount
        finally:
            dll.kernel32.CloseHandle(h)

    def cpu(self) -> float | None:
        import ctypes
        from ctypes import wintypes

        hwnd = self._window()
        if not hwnd:
            return None
        dll = getattr(ctypes, "windll")  # Windows only  # noqa: B009
        pid = wintypes.DWORD()
        dll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = dll.kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
        if not h:
            return None
        try:
            t = [ctypes.c_ulonglong() for _ in range(4)]  # created, exited, kernel, user
            ok = dll.kernel32.GetProcessTimes(h, *(ctypes.byref(x) for x in t))
            return (t[2].value + t[3].value) / 1e7 if ok else None  # 100 ns units
        finally:
            dll.kernel32.CloseHandle(h)

    def files(self) -> list[tuple[str, int]]:
        try:
            return [(f.name, f.stat().st_size) for f in sorted(self.program.parent.glob(LOG_GLOB))]
        except OSError:
            return []

    def newest(self) -> tuple[str, int] | None:
        try:
            files = sorted(self.program.parent.glob(LOG_GLOB), key=lambda f: f.stat().st_mtime)
            return (files[-1].name, files[-1].stat().st_size) if files else None
        except OSError:
            return None


class RouterLogger:
    """One logging at a time: idle -> logging (``record_s``) -> saving -> idle."""

    def __init__(self, emit: Emit, window_of: Callable[[str], Window] = WinWindow,
                 clock: Callable[[], float] = time.monotonic) -> None:  # fmt: skip
        self.emit, self.window_of, self.clock = emit, window_of, clock
        self.cfg: dict[str, Any] | None = None
        self.state = "idle"
        self._win: Window | None = None
        self._lock = threading.Lock()
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self._until = self._since = self._stable_since = 0.0
        self._last: tuple[tuple[str, int] | None, int | None] | None = None
        self._ignored = 0
        self._before: set[str] = set()  # its log files before this Start
        self._cpu: tuple[float, float] | None = None  # (its CPU seconds, when) at the last look
        self._told: set[str] = set()

    def configure(self, cfg: dict[str, Any] | None) -> None:
        """This test's choice (None: off). A logging or save under way still finishes."""
        with self._lock:
            self.cfg = cfg
            self._told.clear()
            if cfg is not None and self.state == "idle":
                self._win = self.window_of(str(cfg["program"]))
            if cfg is not None and sys.platform == "win32" and self._thread is None:
                self._thread = threading.Thread(target=self._loop, daemon=True,
                                                name="router-logger")  # fmt: skip
                self._thread.start()

    def close(self) -> None:
        self._halt.set()

    def trigger(self, reason: str, device: str) -> bool:
        """A problem the router is to blame for: Start, when the program is idle."""
        with self._lock:
            if self.cfg is None or self._win is None:
                return False
            if self.state != "idle":
                self._ignored += 1  # logging or saving: not logged again
                return False
            st = self._win.state()
            if st is None:
                self._tell_once("gone", "warn", "Router Logger: its program window is not "
                                "open, nothing pressed (open it, or set S-69)", device)  # fmt: skip
                return False
            if not st[0] or st[1]:  # it is logging already (started by hand)
                self._tell_once("busy", "info", "Router Logger is already logging "
                                "(started by hand): not pressed", device)  # fmt: skip
                return False
            self._before = {name for name, _ in self._win.files()}
            if not self._win.press(START):
                return False
            record_s = int(self.cfg["record_s"])
            self.state, self._until, self._ignored = "logging", self.clock() + record_s, 0
            self.emit("ZL-INFO", "info", f"Router Logger started: {device} {reason} "
                      f"(logs {record_s} s, then Stop)", device)  # fmt: skip
            return True

    def step(self) -> None:
        """One look (every ``POLL_S``): Stop when the time is up, then wait for the save."""
        with self._lock:
            win, now = self._win, self.clock()
            if win is None or self.state == "idle":
                return
            if self.state == "logging":
                if now < self._until:
                    return
                st = win.state()
                if st is not None and st[1]:
                    win.press(STOP)
                self.state, self._since, self._stable_since = "saving", now, now
                self._last = (win.newest(), win.writes())
                self._cpu = None
                self.emit("ZL-INFO", "info", "Router Logger: Stop pressed; waiting until it "
                          "has saved (nothing is pressed meanwhile)", "")  # fmt: skip
                return
            st, newest = win.state(), win.newest()
            seen = (newest, win.writes())  # the file and the program's own disk writes
            if seen != self._last or self._busy(win, now):
                self._last, self._stable_since = seen, now
            if st is None:
                self.state = "idle"
                self.emit("ZL-INFO", "warn", "Router Logger: its window closed while it "
                          "was saving", "")  # fmt: skip
                return
            if st[0] and not st[1] and now - self._stable_since >= STABLE_S:
                self.state = "idle"
                # 2026-11-06 user: "보통 파일이두개 정도로 나뉘어서" (its split): every file
                made = [(n, s) for n, s in win.files() if n not in self._before]
                if not made and newest:
                    made = [newest]
                names = (
                    ", ".join(n for n, _ in made)
                    if len(made) <= SHOW_FILES
                    else f"{len(made)} files ({made[0][0]} … {made[-1][0]})"
                ) or "no file"
                total = sum(s for _, s in made)
                more = (f"; {self._ignored} more problem(s) meanwhile were not logged again"
                        if self._ignored else "")  # fmt: skip
                self.emit("ZL-INFO", "info", f"Router Logger saved {names} ({total:,} B) in "
                          f"{now - self._since:.0f} s{more}", "")  # fmt: skip
                if self.cfg is not None:  # the next problem: the program as set now
                    self._win = self.window_of(str(self.cfg["program"]))
            elif now - self._since >= SLOW_SAVE_S:
                self._tell_once("slow", "warn", f"Router Logger is still saving after "
                                f"{SLOW_SAVE_S / 60:.0f} min: ZeroLoss waits and presses "
                                "nothing", "")  # fmt: skip

    def _busy(self, win: Window, now: float) -> bool:
        """The program worked hard since the last look (a save that writes nothing for a
        while still uses the CPU); not known = not busy (the file and writes still count)."""
        used = win.cpu()
        last, self._cpu = self._cpu, (None if used is None else (used, now))
        if used is None or last is None or now <= last[1]:
            return False
        return (used - last[0]) / (now - last[1]) > BUSY_CPU

    def _tell_once(self, key: str, level: str, text: str, device: str) -> None:
        if key not in self._told:
            self._told.add(key)
            self.emit("ZL-INFO", level, text, device)

    def _loop(self) -> None:
        while not self._halt.wait(POLL_S):
            try:
                self.step()
            except Exception as e:  # the watch never dies: said once, tried again
                self._tell_once("error", "warn", f"Router Logger check failed: {e!r}", "")
