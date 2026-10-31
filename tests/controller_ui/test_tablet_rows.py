"""Tablet test rows (11-ui 3, 2026-10-23): a row per tablet on a PC's USB; picking gives
the names tablet-1..3; the other test type's receivers stay saved."""

from __future__ import annotations

from typing import Any

from controller.ui.view import (
    TABLET,
    receiver_change,
    role_change,
    setup_rows,
    tablet_role_change,
)

TAB = {"serial": "R32Y", "state": "device", "model": "SM X930",
       "battery": {"level": 22, "charging": False}, "wifi_ip": "192.168.45.167",
       "app": {"installed": True, "version": "20261023.1333"}}  # fmt: skip


def screen(receivers: list[dict[str, Any]], tablets: list[dict[str, Any]] | None = None,
           test_type: str = "IPTV test") -> dict[str, Any]:  # fmt: skip
    # as controller/setup.py roles(): the IPTV test uses PCs and tablets, others PCs only
    mine = [r for r in receivers if test_type == "IPTV test" or not r.get("serial")]
    return {"setup": {"test_type": test_type, "receivers": receivers, "settings": {}},
            "roles": {"sender": "tx", "receivers": mine},
            "sides": {"tx": {"reachable": True, "side": "wan", "tablets": []},
                      "rx": {"reachable": True, "side": "lan",
                             "tablets": [TAB] if tablets is None else tablets}}}  # fmt: skip


def test_a_tablet_row_says_its_pc_battery_wifi_and_what_to_do() -> None:
    rows = setup_rows(screen([]))
    tab = [r for r in rows if r.tablet]
    assert [r.name for r in tab] == [TABLET + "R32Y"] and tab[0].role == "unused"
    assert "on rx USB" in tab[0].side and "192.168.45.167" in tab[0].side
    assert "charger" in tab[0].why  # 22 %, not charging


def test_picking_tablets_names_them_and_three_at_most() -> None:
    s = screen([])
    got = tablet_role_change(s, TABLET + "R32Y", "receiver")["receivers"]
    assert got == [{"device": "tablet-1", "ch": 1, "host": "rx", "serial": "R32Y"}]
    many = [{"device": f"tablet-{i}", "ch": 1, "host": "rx", "serial": f"S{i}"} for i in (1, 2, 3)]
    on_usb = [TAB, *({**TAB, "serial": f"S{i}"} for i in (1, 2, 3))]
    full = tablet_role_change(screen(many, on_usb), TABLET + "R32Y", "receiver")["receivers"]
    assert [r["serial"] for r in full] == ["S1", "S2", "S3"]  # three on USB: no fourth
    # 2026-10-31 user ("한대밖에 안골라지거든 태블릿 두대중"): unplugged ones give their place
    gone = [TAB, {**TAB, "serial": "S2"}]
    two = tablet_role_change(screen(many, gone), TABLET + "R32Y", "receiver")["receivers"]
    assert [r["serial"] for r in two] == ["S2", "R32Y"]


def test_channel_and_buffer_by_serial_and_pc_receiver_kept() -> None:
    pc = {"device": "rx", "ch": 2, "network": "Wired"}
    t = {"device": "tablet-1", "ch": 1, "host": "rx", "serial": "R32Y"}
    out = receiver_change(screen([pc, t]), TABLET + "R32Y", ch=3, buffer_ms=2000)["receivers"]
    assert {"device": "tablet-1", "ch": 3, "host": "rx", "serial": "R32Y",
            "buffer_ms": 2000} in out and pc in out  # fmt: skip
    gone = tablet_role_change(screen([pc, t]), TABLET + "R32Y", "unused")["receivers"]
    assert gone == [pc]  # the PC test's receiver stays for when it is picked again
    back = role_change(screen([pc, t], test_type="Throughput · All"), "tx", "sender")
    assert t in back["receivers"]  # another test type keeps the tablets saved


def test_the_row_widget_shows_a_tablet_with_its_wifi_and_password_box() -> None:
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from controller.ui.control_state import Ctl
    from controller.ui.main_window import SetupRow

    app = QApplication.instance() or QApplication([])
    assert app is not None
    picked: list[tuple[str, str]] = []
    row = SetupRow(lambda r: None, lambda c: None, lambda n: None,
                   on_tabwifi=lambda s, sec: picked.append((s, sec)))  # fmt: skip
    tab = {
        **TAB,
        "networks": [{"ssid": "Lab", "security": "WPA2", "band": "5GHz", "rssi_dbm": -40}],
    }
    t = {"device": "tablet-1", "ch": 2, "host": "rx", "serial": "R32Y",
         "wifi": {"ssid": "Lab", "security": "WPA2"}}  # fmt: skip
    v = next(r for r in setup_rows(screen([t], [tab])) if r.tablet)
    row.show_view(v, Ctl(True))
    assert [row.role.itemData(i) for i in range(row.role.count())] == ["receiver", "unused"]
    assert not row.address.isVisibleTo(row) and not row.card.isVisibleTo(row)
    assert row.ch.isVisibleTo(row) and row.tabwifi.isVisibleTo(row)
    assert row.tabwifi.currentData() == ("Lab", "WPA2") and row.tabpw is not None
    assert row.label.toolTip() or "tablet-1 · SM X930" in row.label.text()
    row.tabwifi.setCurrentIndex(0)
    row.tabwifi.activated.emit(0)
    assert picked == [("", "")]  # keep its own Wi-Fi


def test_a_tablet_test_keeps_one_pc_receiver_beside_the_tablets() -> None:
    """2026-10-23 user: "수신기 pc 한대, 태블릿 두대"."""
    t1 = {"device": "tablet-1", "ch": 1, "host": "rx", "serial": "R32Y"}
    pc = {"device": "rx", "ch": 1, "network": "Wireless"}
    got = role_change(screen([t1]), "rx", "receiver")["receivers"]
    assert {"device": "rx", "ch": 1, "network": "Wired"} in got and t1 in got
    more = tablet_role_change(screen([pc, t1], [TAB, {**TAB, "serial": "S2"}]), TABLET + "S2",
                              "receiver")["receivers"]  # fmt: skip
    assert pc in more and t1 in more and any(r.get("device") == "tablet-2" for r in more)


def test_a_tablets_only_test_says_the_pc_relays_them() -> None:
    """User 2026-10-24: a tablet test may be three tablets and no PC receiver."""
    tabs = [{"device": f"tablet-{i}", "ch": i, "host": "rx", "serial": f"S{i}"} for i in (1, 2, 3)]
    rows = {
        r.name: r for r in setup_rows(screen(tabs, [{**TAB, "serial": f"S{i}"} for i in (1, 2, 3)]))
    }
    assert rows["rx"].role == "unused" and "relays 3 tablet(s)" in rows["rx"].side
    assert sum(1 for r in rows.values() if r.tablet and r.role == "receiver") == 3
