"""Paste-in import of a depot's manifest history from SteamDB."""

from __future__ import annotations

from PySide6.QtCore import QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..appinfo import AppInfo
from ..history import fmt_time, steamdb_manifests_url
from ..state import State
from ..steam import Game
from ..steamdb import Entry, parse


def main_depot(game: Game) -> str | None:
    """The depot that carries the game itself: biggest non-DLC one."""
    base = [d for d, x in game.depots.items() if not x.dlc_app_id] or list(game.depots)
    return max(base, key=lambda d: game.depots[d].size, default=None)


class SteamDBImportDialog(QDialog):
    imported = Signal()

    def __init__(self, state: State, game: Game, info: AppInfo | None, parent=None):
        super().__init__(parent)
        self.state, self.game = state, game
        self.setWindowTitle(f"Версии со SteamDB — {game.name}")
        self.resize(760, 620)
        self.entries: list[Entry] = []

        lay = QVBoxLayout(self)
        intro = QLabel(
            "На открывшейся странице SteamDB выделите таблицу манифестов (вместе с датами), "
            "скопируйте её (Ctrl+C) и вставьте сюда. Программа запомнит версии, и в следующий раз "
            "они будут в списке сразу.\n"
            "Подходят и строки из кнопок копирования SteamDB («-app … -depot … -manifest …» "
            "или «download_depot …»). Если SteamDB показывает не всю историю, войдите на сайт через Steam."
        )
        intro.setWordWrap(True)
        lay.addWidget(intro)

        row = QHBoxLayout()
        row.addWidget(QLabel("Депо:"))
        self.depot = QComboBox()
        for d, x in game.depots.items():
            di = info.depots.get(d) if info else None
            label = d
            if di and di.name:
                label += f" — {di.name}"
            if x.dlc_app_id:
                label += f" (DLC {x.dlc_app_id})"
            known = len(state.steamdb(d))
            if known:
                label += f"  · уже импортировано: {known}"
            self.depot.addItem(label, d)
        md = main_depot(game)
        if md:
            self.depot.setCurrentIndex(max(0, self.depot.findData(md)))
        self.depot.currentIndexChanged.connect(self._reparse)
        row.addWidget(self.depot, 1)
        open_btn = QPushButton("Открыть на SteamDB")
        open_btn.clicked.connect(self.open_steamdb)
        row.addWidget(open_btn)
        lay.addLayout(row)

        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("Вставьте сюда таблицу манифестов со SteamDB…")
        self.text.textChanged.connect(self._reparse)
        lay.addWidget(self.text, 1)

        self.preview = QTableWidget(0, 4)
        self.preview.setHorizontalHeaderLabels(["Депо", "Манифест", "Дата", ""])
        self.preview.verticalHeader().hide()
        self.preview.setEditTriggers(QTableWidget.NoEditTriggers)
        hh = self.preview.horizontalHeader()
        for c in range(4):
            hh.setSectionResizeMode(c, QHeaderView.Stretch if c == 1 else QHeaderView.ResizeToContents)
        lay.addWidget(self.preview, 1)

        bottom = QHBoxLayout()
        self.summary = QLabel()
        bottom.addWidget(self.summary, 1)
        self.import_btn = QPushButton("Импортировать")
        self.import_btn.setDefault(True)
        self.import_btn.clicked.connect(self._import)
        bottom.addWidget(self.import_btn)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        bottom.addWidget(close)
        lay.addLayout(bottom)
        self._reparse()

    def open_steamdb(self) -> None:
        QDesktopServices.openUrl(QUrl(steamdb_manifests_url(self.depot.currentData())))

    def _reparse(self) -> None:
        self.entries = parse(self.text.toPlainText(), self.depot.currentData())
        self.preview.setRowCount(len(self.entries))
        new = 0
        for r, e in enumerate(self.entries):
            if e.depot_id not in self.game.depots:
                status = "депо не установлено — пропуск"
            elif e.manifest in self.state.steamdb(e.depot_id):
                status = "уже есть"
            else:
                status = "новый"
                new += 1
            for c, v in enumerate([e.depot_id, e.manifest, fmt_time(e.time) if e.time else "без даты", status]):
                self.preview.setItem(r, c, QTableWidgetItem(v))
        self.summary.setText(f"Найдено манифестов: {len(self.entries)}, новых: {new}" if self.entries else "")
        self.import_btn.setEnabled(any(e.depot_id in self.game.depots for e in self.entries))

    def _import(self) -> None:
        self.state.load()
        n = 0
        for e in self.entries:
            if e.depot_id in self.game.depots:
                n += self.state.add_steamdb(e.depot_id, e.manifest, e.time)
        self.state.mark_steamdb_prompted(self.game.app_id)
        self.state.save()
        self.text.clear()
        self.summary.setText(f"Импортировано новых: {n}. Можно вставить список для другого депо.")
        self.imported.emit()
