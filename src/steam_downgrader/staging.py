"""Finding and checking downloaded-but-not-applied depot content.

Two places hold staged content:
* Steam's own ``download_depot`` output (content/app_X/depot_Y), whose
  manifest we learn from console_log;
* our staging dir for DepotDownloader (staging/<app>/<depot>_<manifest>/),
  marked finished by a ``.complete`` file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .history import History, depotcache_manifest
from .manifest import read_manifest
from .state import staging_dir
from .steam import Steam

SKIP_DIRS = {".DepotDownloader"}


@dataclass
class Staged:
    app_id: str
    depot_id: str
    manifest: str  # "" if unknown (download_depot still running or log rotated)
    path: Path
    source: str  # "steam" | "depotdownloader"
    complete: bool


def find_staged(steam: Steam, history: History, app_id: str) -> list[Staged]:
    out: list[Staged] = []
    for content in steam.content_dirs():
        app_dir = content / f"app_{app_id}"
        if not app_dir.is_dir():
            continue
        for d in sorted(app_dir.glob("depot_*")):
            depot = d.name.removeprefix("depot_")
            dls = history.downloads(app_id, depot)
            # No "complete" line yet means it's still downloading (or the log
            # rotated away); check_staged() verifies the files either way.
            manifest = dls[-1][0] if dls else ""
            out.append(Staged(app_id, depot, manifest, d, "steam", bool(manifest)))
    ours = staging_dir() / app_id
    if ours.is_dir():
        for d in sorted(ours.iterdir()):
            if d.is_dir() and "_" in d.name:
                depot, manifest = d.name.split("_", 1)
                out.append(Staged(app_id, depot, manifest, d, "depotdownloader", (d / ".complete").exists()))
    return out


@dataclass
class StageCheck:
    files_expected: int
    files_ok: int
    missing: list[str]
    wrong_size: list[str]
    from_manifest: bool  # False: no depotcache manifest, we trust the folder

    @property
    def ok(self) -> bool:
        return not self.missing and not self.wrong_size


def staged_file_list(steam: Steam, st: Staged) -> tuple[list[tuple[str, int]], bool]:
    """(relative path, size) of every file the staged depot should contain."""
    mp = depotcache_manifest(steam, st.depot_id, st.manifest) if st.manifest else None
    if mp:
        try:
            m = read_manifest(mp)
            if not m.filenames_encrypted:
                return [(f.name, f.size) for f in m.files if not f.is_dir], True
        except (OSError, ValueError):
            pass
    files = []
    for root, dirs, names in os.walk(st.path):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for n in names:
            if n == ".complete":
                continue
            p = Path(root) / n
            files.append((p.relative_to(st.path).as_posix(), p.stat().st_size))
    return files, False


def check_staged(steam: Steam, st: Staged) -> StageCheck:
    files, from_manifest = staged_file_list(steam, st)
    missing, wrong = [], []
    for rel, size in files:
        p = st.path / rel
        try:
            s = p.stat().st_size
        except OSError:
            missing.append(rel)
            continue
        if from_manifest and s != size:
            wrong.append(rel)
    return StageCheck(len(files), len(files) - len(missing) - len(wrong), missing, wrong, from_manifest)
