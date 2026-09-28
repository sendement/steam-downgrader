"""Sign in to Steam: QR code (default) or account name + password + Steam Guard.

All HTTP calls run in Worker threads; polling is a QTimer at the interval
Steam asks for. On success the refresh token is saved (auth.save_token) and
the dialog accepts.
"""

from __future__ import annotations

import segno
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import auth
from .worker import Worker

QR_PX = 280


def qr_pixmap(url: str, px: int = QR_PX) -> QPixmap:
    """Render the challenge URL; black on white regardless of theme (scanners
    need the contrast), with the standard 4-module quiet zone."""
    matrix = segno.make(url, error="m", micro=False).matrix
    n = len(matrix) + 8
    scale = max(1, px // n)
    img = QImage(n * scale, n * scale, QImage.Format_RGB32)
    img.fill(QColor("white"))
    p = QPainter(img)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("black"))
    for y, row in enumerate(matrix):
        for x, bit in enumerate(row):
            if bit:
                p.drawRect((x + 4) * scale, (y + 4) * scale, scale, scale)
    p.end()
    return QPixmap.fromImage(img)


class LoginDialog(QDialog):
    def __init__(self, reason: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Вход в Steam")
        self.setMinimumWidth(420)
        self._workers: list[Worker] = []
        self.account = ""

        lay = QVBoxLayout(self)
        intro = QLabel(
            (f"<b>{reason}</b><br>" if reason else "")
            + "Вход нужен, чтобы скачивать старые версии купленных игр. Программа сохраняет только "
            "токен входа (как клиент Steam), пароль нигде не хранится."
        )
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self.tabs = QTabWidget()
        lay.addWidget(self.tabs)

        # --- QR ---
        qr = QWidget()
        ql = QVBoxLayout(qr)
        self.qr_img = QLabel(alignment=Qt.AlignCenter)
        self.qr_img.setMinimumSize(QSize(QR_PX, QR_PX))
        ql.addWidget(self.qr_img)
        self.qr_status = QLabel(alignment=Qt.AlignCenter)
        self.qr_status.setWordWrap(True)
        ql.addWidget(self.qr_status)
        self.qr_retry = QPushButton("Новый QR-код")
        self.qr_retry.clicked.connect(self._qr_start)
        self.qr_retry.hide()
        ql.addWidget(self.qr_retry, 0, Qt.AlignCenter)
        self.tabs.addTab(qr, "QR-код")

        # --- credentials ---
        cred = QWidget()
        cl = QVBoxLayout(cred)
        form = QFormLayout()
        self.user = QLineEdit(placeholderText="имя аккаунта (не e-mail)")
        self.password = QLineEdit(echoMode=QLineEdit.Password)
        self.password.returnPressed.connect(self._cred_start)
        form.addRow("Логин:", self.user)
        form.addRow("Пароль:", self.password)
        cl.addLayout(form)
        self.login_btn = QPushButton("Войти")
        self.login_btn.clicked.connect(self._cred_start)
        cl.addWidget(self.login_btn)
        self.guard_box = QWidget()
        gl = QFormLayout(self.guard_box)
        gl.setContentsMargins(0, 8, 0, 0)
        self.code = QLineEdit(placeholderText="код Steam Guard")
        self.code.returnPressed.connect(self._submit_code)
        self.code_btn = QPushButton("Отправить код")
        self.code_btn.clicked.connect(self._submit_code)
        gl.addRow(self.code, self.code_btn)
        self.guard_box.hide()
        cl.addWidget(self.guard_box)
        self.cred_status = QLabel()
        self.cred_status.setWordWrap(True)
        cl.addWidget(self.cred_status)
        cl.addStretch()
        self.tabs.addTab(cred, "Логин и пароль")

        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self.qr: auth.QrSession | None = None
        self.cred: auth.CredentialsSession | None = None
        self._code_type = auth.GUARD_DEVICE_CODE
        self.qr_timer = QTimer(self, timeout=self._qr_poll)
        self.cred_timer = QTimer(self, timeout=self._cred_poll)
        self._polling = False
        self.tabs.currentChanged.connect(self._tab_changed)
        QTimer.singleShot(0, self._qr_start)

    # --- plumbing ---------------------------------------------------------------

    def _bg(self, fn, *args, done=None, failed=None) -> None:
        w = Worker(fn, *args, parent=self)
        if done:
            w.done.connect(done)
        w.failed.connect(failed or (lambda e: None))
        w.finished.connect(lambda: self._workers.remove(w))
        self._workers.append(w)
        w.start()

    def _success(self, r: auth.PollResult) -> None:
        self.qr_timer.stop()
        self.cred_timer.stop()
        auth.save_token(r.account_name, r.refresh_token)
        self.account = r.account_name
        self.accept()

    def _tab_changed(self, idx: int) -> None:
        # Only the visible method polls.
        if idx == 0:
            self.cred_timer.stop()
            if self.qr and not self.qr_timer.isActive():
                self.qr_timer.start()
            elif not self.qr:
                self._qr_start()
        else:
            self.qr_timer.stop()
            if self.cred and self.guard_box.isVisible() is False and self.cred_status.text():
                self.cred_timer.start()

    def done(self, r: int) -> None:
        self.qr_timer.stop()
        self.cred_timer.stop()
        for w in list(self._workers):
            w.wait(3000)
        super().done(r)

    # --- QR ----------------------------------------------------------------------

    def _qr_start(self) -> None:
        self.qr_timer.stop()
        self.qr_retry.hide()
        self.qr_status.setText("Получаю QR-код…")
        sess = auth.QrSession()

        def ok(url: str):
            self.qr = sess
            self.qr_img.setPixmap(qr_pixmap(url))
            self.qr_status.setText("Откройте приложение Steam → Steam Guard → значок QR и отсканируйте код.")
            self.qr_timer.setInterval(int(sess.interval * 1000))
            if self.tabs.currentIndex() == 0:
                self.qr_timer.start()

        self._bg(sess.start, done=ok, failed=self._qr_failed)

    def _qr_failed(self, err: str) -> None:
        self.qr_timer.stop()
        self.qr = None
        self.qr_img.clear()
        self.qr_status.setText(f"⚠ {err}")
        self.qr_retry.show()

    def _qr_poll(self) -> None:
        if self._polling or not self.qr:
            return
        self._polling = True
        sess = self.qr

        def ok(r: auth.PollResult):
            self._polling = False
            if sess is not self.qr:
                return
            if r.done:
                self._success(r)
                return
            if r.new_challenge_url:
                self.qr_img.setPixmap(qr_pixmap(r.new_challenge_url))
            if r.remote_interaction:
                self.qr_status.setText("Код отсканирован — подтвердите вход в приложении Steam.")

        def fail(e: str):
            self._polling = False
            if sess is self.qr:
                # An expired session is normal after a while; just start over.
                self._qr_failed(e) if "Нет связи" in e else self._qr_start()

        self._bg(sess.poll, done=ok, failed=fail)

    # --- credentials ------------------------------------------------------------------

    def _cred_start(self) -> None:
        user, pw = self.user.text().strip(), self.password.text()
        if not user or not pw:
            self.cred_status.setText("Введите логин и пароль.")
            return
        self.cred_timer.stop()
        self.login_btn.setEnabled(False)
        self.guard_box.hide()
        self.cred_status.setText("Вход…")
        sess = auth.CredentialsSession()

        def ok(allowed: list[int]):
            self.cred = sess
            self.password.clear()
            self.login_btn.setEnabled(True)
            msgs = []
            if auth.GUARD_DEVICE_CONFIRM in allowed:
                msgs.append("Подтвердите вход в мобильном приложении Steam")
            if auth.GUARD_DEVICE_CODE in allowed:
                self._code_type = auth.GUARD_DEVICE_CODE
                self.code.setPlaceholderText("код из приложения Steam Guard")
                self.guard_box.show()
                msgs.append("или введите код из приложения")
            elif auth.GUARD_EMAIL_CODE in allowed:
                self._code_type = auth.GUARD_EMAIL_CODE
                self.code.setPlaceholderText("код из письма")
                self.guard_box.show()
                msgs.append("Введите код из письма, отправленного на почту аккаунта")
            elif auth.GUARD_EMAIL_CONFIRM in allowed:
                msgs.append("Подтвердите вход по ссылке из письма")
            self.cred_status.setText(" ".join(msgs) + "." if msgs else "Проверка…")
            if self.guard_box.isVisible():
                self.code.setFocus()
            self.cred_timer.setInterval(int(sess.interval * 1000))
            self.cred_timer.start()

        def fail(e: str):
            self.login_btn.setEnabled(True)
            self.cred_status.setText(f"⚠ {e}")

        self._bg(sess.start, user, pw, done=ok, failed=fail)

    def _submit_code(self) -> None:
        if not self.cred or not self.code.text().strip():
            return
        self.code_btn.setEnabled(False)
        sess, code = self.cred, self.code.text().strip()

        def ok(_r):
            self.code_btn.setEnabled(True)
            self.cred_status.setText("Код принят, вход…")
            self._cred_poll()

        def fail(e: str):
            self.code_btn.setEnabled(True)
            self.code.selectAll()
            self.cred_status.setText(f"⚠ {e}")

        self._bg(sess.submit_code, code, self._code_type, done=ok, failed=fail)

    def _cred_poll(self) -> None:
        if self._polling or not self.cred:
            return
        self._polling = True
        sess = self.cred

        def ok(r: auth.PollResult):
            self._polling = False
            if sess is self.cred and r.done:
                self._success(r)

        def fail(e: str):
            self._polling = False
            self.cred_timer.stop()
            self.cred_status.setText(f"⚠ {e}. Попробуйте войти ещё раз.")

        self._bg(sess.poll, done=ok, failed=fail)
