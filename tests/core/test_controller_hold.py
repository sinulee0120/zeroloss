"""A silent Controller does not stop a test while its hold lasts (F11, S-56, GAP-130).

The user chose "이어받기": the Controller PC slept 52 minutes into a 100 h test and the
receiver stopped after 10 s. Now each heartbeat may carry ``hold_s`` (to the planned
end); a node goes on while it lasts, and stops as before once it is over.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any

from core.lifecycle.state_flow import State
from core.node import HOLD_MAX_S, Node


def node(state: State, last: float, hold_until: float) -> Any:
    n = Node.__new__(Node)
    n.flow = SimpleNamespace(state=state)  # type: ignore[assignment]
    n.last_controller_request, n.hold_until, n.lost_after_s = last, hold_until, 10.0
    return n


def test_silence_stops_without_a_hold_and_not_with_one() -> None:
    assert node(State.RUNNING, 0.0, 0.0).silence_stop(11.0)  # the 10 s rule
    assert node(State.RUNNING, 0.0, 3600.0).silence_stop(1800.0) is None  # held
    assert node(State.RUNNING, 0.0, 3600.0).silence_stop(3601.0)  # the hold is over
    assert node(State.IDLE, 0.0, 0.0).silence_stop(99.0) is None  # nothing to stop


def test_the_heartbeat_sets_the_hold() -> None:
    n = node(State.RUNNING, 0.0, 0.0)
    q = SimpleNamespace(int=lambda name, default=None: {"seq": 7, "hold_s": 600}.get(name, default))
    n.device_id, n.boot_id = "d", "b"
    with contextlib.suppress(AttributeError):  # the rest needs a whole node
        n.heartbeat(q)
    assert n.hold_until > 0
    assert HOLD_MAX_S >= 200 * 3600


def test_a_pc_waiting_its_turn_stays_awake_while_the_test_says_so() -> None:
    """2026-10-22 user: "iptv테스트할땐 sleep안들어가게 하는데 iperf일땐 sleep들어가는거같다"."""
    n = node(State.IDLE, 0.0, 0.0)  # waiting its turn: its node is IDLE
    asked: list[tuple[str, bool]] = []
    n.want_awake = lambda why, on: asked.append((why, on))
    q = SimpleNamespace(int=lambda name, default=None: {"seq": 7, "awake_s": 60}.get(name, default))
    n.device_id, n.boot_id = "d", "b"
    with contextlib.suppress(AttributeError):
        n.heartbeat(q)
    assert asked == [("in a test", True)] and n.awake_until > 0


def test_only_a_test_asks_the_pcs_to_stay_awake() -> None:
    """2026-10-23 receiverPC: the idle check's heartbeats held the T460's time sync 6 min
    before Start; awake_s goes only with a test's heartbeats."""
    from controller.run import get_heartbeat

    asked: list[dict[str, object]] = []

    class Client:
        def get(self, path: str, timeout_s: float = 1.0, **q: object) -> dict[str, object]:
            asked.append(q)
            return {}

    get_heartbeat(Client(), 1.0)  # type: ignore[arg-type]  # the idle check
    get_heartbeat(Client(), 1.0, awake=True)  # type: ignore[arg-type]  # a test
    assert "awake_s" not in asked[0] and asked[1]["awake_s"] == 60
