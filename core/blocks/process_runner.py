"""Run one external program: start, watch, stop with confirmation, bind to us.

docs/03-features.md "child process cleanup", docs/12-architecture.md 4-3, SYS-006, SYS-007.

- State flow IDLE -> STARTING -> RUNNING -> STOPPING -> IDLE, FAULT on unexpected exit or
  a stop that could not be confirmed. Commands outside their state are refused.
- Start: launch -> optional readiness check within a time limit. A failed start kills
  what was launched (rollback) and returns to IDLE.
- Stop: polite signal -> wait -> force kill of the whole group -> confirm nothing is left.
- Bound to this process: Linux uses a new session (group) plus a parent-death signal;
  Windows uses a Job Object that kills every member when our handle closes.
  Linux delivers the parent-death signal when the *thread* that started the child ends,
  so every child is started by one long-lived thread here (``_spawn``), whoever asks:
  2026-10-09 the sender's tsp was started inside an HTTP request thread and got SIGKILL
  about 3 min later when that thread ended (the "tsp exited (-9)" of every long run).
- A run record (name, PID, run token) is kept while running, so a restarted node can
  stop leftovers from a crashed run (``stop_leftovers``). The token is put in the child's
  environment and inherited by its children; only processes carrying it are stopped,
  so a reused PID is never killed. Restarts after an unexpected
  exit are the module's policy (SND-005, RCV-003), not this block's.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import IO, Any

_LINUX = sys.platform.startswith("linux")
_WINDOWS = sys.platform == "win32"
FORCE_CONFIRM_S = 5.0
TOKEN_ENV = "ZEROLOSS_RUN_TOKEN"
# 2026-10-24 user ("cmd창이 계속 떴다 사라져"): a windowed program (the Controller) that
# starts a console tool gets a console window flashing on screen for each one. Every
# subprocess call that can run on Windows passes this (0 elsewhere); it hides only a
# console, never a program's own window (mpv).
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class ProcState(StrEnum):
    IDLE = "IDLE"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    FAULT = "FAULT"


class StartError(Exception):
    """Launch failed or the readiness check did not pass in time (already rolled back)."""


class StopError(Exception):
    """The program is still alive after the forced kill (SYS-006)."""


@dataclass(frozen=True)
class StopResult:
    forced: bool
    exit_code: int | None


class ProcessRunner:
    def __init__(
        self,
        name: str,
        argv: Sequence[str],
        record_dir: Path,
        log_path: Path | None = None,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        stop_timeout_s: float = 15.0,
    ) -> None:
        self.name = name
        self.argv = list(argv)
        self.stop_timeout_s = stop_timeout_s
        self.state = ProcState.IDLE
        self.exit_code: int | None = None
        self._record = record_dir / f"{name}.json"
        self._log_path = log_path
        self._cwd = cwd
        self._env = env
        self._proc: subprocess.Popen[bytes] | None = None
        self._log: IO[bytes] | None = None
        self._job: Any = None
        self._token = ""
        self._lock = threading.Lock()
        record_dir.mkdir(parents=True, exist_ok=True)

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def start(self, ready: Callable[[], bool] | None = None, ready_timeout_s: float = 10.0) -> None:
        with self._lock:
            if self.state is not ProcState.IDLE:
                raise RuntimeError(f"{self.name}: cannot start while {self.state}")
            self.state = ProcState.STARTING
        try:
            self._launch()
            deadline = time.monotonic() + ready_timeout_s
            while ready is not None and not ready():
                if self._proc is not None and self._proc.poll() is not None:
                    raise StartError(f"{self.name} exited with {self._proc.returncode} on start")
                if time.monotonic() > deadline:
                    raise StartError(f"{self.name} not ready in {ready_timeout_s:g} s")
                time.sleep(0.05)
        except BaseException as e:
            self._kill_group()
            gone = self._proc is None or self._confirm_gone()
            if gone:
                self._finish()
            with self._lock:
                self.state = ProcState.IDLE if gone else ProcState.FAULT
            if isinstance(e, OSError):
                raise StartError(f"{self.name}: {e}") from e
            raise
        with self._lock:
            self.state = ProcState.RUNNING

    def poll(self) -> ProcState:
        """Call once per second. A RUNNING program that exited on its own becomes FAULT."""
        with self._lock:
            if self.state is ProcState.RUNNING and self._proc and self._proc.poll() is not None:
                self.exit_code = self._proc.returncode
                self.state = ProcState.FAULT
            return self.state

    def stop(self) -> StopResult:
        """Stop and confirm. Also cleans up after FAULT. Raises StopError (state FAULT)."""
        with self._lock:
            if self.state not in (ProcState.RUNNING, ProcState.FAULT):
                raise RuntimeError(f"{self.name}: cannot stop while {self.state}")
            self.state = ProcState.STOPPING
        assert self._proc is not None
        forced = False
        if self._proc.poll() is None:
            self._polite_stop()
            try:
                self._proc.wait(self.stop_timeout_s)
            except subprocess.TimeoutExpired:
                forced = True
        # Children of the program may still be running even when it exited.
        self._kill_group()
        if not self._confirm_gone():
            with self._lock:
                self.state = ProcState.FAULT
            raise StopError(f"{self.name} (PID {self._proc.pid}) did not stop")
        self.exit_code = self._proc.returncode
        self._finish()
        with self._lock:
            self.state = ProcState.IDLE
        return StopResult(forced, self.exit_code)

    # ---------------------------------------------------------------- helpers

    def _launch(self) -> None:
        self._proc = None
        self.exit_code = None
        self._token = uuid.uuid4().hex
        env = dict(self._env if self._env is not None else os.environ)
        env[TOKEN_ENV] = self._token
        self._log = self._log_path.open("ab") if self._log_path else None
        out = self._log if self._log else subprocess.DEVNULL
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": out,
            "stderr": subprocess.STDOUT if self._log else subprocess.DEVNULL,
            "cwd": self._cwd,
            "env": env,
        }
        if sys.platform == "win32":
            # with a console of our own the child shares it (CTRL_BREAK reaches it); without
            # one it would open a visible console, so it gets a hidden one
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | (
                0 if _has_console() else NO_WINDOW
            )
        else:
            kwargs["start_new_session"] = True
            if _LINUX:
                kwargs["preexec_fn"] = _die_with_parent
        self._proc = _spawn(self.argv, kwargs)
        if sys.platform == "win32":
            self._job = _WinJob()
            self._job.assign(self._proc)
        self._write_record()

    def _polite_stop(self) -> None:
        assert self._proc is not None
        with contextlib.suppress(OSError):
            if sys.platform == "win32":
                self._proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(self._proc.pid, signal.SIGTERM)

    def _kill_group(self) -> None:
        if self._proc is None:
            return
        with contextlib.suppress(OSError):
            if _WINDOWS:
                if self._job is not None:
                    self._job.terminate()
                else:
                    self._proc.kill()
            else:
                # Only processes carrying our token: the group ID may be reused after
                # the leader was reaped.
                for pid in _members(self._proc.pid, self._token):
                    with contextlib.suppress(OSError):
                        os.kill(pid, signal.SIGKILL)

    def _confirm_gone(self) -> bool:
        assert self._proc is not None
        try:
            self._proc.wait(FORCE_CONFIRM_S)
        except subprocess.TimeoutExpired:
            return False
        if _WINDOWS:
            return self._job is None or self._job.empty()
        deadline = time.monotonic() + FORCE_CONFIRM_S
        while _members(self._proc.pid, self._token):
            if time.monotonic() > deadline:
                return False
            time.sleep(0.02)
        return True

    def _finish(self) -> None:
        if self._log:
            self._log.close()
            self._log = None
        if self._job is not None:
            self._job.close()
            self._job = None
        self._record.unlink(missing_ok=True)

    def _write_record(self) -> None:
        assert self._proc is not None
        rec = {
            "name": self.name,
            "pid": self._proc.pid,
            "token": self._token,
            "argv": self.argv,
        }
        tmp = self._record.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._record)


def stop_leftovers(record_dir: Path, keep: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    """On node start: stop programs a crashed run left behind (SYS-007). Returns what was
    stopped. Only processes carrying the run token are touched. ``keep``: record names of
    helpers the running node owns (Reset All must not stop its own ``iw event``)."""
    stopped = []
    for path in sorted(record_dir.glob("*.json")):
        if path.stem in keep:
            continue
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            continue
        pid = int(rec["pid"])
        found = _members(pid, str(rec.get("token", ""))) if _LINUX else []
        for member in found:
            with contextlib.suppress(OSError):
                os.kill(member, signal.SIGKILL)
        if found:
            stopped.append(rec)
        # Windows: the Job Object already killed everything when the node died.
        path.unlink(missing_ok=True)
    return stopped


# -------------------------------------------------------------------- the spawner

SPAWNER_NAME = "zl-spawner"
_spawn_lock = threading.Lock()
_spawn_jobs: queue.SimpleQueue[tuple[list[str], dict[str, Any], list[Any], threading.Event]] = (
    queue.SimpleQueue()
)
_spawner: threading.Thread | None = None


def _has_console() -> bool:
    import ctypes

    return bool(ctypes.windll.kernel32.GetConsoleWindow())  # type: ignore[attr-defined]


def _spawn(argv: list[str], kwargs: dict[str, Any]) -> subprocess.Popen[bytes]:
    """``Popen`` on the one spawner thread, which lives as long as this process, so the
    parent-death signal comes only when the process ends (see the module note)."""
    global _spawner
    if threading.current_thread().name == SPAWNER_NAME:
        return subprocess.Popen(argv, **kwargs)
    with _spawn_lock:
        if _spawner is None or not _spawner.is_alive():
            _spawner = threading.Thread(target=_spawn_loop, name=SPAWNER_NAME, daemon=True)
            _spawner.start()
    out: list[Any] = []
    done = threading.Event()
    _spawn_jobs.put((argv, kwargs, out, done))
    done.wait()
    if isinstance(out[0], BaseException):
        raise out[0]
    proc: subprocess.Popen[bytes] = out[0]
    return proc


def _spawn_loop() -> None:
    while True:
        argv, kwargs, out, done = _spawn_jobs.get()
        try:
            out.append(subprocess.Popen(argv, **kwargs))
        except BaseException as e:  # handed back to the caller
            out.append(e)
        finally:
            done.set()


# -------------------------------------------------------------------- Linux helpers


def _die_with_parent() -> None:  # pragma: no cover - runs in the child
    import ctypes

    pr_set_pdeathsig = 1
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(pr_set_pdeathsig, signal.SIGKILL)


def _stat(pid: int) -> list[str] | None:
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as f:
            raw = f.read()
    except OSError:
        return None
    # Field 2 (comm) may contain spaces; the rest follows the last ")".
    return raw[raw.rfind(")") + 2 :].split()


def _has_token(pid: int, token: str) -> bool:
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            return f"{TOKEN_ENV}={token}".encode() in f.read().split(b"\0")
    except OSError:
        return False


def _members(pgid: int, token: str) -> list[int]:
    """Live (non-zombie) processes in group ``pgid`` that carry our run token."""
    if not token:
        return []
    members = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        st = _stat(pid)
        if not st or st[0] == "Z" or int(st[2]) != pgid:  # state, pgrp
            continue
        if _has_token(pid, token):
            members.append(pid)
    return members


# ------------------------------------------------------------------ Windows helpers


if sys.platform == "win32":  # pragma: no cover - checked on the Windows PC

    class _WinJob:
        """Job Object with KILL_ON_JOB_CLOSE: closing our handle (or our death) kills all."""

        def __init__(self) -> None:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._k32 = k32

            class _IoCounters(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

            class _Basic(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class _Extended(ctypes.Structure):
                _fields_ = [
                    ("Basic", _Basic),
                    ("Io", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            k32.CreateJobObjectW.restype = wintypes.HANDLE
            self._handle = k32.CreateJobObjectW(None, None)
            if not self._handle:
                raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
            info = _Extended()
            info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            extended_limit_information = 9
            if not k32.SetInformationJobObject(
                self._handle, extended_limit_information, ctypes.byref(info), ctypes.sizeof(info)
            ):
                raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

        def assign(self, proc: subprocess.Popen[bytes]) -> None:
            import ctypes

            handle = int(proc._handle)  # type: ignore[attr-defined]
            if not self._k32.AssignProcessToJobObject(self._handle, handle):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")

        def terminate(self) -> None:
            self._k32.TerminateJobObject(self._handle, 1)

        def empty(self) -> bool:
            import ctypes
            from ctypes import wintypes

            class _Accounting(ctypes.Structure):
                _fields_ = [
                    ("TotalUserTime", ctypes.c_int64),
                    ("TotalKernelTime", ctypes.c_int64),
                    ("ThisPeriodTotalUserTime", ctypes.c_int64),
                    ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                    ("TotalPageFaultCount", wintypes.DWORD),
                    ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD),
                    ("TotalTerminatedProcesses", wintypes.DWORD),
                ]

            acc = _Accounting()
            basic_accounting_information = 1
            ok = self._k32.QueryInformationJobObject(
                self._handle,
                basic_accounting_information,
                ctypes.byref(acc),
                ctypes.sizeof(acc),
                None,
            )
            return bool(ok) and acc.ActiveProcesses == 0

        def close(self) -> None:
            if self._handle:
                self._k32.CloseHandle(self._handle)
                self._handle = None
