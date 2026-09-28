"""Background download queue for the built-in downloader.

One worker process (steam_downgrader.native.worker) at a time; the rest wait.
The worker speaks JSON events on stdout and logs diagnostics on stderr. It
needs no input: sign-in happens beforehand in the login window, and when the
stored token is missing or expired (exit code 3) the job goes back to the queue,
the queue pauses and ``login_needed`` asks the window to sign in again.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field

from PySide6.QtCore import QObject, QProcess, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..downloader import download_dir
from ..shadercache import human

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "в очереди", "загрузка", "готово", "ошибка", "отменено"
EXIT_AUTH = 3
_LOG_LIMIT = 4000


@dataclass
class Job:
    app_id: str
    depot_id: str
    manifest: str
    name: str
    extra: list[str] = field(default_factory=list)  # extra worker args
    status: str = QUEUED
    pct: float = 0.0
    detail: str = ""  # "25 МБ/с · ~3 мин" etc.
    log: list[str] = field(default_factory=list)
    error: str = ""
    finished_ok: bool = False

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.app_id, self.depot_id, self.manifest)

    @property
    def active(self) -> bool:
        return self.status in (QUEUED, RUNNING)


def notify(title: str, body: str) -> None:
    """Desktop notification where available; silently nothing otherwise."""
    exe = shutil.which("notify-send")
    if exe:
        subprocess.Popen([exe, "-a", "Steam Downgrader", "-i", "steam", title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class DownloadManager(QObject):
    changed = Signal()  # queue or progress changed
    job_done = Signal(object)  # Job, finished in any way
    login_needed = Signal(str)  # reason; queue is paused until resume()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.jobs: list[Job] = []
        self.current: Job | None = None
        self._partial = ""
        self._paused = False
        self.proc = QProcess(self)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.readyReadStandardError.connect(self._read_stderr)
        self.proc.finished.connect(self._finished)
        self.proc.errorOccurred.connect(self._error)

    # --- queue -------------------------------------------------------------------

    def enqueue(self, app_id: str, depot_id: str, manifest: str, name: str, extra: list[str] | None = None) -> bool:
        if any(j.key == (app_id, depot_id, manifest) and j.active for j in self.jobs):
            return False
        self.jobs.append(Job(app_id, depot_id, manifest, name, extra=list(extra or [])))
        self.changed.emit()
        QTimer.singleShot(0, self._start_next)
        return True

    def job_for(self, app_id: str, depot_id: str, manifest: str) -> Job | None:
        return next((j for j in reversed(self.jobs) if j.key == (app_id, depot_id, manifest) and j.active), None)

    def pending(self) -> list[Job]:
        return [j for j in self.jobs if j.active]

    def cancel(self, job: Job) -> None:
        if job is self.current and self.proc.state() != QProcess.NotRunning:
            job.status = CANCELLED
            self.proc.kill()  # _finished() moves on
        elif job.status == QUEUED:
            job.status = CANCELLED
            self.job_done.emit(job)
        self.changed.emit()

    def cancel_all(self) -> None:
        for j in self.pending():
            self.cancel(j)

    def resume(self) -> None:
        """After a successful sign-in."""
        self._paused = False
        QTimer.singleShot(0, self._start_next)

    def abort_waiting_for_login(self) -> None:
        self._paused = False
        for j in self.jobs:
            if j.status == QUEUED:
                j.status, j.error = FAILED, "нужен вход в Steam"
                self.job_done.emit(j)
        self.changed.emit()

    def clear_finished(self) -> None:
        self.jobs = [j for j in self.jobs if j.active]
        self.changed.emit()

    # --- process -------------------------------------------------------------------

    def _start_next(self) -> None:
        if self.current is not None or self._paused:
            return
        job = next((j for j in self.jobs if j.status == QUEUED), None)
        if not job:
            return
        # SD_NATIVE_WORKER: a script to run instead (tests use a fake worker).
        entry = [os.environ["SD_NATIVE_WORKER"]] if os.environ.get("SD_NATIVE_WORKER") else ["-m", "steam_downgrader.native.worker"]
        args = [*entry, "--app", job.app_id, "--depot", job.depot_id, "--manifest", job.manifest,
                "--out", str(download_dir(*job.key)), *job.extra]
        self.current, self._partial = job, ""
        job.status, job.detail, job.finished_ok, job.error = RUNNING, "вход…", False, ""
        job.log.append(f"$ steam-downgrader worker {' '.join(args[len(entry):])}")
        self.proc.start(sys.executable, args)
        self.changed.emit()

    def _append(self, job: Job, line: str) -> None:
        job.log.append(line)
        if len(job.log) > _LOG_LIMIT:
            del job.log[: len(job.log) - _LOG_LIMIT]

    def _read_stderr(self) -> None:
        job = self.current
        if job is None:
            return
        for line in bytes(self.proc.readAllStandardError()).decode("utf-8", "replace").splitlines():
            if line.strip():
                self._append(job, line)

    def _read(self) -> None:
        job = self.current
        if job is None:
            return
        text = self._partial + bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        *lines, self._partial = text.split("\n")
        for line in lines:
            if line.strip():
                self._event(job, line)
        if lines:
            self.changed.emit()

    def _event(self, job: Job, line: str) -> None:
        try:
            ev = json.loads(line)
        except ValueError:
            self._append(job, line)
            return
        kind = ev.get("ev")
        if kind == "progress":
            job.pct = ev["done"] * 100.0 / (ev.get("total") or 1)
            if ev.get("phase") == "scan":
                job.detail = "проверка установленных файлов"
            else:
                left = max(0, ev["to_download"] - ev["downloaded"])
                speed = ev.get("speed") or 0
                eta = f" · ~{_eta(left / speed)}" if speed > 0 and left else ""
                job.detail = f"{human(speed)}/с{eta}" if speed else "загрузка"
        elif kind == "status":
            job.detail = ev["text"].rstrip("…").lower()
            self._append(job, ev["text"])
        elif kind == "plan":
            job.pct = (ev["unchanged"] + ev["reused"] + ev["resumed"]) * 100.0 / (ev["total"] or 1)
            self._append(job, (
                f"Всего {human(ev['total'])}: без изменений {human(ev['unchanged'])}, с диска {human(ev['reused'])}, "
                f"уже скачано {human(ev['resumed'])}, скачать {human(ev['to_download'])}"
            ))
        elif kind == "done":
            job.finished_ok = True
            self._append(job, f"Готово: скачано {human(ev['downloaded'])}, взято с диска {human(ev['reused'] + ev['unchanged'])}")
        elif kind == "error":
            job.error = ev["message"]
            self._append(job, "Ошибка: " + ev["message"])
        elif kind == "auth_required":
            self._append(job, "Нужен вход в Steam: " + ev.get("reason", ""))
        else:
            self._append(job, ev.get("msg") or ev.get("text") or line)

    def _error(self, err) -> None:
        if err == QProcess.FailedToStart and self.current:
            self.current.error = "загрузчик не запустился"
            self._finished(-1, None)

    def _finished(self, code: int, _status) -> None:
        job = self.current
        if job is None:
            return
        self.current = None
        if job.status != CANCELLED and code == EXIT_AUTH:
            # Token missing/expired: back to the queue until the user signs in.
            job.status, job.pct, job.detail = QUEUED, 0.0, ""
            self._paused = True
            self.changed.emit()
            self.login_needed.emit(job.name)
            return
        if job.status != CANCELLED:
            if code == 0 and job.finished_ok:
                job.status, job.pct = DONE, 100.0
            else:
                job.status = FAILED
                job.error = job.error or f"загрузчик завершился с кодом {code}"
        self.job_done.emit(job)
        self.changed.emit()
        QTimer.singleShot(0, self._start_next)

    def shutdown(self) -> None:
        for j in self.jobs:
            if j.status == QUEUED:
                j.status = CANCELLED
        if self.proc.state() != QProcess.NotRunning:
            if self.current:
                self.current.status = CANCELLED
            self.proc.kill()
            self.proc.waitForFinished(3000)

    def summary(self) -> str:
        pend = self.pending()
        if not pend:
            return ""
        cur = self.current
        if self._paused:
            head = "⬇ ждёт входа в Steam"
        elif cur:
            head = f"⬇ {cur.name} · депо {cur.depot_id}: {cur.pct:.0f}%" + (f" · {cur.detail}" if cur.detail else "")
        else:
            head = "⬇ ожидание"
        rest = len(pend) - (1 if cur else 0)
        return head + (f"  (+{rest} в очереди)" if rest else "")


def _eta(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s} с"
    if s < 3600:
        return f"{s // 60} мин"
    return f"{s // 3600} ч {s % 3600 // 60} мин"


class DownloadsWindow(QDialog):
    """Queue overview plus the selected job's log."""

    def __init__(self, mgr: DownloadManager, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Загрузки")
        self.resize(900, 600)
        self.mgr = mgr
        self._shown: Job | None = None
        self._shown_len = 0

        lay = QVBoxLayout(self)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Игра", "Депо", "Манифест", "Статус", ""])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Fixed)
        self.table.setColumnWidth(4, 90)
        hh.setSectionResizeMode(3, QHeaderView.Interactive)
        self.table.setColumnWidth(3, 340)
        self.table.itemSelectionChanged.connect(self._show_selected)
        lay.addWidget(self.table, 1)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)
        self.out = QPlainTextEdit(readOnly=True)
        mono = QFont("monospace")
        mono.setStyleHint(QFont.Monospace)
        mono.setPointSize(8)
        self.out.setFont(mono)
        self.out.setLineWrapMode(QPlainTextEdit.NoWrap)
        lay.addWidget(self.out, 2)

        row = QHBoxLayout()
        clear = QPushButton("Убрать завершённые")
        clear.clicked.connect(mgr.clear_finished)
        row.addWidget(clear)
        row.addStretch()
        close = QPushButton("Скрыть")
        close.clicked.connect(self.hide)
        row.addWidget(close)
        lay.addLayout(row)

        mgr.changed.connect(self.sync)
        self.sync()

    def sync(self) -> None:
        jobs = self.mgr.jobs
        sel = self._shown
        if self.table.rowCount() != len(jobs):
            self.table.setRowCount(len(jobs))
        for r, j in enumerate(jobs):
            for c, v in enumerate([j.name, j.depot_id, j.manifest]):
                it = self.table.item(r, c)
                if it is None or it.text() != v:
                    self.table.setItem(r, c, QTableWidgetItem(v))
            bar = self.table.cellWidget(r, 3)
            if not isinstance(bar, QProgressBar):
                bar = QProgressBar()
                self.table.setCellWidget(r, 3, bar)
            bar.setRange(0, 1000)
            bar.setValue(int(j.pct * 10))
            bar.setFormat(f"{j.pct:.1f}%" + (f" · {j.detail}" if j.detail else "") if j.status == RUNNING
                          else "⚠ ошибка" if j.status == FAILED else j.status)
            bar.setToolTip(j.error)
            btn = self.table.cellWidget(r, 4)
            if j.active:
                if not isinstance(btn, QPushButton):
                    btn = QPushButton("Отмена")
                    btn.clicked.connect(lambda _=False, b=btn: self.mgr.cancel(b.job))
                    self.table.setCellWidget(r, 4, btn)
                btn.job = j  # rows shift when finished jobs are cleared
            elif btn is not None:
                self.table.removeCellWidget(r, 4)
        if sel is None and self.mgr.current:
            sel = self.mgr.current
        if sel is not None and sel in jobs and not self.table.selectedItems():
            self.table.selectRow(jobs.index(sel))
        self._render_log()

    def _show_selected(self) -> None:
        rows = {i.row() for i in self.table.selectedItems()}
        if rows:
            r = rows.pop()
            if r < len(self.mgr.jobs):
                self._shown = self.mgr.jobs[r]
                self._shown_len = 0
                self.out.clear()
                self._render_log()

    def _render_log(self) -> None:
        j = self._shown
        if j is None:
            return
        if len(j.log) < self._shown_len:  # trimmed
            self.out.clear()
            self._shown_len = 0
        new = j.log[self._shown_len :]
        if new:
            self.out.moveCursor(QTextCursor.End)
            self.out.insertPlainText("\n".join(new) + "\n")
            self.out.moveCursor(QTextCursor.End)
            self._shown_len = len(j.log)
        if j.status == FAILED:
            self.hint.setText(f"<span style='color:#c0392b'>Ошибка: {j.error}</span>")
        elif j.status != RUNNING:
            self.hint.setText("")

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(e)
