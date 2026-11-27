"""Node package update on Windows (docs/06-install.md 11.5 "Windows 구현 규칙", 09 RUN-013).

2026-11-28 user ("Run 13도 하고"): the Windows sender and receiver PCs update from the
Controller like the Ubuntu ones. The package holds ``ZeroLoss-Agent-Setup-<build>.exe``;
the setup stops and re-creates both services (ZeroLossAgent, ZeroLossDeployer), so it
cannot be the Deployer's own child job: a small ``cmd`` script starts it detached from the
Deployer and out of its job, writes the setup's exit code to a file and starts both
services again (the Linux ``systemd-run`` job's part). ``NodeUpdate`` is the same as on
Linux: the new Deployer reads ``update.json`` at start, checks the node's build and goes
back to ``previous.exe`` when the update did not take.

Agent Desktop (the player's window, in the signed-in person's session) is closed before
the setup (its files are replaced) and started again in that session afterwards: the
Deployer runs as LocalSystem in session 0, so it starts it with the person's own token
(``WTSQueryUserToken`` + ``CreateProcessAsUserW``). Nobody signed in: it starts at the
next sign-in from the Run key, as always.
"""

from __future__ import annotations

import contextlib
import ctypes
import re
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

from agent.deployer.errors import DeployError, damaged
from agent.deployer.node_update import DEB_NAME, SETUP, Launcher

SETUP_NAME = re.compile(r"^ZeroLoss-Agent-Setup-(\d{8}\.\d{4})\.exe$")
DESKTOP_EXE = "zeroloss-agent-desktop.exe"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ZeroLoss Agent Desktop"  # installer/windows/zeroloss-agent.iss [Registry]
SETUP_ARGS = "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /NOCANCEL"
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
STILL_ACTIVE = 259


def job_script() -> str:
    """The install job: ``%1`` setup, ``%2`` its log, ``%3`` exit code file. Only fixed
    paths reach the command line (11.7: no command execution over 10991)."""
    lines = [
        "@echo off",
        "setlocal",
        f"taskkill /F /T /IM {DESKTOP_EXE} >nul 2>&1",
        f'"%~1" {SETUP_ARGS} /LOG="%~2"',
        "set c=%ERRORLEVEL%",
        '>"%~3.tmp" echo %c%',  # the redirect first: "echo 0>f" would be stream 0
        'move /Y "%~3.tmp" "%~3" >nul',
        "sc start ZeroLossAgent >nul 2>&1",
        "sc start ZeroLossDeployer >nul 2>&1",
        "exit /b %c%",
    ]
    return "\r\n".join(lines) + "\r\n"


class WindowsLauncher(Launcher):
    os_name, ext, tool = "windows", ".exe", SETUP

    def __init__(self, folder: Path) -> None:
        super().__init__(script="")
        self.folder = Path(folder)  # NodeUpdate.folder: the job's pid file lives there

    def fields(self, deb: Path, name: str = "") -> dict[str, str]:
        """The setup's build from its name in the package, and a Windows program in it."""
        m = SETUP_NAME.match(name)
        if m is None:
            raise damaged(f"{name!r} is not ZeroLoss-Agent-Setup-<build>.exe")
        try:
            with deb.open("rb") as f:
                head = f.read(2)
        except OSError as e:
            raise damaged(f"cannot read the setup: {e}") from e
        if head != b"MZ":
            raise damaged("the setup is not a Windows program")
        return {"Package": DEB_NAME, "Version": m.group(1)}

    def _pid_file(self, job_id: str) -> Path:
        return self.folder / f"update-{job_id}.pid"

    def launch(self, job_id: str, deb: Path, log: Path, status: Path) -> None:
        script = self.folder / f"update-{job_id}.cmd"
        argv = ["cmd.exe", "/d", "/c", str(script), str(deb), str(log), str(status)]
        try:
            script.write_text(job_script(), encoding="ascii")
            proc = _detached(argv, cwd=self.folder)
            self._pid_file(job_id).write_text(str(proc.pid), encoding="ascii")
        except OSError as e:
            raise DeployError(
                "INS-028", "The install could not be started", str(e)[-200:],
                "Run the setup on that PC by hand.",
            ) from e  # fmt: skip

    def running(self, job_id: str) -> bool:
        try:
            pid = int(self._pid_file(job_id).read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            return False
        return _alive_cmd(pid)

    def after(self) -> str | None:
        """Agent Desktop again, in the signed-in person's session."""
        if _desktop_running():
            return None
        command = _desktop_command()
        if not command:
            return "no Agent Desktop entry in the Run key; it was not started again"
        return start_in_user_session(command)


def _detached(argv: list[str], cwd: Path) -> subprocess.Popen[bytes]:
    """Out of the Deployer's process tree and, where allowed, its job: the setup stops
    the Deployer service mid-way."""
    base = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    for flags in (base | CREATE_BREAKAWAY_FROM_JOB, base):
        try:
            return subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    close_fds=True, creationflags=flags)  # fmt: skip
        except PermissionError:  # a job that does not allow breaking away
            continue
    raise OSError("cmd.exe could not be started")


def _alive_cmd(pid: int) -> bool:
    """That process still runs and is cmd.exe (not a new process with the same number)."""
    if sys.platform != "win32":
        return False
    k32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    _handle_types(k32)
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != STILL_ACTIVE:
            return False
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(len(buf))
        if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return True  # alive; the name is a nicety
        return buf.value.lower().endswith("cmd.exe")
    finally:
        k32.CloseHandle(h)


def _handle_types(k32: Any) -> None:
    """64-bit handles: without these ctypes would pass them as 32-bit ints."""
    h, dw = wintypes.HANDLE, wintypes.DWORD
    k32.OpenProcess.restype = h
    k32.OpenProcess.argtypes = [dw, wintypes.BOOL, dw]
    k32.GetExitCodeProcess.argtypes = [h, ctypes.POINTER(dw)]
    k32.QueryFullProcessImageNameW.argtypes = [h, dw, wintypes.LPWSTR, ctypes.POINTER(dw)]
    k32.CloseHandle.argtypes = [h]


def _desktop_running() -> bool:
    try:
        done = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {DESKTOP_EXE}", "/NH"],
                              capture_output=True, text=True, timeout=15, check=False,
                              creationflags=CREATE_NO_WINDOW)  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return False
    return DESKTOP_EXE.lower() in done.stdout.lower()


def _desktop_command() -> str | None:
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, RUN_VALUE)
    except OSError:
        return None
    return str(value) if value else None


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD), ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]  # fmt: skip


class _ProcessInfo(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD),
    ]  # fmt: skip


def start_in_user_session(command: str) -> str | None:
    """``command`` in the console session's desktop as the person signed in there. A
    note when it could not (nobody signed in: the Run key starts it at sign-in)."""
    if sys.platform != "win32":
        return "not Windows"
    k32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    wts: Any = ctypes.WinDLL("wtsapi32", use_last_error=True)
    adv: Any = ctypes.WinDLL("advapi32", use_last_error=True)
    env_dll: Any = ctypes.WinDLL("userenv", use_last_error=True)
    _handle_types(k32)
    k32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
    session = k32.WTSGetActiveConsoleSessionId()
    if session == 0xFFFFFFFF:
        return "nobody is signed in; Agent Desktop starts at the next sign-in"
    token = wintypes.HANDLE()
    if not wts.WTSQueryUserToken(wintypes.ULONG(session), ctypes.byref(token)):
        return (f"nobody is signed in (WTSQueryUserToken {ctypes.get_last_error()}); "
                "Agent Desktop starts at the next sign-in")  # fmt: skip
    env = ctypes.c_void_p()
    try:
        if not env_dll.CreateEnvironmentBlock(ctypes.byref(env), token, False):
            env = ctypes.c_void_p()
        si = _StartupInfo()
        si.cb = ctypes.sizeof(si)
        si.lpDesktop = "winsta0\\default"
        pi = _ProcessInfo()
        line = ctypes.create_unicode_buffer(command)
        flags = 0x00000400  # CREATE_UNICODE_ENVIRONMENT
        ok = adv.CreateProcessAsUserW(token, None, line, None, None, False, flags,
                                      env, None, ctypes.byref(si), ctypes.byref(pi))  # fmt: skip
        if not ok:
            err = ctypes.get_last_error()
            return f"Agent Desktop could not be started (CreateProcessAsUser {err})"
        k32.CloseHandle(pi.hThread)
        k32.CloseHandle(pi.hProcess)
        return None
    finally:
        if env:
            with contextlib.suppress(Exception):
                env_dll.DestroyEnvironmentBlock(env)
        k32.CloseHandle(token)


def default_launcher(folder: Path) -> Launcher:
    """The launcher for this PC's system."""
    return WindowsLauncher(folder) if sys.platform == "win32" else Launcher()
