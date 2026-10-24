"""What the Agent Desktop status window shows (docs/03-features.md F20, 11-ui 6).

One pure function from the node's ``/v1/desktop/status`` (02 2-12) and the modules'
``status_rows`` to the window's text and color. The window draws only this (11-ui
1.1: the screen draws the store, nothing else). Colors: green fine, yellow watch,
red problem, grey idle (F20 원칙). They tell "running, and fine?" on site; the
Controller alone judges.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

ACTIVE = ("STARTING", "RUNNING", "STOPPING")
ORDER = ("grey", "green", "yellow", "red")  # the window's color is the worst row's


@dataclass(frozen=True)
class Row:
    title: str
    color: str
    big: tuple[tuple[str, str], ...] = ()
    lines: tuple[str, ...] = ()
    detail: tuple[str, str] | None = None  # (key, line) of this second for [Details]


@dataclass(frozen=True)
class StatusView:
    title: str
    color: str
    rows: tuple[Row, ...]
    footer: tuple[str, ...]
    notice: str | None = None  # one line across the top when something is wrong
    problems: tuple[str, ...] = field(default=())


def status_view(status: dict[str, Any] | None,
                rows_of: dict[str, Callable[[dict[str, Any]], list[dict[str, Any]]]],
                refused: str | None = None, entry_errors: dict[str, str] | None = None,
                ) -> StatusView:  # fmt: skip
    title = "ZeroLoss Agent"
    if refused:
        return StatusView(title, "red", (), (), refused)
    if status is None:
        return StatusView(title, "red", (), ("Controller: —",),
                          "Node not answering on this PC")  # fmt: skip
    node = status.get("node") or {}
    active = node.get("state") in ACTIVE
    rows: list[Row] = []
    roles: set[str] = set()
    for mid, data in sorted((status.get("module_data") or {}).items()):
        fn = rows_of.get(mid)
        if fn is None:
            continue
        try:
            got = fn(dict(data))
        except Exception as e:  # a module's bad row must not blank the window
            rows.append(Row(f"{mid}: cannot show", "yellow", (), (repr(e)[:80],)))
            continue
        for r in got:
            if r.get("role"):
                roles.add(str(r["role"]))
            d = r.get("detail")
            rows.append(Row(
                str(r.get("title", mid)), str(r.get("color", "grey")),
                tuple((str(a), str(b)) for a, b in r.get("big") or []),
                tuple(str(x) for x in r.get("lines") or []),
                (f"{mid}/{d['key']}", str(d["line"])) if isinstance(d, dict) else None,
            ))  # fmt: skip
    for mid, why in sorted((entry_errors or {}).items()):
        rows.append(Row(f"{mid}: not loaded", "yellow", (), (why[:80],)))
    ctl = status.get("controller") or {}
    connected = bool(ctl.get("connected"))
    notice = None
    if active and not connected:
        notice = "Controller connection lost"
    elif node.get("state") == "FAULT":
        notice = "Node fault — see the Controller"
    if not rows:
        rows.append(Row("Idle" if not active else str(node.get("state", "")).title(),
                        "grey" if not active else "yellow"))  # fmt: skip
    color = max((r.color for r in rows), key=lambda c: ORDER.index(c) if c in ORDER else 0)
    if notice:
        color = "red"
    wan = status.get("wan") or {}
    wan_nic = wan.get("nic") if wan.get("state") == "ready" else None
    ips = [f"{x['ip']} ({x['nic']}{', Wi-Fi' if x.get('wireless') else ''}"
           f"{', router WAN' if x['nic'] == wan_nic else ''})"
           for x in status.get("my_ips") or [] if x.get("ip")]  # fmt: skip
    footer = (
        "Controller: Connected" if connected else "Controller: Not connected",
        *(f"My IP: {x}" for x in ips or ["—"]),
        *isp_line(wan),
    )
    if roles:  # F20: "ZeroLoss Sender" / "ZeroLoss Receiver", from what runs here
        title = "ZeroLoss " + " + ".join(x.title() for x in sorted(roles, reverse=True))
    problems = tuple(f"{p.get('code', '')} {p.get('text', '')}".strip()
                     for p in (status.get("problems") or [])[-3:])  # fmt: skip
    return StatusView(title, color, tuple(rows), footer, notice, problems)


def isp_line(wan: dict[str, Any]) -> tuple[str, ...]:
    """20 W-4: is this PC giving the router WAN its address (07 3.2 B)? Nothing on a PC
    where the automatic role found no router WAN port (most PCs)."""
    state, nic, why = wan.get("state"), wan.get("nic"), wan.get("why")
    if state == "ready":
        return (f"ISP role: ready on {nic} (router WAN gets its address here)",)
    if state in ("waiting_nic", "conflict", "error"):
        return (f"ISP role: {state.replace('_nic', '')} — {why or '?'}",)
    return ()


class Details:
    """F20 [Details]: the last 1,000 one-second lines (F12), each second once."""

    KEEP = 1000

    def __init__(self) -> None:
        self.lines: list[str] = []
        self._seen: set[str] = set()
        self._order: list[str] = []

    def add(self, view: StatusView) -> bool:
        """True when a new line came in (the window redraws only then, 11-ui 1.13)."""
        new = False
        for r in view.rows:
            if r.detail is None or r.detail[0] in self._seen:
                continue
            key, line = r.detail
            self._seen.add(key)
            self._order.append(key)
            self.lines.append(line)
            new = True
        if len(self.lines) > self.KEEP:
            cut = len(self.lines) - self.KEEP
            for key in self._order[:cut]:
                self._seen.discard(key)
            self._order, self.lines = self._order[cut:], self.lines[cut:]
        return new
