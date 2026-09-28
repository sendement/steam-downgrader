from __future__ import annotations

import time

from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from ..auth import clear_token, load_token
from ..state import State


def download_backend(state: State) -> str:
    """"native" (default) or "console". Old "depotdownloader" settings map to native."""
    return "console" if state.setting("backend") == "console" else "native"


class SettingsDialog(QDialog):
    def __init__(self, state: State, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки")
        self.state = state
        lay = QVBoxLayout(self)

        lay.addWidget(QLabel("<b>Способ загрузки старых версий</b>"))
        self.rb_native = QRadioButton("Встроенный загрузчик (рекомендуется) — в фоне, качает только изменившееся")
        self.rb_console = QRadioButton("Консоль Steam (download_depot) — без входа в аккаунт, команды вставляются вручную")
        grp = QButtonGroup(self)
        grp.addButton(self.rb_native)
        grp.addButton(self.rb_console)
        (self.rb_console if download_backend(state) == "console" else self.rb_native).setChecked(True)
        lay.addWidget(self.rb_native)

        acc = QHBoxLayout()
        acc.setContentsMargins(24, 0, 0, 8)
        self.account_label = QLabel()
        acc.addWidget(self.account_label, 1)
        self.account_btn = QPushButton()
        self.account_btn.clicked.connect(self._account)
        acc.addWidget(self.account_btn)
        lay.addLayout(acc)
        self._update_account()
        lay.addWidget(self.rb_console)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _update_account(self) -> None:
        tok = load_token()
        if tok:
            exp = time.strftime("%d.%m.%Y", time.localtime(tok.expires)) if tok.expires else "?"
            self.account_label.setText(f"Вход выполнен: <b>{tok.account_name}</b> (токен до {exp})")
            self.account_btn.setText("Выйти")
        else:
            self.account_label.setText("Вход в Steam не выполнен")
            self.account_btn.setText("Войти…")

    def _account(self) -> None:
        if load_token():
            clear_token()
        else:
            from .login_dialog import LoginDialog

            LoginDialog(parent=self).exec()
        self._update_account()

    def _save(self) -> None:
        self.state.set_setting("backend", "console" if self.rb_console.isChecked() else "native")
        for stale in ("dd_path", "dd_username"):
            self.state.data["settings"].pop(stale, None)
        self.state.save()
        self.accept()
