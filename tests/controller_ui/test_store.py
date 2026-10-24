"""Screen state copy (11-ui 1, 1.12): workers feed one queue, the store changes only on
real differences, commands run one at a time, a dead service shows as disconnected."""

from __future__ import annotations

import queue
import random
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from controller.service import ControllerService
from controller.ui.store import Commands, Done, Feed, Message, Status, Store

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def svc(tmp_path: Path) -> Iterator[ControllerService]:
    s = ControllerService(
        tmp_path / "ctl",
        tmp_path / "rep",
        REPO / "modules",
        host="127.0.0.1",
        port=0,
        local_port=0,
    )
    s.start()
    yield s
    s.stop()


def next_msg(q: queue.Queue[Message], kind: str, timeout_s: float = 5.0) -> object:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        k, v = q.get(timeout=end - time.monotonic())
        if k == kind:
            return v
    raise AssertionError(f"no {kind}")


def test_feed_reads_status_and_notices_a_dead_service(svc: ControllerService) -> None:
    q: queue.Queue[Message] = queue.Queue()
    feed = Feed(svc.node.local.port, q, interval_s=0.2)
    feed.start()
    try:
        s = next_msg(q, "status")
        assert isinstance(s, Status) and s.connected and s.run["state"] == "IDLE"
        svc.stop()
        for _ in range(20):
            s = next_msg(q, "status")
            assert isinstance(s, Status)
            if not s.connected:
                break
        assert not s.connected and s.error
    finally:
        feed.stop()


def test_commands_report_errors_and_ignore_double_clicks(svc: ControllerService) -> None:
    q: queue.Queue[Message] = queue.Queue()
    pokes: list[int] = []
    cmd = Commands(svc.node.local.port, q, after=lambda: pokes.append(1))
    cmd.start()
    try:
        assert cmd.send("add", "/v1/controller/devices/add", name="", host="x")
        assert not cmd.send("add", "/v1/controller/devices/add", name="", host="x")  # busy
        d = next_msg(q, "done")
        assert isinstance(d, Done) and not d.ok and d.error and d.error["code"].startswith("ZL-")
        assert not cmd.busy("add") and pokes
        assert cmd.send("add", "/v1/controller/devices/add", name="", host="x")  # free again
        next_msg(q, "done")
    finally:
        cmd.stop()


def test_store_changes_only_on_real_difference() -> None:
    st = Store()
    a = Status(True, "b1", {"state": "IDLE"})
    assert st.apply(("status", a)) and st.version == 1
    assert not st.apply(("status", Status(True, "b1", {"state": "IDLE"}))) and st.version == 1
    assert st.apply(("status", Status(False, error="x"))) and st.version == 2
    assert not st.controls(True)["start_stop"].enabled  # disconnected locks
    assert not st.apply(("bogus", None))


def test_store_random_messages_keep_invariants() -> None:
    rng = random.Random(924)
    st = Store()
    states = ["IDLE", "PREPARING", "RUNNING", "STOPPING", "REPORTING", "FAULT"]
    for _ in range(2000):
        if rng.random() < 0.8:
            run = {"state": rng.choice(states), "result": rng.choice([None, {"verdict": "PASS"}])}
            msg: Message = ("status", Status(rng.random() < 0.9, "b", run))
        else:
            msg = ("done", Done("start", rng.random() < 0.5, {}))
        before = st.version
        changed = st.apply(msg)
        assert st.version == before + (1 if changed else 0)
        c = st.controls(rng.random() < 0.5)
        if not st.status.connected:
            assert not any(x.enabled for x in c.values())
        elif c["start_stop"].enabled:
            assert st.run_state in ("IDLE", "RUNNING")


def test_sent_until_done() -> None:
    st = Store()
    assert st.apply(("sent", "stop")) and st.pending == {"stop"}
    assert not st.apply(("sent", "stop"))  # the same wait, no redraw
    st.apply(("done", Done("stop", True, {})))
    assert st.pending == frozenset()


def test_screen_edits_show_at_once_and_only_between_tests() -> None:
    """F14 / 1.12: an edit changes the shown choice at once; the plan waits for the
    service's answer; while a test runs the edit is refused when handled."""
    st = Store()
    assert not st.apply(("edit", {"quality": "HD"}))  # nothing read yet
    st.apply(("setup", {"setup": {"quality": "UHD", "duration_s": 600, "settings": {}},
                        "roles": {"why": None}, "plan": {"quality": "UHD"}}))  # fmt: skip
    assert st.plan_ok and st.choice["quality"] == "UHD"
    assert st.apply(("edit", {"quality": "HD"})) and st.choice["quality"] == "HD"
    assert st.plan is None and st.not_ready_why == "Saving…"
    assert not st.apply(("edit", {"quality": "HD"}))  # the same value: no redraw
    st.apply(("status", Status(True, run={"state": "RUNNING"})))
    assert not st.apply(("edit", {"quality": "UHD"})) and st.choice["quality"] == "HD"


def test_a_finished_run_pops_up_once() -> None:
    """F15: every finish shows the result once; an old result at connect does not."""

    def st(state: str, run_id: str | None) -> Status:
        result = {"run_id": run_id, "verdict": "PASS"} if run_id else None
        return Status(True, "b", {"state": state, "result": result}, ())

    s = Store()
    s.apply(("status", st("IDLE", "R0")))  # already there when the screen opened
    assert s.take_finished() is None
    s.apply(("status", st("RUNNING", "R0")))
    s.apply(("status", st("IDLE", "R1")))
    assert (s.take_finished() or {}).get("run_id") == "R1"
    assert s.take_finished() is None  # once
    s.apply(("status", Status(False)))  # reconnect: the same result is not new
    s.apply(("status", st("IDLE", "R1")))
    assert s.take_finished() is None
    s.apply(("status", st("FAULT", "R2")))  # a safe stop also pops up
    assert (s.take_finished() or {}).get("run_id") == "R2"


def test_setup_feed_asks_devices_only_between_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """07 7: the screen choice (it asks every device) is read only while no test runs."""
    from controller.ui import store as store_mod
    from controller.ui.store import SetupFeed

    asked: list[str] = []

    class FakeClient:
        def get(self, path: str, **_: object) -> dict[str, object]:
            asked.append(path)
            return {"settings": {}} if path.endswith("/settings") else {"setup": {}}

        def close(self) -> None:
            pass

    monkeypatch.setattr(store_mod, "_client", lambda port: FakeClient())
    out: queue.Queue[store_mod.Message] = queue.Queue()
    feed = SetupFeed(1, out, interval_s=0.05)
    feed.between_tests(False)  # a test runs from the start
    feed.start()
    time.sleep(0.4)
    assert asked == []  # nothing asked during the test
    feed.between_tests(True)
    end = time.monotonic() + 3
    while len(asked) < 3 and time.monotonic() < end:
        time.sleep(0.02)
    assert asked[0].endswith("/settings") and asked[1].endswith("/setup")
    feed.between_tests(False)
    time.sleep(0.2)
    n = len(asked)
    time.sleep(0.4)
    assert len(asked) <= n + 1  # at most the round already under way
    feed.stop()
    assert not feed._thread.is_alive()


def test_a_cancel_is_never_stuck_behind_a_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """GAP-123 (real window, 2026-10-09): a Start answers only when the videos are made
    (13 min for that movie); a Cancel queued behind it on the one command line never
    reached the service. A Start now has its own thread; a Stop goes at once."""
    import queue as q
    import threading
    import time

    from controller.ui.store import Commands

    release = threading.Event()
    done_names: list[str] = []

    def one(self: Commands, client: object, job: tuple[str, str, dict[str, object], float]) -> None:
        if job[0] == "run":
            release.wait(10)  # the start that waits for the videos
        with self._lock:
            self._busy.discard(job[0])
        done_names.append(job[0])

    monkeypatch.setattr(Commands, "_one", one)
    out: q.Queue[object] = q.Queue()
    c = Commands(1, out, lambda: None)  # type: ignore[arg-type]
    monkeypatch.setattr(
        "controller.ui.store._client", lambda port: type("C", (), {"close": lambda s: None})()
    )
    c.start()
    assert c.send("run", "/v1/controller/run", timeout_s=3600)
    assert c.send("stop", "/v1/controller/stop")
    end = time.monotonic() + 5
    while "stop" not in done_names:
        assert time.monotonic() < end, "the Stop waited behind the Start"
        time.sleep(0.01)
    assert "run" not in done_names  # the Start is still running
    release.set()
    c.stop()
