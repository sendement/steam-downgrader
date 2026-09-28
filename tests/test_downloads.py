"""Background DepotDownloader queue against a fake DepotDownloader."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_e2e import APP, DEPOT, FakeSteam, build_fake_steam  # noqa: E402

from PySide6.QtWidgets import QApplication  # noqa: E402

FAKE = str(Path(__file__).resolve().parent / "fake_depotdownloader.py")


def _wait(app, cond, timeout=15.0):
    end = time.monotonic() + timeout
    while not cond():
        app.processEvents()
        if time.monotonic() > end:
            raise AssertionError("timeout")
        time.sleep(0.01)


def test_queue() -> None:
    root, *_ = build_fake_steam(Path(tempfile.mkdtemp()))
    from steam_downgrader.gui.downloads import DONE, FAILED, DownloadManager
    from steam_downgrader.history import History
    from steam_downgrader.staging import check_staged, find_staged
    from steam_downgrader.state import State

    app = QApplication.instance() or QApplication([])
    state = State()
    state.set_setting("dd_path", FAKE)
    state.set_setting("dd_username", "gaben")
    mgr = DownloadManager(state)
    asked: list[str] = []

    def asker(kind, job, prompt):
        asked.append(kind)
        return {"password": "hunter2", "code": "12345"}[kind]

    mgr.asker = asker
    GOOD, BAD = "5555555555555555555", "999"
    assert mgr.enqueue(APP, DEPOT, GOOD, "Fake Game")
    assert mgr.enqueue(APP, DEPOT, BAD, "Fake Game")
    assert not mgr.enqueue(APP, DEPOT, GOOD, "Fake Game"), "duplicates are ignored"
    _wait(app, lambda: not mgr.pending())

    good, bad = mgr.jobs
    assert good.status == DONE and good.pct == 100.0, (good.status, good.log)
    assert bad.status == FAILED and "Unable to download manifest 999" in bad.error, bad.error
    # Password asked once per session, the 2FA code per login.
    assert asked == ["password", "code", "code"], asked
    assert not any("hunter2" in line for j in mgr.jobs for line in j.log), "password must not be logged"

    staged = {s.manifest: s for s in find_staged(FakeSteam(root), History(FakeSteam(root), state), APP)}
    assert staged[GOOD].complete and check_staged(FakeSteam(root), staged[GOOD]).ok
    assert not staged[BAD].complete

    # Cancelling a queued job never starts it.
    mgr.enqueue(APP, DEPOT, "7777777777777777777", "Fake Game")
    mgr.cancel(mgr.jobs[-1])
    _wait(app, lambda: not mgr.pending())
    assert mgr.jobs[-1].status == "отменено"
    print("downloads ok")


def test_gui_background_download_and_apply() -> None:
    root, game_dir, *_ = build_fake_steam(Path(tempfile.mkdtemp()))
    import steam_downgrader.gui.main_window as mw
    from PySide6.QtWidgets import QInputDialog, QMessageBox
    from steam_downgrader.state import State

    mw.fetch_patch_notes = lambda app, name: []
    mw.notify = lambda *a: None
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No if "Запустить Steam" in str(a) else QMessageBox.Yes)
    QInputDialog.getText = staticmethod(lambda *a, **k: ("12345", True))
    app = QApplication.instance() or QApplication([])
    GOOD = "5555555555555555555"
    st = State()
    st.set_setting("backend", "depotdownloader")
    st.set_setting("dd_path", FAKE)
    st.add_steamdb(DEPOT, GOOD, 1650000000)
    st.save()

    w = mw.MainWindow(FakeSteam(root), State())
    w.refresh()
    row = next(i for i in range(w.versions.topLevelItemCount())
               if w.versions.topLevelItem(i).data(0, mw.ROLE_VERSION).depots[DEPOT] == GOOD)
    w.versions.setCurrentItem(w.versions.topLevelItem(row))
    w._download()
    assert w.dl.pending(), "queued, no modal dialog"
    _wait(app, lambda: not w.dl.pending())
    app.processEvents()
    assert w.versions.topLevelItem(row).text(4) == "загружена, можно применять", w.versions.topLevelItem(row).text(4)
    assert w.depots.item(0, 3).text() == "загружено ✓"
    assert w.dl_window is not None, "QR/log window opened for the QR login"

    w._apply()
    _wait(app, lambda: w.worker is None)
    assert (game_dir / "bin/game.exe").read_bytes() == b"x"
    assert not (game_dir / ".DepotDownloader").exists(), "DepotDownloader's metadata must not land in the game"
    assert State().lock_for(APP)["actual_depots"] == {DEPOT: GOOD}
    print("gui downloads ok")



def test_native_login_resume_and_delta_apply() -> None:
    root, game_dir, *_ = build_fake_steam(Path(tempfile.mkdtemp()))
    import steam_downgrader.gui.main_window as mw
    from PySide6.QtWidgets import QDialog, QMessageBox
    from steam_downgrader import auth
    from steam_downgrader.gui import login_dialog
    from steam_downgrader.state import State

    os.environ["SD_NATIVE_WORKER"] = str(Path(__file__).resolve().parent / "fake_native_worker.py")
    mw.fetch_patch_notes = lambda app, name: []
    mw.notify = lambda *a: None
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No if "Запустить Steam" in str(a) else QMessageBox.Yes)
    logins: list[str] = []

    def fake_exec(self):
        logins.append(self.windowTitle())
        auth.save_token("gaben", "x.eyJleHAiOjk5OTk5OTk5OTl9.y")  # exp far away
        self.account = "gaben"
        return QDialog.Accepted

    login_dialog.LoginDialog.exec = fake_exec
    mw.LoginDialog.exec = fake_exec
    app = QApplication.instance() or QApplication([])
    GOOD = "6666666666666666666"
    st = State()
    st.set_setting("backend", "native")
    st.add_steamdb(DEPOT, GOOD, 1650000000)
    st.save()
    # A stale token: the download starts without asking, the worker rejects it.
    auth.save_token("gaben", "x.eyJleHAiOjk5OTk5OTk5OTl9.y")

    w = mw.MainWindow(FakeSteam(root), State())
    w.refresh()
    row = next(i for i in range(w.versions.topLevelItemCount())
               if w.versions.topLevelItem(i).data(0, mw.ROLE_VERSION).depots[DEPOT] == GOOD)
    w.versions.setCurrentItem(w.versions.topLevelItem(row))
    auth.clear_token()  # expires between the click and the worker start
    auth.save_token("gaben", "x.eyJleHAiOjk5OTk5OTk5OTl9.y")
    Path(auth._token_path()).write_text("{}")  # now unreadable -> worker says auth_required
    w._download()  # no token -> login first
    assert logins == ["Вход в Steam"], logins
    _wait(app, lambda: not w.dl.pending())
    job = w.dl.jobs[-1]
    assert job.status == "готово", (job.status, job.error, job.log)
    assert any("worker diagnostics" in line for line in job.log), "stderr goes to the job log"
    assert "--base-manifest" in job.extra and "2222222222222222222" in job.extra
    app.processEvents()
    assert w.versions.topLevelItem(row).text(4) == "загружена, можно применять"

    w._apply()
    _wait(app, lambda: w.worker is None)
    assert (game_dir / "bin/game.exe").read_bytes() == b"native-exe"
    assert (game_dir / "data/a.pak").read_bytes() == b"new-a", "unchanged file left in place"
    assert not (game_dir / "data/b_new.pak").exists(), "files of the newer build are still removed"
    print("native ok")


def test_native_auth_pause() -> None:
    """Token rejected mid-queue: queue pauses, login, resumes."""
    build_fake_steam(Path(tempfile.mkdtemp()))
    from steam_downgrader import auth
    from steam_downgrader.gui.downloads import DONE, DownloadManager
    from steam_downgrader.state import State

    os.environ["SD_NATIVE_WORKER"] = str(Path(__file__).resolve().parent / "fake_native_worker.py")
    app = QApplication.instance() or QApplication([])
    mgr = DownloadManager(State())
    asked: list[str] = []
    mgr.login_needed.connect(lambda r: (asked.append(r), auth.save_token("gaben", "x.eyJleHAiOjk5OTk5OTk5OTl9.y"), mgr.resume()))
    mgr.enqueue(APP, DEPOT, "1", "Fake", backend="native")
    mgr.enqueue(APP, DEPOT, "2", "Fake", backend="native")
    _wait(app, lambda: not mgr.pending())
    assert len(asked) == 1 and [j.status for j in mgr.jobs] == [DONE, DONE], ([j.status for j in mgr.jobs], asked)
    print("auth pause ok")


if __name__ == "__main__":
    test_queue()
    test_gui_background_download_and_apply()
    test_native_login_resume_and_delta_apply()
    test_native_auth_pause()
