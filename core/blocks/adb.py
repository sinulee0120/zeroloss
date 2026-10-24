"""Android tablets on this PC's USB, through adb (01-overview D3, 02 4 ``/v1/tablets``).

One job: run the few adb commands a receiving PC needs, each with a time limit, on one
tablet chosen by its serial. Up to three tablets take part in a test, on the same channel
or on different ones (01-overview 4): every command names its serial, and each tablet
gets its own forward (the PC's port is picked by adb, ``tcp:0``), so two tablets never
share one. Nothing typed by a person is ever run: the commands are fixed below and a
serial is checked before it is used.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

TIMEOUT_S = 20.0
INSTALL_TIMEOUT_S = 180.0  # an APK of ~11 MB over USB 2, with the package check
SERIAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,63}$")  # never an option ("-s")
# adb devices -l: "<serial>  <state> usb:1-1 product:x model:y device:z transport_id:3"
_DEVICE_RE = re.compile(r"^(\S+)\s+(device|unauthorized|offline|no permissions|recovery"
                        r"|sideload|bootloader|authorizing|connecting)\b(.*)$")  # fmt: skip

Runner = Callable[[Sequence[str], float], tuple[int, str]]


def run_command(argv: Sequence[str], timeout: float) -> tuple[int, str]:
    """The real runner: exit code and output; a missing program or a hang is code 127/124."""
    try:
        done = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout, check=False,
            stdin=subprocess.DEVNULL, errors="replace",
        )  # fmt: skip
    except FileNotFoundError as e:
        return 127, str(e)
    except subprocess.TimeoutExpired:
        return 124, f"no answer in {timeout:.0f} s"
    except OSError as e:
        return 126, str(e)
    return done.returncode, (done.stdout + done.stderr).strip()


@dataclass(frozen=True)
class Tablet:
    serial: str
    state: str  # device | unauthorized | no permissions | offline | …
    model: str = ""
    usb: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"serial": self.serial, "state": self.state, "model": self.model, "usb": self.usb}


class AdbError(Exception):
    """An adb command failed: ``why`` is adb's own words (or the time limit)."""

    def __init__(self, what: str, why: str) -> None:
        super().__init__(f"{what}: {why}")
        self.what = what
        self.why = why


def check_serial(serial: str) -> str:
    if not SERIAL_RE.match(serial):
        raise ValueError(f"not a tablet serial: {serial!r}")
    return serial


def parse_devices(out: str) -> list[Tablet]:
    tablets = []
    for line in out.splitlines():
        m = _DEVICE_RE.match(line.strip())
        if not m or line.startswith(("List of devices", "*")):
            continue
        rest = dict(kv.split(":", 1) for kv in m.group(3).split() if ":" in kv)
        tablets.append(Tablet(m.group(1), m.group(2), rest.get("model", "").replace("_", " "),
                              rest.get("usb", "")))  # fmt: skip
    return tablets


class Adb:
    """adb at ``path`` (None: not installed, every call says so)."""

    def __init__(self, path: Callable[[], str | None], runner: Runner = run_command) -> None:
        self._path = path
        self._run = runner

    def _adb(self, args: Sequence[str], what: str, timeout: float = TIMEOUT_S) -> str:
        adb = self._path()
        if adb is None:
            raise AdbError(what, "adb is not installed on this PC")
        rc, out = self._run([adb, *args], timeout)
        if rc != 0:
            raise AdbError(what, out or f"exit code {rc}")
        return out

    def devices(self) -> list[Tablet]:
        return parse_devices(self._adb(["devices", "-l"], "list tablets"))

    def shell(self, serial: str, args: Sequence[str], timeout: float = TIMEOUT_S) -> str:
        return self._adb(["-s", check_serial(serial), "shell", *args], "tablet command", timeout)

    def forward(self, serial: str, tablet_port: int) -> int:
        """A port on this PC (chosen by adb) that reaches ``tablet_port`` on the tablet."""
        out = self._adb(["-s", check_serial(serial), "forward", "tcp:0", f"tcp:{tablet_port}"],
                        "forward to the tablet")  # fmt: skip
        m = re.search(r"^(\d+)\s*$", out, re.M)
        if not m:
            raise AdbError("forward to the tablet", f"no port in adb's answer: {out!r}")
        return int(m.group(1))

    def unforward(self, serial: str, local_port: int) -> None:
        self._adb(["-s", check_serial(serial), "forward", "--remove", f"tcp:{local_port}"],
                  "remove the forward")  # fmt: skip

    def forwards(self) -> list[tuple[str, int, str]]:
        """(serial, PC port, tablet side) of every forward adb holds now."""
        rows = []
        for line in self._adb(["forward", "--list"], "list forwards").splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[1].startswith("tcp:") and parts[1][4:].isdigit():
                rows.append((parts[0], int(parts[1][4:]), parts[2]))
        return rows

    def install(self, serial: str, apk: str) -> str:
        """-r keeps the app's data, -g grants its runtime permissions at once."""
        return self._adb(["-s", check_serial(serial), "install", "-r", "-g", apk],
                         "install the app", INSTALL_TIMEOUT_S)  # fmt: skip
