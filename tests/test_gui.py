"""Drive the main window's rollback button against the synthetic Steam tree."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_e2e import APP, DEPOT, NEW, OLD, FakeSteam, build_fake_steam  # noqa: E402

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402


def test_gui_rollback() -> None:
    root, game_dir, acf, _content = build_fake_steam(Path(tempfile.mkdtemp()))
    from steam_downgrader.gui.main_window import MainWindow
    from steam_downgrader.state import State
    from steam_downgrader.steam import read_acf

    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No if "Запустить Steam" in str(a) else QMessageBox.Yes)
    QMessageBox.critical = staticmethod(lambda *a, **k: print("CRITICAL", a[2]))
    app = QApplication.instance() or QApplication([])
    w = MainWindow(FakeSteam(root), State())
    w.refresh()
    assert w.game and w.game.app_id == APP
    states = [w.versions.topLevelItem(i).text(3) for i in range(w.versions.topLevelItemCount())]
    assert states == ["установлена ✓", "загружена, можно применять"], states

    w.versions.setCurrentItem(w.versions.topLevelItem(1))
    assert w._targets() == {DEPOT: OLD}
    w._apply()
    while w.worker is not None:
        app.processEvents()
    assert (game_dir / "bin/game.exe").read_bytes() == b"old-exe"
    st = read_acf(acf)["AppState"]
    assert st["InstalledDepots"][DEPOT]["manifest"] == NEW
    lk = State().lock_for(APP)
    assert lk and lk["actual_depots"] == {DEPOT: OLD}, lk
    # After refresh the old build is shown as installed and the list item has a lock.
    assert w.versions.topLevelItem(1).text(3) == "установлена ✓"
    assert w.game_list.currentItem().text().startswith("🔒")

    w._unlock(False)
    while w.worker is not None:
        app.processEvents()
    assert not State().locks
    assert read_acf(acf)["AppState"]["InstalledDepots"][DEPOT]["manifest"] == OLD
    print("gui ok")


if __name__ == "__main__":
    test_gui_rollback()
