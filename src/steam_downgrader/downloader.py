"""Download backends.

* Steam console (default, no credentials): we generate ``download_depot``
  commands, the user pastes them into steam://open/console, and completion is
  picked up from console_log.
* DepotDownloader (SteamRE/DepotDownloader): fully automatic, logs in with
  the user's own account (QR code or password + Steam Guard, remembered).
"""

from __future__ import annotations

import io
import json
import os
import platform
import shutil
import stat
import sys
import urllib.request
import zipfile
from pathlib import Path

from .state import data_dir, staging_dir

DD_RELEASES = "https://api.github.com/repos/SteamRE/DepotDownloader/releases/latest"


def console_command(app_id: str, depot_id: str, manifest: str) -> str:
    return f"download_depot {app_id} {depot_id} {manifest}"


def dd_target_dir(app_id: str, depot_id: str, manifest: str) -> Path:
    return staging_dir() / app_id / f"{depot_id}_{manifest}"


def find_depotdownloader(configured: str | None = None) -> Path | None:
    if configured and Path(configured).is_file():
        return Path(configured)
    for name in ("DepotDownloader", "depotdownloader"):
        p = shutil.which(name)
        if p:
            return Path(p)
    local = data_dir() / "DepotDownloader" / ("DepotDownloader.exe" if sys.platform.startswith("win") else "DepotDownloader")
    return local if local.is_file() else None


def dd_args(
    app_id: str,
    depot_id: str,
    manifest: str,
    username: str = "",
    qr: bool = False,
) -> list[str]:
    args = [
        "-app", app_id,
        "-depot", depot_id,
        "-manifest", manifest,
        "-dir", str(dd_target_dir(app_id, depot_id, manifest)),
        "-max-downloads", "16",
    ]
    if username:
        args += ["-username", username, "-remember-password"]
    elif qr:
        args += ["-qr", "-remember-password"]
    return args


def _asset_name() -> str:
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("aarch64", "arm64") else "x64"
    if sys.platform.startswith("win"):
        return f"DepotDownloader-windows-{arch}.zip"
    if sys.platform == "darwin":
        return f"DepotDownloader-macos-{arch}.zip"
    return f"DepotDownloader-linux-{arch}.zip"


def fetch_depotdownloader() -> Path:
    """Download the latest self-contained DepotDownloader release from GitHub."""
    req = urllib.request.Request(DD_RELEASES, headers={"Accept": "application/vnd.github+json", "User-Agent": "steam-downgrader"})
    with urllib.request.urlopen(req, timeout=30) as r:
        release = json.load(r)
    want = _asset_name()
    asset = next((a for a in release.get("assets", []) if a.get("name") == want), None)
    if not asset:
        raise RuntimeError(f"В релизе {release.get('tag_name')} нет {want}")
    with urllib.request.urlopen(asset["browser_download_url"], timeout=120) as r:
        blob = r.read()
    dest = data_dir() / "DepotDownloader"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        z.extractall(dest)
    exe = dest / ("DepotDownloader.exe" if sys.platform.startswith("win") else "DepotDownloader")
    if not exe.is_file():
        raise RuntimeError("В архиве нет исполняемого файла DepotDownloader")
    os.chmod(exe, exe.stat().st_mode | stat.S_IXUSR)
    return exe
