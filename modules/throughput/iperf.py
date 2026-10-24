"""iperf3 command lines and results (docs/modules/throughput.md 0, 4).

The LAN-side PC is always the client and opens the connection (NAT as it is, no port
forwarding); "receive" is iperf3's reverse mode (``-R``). The client writes one JSON
document (``-J``) when the test ends. ``parse`` reads the receiving side's rate: for TCP
``end.sum_received``, for UDP ``end.sum`` with loss and jitter. A result that carries
iperf3's own ``error`` or no rate is a failure with that reason, never a 0 Mbps pass.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

PORT = 5211  # the server port (one test at a time: the PCs go one after another)
CONNECT_TIMEOUT_MS = 5000


def server_argv(binary: str, port: int = PORT, bind: str | None = None) -> list[str]:
    argv = [binary, "-s", "-p", str(port)]
    if bind:
        argv += ["-B", bind]
    return argv


def client_argv(
    binary: str,
    server: str,
    seconds: int,
    receive: bool,
    port: int = PORT,
    protocol: str = "tcp",
    streams: int = 1,
    udp_mbps: float | None = None,
    bind: str | None = None,
) -> list[str]:
    """``receive``: the server sends to this PC (``-R``). UDP needs a target rate."""
    if protocol not in ("tcp", "udp"):
        raise ValueError("protocol must be tcp or udp")
    argv = [
        binary,
        "-c",
        server,
        "-p",
        str(port),
        "-t",
        str(int(seconds)),
        "-J",
        "-P",
        str(max(1, int(streams))),
        "--connect-timeout",
        str(CONNECT_TIMEOUT_MS),
    ]
    if bind:
        argv += ["-B", bind]
    if receive:
        argv.append("-R")
    if protocol == "udp":
        if not udp_mbps or udp_mbps <= 0:
            raise ValueError("UDP needs a positive target rate")
        argv += ["-u", "-b", f"{udp_mbps:g}M"]
    return argv


@dataclass(frozen=True)
class IperfResult:
    ok: bool
    mbps: float | None = None  # what the receiving side got
    sender_mbps: float | None = None
    retransmits: int | None = None  # TCP
    lost_pct: float | None = None  # UDP
    jitter_ms: float | None = None  # UDP
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _num(d: Any, key: str) -> float | None:
    v = d.get(key) if isinstance(d, dict) else None
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def parse(text: str, protocol: str = "tcp") -> IperfResult:
    """The client's ``-J`` output. Anything unreadable is a failure with a reason."""
    try:
        doc = json.loads(text) if text.strip() else None
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        line = next((x for x in text.strip().splitlines()[-1:]), "")[:200]
        return IperfResult(False, error=f"iperf3 gave no result ({line or 'no output'})")
    if doc.get("error"):
        return IperfResult(False, error=f"iperf3: {doc['error']}")
    found = doc.get("end")
    end: dict[str, Any] = found if isinstance(found, dict) else {}
    if protocol == "udp":
        total = end.get("sum") if isinstance(end.get("sum"), dict) else {}
        bps = _num(total, "bits_per_second")
        if not bps:
            return IperfResult(False, error="iperf3 UDP result has no rate")
        return IperfResult(True, mbps=round(bps / 1e6, 3), sender_mbps=round(bps / 1e6, 3),
                           lost_pct=_num(total, "lost_percent"),
                           jitter_ms=_num(total, "jitter_ms"))  # fmt: skip
    got = end.get("sum_received") if isinstance(end.get("sum_received"), dict) else {}
    sent = end.get("sum_sent") if isinstance(end.get("sum_sent"), dict) else {}
    bps, sbps = _num(got, "bits_per_second"), _num(sent, "bits_per_second")
    if not bps:
        return IperfResult(False, error="iperf3 TCP result has no received rate")
    rtx = _num(sent, "retransmits")
    return IperfResult(True, mbps=round(bps / 1e6, 3),
                       sender_mbps=round(sbps / 1e6, 3) if sbps else None,
                       retransmits=int(rtx) if rtx is not None else None)  # fmt: skip
