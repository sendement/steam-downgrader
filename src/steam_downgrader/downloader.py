"""Helpers shared by the download paths.

* Built-in downloader (default): steam_downgrader.native.worker, driven by
  gui/downloads.py, writes into ``download_dir``.
* Steam console (no sign-in): the user pastes ``download_depot`` commands;
  gui/download_dialog.py watches console_log.
"""

from __future__ import annotations

from pathlib import Path

from .state import staging_dir


def console_command(app_id: str, depot_id: str, manifest: str) -> str:
    return f"download_depot {app_id} {depot_id} {manifest}"


def download_dir(app_id: str, depot_id: str, manifest: str) -> Path:
    return staging_dir() / app_id / f"{depot_id}_{manifest}"
