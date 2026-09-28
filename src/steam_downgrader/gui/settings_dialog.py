from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from ..downloader import fetch_depotdownloader, find_depotdownloader
from ..state import State


class SettingsDialog(QDialog):
    def __init__(self, state: State, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки")
        self.state = state
        lay = QVBoxLayout(self)

        lay.addWidget(QLabel("<b>Способ загрузки старых версий</b>"))
        self.rb_console = QRadioButton("Консоль Steam (download_depot) — без логина, вставляете команды вручную")
        self.rb_dd = QRadioButton("DepotDownloader — в фоне, без консоли; вход своим аккаунтом Steam")
        grp = QButtonGroup(self)
        grp.addButton(self.rb_console)
        grp.addButton(self.rb_dd)
        (self.rb_dd if state.setting("backend") == "depotdownloader" else self.rb_console).setChecked(True)
        lay.addWidget(self.rb_console)
        lay.addWidget(self.rb_dd)

        dd_box = QWidget()
        form = QFormLayout(dd_box)
        form.setContentsMargins(24, 0, 0, 0)
        path_row = QHBoxLayout()
        self.dd_path = QLineEdit(state.setting("dd_path", "") or str(find_depotdownloader() or ""))
        path_row.addWidget(self.dd_path, 1)
        browse = QPushButton("…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(browse)
        fetch = QPushButton("Скачать с GitHub")
        fetch.clicked.connect(self._fetch)
        path_row.addWidget(fetch)
        form.addRow("Путь:", path_row)

        self.rb_qr = QRadioButton("QR-код (сканировать приложением Steam)")
        self.rb_user = QRadioButton("Логин:")
        g2 = QButtonGroup(self)
        g2.addButton(self.rb_qr)
        g2.addButton(self.rb_user)
        user_row = QHBoxLayout()
        user_row.addWidget(self.rb_user)
        self.username = QLineEdit(state.setting("dd_username", ""))
        self.username.setPlaceholderText("имя аккаунта Steam")
        user_row.addWidget(self.username, 1)
        (self.rb_user if state.setting("dd_username") else self.rb_qr).setChecked(True)
        form.addRow("Вход:", self.rb_qr)
        form.addRow("", user_row)
        hint = QLabel(
            "Загрузка идёт в фоне, с очередью; прогресс — в строке состояния и в окне «Загрузки».\n"
            "Пароль или QR нужны только при первом входе: DepotDownloader запоминает токен сам\n"
            "(-remember-password). Пароль передаётся через stdin и нигде не сохраняется."
        )
        hint.setStyleSheet("color: gray")
        form.addRow("", hint)
        lay.addWidget(dd_box)
        self.rb_dd.toggled.connect(dd_box.setEnabled)
        dd_box.setEnabled(self.rb_dd.isChecked())

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "DepotDownloader", self.dd_path.text())
        if path:
            self.dd_path.setText(path)

    def _fetch(self) -> None:
        try:
            self.dd_path.setText(str(fetch_depotdownloader()))
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Ошибка", f"Не удалось скачать DepotDownloader:\n{e}")

    def _save(self) -> None:
        self.state.set_setting("backend", "depotdownloader" if self.rb_dd.isChecked() else "console")
        self.state.set_setting("dd_path", self.dd_path.text().strip())
        self.state.set_setting("dd_username", self.username.text().strip() if self.rb_user.isChecked() else "")
        self.state.save()
        self.accept()
