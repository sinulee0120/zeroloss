"""Run this program as a Windows service (docs/06-install.md 4.2: ``ZeroLossAgent``,
``ZeroLossDeployer``), with the service manager's own protocol and no extra package.

2026-11-21 user ("윈도우즈 sender,receiver도 원클릭 인스톨되게 해주고"): the Windows sender
and receiver PCs run the same node and Deployer as the Linux ones, started by Windows at
boot like systemd starts them there. ``run(name, start, stop)`` blocks while the service
runs: Windows calls ServiceMain, ``start`` runs, the service says RUNNING, a Stop or a
shutdown calls ``stop`` and the service says STOPPED. Run from a console instead (not by
the service manager), it returns False at once and the caller runs in the foreground.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

SERVICE_WIN32_OWN_PROCESS = 0x10
STOPPED, START_PENDING, STOP_PENDING, RUNNING = 1, 2, 3, 4
ACCEPT_STOP, ACCEPT_SHUTDOWN = 0x1, 0x4
CONTROL_STOP, CONTROL_INTERROGATE, CONTROL_SHUTDOWN = 1, 4, 5
NOT_FROM_THE_MANAGER = 1063  # ERROR_FAILED_SERVICE_CONTROLLER_CONNECT: run from a console
NO_ERROR = 0
START_WAIT_MS = 60_000  # how long Windows waits for ``start`` (the node starts in seconds)


class _Status(ctypes.Structure):
    _fields_ = [
        ("service_type", wintypes.DWORD), ("current_state", wintypes.DWORD),
        ("controls_accepted", wintypes.DWORD), ("win32_exit_code", wintypes.DWORD),
        ("service_exit_code", wintypes.DWORD), ("check_point", wintypes.DWORD),
        ("wait_hint", wintypes.DWORD),
    ]  # fmt: skip


def run(name: str, start: Callable[[], None], stop: Callable[[], None]) -> bool:
    """Serve as the Windows service ``name`` until Windows stops it; False when this
    process was not started by the service manager (then nothing was started)."""
    if sys.platform != "win32":
        return False
    advapi = getattr(ctypes, "windll").advapi32  # Windows only  # noqa: B009
    winfunc = getattr(ctypes, "WINFUNCTYPE")  # Windows only  # noqa: B009
    main_t = winfunc(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))
    handler_t = winfunc(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                        ctypes.c_void_p)  # fmt: skip
    advapi.RegisterServiceCtrlHandlerExW.restype = ctypes.c_void_p
    advapi.RegisterServiceCtrlHandlerExW.argtypes = (wintypes.LPCWSTR, handler_t, ctypes.c_void_p)
    advapi.SetServiceStatus.argtypes = (ctypes.c_void_p, ctypes.POINTER(_Status))
    ended = threading.Event()
    state: dict[str, Any] = {"handle": None, "status": _Status(SERVICE_WIN32_OWN_PROCESS)}

    def report(current: int, wait_ms: int = 0, exit_code: int = NO_ERROR) -> None:
        s = state["status"]
        s.current_state = current
        s.controls_accepted = (ACCEPT_STOP | ACCEPT_SHUTDOWN) if current == RUNNING else 0
        s.win32_exit_code = exit_code
        s.check_point = s.check_point + 1 if current in (START_PENDING, STOP_PENDING) else 0
        s.wait_hint = wait_ms
        if state["handle"]:
            advapi.SetServiceStatus(state["handle"], ctypes.byref(s))

    def handler(control: int, _type: int, _data: Any, _ctx: Any) -> int:
        if control in (CONTROL_STOP, CONTROL_SHUTDOWN):
            report(STOP_PENDING, 30_000)
            ended.set()
        elif control == CONTROL_INTERROGATE:
            report(state["status"].current_state)
        return NO_ERROR

    def service_main(_argc: int, _argv: Any) -> None:
        state["handle"] = advapi.RegisterServiceCtrlHandlerExW(name, state["handler"], None)
        report(START_PENDING, START_WAIT_MS)
        try:
            start()
        except Exception:  # Windows is told it stopped; the event log has the code
            report(STOPPED, exit_code=1)
            return
        report(RUNNING)
        ended.wait()
        try:
            stop()
        finally:
            report(STOPPED)

    state["handler"] = handler_t(handler)  # kept: Windows calls them back
    state["main"] = main_t(service_main)

    class _Entry(ctypes.Structure):
        _fields_ = [("name", wintypes.LPWSTR), ("main", main_t)]

    table = (_Entry * 2)(_Entry(name, state["main"]), _Entry(None, main_t()))
    # True once the service has stopped; False (1063) when not started by the manager
    return bool(advapi.StartServiceCtrlDispatcherW(table))
