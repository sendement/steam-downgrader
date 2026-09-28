from __future__ import annotations

import sys
import time

from PySide6.QtCore import QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..appinfo import AppInfoCache
from ..downloader import find_depotdownloader
from ..history import (
    Candidate,
    History,
    Version,
    fmt_time,
    steamdb_manifests_url,
    steamdb_patchnotes_url,
)
from ..ops import OpError, actual_depots, apply_depot, lock, relock, unlock
from ..staging import Staged, check_staged, find_staged
from ..state import State
from ..steam import Game, Steam, is_tool
from .download_dialog import ConsoleDownloadDialog, DepotDownloaderDialog
from .settings_dialog import SettingsDialog
from .steamdb_dialog import SteamDBImportDialog
from .worker import Worker

ROLE_GID = Qt.UserRole
ROLE_VERSION = Qt.UserRole + 1


class MainWindow(QMainWindow):
    def __init__(self, steam: Steam, state: State):
        super().__init__()
        self.steam = steam
        self.state = state
        self.info_cache: AppInfoCache | None = None
        self.history: History | None = None
        self.games: list[Game] = []
        self.game: Game | None = None
        self.actual: dict[str, str] = {}
        self.cands: dict[str, list[Candidate]] = {}
        self.staged: dict[tuple[str, str], tuple[Staged, bool]] = {}  # (depot, gid) -> (staged, verified ok)
        self.worker: Worker | None = None
        self._dialogs: list = []
        self._refreshing = False

        self.setWindowTitle("Steam Downgrader")
        self.resize(1280, 820)
        self._build_ui()
        self._steam_timer = QTimer(self, interval=3000, timeout=self._update_steam_status)
        self._steam_timer.start()
        QTimer.singleShot(0, self.refresh)

    # --- UI construction -------------------------------------------------------

    def _build_ui(self) -> None:
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)
        tb.addAction("⟳ Обновить", self.refresh)
        tb.addSeparator()
        self.steam_label = QLabel()
        tb.addWidget(self.steam_label)
        self.steam_btn = QPushButton()
        self.steam_btn.clicked.connect(self._toggle_steam)
        tb.addWidget(self.steam_btn)
        tb.addSeparator()
        self.autorelock_cb = QCheckBox("Авто-relock при выходе новых сборок")
        self.autorelock_cb.setToolTip(
            "systemd-служба: когда Steam узнаёт о новой сборке (обновляется appinfo.vdf),\n"
            "подмена в appmanifest у защищённых игр подтягивается к ней."
        )
        if sys.platform.startswith("linux"):
            from ..cli import relock_units_installed

            self.autorelock_cb.setChecked(relock_units_installed())
            self.autorelock_cb.toggled.connect(self._toggle_autorelock)
            tb.addWidget(self.autorelock_cb)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        tb.addAction("⚙ Настройки", self._settings)

        split = QSplitter(Qt.Horizontal)
        self.setCentralWidget(split)

        # left: games
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(4, 4, 4, 4)
        self.search = QLineEdit(placeholderText="Поиск…")
        self.search.textChanged.connect(self._filter_games)
        ll.addWidget(self.search)
        self.game_list = QListWidget()
        self.game_list.setIconSize(QSize(92, 43))
        self.game_list.currentItemChanged.connect(self._on_game_selected)
        ll.addWidget(self.game_list)
        split.addWidget(left)

        # right: details
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(4, 4, 4, 4)

        head = QHBoxLayout()
        self.header_img = QLabel()
        self.header_img.setFixedSize(230, 107)
        self.header_img.setScaledContents(True)
        head.addWidget(self.header_img)
        info_col = QVBoxLayout()
        self.title = QLabel()
        self.title.setStyleSheet("font-size: 18pt; font-weight: 600")
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info_col.addWidget(self.title)
        self.subtitle = QLabel()
        self.subtitle.setTextInteractionFlags(Qt.TextSelectableByMouse)
        info_col.addWidget(self.subtitle)
        self.lock_label = QLabel()
        self.lock_label.setWordWrap(True)
        info_col.addWidget(self.lock_label)
        info_col.addStretch()
        head.addLayout(info_col, 1)
        rl.addLayout(head)

        vsplit = QSplitter(Qt.Vertical)

        vbox = QGroupBox("Версии")
        vl = QVBoxLayout(vbox)
        self.versions = QTreeWidget()
        self.versions.setHeaderLabels(["Дата сборки", "Версия", "Источник", "Состояние"])
        self.versions.setRootIsDecorated(False)
        self.versions.setAlternatingRowColors(True)
        self.versions.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.versions.itemSelectionChanged.connect(self._on_version_selected)
        vl.addWidget(self.versions)
        links = QHBoxLayout()
        hint = QLabel(
            "Нет нужной версии? Импортируйте список со SteamDB или впишите ID манифеста в колонку «Целевой манифест»."
        )
        hint.setStyleSheet("color: gray")
        hint.setWordWrap(True)
        links.addWidget(hint, 1)
        imp = QPushButton("Импорт версий со SteamDB")
        imp.clicked.connect(lambda: self._steamdb_import(open_page=True))
        links.addWidget(imp)
        pn = QPushButton("Патчноуты на SteamDB")
        pn.clicked.connect(lambda: self.game and QDesktopServices.openUrl(QUrl(steamdb_patchnotes_url(self.game.app_id))))
        links.addWidget(pn)
        vl.addLayout(links)
        vsplit.addWidget(vbox)

        dbox = QGroupBox("Депо")
        dl = QVBoxLayout(dbox)
        self.depots = QTableWidget(0, 5)
        self.depots.setHorizontalHeaderLabels(["Депо", "Установлено", "Целевой манифест", "Загрузка", ""])
        self.depots.verticalHeader().hide()
        self.depots.setSelectionMode(QAbstractItemView.NoSelection)
        hh = self.depots.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        dl.addWidget(self.depots)

        opts = QHBoxLayout()
        self.cb_strong = QCheckBox("Сильная защита (файлы игры только для чтения)")
        self.cb_strong.setToolTip(
            "Даже если Steam всё-таки начнёт обновление, оно упадёт с ошибкой записи,\n"
            "а не перезапишет старую версию. Игры, которые пишут конфиги/логи в свою папку,\n"
            "могут на это ругаться — тогда оставьте выключенным."
        )
        self.cb_delete = QCheckBox("Удалять файлы, которых нет в старой версии")
        self.cb_delete.setChecked(True)
        self.cb_delete.setToolTip("Удаляются только файлы из манифеста текущей версии этого депо — сейвы и конфиги не трогаются.")
        self.cb_move = QCheckBox("Переносить загрузку, а не копировать")
        self.cb_move.setChecked(True)
        self.cb_move.setToolTip("Быстро и не занимает место дважды; загруженная копия после применения исчезает.")
        for cb in (self.cb_strong, self.cb_delete, self.cb_move):
            opts.addWidget(cb)
        opts.addStretch()
        dl.addLayout(opts)

        acts = QHBoxLayout()
        self.btn_download = QPushButton("⬇ Скачать выбранную версию")
        self.btn_download.clicked.connect(self._download)
        acts.addWidget(self.btn_download)
        self.btn_apply = QPushButton("⏪ Откатить и защитить")
        self.btn_apply.setStyleSheet("font-weight: 600")
        self.btn_apply.clicked.connect(self._apply)
        acts.addWidget(self.btn_apply)
        acts.addStretch()
        self.btn_lock = QPushButton("🔒 Защитить текущую версию")
        self.btn_lock.clicked.connect(self._lock_only)
        acts.addWidget(self.btn_lock)
        self.btn_unlock = QToolButton()
        self.btn_unlock.setText("🔓 Снять защиту")
        self.btn_unlock.setPopupMode(QToolButton.InstantPopup)
        m = QMenu(self.btn_unlock)
        m.addAction("…и вернуть актуальную версию (проверка файлов Steam)", lambda: self._unlock(True))
        m.addAction("…оставив файлы как есть (Steam обновит игру сам)", lambda: self._unlock(False))
        self.btn_unlock.setMenu(m)
        acts.addWidget(self.btn_unlock)
        dl.addLayout(acts)
        vsplit.addWidget(dbox)

        self.log = QPlainTextEdit(readOnly=True)
        self.log.setMaximumBlockCount(2000)
        vsplit.addWidget(self.log)
        vsplit.setSizes([300, 300, 120])
        rl.addWidget(vsplit, 1)
        split.addWidget(right)
        split.setSizes([330, 950])

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(360)
        self.progress.hide()
        self.status_msg = QLabel()
        self.statusBar().addWidget(self.status_msg, 1)
        self.statusBar().addPermanentWidget(self.progress)
        self._set_detail_enabled(False)

    # --- helpers ---------------------------------------------------------------

    def _log(self, msg: str) -> None:
        self.log.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def _set_detail_enabled(self, on: bool) -> None:
        for w in (self.btn_download, self.btn_apply, self.btn_lock, self.btn_unlock, self.versions, self.depots):
            w.setEnabled(on)

    def _busy(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def _run(self, title: str, fn, *args, on_done=None, **kwargs) -> None:
        self._set_detail_enabled(False)
        self.game_list.setEnabled(False)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.status_msg.setText(title)
        self._log(title)
        w = Worker(fn, *args, with_progress="progress" in fn.__code__.co_varnames, parent=self, **kwargs)
        w.progress.connect(self._on_progress)

        def finish(result=None, err: str | None = None):
            self.progress.hide()
            self.game_list.setEnabled(True)
            self.status_msg.setText("")
            self.worker = None
            if err:
                self._log(f"Ошибка: {err}")
                QMessageBox.critical(self, "Ошибка", err)
            elif on_done:
                on_done(result)
            self.refresh()

        w.done.connect(lambda r: finish(r))
        w.failed.connect(lambda e: finish(err=e))
        self.worker = w
        w.start()

    def _on_progress(self, msg: str, done: int, total: int) -> None:
        self.status_msg.setText(msg)
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        else:
            self.progress.setRange(0, 0)

    # --- steam client ------------------------------------------------------------

    def _update_steam_status(self) -> None:
        running = self.steam.is_running()
        self.steam_label.setText("  Steam: 🟢 запущен  " if running else "  Steam: ⚪ закрыт  ")
        self.steam_btn.setText("Закрыть Steam" if running else "Запустить Steam")
        self.steam_btn.setEnabled(not self._busy())

    def _toggle_steam(self) -> None:
        if self.steam.is_running():
            self._run("Закрываю Steam…", self._shutdown_steam)
        else:
            self.steam.start()
            self._log("Steam запускается")
        QTimer.singleShot(1500, self._update_steam_status)

    def _shutdown_steam(self) -> None:
        if not self.steam.shutdown():
            raise OpError("Steam не закрылся за 30 секунд.")

    def _toggle_autorelock(self, on: bool) -> None:
        from ..cli import install_relock_units, remove_relock_units

        try:
            if on:
                install_relock_units(self.steam)
                self._log("Служба авто-relock установлена (systemctl --user steam-downgrader-relock.path)")
            else:
                remove_relock_units()
                self._log("Служба авто-relock удалена")
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "systemd", str(e))

    def _settings(self) -> None:
        SettingsDialog(self.state, self).exec()

    # --- refresh -----------------------------------------------------------------

    def refresh(self) -> None:
        if self._busy():
            return
        self.state.load()
        try:
            self.info_cache = AppInfoCache(self.steam.appinfo_path)
        except (OSError, ValueError) as e:
            self.info_cache = None
            self._log(f"appinfo.vdf не прочитан: {e}")
        self.history = History(self.steam, self.state)
        self.games = [g for g in self.steam.games() if not is_tool(g)]

        # Remember real installs we see, so builds stay rollback targets later.
        dirty = False
        for g in self.games:
            if g.state_flags == 4 and not self.state.lock_for(g.app_id) and g.depots:
                dirty |= self.state.record_snapshot(g.app_id, g.buildid, {d: x.manifest for d, x in g.depots.items()})
        if dirty:
            self.state.save()

        if self.info_cache and self.state.locks:
            try:
                for name, old, new in relock(self.steam, self.state, self.info_cache):
                    self._log(f"{name}: вышла сборка {new}, подмена обновлена ({old} → {new})")
                    if self.steam.is_running():
                        self._log("  Перезапустите Steam, чтобы он не пытался обновить игру.")
            except OSError as e:
                self._log(f"relock: {e}")
            self.games = [g for g in self.steam.games() if not is_tool(g)]

        current = self.game.app_id if self.game else None
        self._refreshing = True
        self.game_list.blockSignals(True)
        self.game_list.clear()
        for g in self.games:
            locked = bool(self.state.lock_for(g.app_id))
            it = QListWidgetItem(("🔒 " if locked else "") + g.name)
            it.setData(Qt.UserRole, g.app_id)
            img = self.steam.library_image(g.app_id)
            pm = QPixmap(str(img)) if img else QPixmap()
            if pm.isNull():
                pm = QPixmap(92, 43)
                pm.fill(QColor(90, 90, 100))
            it.setIcon(QIcon(pm.scaled(92, 43, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)))
            self.game_list.addItem(it)
        self.game_list.blockSignals(False)
        self._filter_games(self.search.text())
        self._update_steam_status()

        for i in range(self.game_list.count()):
            if self.game_list.item(i).data(Qt.UserRole) == current:
                self.game_list.setCurrentRow(i)
                break
        else:
            if self.game_list.count() and current is None:
                self.game_list.setCurrentRow(0)
        self._refreshing = False

    def _filter_games(self, text: str) -> None:
        t = text.lower().strip()
        for i in range(self.game_list.count()):
            it = self.game_list.item(i)
            it.setHidden(bool(t) and t not in it.text().lower() and t != it.data(Qt.UserRole))

    # --- game view ---------------------------------------------------------------

    def _on_game_selected(self, cur: QListWidgetItem | None, _prev=None) -> None:
        if cur is None:
            return
        app_id = cur.data(Qt.UserRole)
        self.game = next((g for g in self.games if g.app_id == app_id), None)
        if self.game:
            self._show_game(self.game)
            # First time the user opens a game: offer its SteamDB history.
            g = self.game
            if not self._refreshing and not self.state.steamdb_prompted(g.app_id) and not any(
                self.state.steamdb(d) for d in g.depots
            ):
                self.state.mark_steamdb_prompted(g.app_id)
                self.state.save()
                QTimer.singleShot(0, lambda: self._steamdb_import(open_page=True))

    def _show_game(self, g: Game) -> None:
        info = self.info_cache.get(g.app_id) if self.info_cache else None
        self.actual = actual_depots(self.state, g)
        self.cands = self.history.candidates(g, info, self.actual)
        versions = self.history.versions(g, info, self.cands)
        lk = self.state.lock_for(g.app_id)

        self.staged = {}
        for st in find_staged(self.steam, self.history, g.app_id):
            if st.manifest:
                self.staged[(st.depot_id, st.manifest)] = (st, check_staged(self.steam, st).ok and st.complete)

        img = self.steam.library_image(g.app_id)
        self.header_img.setPixmap(QPixmap(str(img)) if img else QPixmap())
        self.title.setText(g.name)
        latest = info.branches.get(g.branch) if info else None
        real_build = lk.get("actual_buildid") if lk else g.buildid
        parts = [f"AppID {g.app_id}", f"ветка {g.branch}", f"установлена сборка {real_build or 'неизвестна (откат)'}"]
        if latest:
            parts.append(f"актуальная {latest.buildid} от {fmt_time(latest.time_updated)}")
        self.subtitle.setText(" · ".join(parts) + f"\n{g.install_dir}")
        if lk:
            kind = "сильная (файлы только для чтения)" if lk.get("strong") else "обычная"
            self.lock_label.setText(
                f"🔒 <b>Защищено от обновлений</b> с {fmt_time(lk['locked_at'])} — {kind}. "
                f"Steam видит сборку {lk.get('spoofed_buildid')}."
            )
            self.lock_label.setStyleSheet("color: #3a9d5d")
        else:
            self.lock_label.setText("Не защищено — Steam обновит игру при выходе новой сборки.")
            self.lock_label.setStyleSheet("color: gray")

        # versions
        self.versions.blockSignals(True)
        self.versions.clear()
        installed_item = None
        for v in versions:
            state = self._version_state(v)
            src = v.source + ("" if v.exact else " (восстановлено)")
            title = v.title + (f"  [build {v.buildid}]" if v.buildid and str(v.buildid) not in v.title else "")
            it = QTreeWidgetItem([fmt_time(v.time), title, src, state])
            it.setData(0, ROLE_VERSION, v)
            if state.startswith("установлена"):
                f = it.font(1)
                f.setBold(True)
                for c in range(4):
                    it.setFont(c, f)
                installed_item = it
            if not v.exact:
                it.setToolTip(2, "Состав депо восстановлен по датам манифестов в depotcache — проверьте перед применением.")
            self.versions.addTopLevelItem(it)
        for c in (0, 2, 3):
            self.versions.resizeColumnToContents(c)
        self.versions.blockSignals(False)

        # depots
        self.depots.setRowCount(len(g.depots))
        for r, (d, dep) in enumerate(g.depots.items()):
            di = info.depots.get(d) if info else None
            label = d
            extra = []
            if di and di.name:
                extra.append(di.name)
            if dep.dlc_app_id:
                extra.append(f"DLC {dep.dlc_app_id}")
            if di and di.language:
                extra.append(di.language)
            if extra:
                label += "  (" + ", ".join(extra) + ")"
            it = QTableWidgetItem(label)
            it.setData(Qt.UserRole, d)
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            self.depots.setItem(r, 0, it)

            cur = self.actual.get(d, "")
            cur_c = next((c for c in self.cands.get(d, []) if c.manifest == cur), None)
            cit = QTableWidgetItem(cur + (f"\n{fmt_time(cur_c.created)}" if cur_c and cur_c.created else ""))
            cit.setFlags(cit.flags() & ~Qt.ItemIsEditable)
            self.depots.setItem(r, 1, cit)

            combo = QComboBox()
            combo.setEditable(True)
            combo.setInsertPolicy(QComboBox.NoInsert)
            for c in self.cands.get(d, []):
                bits = [c.manifest]
                if c.created:
                    bits.append(fmt_time(c.created))
                if c.buildid:
                    bits.append(f"build {c.buildid}")
                if c.label:
                    bits.append(c.label)
                if c.manifest == cur:
                    bits.append("установлен")
                combo.addItem("  ·  ".join(bits), c.manifest)
            combo.setCurrentIndex(max(0, combo.findData(cur)))
            combo.currentTextChanged.connect(lambda _t, row=r: self._update_depot_status(row))
            self.depots.setCellWidget(r, 2, combo)
            self.depots.setItem(r, 3, QTableWidgetItem())
            db = QPushButton("SteamDB")
            db.setToolTip("История манифестов этого депо на SteamDB")
            db.clicked.connect(lambda _=False, dd=d: QDesktopServices.openUrl(QUrl(steamdb_manifests_url(dd))))
            self.depots.setCellWidget(r, 4, db)
            self._update_depot_status(r)
        self.depots.resizeRowsToContents()

        self._set_detail_enabled(True)
        self.btn_unlock.setEnabled(bool(lk))
        self.btn_lock.setText("🔒 Обновить защиту" if lk else "🔒 Защитить текущую версию")
        self.cb_strong.setChecked(bool(lk and lk.get("strong")))
        if installed_item:
            self.versions.setCurrentItem(installed_item)

    def _steamdb_import(self, open_page: bool) -> None:
        g = self.game
        if not g or self._busy():
            return
        dlg = SteamDBImportDialog(self.state, g, self.info_cache.get(g.app_id) if self.info_cache else None, self)
        dlg.imported.connect(lambda: self._log(f"{g.name}: версии со SteamDB импортированы"))
        dlg.finished.connect(lambda _r: self.refresh())
        if open_page:
            dlg.open_steamdb()
        dlg.show()
        self._dialogs.append(dlg)

    def _version_state(self, v: Version) -> str:
        diff = {d: m for d, m in v.depots.items() if m and m != self.actual.get(d)}
        if not diff:
            return "установлена ✓"
        if all(self.staged.get((d, m), (None, False))[1] for d, m in diff.items()):
            return "загружена, можно применять"
        return ""

    def _on_version_selected(self) -> None:
        items = self.versions.selectedItems()
        if not items:
            return
        v: Version = items[0].data(0, ROLE_VERSION)
        for r in range(self.depots.rowCount()):
            d = self.depots.item(r, 0).data(Qt.UserRole)
            combo: QComboBox = self.depots.cellWidget(r, 2)
            gid = v.depots.get(d) or self.actual.get(d, "")
            idx = combo.findData(gid)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            else:
                combo.setEditText(gid)

    def _target(self, row: int) -> str:
        combo: QComboBox = self.depots.cellWidget(row, 2)
        idx = combo.currentIndex()
        if idx >= 0 and combo.itemText(idx) == combo.currentText():
            return combo.itemData(idx)
        return "".join(ch for ch in combo.currentText().split("·")[0] if ch.isdigit())

    def _targets(self) -> dict[str, str]:
        return {self.depots.item(r, 0).data(Qt.UserRole): self._target(r) for r in range(self.depots.rowCount())}

    def _update_depot_status(self, row: int) -> None:
        d = self.depots.item(row, 0).data(Qt.UserRole)
        gid = self._target(row)
        item = self.depots.item(row, 3)
        if not gid:
            item.setText("⚠ введите ID манифеста")
        elif gid == self.actual.get(d):
            item.setText("без изменений")
        elif (d, gid) in self.staged:
            item.setText("загружено ✓" if self.staged[(d, gid)][1] else "загружено не полностью ⚠")
        else:
            item.setText("нужно скачать")

    def _changes(self) -> dict[str, str] | None:
        t = self._targets()
        bad = [d for d, g in t.items() if not g]
        if bad:
            QMessageBox.warning(self, "Манифест", f"Не указан манифест для депо {', '.join(bad)}.")
            return None
        # Persist hand-typed ids so they show up next time.
        known = {(d, c.manifest) for d, cs in self.cands.items() for c in cs}
        new = [(d, g) for d, g in t.items() if (d, g) not in known]
        for d, g in new:
            self.state.add_manual(d, g)
        if new:
            self.state.save()
        return {d: g for d, g in t.items() if g != self.actual.get(d)}

    # --- actions -------------------------------------------------------------------

    def _download(self) -> None:
        g = self.game
        changes = self._changes()
        if changes is None:
            return
        jobs = [(g.app_id, d, m) for d, m in changes.items() if not self.staged.get((d, m), (None, False))[1]]
        if not jobs:
            QMessageBox.information(self, "Загрузка", "Всё нужное уже загружено (или выбрана установленная версия).")
            return

        if self.state.setting("backend") == "depotdownloader":
            exe = find_depotdownloader(self.state.setting("dd_path"))
            if not exe:
                QMessageBox.warning(self, "DepotDownloader", "DepotDownloader не найден — укажите путь или скачайте его в настройках.")
                self._settings()
                return
            user = self.state.setting("dd_username", "")
            password = ""
            if user:
                password, ok = QInputDialog.getText(
                    self, "Вход в Steam",
                    f"Пароль для {user}.\nОставьте пустым, если уже входили через эту программу (токен сохранён).",
                    QLineEdit.Password,
                )
                if not ok:
                    return
            dlg = DepotDownloaderDialog(exe, jobs, user, qr=not user, password=password, parent=self)
            dlg.finished.connect(lambda _r: self.refresh())
            dlg.show()
        else:
            if not self.steam.is_running():
                if QMessageBox.question(self, "Steam", "Для download_depot нужен запущенный Steam. Запустить?") == QMessageBox.Yes:
                    self.steam.start()
            dlg = ConsoleDownloadDialog(self.steam, jobs, self)
            dlg.finished_all.connect(lambda: self._log("download_depot: все депо загружены"))
            dlg.finished.connect(lambda _r: self.refresh())
            dlg.show()
        self._dialogs.append(dlg)

    def _selected_buildid(self, changes: dict[str, str]) -> int | None:
        items = self.versions.selectedItems()
        if not items:
            return None
        v: Version = items[0].data(0, ROLE_VERSION)
        target = {**self.actual, **changes}
        if v.buildid and v.exact and all(target.get(d) == m for d, m in v.depots.items() if m):
            return v.buildid
        return None

    def _apply(self) -> None:
        g = self.game
        changes = self._changes()
        if changes is None:
            return
        if not changes:
            QMessageBox.information(self, "Откат", "Выбрана установленная версия. Выберите старую версию в списке или впишите манифесты.")
            return
        missing = [d for d, m in changes.items() if not self.staged.get((d, m), (None, False))[1]]
        if missing:
            QMessageBox.warning(
                self, "Откат",
                "Сначала скачайте выбранную версию — не загружены депо: " + ", ".join(missing),
            )
            return
        strong = self.cb_strong.isChecked()
        lines = [f"• депо {d}: {self.actual.get(d)} → {m}" for d, m in changes.items()]
        msg = (
            f"<b>{g.name}</b> будет откачена:<br>" + "<br>".join(lines) + "<br><br>"
            + ("Лишние файлы новой версии будут удалены.<br>" if self.cb_delete.isChecked() else "")
            + ("Загруженные файлы будут перенесены в папку игры.<br>" if self.cb_move.isChecked() else "")
            + f"Защита: {'сильная' if strong else 'обычная'}.<br>"
            + ("<br><b>Steam будет закрыт.</b>" if self.steam.is_running() else "")
        )
        if QMessageBox.question(self, "Откатить?", msg) != QMessageBox.Yes:
            return

        stageds = [self.staged[(d, m)][0] for d, m in changes.items()]
        buildid = self._selected_buildid(changes)
        move, delete = self.cb_move.isChecked(), self.cb_delete.isChecked()
        steam, state, cache, app_id = self.steam, self.state, self.info_cache, g.app_id

        def job(progress):
            if steam.is_running():
                progress("Закрываю Steam…", 0, 0)
                if not steam.shutdown():
                    raise OpError("Steam не закрылся за 30 секунд.")
            results = []
            for st in stageds:
                results.append(apply_depot(steam, state, steam.game(app_id), st, move=move, delete_extra=delete, progress=progress))
                # Lock after every depot: if a later one fails, the state still
                # knows what is really on disk and Steam can't "fix" it meanwhile.
                progress("Включаю защиту…", 0, 0)
                lock(steam, state, cache, steam.game(app_id), real_depots={st.depot_id: st.manifest}, strong=strong)
            lock(steam, state, cache, steam.game(app_id), real_depots=changes, real_buildid=buildid, strong=strong)
            return results

        def done(results):
            for r in results:
                self._log(f"депо {r.depot_id} → {r.manifest}: {r.files_moved} файлов записано, {r.files_deleted} удалено")
            self._log(f"{g.name}: откат выполнен, защита включена")
            if QMessageBox.question(self, "Готово", "Откат выполнен и защищён от обновлений.\nЗапустить Steam?") == QMessageBox.Yes:
                self.steam.start()

        self._run(f"Откат {g.name}…", job, on_done=done)

    def _lock_only(self) -> None:
        g = self.game
        if not self.info_cache:
            return
        strong = self.cb_strong.isChecked()
        if not self.state.lock_for(g.app_id):
            if QMessageBox.question(
                self, "Защита",
                f"Заморозить {g.name} на установленной версии и не давать Steam её обновлять?"
                + ("\n\nSteam будет закрыт." if self.steam.is_running() else ""),
            ) != QMessageBox.Yes:
                return
        steam, state, cache, app_id = self.steam, self.state, self.info_cache, g.app_id

        def job(progress):
            if steam.is_running():
                progress("Закрываю Steam…", 0, 0)
                if not steam.shutdown():
                    raise OpError("Steam не закрылся за 30 секунд.")
            lock(steam, state, cache, steam.game(app_id), strong=strong)

        self._run(f"Защита {g.name}…", job, on_done=lambda _r: self._log(f"{g.name}: защита включена"))

    def _unlock(self, validate: bool) -> None:
        g = self.game
        what = "вернёт актуальную версию (проверка файлов)" if validate else "обновит игру при следующем запуске"
        if QMessageBox.question(
            self, "Снять защиту",
            f"Снять защиту с {g.name}? Steam {what}."
            + ("\n\nSteam будет закрыт." if self.steam.is_running() else ""),
        ) != QMessageBox.Yes:
            return
        steam, state, app_id = self.steam, self.state, g.app_id

        def job(progress):
            if steam.is_running():
                progress("Закрываю Steam…", 0, 0)
                if not steam.shutdown():
                    raise OpError("Steam не закрылся за 30 секунд.")
            unlock(steam, state, steam.game(app_id), validate=validate)

        self._run(f"Снятие защиты {g.name}…", job, on_done=lambda _r: self._log(f"{g.name}: защита снята"))
