"""Dialogs that fetch depot content: via the Steam console or DepotDownloader."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QProcess, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QGuiApplication, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..downloader import console_command, dd_args, dd_target_dir
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


class DepotDownloaderDialog(QDialog):
    """Runs DepotDownloader for each job in turn, streaming its output."""

    finished_all = Signal()

    def __init__(self, exe: Path, jobs: list[Job], username: str, qr: bool, password: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Загрузка через DepotDownloader")
        self.resize(860, 560)
        self.exe, self.jobs, self.username, self.qr, self.password = exe, list(jobs), username, qr, password
        self.idx = -1
        self.ok: list[Job] = []
        self._cancelled = False

        lay = QVBoxLayout(self)
        self.label = QLabel()
        lay.addWidget(self.label)
        self.bar = QProgressBar()
        lay.addWidget(self.bar)
        self.out = QPlainTextEdit(readOnly=True)
        mono = QFont("monospace")
        mono.setStyleHint(QFont.Monospace)
        mono.setPointSize(8)
        self.out.setFont(mono)
        self.out.setLineWrapMode(QPlainTextEdit.NoWrap)
        lay.addWidget(self.out, 1)

        row = QHBoxLayout()
        row.addWidget(QLabel("Ввод (код Steam Guard и т.п.):"))
        self.input = QLineEdit()
        self.input.returnPressed.connect(self._send)
        row.addWidget(self.input, 1)
        lay.addLayout(row)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.buttons.rejected.connect(self._cancel)
        lay.addWidget(self.buttons)

        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._finished)
        QTimer.singleShot(0, self._next)

    def _next(self) -> None:
        self.idx += 1
        if self.idx >= len(self.jobs):
            self.label.setText(f"Готово: {len(self.ok)} из {len(self.jobs)} депо.")
            self.buttons.setStandardButtons(QDialogButtonBox.Close)
            self.finished_all.emit()
            return
        app, depot, man = self.jobs[self.idx]
        self.label.setText(f"[{self.idx + 1}/{len(self.jobs)}] депо {depot}, манифест {man}")
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        args = dd_args(app, depot, man, self.username, self.qr)
        if self.password and self.idx == 0:
            args += ["-password", self.password]
        dd_target_dir(app, depot, man).mkdir(parents=True, exist_ok=True)
        self._append(f"$ DepotDownloader {' '.join(a if a != self.password else '***' for a in args)}\n")
        self.proc.start(str(self.exe), args)

    def _append(self, text: str) -> None:
        self.out.moveCursor(QTextCursor.End)
        self.out.insertPlainText(text)
        self.out.moveCursor(QTextCursor.End)

    def _read(self) -> None:
        text = bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        self._append(text)
        # Progress lines look like " 12.34% path/to/file"
        for line in reversed(text.splitlines()):
            s = line.strip()
            if "%" in s[:8]:
                try:
                    self.bar.setValue(int(float(s.split("%")[0]) * 10))
                    break
                except ValueError:
                    continue

    def _send(self) -> None:
        if self.proc.state() == QProcess.Running:
            self.proc.write((self.input.text() + "\n").encode())
            self._append("> ***\n" if self.input.echoMode() != QLineEdit.Normal else f"> {self.input.text()}\n")
        self.input.clear()

    def _finished(self, code: int, _status) -> None:
        if self._cancelled:
            return
        job = self.jobs[self.idx]
        target = dd_target_dir(*job)
        if code == 0:
            (target / ".complete").touch()
            self.ok.append(job)
            self._append(f"\n✓ депо {job[1]} загружено в {target}\n\n")
        else:
            self._append(f"\n✗ DepotDownloader завершился с кодом {code}\n\n")
        self._next()

    def _cancel(self) -> None:
        self._cancelled = True
        if self.proc.state() != QProcess.NotRunning:
            self.proc.kill()
            self.proc.waitForFinished(3000)
        self.reject()
