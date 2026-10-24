"""PyInstaller entry: ``ZeroLoss.exe`` on the Windows Controller PC (docs/06-install.md
4.1-1, the portable build before the installer).

Double-click: the Controller service is started in the background (``cli\\zeroloss.exe
serve --role controller``) unless one already answers on 127.0.0.1:10994, then the
Controller Desktop opens on it. Closing the window never stops a test (12 1-0); when
nothing runs, it ends the service too, so nothing is left behind (GAP-116: the user had
to end it in Task Manager). Data and reports go where the installer will put them
(06 4.1).
"""

import faulthandler
import os
import socket
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from controller.ui.app import main as desktop_main
from core.node import LOCAL_PORT
from core.protocol.client import NodeClient
from core.protocol.envelope import ZlError

HOME = Path(sys.executable).resolve().parent  # the folder ZeroLoss.exe is in
DATA = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "ZeroLoss" / "Controller"
REPORTS = Path(os.environ.get("SYSTEMDRIVE", "C:") + "\\") / "ZeroLoss" / "Reports"
START_WAIT_S = 30.0


def answering(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def start_service() -> None:
    """The service outlives this window: its own process group, no console, out of
    this process's job (so closing the window or a terminal does not end it)."""
    (DATA / "logs").mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)
    log = open(DATA / "logs" / "controller-service.log", "ab")  # noqa: SIM115 (child keeps it)
    flags = 0x00000008 | 0x00000200 | 0x08000000 | 0x01000000  # DETACHED, NEW_GROUP,
    # NO_WINDOW, BREAKAWAY_FROM_JOB
    argv = [
        str(HOME / "cli" / "zeroloss.exe"), "serve", "--role", "controller",
        "--data", str(DATA), "--reports", str(REPORTS), "--modules", str(HOME / "modules"),
    ]  # fmt: skip
    try:
        subprocess.Popen(argv, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         creationflags=flags, cwd=str(HOME))  # fmt: skip
    except OSError:  # breakaway refused by the parent job: start without it
        subprocess.Popen(argv, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         creationflags=flags & ~0x01000000, cwd=str(HOME))  # fmt: skip
    end = time.monotonic() + START_WAIT_S
    while time.monotonic() < end and not answering(LOCAL_PORT):
        time.sleep(0.3)


def first_window() -> bool:
    """One Controller window per PC (GAP-117: two quick double-clicks ran two windows
    and two services). A second start brings the open window to the front and ends.
    The mutex lives as long as this process; Windows frees it if the process dies."""
    import ctypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    user = ctypes.WinDLL("user32")  # type: ignore[attr-defined]
    kernel.CreateMutexW(None, False, r"Local\ZeroLossControllerWindow")
    if ctypes.get_last_error() != 183:  # type: ignore[attr-defined]  # ERROR_ALREADY_EXISTS
        return True
    end = time.monotonic() + 10  # the first one may still be opening its window
    while time.monotonic() < end:
        hwnd = user.FindWindowW(None, "ZeroLoss")
        if hwnd:
            user.ShowWindow(hwnd, 9)  # SW_RESTORE
            user.SetForegroundWindow(hwnd)
            break
        time.sleep(0.3)
    return False


def quit_service() -> None:
    """Ask the service to end; it refuses while a test or the broadcast runs."""
    client = NodeClient("127.0.0.1", LOCAL_PORT, f"launcher-{uuid.uuid4()}", "Controller service")
    try:
        gen = int(client.get("/v1/controller/status", timeout_s=2.0).get("generation", 0))
        client.command("/v1/controller/quit", gen, timeout_s=5.0)
    except ZlError:
        pass  # a test runs, or the service is gone already
    finally:
        client.close()


if not first_window():
    raise SystemExit(0)
if not answering(LOCAL_PORT):
    start_service()
# 2026-10-09: the window vanished during a test with no trace. Python errors and fatal
# crashes of the window go to logs/window.log (the service has its own log).
(DATA / "logs").mkdir(parents=True, exist_ok=True)
window_log = open(DATA / "logs" / "window.log", "a", encoding="utf-8")  # noqa: SIM115


def _note(text: str) -> None:
    window_log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    window_log.flush()


def _python_error(kind: type[BaseException], err: BaseException, tb: Any) -> None:
    traceback.print_exception(kind, err, tb, file=window_log)
    window_log.flush()


_note("window opened")
faulthandler.enable(window_log)
sys.excepthook = _python_error
code = desktop_main([])
_note(f"window closed ({code})")
quit_service()
raise SystemExit(code)
