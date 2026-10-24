"""The start checklist (docs/03-features.md F17, 02 2-1 ``checklist``).

One object per Start, ``zeroloss precheck`` or light idle check: numbered steps with
a state each (waiting -> running -> ok / warn / failed, or skipped). A failed step
stops the list; the steps after it are skipped. The texts come from the error (09).
The checks themselves live in the RunController; the pure helpers are here.
"""

from __future__ import annotations

import contextlib
import ipaddress
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.blocks.wan_isp import SERVER
from core.events.bus import utc_now
from core.protocol.envelope import ZlError

STEPS = (
    "Devices connected",
    "Tools ready",
    "Network check",
    "Disk, time & PC speed",
    "Multicast test",
    "Start sending",
    "Start receivers",
)
IDLE_STEPS = 4  # light check: no traffic (03 F17, decided 2026-09-25)
PRECHECK_STEPS = 5  # zeroloss precheck: with the 1-2 s test send
DISK_WARN_MB = 1024  # RCV-008 warning line: a test must end above it
CLOCK_WARN_S = 2.0  # DSC-009
KINDS = ("start", "idle", "precheck")

ISP_PREFIX = int(SERVER.split("/")[1])  # the sender's ISP network (07 3 table B)


@dataclass
class Step:
    n: int
    name: str
    state: str = "waiting"
    detail: str = ""
    device: str | None = None
    code: str | None = None
    what: str | None = None
    why: str | None = None
    fix: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Checklist:
    kind: str
    upto: int
    run_id: str | None = None
    steps: list[Step] = field(default_factory=list)
    updated_utc: str = field(default_factory=utc_now)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(self.kind)
        self.steps = [
            Step(n, name, "waiting" if n <= self.upto else "skipped")
            for n, name in enumerate(STEPS, 1)
        ]

    @contextlib.contextmanager
    def step(self, n: int) -> Iterator[Step]:
        """Run one step; a ZlError marks it failed (with its device for a
        ``DeviceError``), skips the rest and is raised again."""
        s = self.steps[n - 1]
        self._set(s, state="running")
        try:
            yield s
        except ZlError as e:
            self._fail(s, e)
            raise
        except BaseException:
            self._set(s, state="failed", what="Check stopped", why="An internal error.")
            self._skip_after(n)
            raise
        if s.state == "running":
            self._set(s, state="ok")

    def warn(self, s: Step, err: ZlError, device: str) -> None:
        """A warning does not stop the list (e.g. clock difference, DSC-009)."""
        self._set(s, state="warn", device=device, code=err.code, what=err.what, why=err.why,
                  fix=err.fix)  # fmt: skip

    @property
    def ok(self) -> bool:
        return not any(s.state == "failed" for s in self.steps)

    def first_problem(self) -> Step | None:
        return next((s for s in self.steps if s.state == "failed"), None)

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "kind": self.kind,
                "run_id": self.run_id,
                "updated_utc": self.updated_utc,
                "ok": self.ok,
                "steps": [s.to_dict() for s in self.steps],
            }

    def _fail(self, s: Step, e: ZlError) -> None:
        device = e.device if isinstance(e, DeviceError) else None
        self._set(s, state="failed", device=device, code=e.code, what=e.what, why=e.why,
                  fix=e.fix)  # fmt: skip
        self._skip_after(s.n)

    def _skip_after(self, n: int) -> None:
        for later in self.steps[n:]:
            if later.state == "waiting":
                self._set(later, state="skipped")

    def _set(self, s: Step, **values: Any) -> None:
        with self._lock:
            for k, v in values.items():
                setattr(s, k, v)
            self.updated_utc = utc_now()


class DeviceError(ZlError):
    """A ZlError about one device (the checklist row names it)."""

    def __init__(self, device: str, err: ZlError) -> None:
        super().__init__(err.code, err.what, err.why, err.fix, err.detail)
        self.device = device


def own_address(host: str, port: int) -> str | None:
    """The Controller's address toward ``host`` (the route's source; nothing is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, port))
            return str(s.getsockname()[0])
    except OSError:
        return None


def is_wireless(card: dict[str, Any]) -> bool:
    """A Wi-Fi card: heartbeat ``links`` mark it with a ``wifi`` entry (02 5, null when the
    values are not read yet), node info ``my_ips`` with ``wireless``. 2026-10-09 GAP-122:
    the checks here read only ``wireless``, so a home Wi-Fi card looked wired and became
    the receiver's test card (the probe joined on the home Wi-Fi and got nothing)."""
    return bool(card.get("wireless")) or "wifi" in card


def test_card(
    role: str,
    reached_ip: str | None,
    links: list[dict[str, Any]],
    isp_ip: str | None,
    baseline: bool = False,
) -> tuple[str | None, str]:
    """03 F17 (GAP-99 2): the card a device sends or receives on when the plan names none
    -> (address, ""), or (None, why) when it cannot be told (09 NET-022). The router's
    LAN, SSID and addresses change from test to test, so nothing is fixed here.
    ``baseline`` (S-54, 05 4.6.3): no router, so a receiver's card is the one on the
    sender's ISP network — the cable from the sender gave it the lease (GAP-115)."""
    if role == "sender" and isp_ip:
        return isp_ip, ""  # it gives the router WAN its address: that card is the WAN one
    if role == "receiver" and baseline and isp_ip:
        net = ipaddress.ip_network(f"{isp_ip}/{ISP_PREFIX}", strict=False)
        for x in links:
            with contextlib.suppress(ValueError):
                if x.get("ip") and ipaddress.ip_address(str(x["ip"])) in net:
                    return str(x["ip"]), ""
        return None, (f"no card on the sender's network {net}: is the cable from the sender "
                      "plugged in, and did that card get its address?")  # fmt: skip
    if not reached_ip:
        return None, "the Controller does not know which card it reached"
    if reached_ip.startswith("127."):
        return reached_ip, ""  # the Controller's own PC (loopback test)
    card = next((x for x in links if x.get("ip") == reached_ip), None)
    if card is None:
        return None, f"no network card with {reached_ip}"
    if is_wireless(card):
        return None, (f"reached over Wi-Fi {card.get('nic')} ({reached_ip}), which may not be "
                      "the router under test")  # fmt: skip
    return reached_ip, ""


def other_network(
    device: str,
    links: list[dict[str, Any]],
    reached_ip: str | None,
    own_ip: str | None,
    test_ip: str | None = None,
    isp_ready: bool = False,
    via_nat: bool = False,
) -> ZlError | None:
    """09 NET-018 (20 W-3, 13 1, 07 6): is this test PC also reachable around the test
    network? ``links`` = the node's cards (02 5), ``reached_ip`` = the node's address the
    Controller came in on, ``own_ip`` = the Controller's address toward it, ``test_ip`` =
    the card the plan uses there (a Wi-Fi receiver's test Wi-Fi). None when fine. The
    Controller PC itself (a receiver on it too) is never flagged: its internet card is
    there by design."""
    if not reached_ip or reached_ip.startswith("127.") or reached_ip == own_ip:
        return None
    why = ("Control must go through the router under test (07 6), and a test PC must not "
           "be on another network (13 1).")  # fmt: skip
    # 2026-10-15 (GAP-155): never "turn it off / forget it" — the sender PC's Wi-Fi was
    # turned off by that advice and the PC lost its internet with no way back
    fix = (f"Nothing to do: keep that Wi-Fi on {device} (it may be its internet). The test "
           "video still goes through the router on the test card; pick the PC's test "
           "address in its row (IP) to control it through the router too.")  # fmt: skip
    came = next((x for x in links if x.get("ip") == reached_ip), None)
    if came is not None and is_wireless(came) and reached_ip != test_ip:
        return ZlError("NET-018", f"{device} is reached over Wi-Fi {came.get('nic')}, "
                       "not through the router", why, fix)  # fmt: skip
    if isp_ready and not via_nat:
        return ZlError(
            "NET-018",
            f"{device} is reached without the router's NAT on the way",
            f"It gives the router WAN its address, so the Controller should reach it through "
            f"the router. {why}",
            f"Connect {device} only to the router WAN port, then check again.",
        )
    # 2026-10-16 (GAP-161, user: "3 problems"): a PC controlled and tested through the
    # router that also has its own internet Wi-Fi is fine — nothing to do, so no warning
    return None


def controller_route(
    test_nic: str | None, default_nic: str | None, others: list[str]
) -> ZlError | None:
    """09 NET-019 (13 1, 20 GAP-73 5): the Controller's test network card carries its
    default route while another card with an address exists (the internet one) — the
    internet then goes into the test network. One card only: that is fine."""
    if not test_nic or test_nic != default_nic or not others:
        return None
    return ZlError(
        "NET-019",
        f"The test network card {test_nic} carries this PC's default route",
        f"Internet traffic of this PC goes into the test network instead of "
        f"{', '.join(others)} (13 1).",
        "Press Fix in Action Required: the card stops taking the default route and DNS.",
    )


def clock_offset_s(ts_utc: Any, before: datetime, after: datetime) -> float | None:
    """Node clock minus Controller clock, from one heartbeat (D39: middle of the trip)."""
    if not isinstance(ts_utc, str):
        return None
    try:
        node = datetime.fromisoformat(ts_utc.replace("Z", "+00:00"))
    except ValueError:
        return None
    middle = before + (after - before) / 2
    return (node - middle).total_seconds()


def most_hours(free_mb: int, need_mb: float, duration_s: int) -> float:
    """The longest test the free space allows above the RCV-008 warning line."""
    per_hour = need_mb / max(duration_s / 3600, 1e-9)
    return max(0.0, (free_mb - DISK_WARN_MB) / per_hour) if per_hour > 0 else float("inf")
