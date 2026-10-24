"""Control state table (docs/11-ui.md 1.10): every element x every column, written from
the spec table, plus random invariants (playbook)."""

import random

import pytest

from controller.ui.control_state import COLUMN, ELEMENTS, Ctl, control_states

# Columns: IDLE ok, IDLE failed pre-check, STARTING, RUNNING, STOPPING, FAULT.
# Value: E = enabled, L = shown but disabled, H = hidden.
COLS = ["IDLE", "IDLE_BAD", "STARTING", "RUNNING", "STOPPING", "FAULT"]
TABLE = {
    "start_stop": "E L L E L L",
    "test_type": "E E L L L L",
    "quality": "E E L L L L",
    "duration": "E E L L L L",
    "devices": "E E L L L L",
    "send_reports": "E E L L L L",
    "advanced_toggle": "E E E E E E",
    "advanced_values": "E E L L L L",
    "channel": "E E L E L L",
    "snapshot": "E E L E L L",  # IDLE: with a connected device
    "follow_live": "H H H E E E",
    "reports": "E E E E E E",
    "live_view": "E E E E E E",  # live monitor (02 12 /live) (GAP-119)
    "storage": "E E E E E E",  # 20 GAP-82
    "export_diagnostics": "E E E E E E",
    "last_report": "E E E E E E",  # with a report
    "device_manage": "E E L L L L",
    "reset_all": "E E L L L E",  # 2026-10-22 user: "Reset 버튼이 안보이던데"
    "fault_restart": "H H H H H E",  # S6 fault screen (03 F11)
    "fault_shutdown": "H H H H H E",
    "stop_broadcast": "E E L L L E",  # broadcast on
}
LABEL = {
    "IDLE": "Start",
    "IDLE_BAD": "Start",
    "STARTING": "Starting…",
    "RUNNING": "Stop",
    "STOPPING": "Stopping…",
    "FAULT": "Start",
}
STATE_OF = {
    "IDLE": "IDLE",
    "IDLE_BAD": "IDLE",
    "STARTING": "PREPARING",
    "RUNNING": "RUNNING",
    "STOPPING": "STOPPING",
    "FAULT": "FAULT",
}


def _states(col: str) -> dict[str, Ctl]:
    return control_states(
        STATE_OF[col], col != "IDLE_BAD", True, True, any_device_connected=True, broadcast_on=True
    )


def test_table_covers_every_element() -> None:
    assert set(TABLE) == set(ELEMENTS)


@pytest.mark.parametrize("col", COLS)
def test_table(col: str) -> None:
    got = _states(col)
    for el, row in TABLE.items():
        want = row.split()[COLS.index(col)]
        c = got[el]
        have = "H" if not c.visible else ("E" if c.enabled else "L")
        assert have == want, f"{el} in {col}: {have} != {want}"
    assert got["start_stop"].label == LABEL[col]


@pytest.mark.parametrize("state", ["REPORTING", "SAFE_STOP", "RESETTING"])
def test_other_run_states_act_as_stopping(state: str) -> None:
    assert control_states(state, True, True, True) == control_states("STOPPING", True, True, True)


def test_conditional_cells() -> None:
    s = control_states("IDLE", True, False, True, any_device_connected=False)
    assert not s["last_report"].enabled and s["last_report"].tooltip
    assert not s["snapshot"].enabled and s["snapshot"].tooltip
    assert not s["stop_broadcast"].visible


def test_channel_box_during_a_test() -> None:
    """11-ui 1.10 + K-11: the box changes now while running, but not while S-21 is on and
    not twice at once (03 F19)."""
    assert control_states("RUNNING", True, False, True)["channel"].enabled
    auto = control_states("RUNNING", True, False, True, auto_channel=True)["channel"]
    assert not auto.enabled and "S-21" in auto.tooltip
    busy = control_states("RUNNING", True, False, True, pending=frozenset({"channel:rx-pc"}))
    assert not busy["channel"].enabled and busy["channel"].tooltip
    assert control_states("IDLE", True, False, True, auto_channel=True)["channel"].enabled
    assert not control_states("STOPPING", True, False, True)["channel"].enabled


def test_disconnected_locks_everything() -> None:
    s = control_states("RUNNING", True, True, False)
    assert not any(c.enabled for c in s.values())


def test_random_invariants() -> None:
    rng = random.Random(20260924)
    states = [*COLUMN, "UNKNOWN"]
    for _ in range(3000):
        args = (rng.choice(states), rng.random() < 0.5, rng.random() < 0.5, rng.random() < 0.8)
        dev, bc, auto = rng.random() < 0.5, rng.random() < 0.5, rng.random() < 0.5
        pend = frozenset(rng.sample(["run", "stop", "channel:a"], rng.randint(0, 2)))
        s = control_states(*args, any_device_connected=dev, broadcast_on=bc,
                           auto_channel=auto, pending=pend)  # fmt: skip
        assert set(s) == set(ELEMENTS)
        for name, c in s.items():
            if c.visible and not c.enabled:
                assert c.tooltip, f"{name} disabled without a reason ({args})"
        assert not (s["channel"].enabled and args[0] == "RUNNING" and auto)  # K-11
        ss = s["start_stop"]
        if pend & {"run", "stop"}:
            continue  # a sent Start/Stop shows the next column (test below)
        assert ss.enabled == (args[3] and ((args[0] == "IDLE" and args[1]) or args[0] == "RUNNING"))
        assert ss.enabled or ss.popup or not args[3]  # a grey Start/Stop always explains
        editable = s["test_type"].enabled
        assert not (editable and args[0] != "IDLE")  # settings only change when idle


def test_sent_command_waits_in_the_table() -> None:
    """11-ui 1.10: after Start / Stop is sent and before the answer, the STARTING /
    STOPPING cell of Start/Stop is used (GAP-36)."""
    s = control_states("IDLE", True, False, True, pending=frozenset({"run"}))
    assert s["start_stop"] == control_states("PREPARING", True, False, True)["start_stop"]
    s = control_states("RUNNING", True, False, True, pending=frozenset({"stop"}))
    assert s["start_stop"] == control_states("STOPPING", True, False, True)["start_stop"]
    # other elements follow the real state only
    assert s["channel"] == control_states("RUNNING", True, False, True)["channel"]
    # a stale pending in another state changes nothing
    s = control_states("RUNNING", True, False, True, pending=frozenset({"run"}))
    assert s["start_stop"].label == "Stop"


def test_cancel_while_videos_are_made() -> None:
    """11-ui 1.10 / iptv 1-1-2: only while the start makes videos is Start/Stop a live
    Cancel; once Cancel is sent it waits like any sent command (USR-001)."""
    s = control_states("PREPARING", True, False, True, preparing=True)["start_stop"]
    assert s.enabled and s.label == "Cancel" and not s.popup
    assert control_states("PREPARING", True, False, True)["start_stop"].label == "Starting…"
    sent = control_states(
        "PREPARING", True, False, True, pending=frozenset({"stop"}), preparing=True
    )
    assert sent["start_stop"] == control_states("PREPARING", True, False, True)["start_stop"]
    for state in ("IDLE", "RUNNING", "STOPPING", "FAULT"):  # preparing means nothing elsewhere
        assert control_states(state, True, False, True, preparing=True) == control_states(
            state, True, False, True
        )


def test_one_fault_action_at_a_time() -> None:
    """S6: while Reset All, Restart or Shut Down All waits for its answer, all three
    are grey and say why; the Reset button reads Resetting… (11-ui 1.12)."""
    for pending in ({"reset"}, {"shutdown"}, {"restart:rx"}):
        s = control_states("FAULT", True, True, True, pending=frozenset(pending))
        for key in ("reset_all", "fault_restart", "fault_shutdown"):
            assert s[key].visible and not s[key].enabled and s[key].tooltip, (pending, key)
        assert s["reset_all"].label == ("Resetting…" if "reset" in pending else "Reset All")
    s = control_states("FAULT", True, True, True)
    assert all(s[k].enabled for k in ("reset_all", "fault_restart", "fault_shutdown"))


def test_storage_delete_buttons() -> None:
    """11-ui 1.10 Manage storage: one run chosen, never the running one, Deleting… while
    a deletion runs (20 GAP-82)."""
    from controller.ui.control_state import storage_actions

    none = storage_actions(None, False, frozenset())
    assert not none["delete_raw"].enabled and none["delete_raw"].tooltip == "Choose a run first"
    now = storage_actions("R1", True, frozenset())
    assert not now["delete_run"].enabled and now["delete_run"].tooltip == "This test is running"
    ok = storage_actions("R1", False, frozenset())
    assert ok["delete_raw"].enabled and ok["delete_run"].enabled
    busy = storage_actions("R1", False, frozenset({"storage_delete"}))
    assert {c.label for c in busy.values()} == {"Deleting…"}
    assert not any(c.enabled for c in busy.values())


def test_storage_label_and_export() -> None:
    s = control_states("RUNNING", True, True, True, storage_low="Less than 20% ... on rx")
    assert s["storage"].enabled and s["storage"].label == "⚠ Storage low"
    assert control_states("IDLE", True, True, True)["storage"].label == "Storage"
    e = control_states("IDLE", True, True, True, pending=frozenset({"diagnostics"}))
    assert not e["export_diagnostics"].enabled and e["export_diagnostics"].label == "Exporting…"
