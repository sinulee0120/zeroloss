"""Agent Desktop entry: ``zeroloss-agent-desktop`` (docs/03-features.md F20, 06 4.3).

Started at login. One per user session (a second start brings the first window up and
ends). Runs the screen jobs the node lists (the PC player) and shows the small status
window. Closing the window hides it; the tray menu's Quit ends the Desktop and its
players (asked once when a test is running).
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QByteArray, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from agent.desktop.link import DesktopLink
from agent.desktop.screen_guard import Bus, ScreenGuard
from agent.desktop.view import ACTIVE, StatusView, status_view
from agent.desktop.window import StatusWindow
from core.modules.registry import load_desktop_entries
from core.node import LOCAL_PORT, read_build
from core.settings import CORE_DEFAULTS
from core.ui_qt.palette import apply_palette
from core.ui_qt.screen import prepare_hidpi
from core.ui_schema.design import COLORS

INSTALLED_MODULES = Path("/opt/zeroloss/modules")  # 06 4.3 (Windows: V18)


def work_dir() -> Path:
    """The user's own folder: players' pictures and run records, the window's choice."""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ZeroLoss" / "desktop"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "zeroloss" / "desktop"


ON_TOP_DEFAULT = CORE_DEFAULTS["S-53"] == "On"  # 16 S-53


class Settings:
    """F20 / 16 S-53: "always on top" is kept on this PC only."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def get(self) -> dict[str, Any]:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"on_top": ON_TOP_DEFAULT}
        return doc if isinstance(doc, dict) else {"on_top": ON_TOP_DEFAULT}

    def set(self, **values: Any) -> None:
        doc = {**self.get(), **values}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        tmp.replace(self.path)


def dot_icon(color: str) -> QIcon:
    """A plain colored dot, drawn (no picture files, 11-ui vector icons)."""
    pm = QPixmap(32, 32)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(COLORS.get(color, COLORS["grey"])))
    p.setPen(QColor(COLORS["border"]))
    p.drawEllipse(3, 3, 26, 26)
    p.end()
    return QIcon(pm)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="zeroloss-agent-desktop")
    ap.add_argument("--port", type=int, default=LOCAL_PORT, help="the node's local port")
    ap.add_argument("--modules", type=Path, default=INSTALLED_MODULES, help="module folder")
    ap.add_argument("--data", type=Path, default=None, help="this Desktop's own folder")
    args = ap.parse_args(argv)
    prepare_hidpi()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("ZeroLoss Agent Desktop")
    app.setQuitOnLastWindowClosed(False)
    name = f"zeroloss-agent-desktop-{getpass.getuser()}"
    if _bring_up_the_first(name):
        return 0
    apply_palette(app)
    folder = args.data or work_dir()
    entries, errors = load_desktop_entries(args.modules)
    factories = {(mid, kind): make for mid, e in entries.items()
                 for kind, make in getattr(e, "jobs", {}).items()}  # fmt: skip
    rows_of = {mid: e.status_rows for mid, e in entries.items() if hasattr(e, "status_rows")}
    link = DesktopLink(args.port, factories, folder)
    settings = Settings(folder / "desktop.json")

    def view() -> StatusView:
        return status_view(link.status, rows_of, link.refused, errors)

    tray_ok = QSystemTrayIcon.isSystemTrayAvailable()
    win = StatusWindow(view, bool(settings.get().get("on_top", ON_TOP_DEFAULT)),
                       lambda on: settings.set(on_top=on), tray_ok)  # fmt: skip
    server = QLocalServer()
    QLocalServer.removeServer(name)  # a crashed Desktop's leftover name
    server.listen(name)
    server.newConnection.connect(lambda: _show(win))
    link.start()
    guard = _screen_guard(link)
    screen_timer = QTimer()
    if guard is not None:
        screen_timer.timeout.connect(guard.check)
        screen_timer.start(2000)

    def quit_desktop() -> None:
        state = ((link.status or {}).get("node") or {}).get("state")
        if state in ACTIVE and link.running_jobs:
            ask = QMessageBox.question(
                win, "Quit Agent Desktop",
                "A test is running. Quitting stops the player on this PC and the report "
                "shows playback stopped. Quit anyway?",
            )  # fmt: skip
            if ask != QMessageBox.StandardButton.Yes:
                return
        if guard is not None:
            guard.release()
        link.stop()
        app.quit()

    tray: QSystemTrayIcon | None = None
    if tray_ok:
        tray = QSystemTrayIcon(dot_icon("grey"))
        menu = QMenu()
        show = QAction("Show status", menu)
        show.triggered.connect(lambda: _show(win))
        quit_ = QAction("Quit", menu)
        quit_.triggered.connect(quit_desktop)
        menu.addAction(show)
        menu.addAction(quit_)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda _: _show(win))
        tray.setToolTip("ZeroLoss Agent")
        tray.show()
        last = {"color": ""}

        def tray_color() -> None:
            v = win.view
            if tray is not None and v is not None and v.color != last["color"]:
                last["color"] = v.color
                tray.setIcon(dot_icon(v.color))

        win.timer.timeout.connect(tray_color)

    def after_update() -> None:
        """06 11.5 step 7: the node was updated -> this Desktop starts again as the new
        build (installed program only), when no player of a test is running."""
        built = ((link.status or {}).get("node") or {}).get("build")
        if getattr(sys, "frozen", False) and built and built != own and not link.running_jobs:
            link.stop()
            server.close()
            os.execv(sys.executable, [sys.executable, *sys.argv[1:]])

    own = read_build()
    win.timer.timeout.connect(after_update)
    win.place_bottom_right()
    win.show()
    code = app.exec()
    if guard is not None:
        guard.release()
    link.stop()
    return code


def _screen_guard(link: DesktopLink) -> ScreenGuard | None:
    """RCV-006: hold off screen blanking while the player plays (session D-Bus)."""
    try:
        bus = Bus()
    except Exception:  # no Qt D-Bus here (Windows comes with V18): nothing to hold
        return None
    return ScreenGuard(bus, lambda: bool(link.running_jobs), link.tell)


def _bring_up_the_first(name: str) -> bool:
    s = QLocalSocket()
    s.connectToServer(name)
    if not s.waitForConnected(500):
        return False
    s.write(QByteArray(b"show"))
    s.waitForBytesWritten(500)
    s.disconnectFromServer()
    return True


def _show(win: StatusWindow) -> None:
    win.showNormal()
    win.raise_()
    win.activateWindow()


if __name__ == "__main__":
    sys.exit(main())
