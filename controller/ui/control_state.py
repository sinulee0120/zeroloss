"""The control state table (docs/11-ui.md 1.10) as one function.

Every enabled/visible/label decision on the main screen comes from here; widgets never
call setEnabled() on their own. Disabled elements always carry the reason as a tooltip.
"""

from __future__ import annotations

from dataclasses import dataclass

# Run states of the Controller (F11) mapped to the columns of the 1.10 table.
COLUMN = {
    "IDLE": "IDLE",
    "PREPARING": "STARTING",
    "RUNNING": "RUNNING",
    "STOPPING": "STOPPING",
    "REPORTING": "STOPPING",
    "SAFE_STOP": "STOPPING",
    "RESETTING": "STOPPING",
    "FAULT": "FAULT",
}


@dataclass(frozen=True)
class Ctl:
    enabled: bool
    visible: bool = True
    label: str = ""
    tooltip: str = ""
    popup: str = ""  # greyed but clickable: a click explains why (1.10 Start/Stop)


def control_states(
    run_state: str,
    precheck_ok: bool,
    has_last_report: bool,
    connected: bool,
    any_device_connected: bool = False,
    broadcast_on: bool = False,
    precheck_reason: str = "See the Status box for what to fix",
    unavailable: dict[str, str] | None = None,
    pending: frozenset[str] = frozenset(),
    preparing: bool = False,
    storage_low: str = "",
    auto_channel: bool = False,
) -> dict[str, Ctl]:
    """(test state, settings) -> state of every control. ``precheck_ok`` False means the
    'IDLE (pre-check failed)' column. ``connected`` False = the Controller service is not
    reachable: the whole screen locks as "Reconnecting…" (11-ui 1). ``preparing`` = the
    start is making videos: Start/Stop is Cancel then (1.10, iptv 1-1-2). ``storage_low``
    names the PCs short of space (09 SYS-010): the Storage button says so."""
    col = COLUMN.get(run_state, "FAULT")
    wait_col = col  # Start/Stop while a sent command waits for its answer (1.10)
    if col == "IDLE" and "run" in pending:
        wait_col = "STARTING"
    elif col == "RUNNING" and "stop" in pending:
        wait_col = "STOPPING"
    if not connected:
        off = Ctl(False, tooltip="Controller service is not reachable")
        return {k: off for k in ELEMENTS} | {
            "start_stop": Ctl(False, label="Start", tooltip=off.tooltip)
        }
    idle = col == "IDLE"
    locked = "Locked during test"
    s: dict[str, Ctl] = {}
    if wait_col == "STARTING" and idle:
        wait = "Starting, please wait"
        s["start_stop"] = Ctl(False, label="Starting…", tooltip=wait, popup=wait)
    elif wait_col == "STOPPING" and col == "RUNNING":
        wait = "Stopping, please wait"
        s["start_stop"] = Ctl(False, label="Stopping…", tooltip=wait, popup=wait)
    elif idle and precheck_ok:
        s["start_stop"] = Ctl(True, label="Start")
    elif idle:
        s["start_stop"] = Ctl(False, label="Start", tooltip=precheck_reason, popup=precheck_reason)
    elif col == "STARTING" and preparing and "stop" not in pending:
        s["start_stop"] = Ctl(True, label="Cancel", tooltip="Stop making the videos and go back")
    elif col == "STARTING":
        wait = "Starting, please wait"
        s["start_stop"] = Ctl(False, label="Starting…", tooltip=wait, popup=wait)
    elif col == "RUNNING" and broadcast_on:  # F18: the sending goes on (11-ui 1.10)
        s["start_stop"] = Ctl(True, label="Stop", tooltip="Stops measurement only")
    elif col == "RUNNING":
        s["start_stop"] = Ctl(True, label="Stop")
    elif col == "STOPPING":
        wait = "Stopping, please wait"
        s["start_stop"] = Ctl(False, label="Stopping…", tooltip=wait, popup=wait)
    else:
        why = "A fault stopped the test. Press Reset All first"
        s["start_stop"] = Ctl(False, label="Start", tooltip=why, popup=why)
    edit = Ctl(True) if idle else Ctl(False, tooltip=locked)
    for key in (
        "test_type",
        "quality",
        "duration",
        "devices",
        "send_reports",
        "advanced_values",
        "device_manage",
    ):
        s[key] = edit
    s["advanced_toggle"] = Ctl(True)
    if col == "RUNNING" and auto_channel:  # K-11: S-21 changes the channels by itself
        s["channel"] = Ctl(False, tooltip="Automatic TV channel change is on (S-21)")
    elif col == "RUNNING" and any(p.startswith("channel:") for p in pending):
        s["channel"] = Ctl(False, tooltip="Changing the TV channel, please wait")
    else:
        s["channel"] = Ctl(True) if col in ("IDLE", "RUNNING") else Ctl(False, tooltip=locked)
    if col == "RUNNING" or (idle and any_device_connected):
        s["snapshot"] = Ctl(True)
    else:
        s["snapshot"] = Ctl(False, tooltip=locked if not idle else "No device is connected")
    s["follow_live"] = Ctl(True, visible=col in ("RUNNING", "STOPPING", "FAULT"))
    s["reports"] = Ctl(True)
    s["live_view"] = Ctl(True)  # live monitor (02 12 /live) in the browser, always (GAP-119)
    # 20 GAP-82: always usable; the label warns when a PC is short of space (09 SYS-010)
    s["storage"] = Ctl(True, label="⚠ Storage low" if storage_low else "Storage",
                       tooltip=storage_low)  # fmt: skip
    exporting = "diagnostics" in pending
    s["export_diagnostics"] = Ctl(
        not exporting, label="Exporting…" if exporting else "Export diagnostics",
        tooltip="Making the file, please wait" if exporting else "",
    )  # fmt: skip
    s["last_report"] = Ctl(has_last_report, tooltip="" if has_last_report else "No report yet")
    # F11 fault screen (S6): one fault action at a time; the answer comes back first
    acting = (
        "reset" in pending
        or "shutdown" in pending
        or any(p.startswith("restart:") for p in pending)
    )
    wait = "Working on it, please wait"
    # always there (2026-10-22 user: "Reset 버튼이 안보이던데"): it puts every PC's settings
    # back between tests; during a test grey, and a click says why
    between = idle or col == "FAULT"
    during = "Stop the test first, then press Reset All."
    s["reset_all"] = Ctl(
        between and not acting,
        label="Resetting…" if "reset" in pending else "Reset All",
        tooltip=wait if acting else "" if between else during,
        popup=wait if acting else "" if between else during,
    )  # fmt: skip
    fault_why = "" if col == "FAULT" and not acting else wait if acting else "Only after a fault"
    for key in ("fault_restart", "fault_shutdown"):
        s[key] = Ctl(col == "FAULT" and not acting, visible=col == "FAULT", tooltip=fault_why)
    s["stop_broadcast"] = Ctl(
        idle or col == "FAULT",
        visible=broadcast_on,
        tooltip="" if idle or col == "FAULT" else locked,
    )
    for key, why in (unavailable or {}).items():
        s[key] = Ctl(False, s[key].visible, s[key].label, why)
    return s


ELEMENTS = (
    "start_stop",
    "test_type",
    "quality",
    "duration",
    "devices",
    "send_reports",
    "advanced_toggle",
    "advanced_values",
    "channel",
    "snapshot",
    "follow_live",
    "reports",
    "live_view",
    "storage",
    "export_diagnostics",
    "last_report",
    "device_manage",
    "reset_all",
    "fault_restart",
    "fault_shutdown",
    "stop_broadcast",
)


def storage_actions(selected: str | None, in_use: bool, pending: frozenset[str]) -> dict[str, Ctl]:
    """11-ui 1.10 Manage storage (S14): delete buttons need one run chosen, never the
    test running now; while a deletion runs both say Deleting…."""
    if "storage_delete" in pending:
        wait = Ctl(False, label="Deleting…", tooltip="Deleting, please wait")
        return {"delete_raw": wait, "delete_run": wait}
    if selected is None:
        why = "Choose a run first"
    elif in_use:
        why = "This test is running"
    else:
        why = ""
    return {"delete_raw": Ctl(not why, label="Delete raw data", tooltip=why),
            "delete_run": Ctl(not why, label="Delete run", tooltip=why)}  # fmt: skip


def report_row_actions(has_report: bool) -> dict[str, Ctl]:
    """11-ui S8: [Show] only when the run has a report; [Folder] always (20 GAP-83)."""
    why = "" if has_report else "This run has no report"
    return {"show": Ctl(has_report, label="Show", tooltip=why), "folder": Ctl(True, label="Folder")}
