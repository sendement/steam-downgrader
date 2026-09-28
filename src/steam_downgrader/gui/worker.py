from __future__ import annotations

import traceback

from PySide6.QtCore import QThread, Signal


class Worker(QThread):
    """Run ``fn(*args, progress=cb)`` off the UI thread."""

    progress = Signal(str, int, int)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args, with_progress: bool = False, parent=None, **kwargs):
        super().__init__(parent)
        self._fn, self._args, self._kwargs = fn, args, kwargs
        if with_progress:
            self._kwargs["progress"] = self.progress.emit

    def run(self) -> None:
        try:
            self.done.emit(self._fn(*self._args, **self._kwargs))
        except Exception as e:  # noqa: BLE001 -- surfaced to the user
            traceback.print_exc()
            self.failed.emit(str(e) or e.__class__.__name__)
