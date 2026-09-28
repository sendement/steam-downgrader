"""Drive the main window's rollback button against the synthetic Steam tree."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_e2e import APP, DEPOT, NEW, OLD, FakeSteam, build_fake_steam  # noqa: E402

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import steam_downgrader.gui.main_window as _mw  # noqa: E402
from steam_downgrader.gameversion import PatchNote  # noqa: E402

# No network in tests: fixed Steam news.
_mw.fetch_patch_notes = lambda app, name: [
    PatchNote(1590000000, "1.0", "Patch 1.0 is out"),
    PatchNote(1650000000, "1.1", "Patch Notes Version 1.1"),
]


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
    states = [w.versions.topLevelItem(i).text(4) for i in range(w.versions.topLevelItemCount())]
    assert states == ["установлена ✓", "загружена, можно применять"], states

    w.versions.setCurrentItem(w.versions.topLevelItem(1))
    assert w._targets() == {DEPOT: OLD}
    w._apply()
    while w.worker is not None:
        app.processEvents()
    assert (game_dir / "bin/game.exe").read_bytes() == b"old-exe"
    assert not (game_dir / "bin/vkd3d-proton.cache").exists(), "shader caches cleared on rollback"
    assert (root / "steamapps/shadercache" / APP / "transcoded_video.foz").exists()
    st = read_acf(acf)["AppState"]
    assert st["InstalledDepots"][DEPOT]["manifest"] == NEW
    lk = State().lock_for(APP)
    assert lk and lk["actual_depots"] == {DEPOT: OLD}, lk
    # After refresh the old build is shown as installed and the list item has a lock.
    assert w.versions.topLevelItem(1).text(4) == "установлена ✓"
    assert w.game_list.currentItem().text().startswith("🔒")

    w._unlock(False)
    while w.worker is not None:
        app.processEvents()
    assert not State().locks
    assert read_acf(acf)["AppState"]["InstalledDepots"][DEPOT]["manifest"] == OLD
    print("gui ok")


def test_steamdb_prompt_and_import() -> None:
    root, *_ = build_fake_steam(Path(tempfile.mkdtemp()))
    from steam_downgrader.gui import main_window as mw
    from steam_downgrader.gui.steamdb_dialog import SteamDBImportDialog
    from steam_downgrader.state import State

    opened: list[str] = []
    SteamDBImportDialog.open_steamdb = lambda self: opened.append(self.depot.currentData())
    app = QApplication.instance() or QApplication([])
    w = mw.MainWindow(FakeSteam(root), State())
    w.refresh()
    app.processEvents()
    assert not opened, "no prompt for the automatic selection on startup"

    # User clicks the game -> SteamDB page + import dialog.
    w.game_list.setCurrentRow(-1)
    w.game_list.setCurrentRow(0)
    app.processEvents()
    assert opened == [DEPOT], opened
    dlg = next(d for d in w._dialogs if isinstance(d, SteamDBImportDialog))
    dlg.text.setPlainText(
        "Seen Date\tRelative\tManifest ID\n"
        f"1 March 2019 – 10:00:00 UTC\t7 years ago\t9876543210987654321\n"
        f"13 September 2020 – 12:00:00 UTC\t6 years ago\t{OLD}\n"
        "\nDate\tBuild ID\tPatch notes\n"
        "13 September 2020 – 14:00:00 UTC\t5001000\tHotfix 1.0.5\n"
    )
    assert len(dlg.entries) == 2 and [b.buildid for b in dlg.builds] == [5001000]
    dlg._import()
    dlg.accept()
    app.processEvents()

    titles = [w.versions.topLevelItem(i).text(3) for i in range(w.versions.topLevelItemCount())]
    assert "SteamDB (восстановлено)" in titles, titles

    # Next launch: versions are there, no second prompt.
    opened.clear()
    w2 = mw.MainWindow(FakeSteam(root), State())
    w2.refresh()
    w2.game_list.setCurrentRow(-1)
    w2.game_list.setCurrentRow(0)
    app.processEvents()
    assert not opened
    gids = {w2.versions.topLevelItem(i).data(0, mw.ROLE_VERSION).depots[DEPOT] for i in range(w2.versions.topLevelItemCount())}
    assert "9876543210987654321" in gids, gids
    by_gid = {w2.versions.topLevelItem(i).data(0, mw.ROLE_VERSION).depots[DEPOT]: w2.versions.topLevelItem(i)
              for i in range(w2.versions.topLevelItemCount())}
    assert by_gid[OLD].text(1) == "1.0.5" and "5001000" in by_gid[OLD].text(2), by_gid[OLD].text(2)
    assert by_gid[NEW].text(1) == "≈1.1", by_gid[NEW].text(1)
    assert "(версия ≈1.1)" in w2.subtitle.text(), w2.subtitle.text()
    print("steamdb ok")


if __name__ == "__main__":
    test_gui_rollback()
    test_steamdb_prompt_and_import()
