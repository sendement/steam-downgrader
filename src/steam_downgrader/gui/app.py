from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from ..state import State
from ..steam import Steam, default_steam_root


def run() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Steam Downgrader")
    app.setDesktopFileName("steam-downgrader")

    root = default_steam_root()
    if not root:
        QMessageBox.critical(None, "Steam Downgrader", "Не найдена установка Steam.")
        return 1

    from .main_window import MainWindow

    win = MainWindow(Steam(root), State())
    win.show()
    return app.exec()
