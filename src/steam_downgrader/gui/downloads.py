"""Background DepotDownloader queue.

One DepotDownloader process at a time; the rest wait in a queue. Output is
watched for login prompts, which are the only moments the user is involved:

* ``Enter account password for "user": `` -- read from stdin when input is
  redirected, so the password never shows up in the process list;
* ``STEAM GUARD! Please enter your 2-factor auth code…`` / ``…sent to the email…``;
* ``Use the Steam Mobile App to confirm your sign in...`` -- just a notice;
* ``Logging in with QR code...`` -- the log window pops up to show the code.

Success = exit code 0 and a ``Total downloaded`` line.

Jobs with ``backend == "native"`` run the built-in downloader
(steam_downgrader.native.worker) instead, which speaks JSON events and needs
no prompts at all: sign-in happens beforehand in the login window, and a
missing/expired token pauses the queue until the user signs in again.
"""

from __future__ import annotations

import json
import os
import re
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

from ..downloader import dd_args, dd_target_dir, find_depotdownloader
from ..shadercache import human
from ..state import State

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "в очереди", "загрузка", "готово", "ошибка", "отменено"

_PROGRESS_RE = re.compile(r"^\s*(\d{1,3}\.\d\d)% ")
_ERROR_RE = re.compile(r"(?i)^(error|failed|unable|download failed|insufficient)|is not available|not listed|no subscription")
_LOG_LIMIT = 4000


@dataclass
class Job:
    app_id: str
    depot_id: str
    manifest: str
    name: str
    status: str = QUEUED
    pct: float = 0.0
    log: list[str] = field(default_factory=list)
    error: str = ""
    backend: str = "dd"  # "dd" | "native"
    extra: list[str] = field(default_factory=list)  # extra worker args
    detail: str = ""  # "25 МБ/с · 3 мин" etc.
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
    show_log = Signal(object)  # Job: the user must look (QR code)
    notice = Signal(str)  # one-line info for the status bar / log
    login_needed = Signal(str)  # reason; queue is paused until resume()

    def __init__(self, state: State, parent=None):
        super().__init__(parent)
        self.state = state
        self.jobs: list[Job] = []
        self.current: Job | None = None
        self._partial = ""  # output after the last newline (prompts have none)
        self._passwords: dict[str, str] = {}  # this session only
        self._password_sent = False
        self._asking = False  # a modal prompt is open; don't re-enter _read
        self._paused = False  # waiting for the user to sign in
        # asker(kind, job, prompt) -> answer or None to cancel; set by the window.
        self.asker = lambda kind, job, prompt: None
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.readyReadStandardError.connect(self._read_stderr)
        self.proc.finished.connect(self._finished)
        self.proc.errorOccurred.connect(self._error)

    # --- queue -------------------------------------------------------------------

    def enqueue(self, app_id: str, depot_id: str, manifest: str, name: str,
                backend: str = "dd", extra: list[str] | None = None) -> bool:
        if any(j.key == (app_id, depot_id, manifest) and j.active for j in self.jobs):
            return False
        self.jobs.append(Job(app_id, depot_id, manifest, name, backend=backend, extra=list(extra or [])))
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
            if j.status == QUEUED and j.backend == "native":
                j.status, j.error = FAILED, "нужен вход в Steam"
                self.job_done.emit(j)
        self.changed.emit()
        QTimer.singleShot(0, self._start_next)

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
        if job.backend == "native":
            self._start_native(job)
            return
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        exe = find_depotdownloader(self.state.setting("dd_path"))
        if not exe:
            job.status, job.error = FAILED, "DepotDownloader не найден — укажите его в настройках"
            self.job_done.emit(job)
            self.changed.emit()
            QTimer.singleShot(0, self._start_next)
            return
        user = self.state.setting("dd_username", "")
        dd_target_dir(*job.key).mkdir(parents=True, exist_ok=True)
        self.current, self._partial, self._password_sent = job, "", False
        job.status = RUNNING
        job.log.append(f"$ {exe.name} {' '.join(dd_args(*job.key, username=user, qr=not user))}")
        self.proc.start(str(exe), dd_args(*job.key, username=user, qr=not user))
        self.changed.emit()

    def _start_native(self, job: Job) -> None:
        out = dd_target_dir(*job.key)
        # SD_NATIVE_WORKER: a script to run instead (tests use a fake worker).
        entry = [os.environ["SD_NATIVE_WORKER"]] if os.environ.get("SD_NATIVE_WORKER") else ["-m", "steam_downgrader.native.worker"]
        args = [*entry, "--app", job.app_id, "--depot", job.depot_id,
                "--manifest", job.manifest, "--out", str(out), *job.extra]
        self.current, self._partial = job, ""
        job.status, job.detail, job.finished_ok = RUNNING, "вход…", False
        job.log.append(f"$ steam-downgrader worker {' '.join(args[len(entry):])}")
        self.proc.setProcessChannelMode(QProcess.SeparateChannels)
        self.proc.start(sys.executable, args)
        self.changed.emit()

    def _read_stderr(self) -> None:
        job = self.current
        if job is None:
            return
        for line in bytes(self.proc.readAllStandardError()).decode("utf-8", "replace").splitlines():
            if line.strip():
                self._append(job, line)

    def _native_event(self, job: Job, line: str) -> None:
        try:
            ev = json.loads(line)
        except ValueError:
            self._append(job, line)
            return
        kind = ev.get("ev")
        if kind == "progress":
            total = ev.get("total") or 1
            job.pct = ev["done"] * 100.0 / total
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
            job.error = "auth"
            self._append(job, "Нужен вход в Steam: " + ev.get("reason", ""))
        else:
            self._append(job, ev.get("msg") or ev.get("text") or line)

    def _append(self, job: Job, line: str) -> None:
        job.log.append(line)
        if len(job.log) > _LOG_LIMIT:
            del job.log[: len(job.log) - _LOG_LIMIT]

    def _read(self) -> None:
        job = self.current
        if job is None or self._asking:
            return
        text = self._partial + bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        *lines, self._partial = text.replace("\r\n", "\n").split("\n")
        if job.backend == "native":
            for line in lines:
                if line.strip():
                    self._native_event(job, line)
            if lines:
                self.changed.emit()
            return
        for line in lines:
            self._append(job, line)
            m = _PROGRESS_RE.match(line)
            if m:
                job.pct = float(m.group(1))
            elif "Mobile App to confirm" in line:
                self.notice.emit("Steam Guard: подтвердите вход в мобильном приложении Steam")
                notify("Вход в Steam", "Подтвердите вход DepotDownloader в мобильном приложении Steam")
            elif "Logging in with QR code" in line or "QR code has changed" in line:
                self.show_log.emit(job)
        if lines:
            self.changed.emit()
        self._check_prompt(job)

    def _check_prompt(self, job: Job) -> None:
        p = self._partial.strip()
        if not p.endswith(":"):
            return
        if p.startswith("Enter account password"):
            kind = "password"
        elif "auth code" in p:
            kind = "code"
        else:
            return
        self._append(job, p)
        self._partial = ""
        user = self.state.setting("dd_username", "")
        if kind == "password" and not self._password_sent and user in self._passwords:
            answer: str | None = self._passwords[user]
        else:
            self._asking = True
            try:
                answer = self.asker(kind, job, p)
            finally:
                self._asking = False
        if self.proc.state() == QProcess.NotRunning:
            return
        if answer is None:
            self.cancel(job)
            return
        if kind == "password":
            self._passwords[user] = answer
            self._password_sent = True
        self._append(job, "> ***")
        self.proc.write((answer + "\n").encode())
        if self.proc.bytesAvailable():  # output that arrived while the prompt was open
            QTimer.singleShot(0, self._read)

    def _error(self, err) -> None:
        if err == QProcess.FailedToStart and self.current:
            self.current.error = "DepotDownloader не запустился"
            self._finished(-1, None)

    def _finished(self, code: int, _status) -> None:
        job = self.current
        if job is None:
            return
        if self._partial:
            self._append(job, self._partial)
            self._partial = ""
        if job.backend == "native" and code == 3 and job.status != CANCELLED:
            # Token missing/expired: put the job back and wait for sign-in.
            job.status, job.pct, job.detail, job.error = QUEUED, 0.0, "", ""
            self.current = None
            self._paused = True
            self.changed.emit()
            self.login_needed.emit(job.name)
            return
        if job.status != CANCELLED and job.backend == "native":
            if code == 0 and job.finished_ok:
                job.status, job.pct = DONE, 100.0
            else:
                job.status = FAILED
                job.error = job.error or f"загрузчик завершился с кодом {code}"
        elif job.status != CANCELLED:
            ok = code == 0 and any(line.startswith("Total downloaded") for line in job.log[-50:])
            if ok:
                (dd_target_dir(*job.key) / ".complete").touch()
                job.status, job.pct = DONE, 100.0
            else:
                job.status = FAILED
                job.error = job.error or next(
                    (line.strip() for line in reversed(job.log) if _ERROR_RE.search(line.strip())),
                    f"DepotDownloader завершился с кодом {code}",
                )
        self.current = None
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
    """Queue overview plus the selected job's DepotDownloader output."""

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

    def focus_job(self, job: Job) -> None:
        self.sync()
        for r, j in enumerate(self.mgr.jobs):
            if j is job:
                self.table.selectRow(r)
        self.hint.setText("Отсканируйте QR-код приложением Steam (Steam Guard → «Вход по QR-коду»)." if job.status == RUNNING else "")
        self.show()
        self.raise_()

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
