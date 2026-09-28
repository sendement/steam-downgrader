"""Download via the Steam console: hands out download_depot commands and watches the log.
(DepotDownloader runs in the background, see downloads.py.)"""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..downloader import console_command
from ..manifest import read_manifest
from ..history import depotcache_manifest
from ..steam import Steam, parse_download_log_line

Job = tuple[str, str, str]  # app, depot, manifest


def _human(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return f"{n:.1f} {unit}" if unit != "Б" else f"{int(n)} Б"
        n /= 1024
    return str(n)


def _dir_size(p: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


class ConsoleDownloadDialog(QDialog):
    """Hands the user download_depot commands and watches console_log."""

    finished_all = Signal()

    def __init__(self, steam: Steam, jobs: list[Job], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Загрузка через консоль Steam")
        self.resize(820, 360)
        self.steam = steam
        self.jobs = jobs
        self._done: set[tuple[str, str]] = set()

        lay = QVBoxLayout(self)
        intro = QLabel(
            "1. Нажмите «Открыть консоль Steam» (Steam должен быть запущен).\n"
            "2. Скопируйте команду кнопкой «Копировать» и вставьте в консоль, Enter.\n"
            "   Steam качает депо в фоне, без индикатора — прогресс виден здесь.\n"
            "3. Когда все строки станут «готово», закройте окно и примените откат."
        )
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self.table = QTableWidget(len(jobs), 4)
        self.table.setHorizontalHeaderLabels(["Команда", "", "Скачано", "Статус"])
        self.table.verticalHeader().hide()
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        mono = QFont("monospace")
        mono.setStyleHint(QFont.Monospace)
        self.bars: list[QProgressBar] = []
        for r, (app, depot, man) in enumerate(jobs):
            it = QTableWidgetItem(console_command(app, depot, man))
            it.setFont(mono)
            it.setFlags(it.flags() & ~Qt.ItemIsEditable)
            self.table.setItem(r, 0, it)
            btn = QPushButton("Копировать")
            btn.clicked.connect(lambda _=False, t=it.text(): QGuiApplication.clipboard().setText(t))
            self.table.setCellWidget(r, 1, btn)
            bar = QProgressBar()
            bar.setMinimumWidth(200)
            bar.setFormat("—")
            bar.setValue(0)
            self.bars.append(bar)
            self.table.setCellWidget(r, 2, bar)
            self.table.setItem(r, 3, QTableWidgetItem("ожидание"))
        lay.addWidget(self.table)

        row = QHBoxLayout()
        open_btn = QPushButton("Открыть консоль Steam")
        open_btn.clicked.connect(lambda: self.steam.open_url("steam://open/console"))
        row.addWidget(open_btn)
        row.addStretch()
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        row.addWidget(bb)
        lay.addLayout(row)

        # The whole current log is scanned first, so a job finished before the
        # dialog opened shows as done; afterwards only new lines are read.
        self._log = self.steam.logs / "console_log.txt"
        self._offset = 0
        self._timer = QTimer(self, interval=2000, timeout=self._poll)
        self._timer.start()
        self._poll()

    def _poll(self) -> None:
        try:
            with open(self._log, "rb") as f:
                size = f.seek(0, 2)
                if size < self._offset:  # rotated
                    self._offset = 0
                f.seek(self._offset)
                chunk = f.read().decode("utf-8", "replace")
                self._offset = size
        except OSError:
            chunk = ""
        for line in chunk.splitlines():
            r = parse_download_log_line(line)
            if r:
                for app, depot, man in self.jobs:
                    if (app, depot, man) == r:
                        self._done.add((depot, man))

        for r, (app, depot, man) in enumerate(self.jobs):
            bar, status = self.bars[r], self.table.item(r, 3)
            if (depot, man) in self._done:
                bar.setRange(0, 1)
                bar.setValue(1)
                bar.setFormat("100%")
                status.setText("готово ✓")
                continue
            folder = next((c / f"app_{app}" / f"depot_{depot}" for c in self.steam.content_dirs()), None)
            got = _dir_size(folder) if folder and folder.is_dir() else 0
            mp = depotcache_manifest(self.steam, depot, man)
            total = 0
            if mp:
                try:
                    total = read_manifest(mp, with_files=False).size_original
                except (OSError, ValueError):
                    pass
            if total:
                bar.setRange(0, 1000)
                bar.setValue(min(1000, int(got * 1000 / total)))
                bar.setFormat(f"{_human(got)} / {_human(total)}")
                status.setText("загрузка…" if got else "ожидание")
            elif got:
                bar.setRange(0, 0)
                bar.setFormat(_human(got))
                status.setText("загрузка…")
        if self._done and len(self._done) == len(self.jobs):
            self._timer.stop()
            self.finished_all.emit()
