"""Controller Desktop entry: ``zeroloss-desktop`` (docs/12-architecture.md 1-0).

Only a window onto the Controller service at 127.0.0.1:10994; closing it never stops
a test.
"""

from __future__ import annotations

import argparse
import sys

from PySide6.QtWidgets import QApplication

from controller.ui.main_window import MainWindow
from core.node import LOCAL_PORT
from core.ui_qt.palette import apply_palette
from core.ui_qt.screen import prepare_hidpi


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="zeroloss-desktop")
    ap.add_argument("--port", type=int, default=LOCAL_PORT, help="Controller local port")
    args = ap.parse_args(argv)
    prepare_hidpi()
    app = QApplication(sys.argv[:1])
    apply_palette(app)
    win = MainWindow(args.port)
    win.start()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
