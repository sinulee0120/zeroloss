"""Test-wide settings.

- Players run without a window or sound (iptv 3.2, tests only).
- Nodes never start the ISP role by themselves (tests use their own fake NICs).
- A test Controller never opens the real results web port 10993 (2026-10-09: a test
  run held it while the real Controller started, so its live page stayed closed).
- A node that does not answer is never searched from this PC's real addresses
  (source_pick, GAP-153): a test passes its own address list.
- At the end of the whole session no program started by ZeroLoss may still run
  (12 4-3, 20 GAP-48): any live process carrying the run token fails the session.
  Only **this test session's** programs count (they inherit ``SESSION_ENV``): a real
  ZeroLoss service running on the same PC (the lab dev PC is also a receiver node,
  2026-09-26) is not the tests' leftover (G-18: the checker picks its target exactly).
"""

import os
import sys
import uuid
from collections.abc import Iterator

import pytest

os.environ.setdefault("ZEROLOSS_PLAYER_HEADLESS", "1")
os.environ["ZEROLOSS_WAN_AUTO"] = "0"  # no test node touches this PC's real NICs (07 3.2 B)
os.environ["ZEROLOSS_NO_ROUTE_CHANGES"] = "1"  # tests never change this PC's routes
os.environ["ZEROLOSS_IDLE_WATCH"] = "0"  # no test Controller asks devices by itself (GAP-153)
SESSION_ENV = "ZEROLOSS_TEST_SESSION"
os.environ[SESSION_ENV] = uuid.uuid4().hex  # inherited by every program the tests start


@pytest.fixture(scope="session", autouse=True)
def no_real_web_port() -> Iterator[None]:
    from controller.service import ControllerService

    init = ControllerService.__init__
    before = init.__defaults__
    assert before is not None and before[-1] == 10993
    init.__defaults__ = (*before[:-1], 0)  # any free port
    yield
    init.__defaults__ = before


@pytest.fixture(autouse=True)
def no_real_source_search(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from core.protocol import source_pick

    source_pick.reset()
    monkeypatch.setattr(source_pick, "local_addresses", lambda: [])
    yield
    source_pick.reset()


def _ours_alive() -> list[str]:
    if not sys.platform.startswith("linux"):
        return []  # Windows relies on the Job Object (V18)
    from core.blocks.process_runner import TOKEN_ENV

    found = []
    for d in os.listdir("/proc"):
        if not d.isdigit() or int(d) == os.getpid():
            continue
        try:
            with open(f"/proc/{d}/environ", "rb") as f:
                env = f.read()
            mine = f"{SESSION_ENV}={os.environ[SESSION_ENV]}".encode()
            if TOKEN_ENV.encode() + b"=" not in env or mine not in env.split(b"\0"):
                continue
            with open(f"/proc/{d}/stat") as f:
                if f.read().split(")")[-1].split()[0] == "Z":
                    continue
            with open(f"/proc/{d}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode(errors="replace")
            found.append(f"{d}: {cmd[:120]}")
        except OSError:
            continue
    return found


@pytest.fixture(scope="session", autouse=True)
def no_program_outlives_the_tests() -> Iterator[None]:
    yield
    left = _ours_alive()
    assert not left, "programs started by ZeroLoss are still running:\n" + "\n".join(left)
