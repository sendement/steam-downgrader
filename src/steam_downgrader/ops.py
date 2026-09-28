"""The operations that touch a game install: apply, lock, relock, unlock.

How the lock works
------------------
Steam decides a game needs updating by comparing the appmanifest (buildid and
each installed depot's manifest) with the branch's current build from PICS.
After a rollback we therefore write the *latest* buildid/manifests into the
appmanifest -- Steam believes the game is current and leaves it alone -- and
remember the real, older manifests in our state. On top of that:

* AutoUpdateBehavior=1 ("only update when launched") so a background update
  can't sneak in between a new build appearing and the next relock;
* the appmanifest is made read-only so the client can't rewrite it;
* optionally ("strong") the whole install is made read-only, so even an
  update Steam does start fails with a disk write error instead of
  overwriting the old files.

When a newer build ships, the spoofed buildid falls behind; ``relock()``
re-reads appinfo.vdf and bumps it. Run it at startup / from the systemd
path unit and restart Steam if it had already queued the update.
"""

from __future__ import annotations

import os
import shutil
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .appinfo import AppInfoCache
from .history import depotcache_manifest
from .manifest import read_manifest
from .staging import Staged, staged_file_list
from .state import State
from .steam import Game, Steam, read_acf, write_acf

Progress = Callable[[str, int, int], None]  # message, done, total


class OpError(RuntimeError):
    pass


def _noop(_msg: str, _done: int, _total: int) -> None:
    pass


def actual_depots(state: State, game: Game) -> dict[str, str]:
    """What is really on disk. For a locked game the appmanifest lies."""
    lock = state.lock_for(game.app_id)
    real = {d: dep.manifest for d, dep in game.depots.items()}
    if lock:
        real.update(lock.get("actual_depots", {}))
    return real


def _require_steam_closed(steam: Steam) -> None:
    if steam.is_running():
        raise OpError("Steam запущен — закройте его перед этой операцией.")


# --- permissions ---------------------------------------------------------------


def _set_tree_writable(root: Path, writable: bool) -> None:
    mask = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    paths = [root]
    for r, dirs, files in os.walk(root):
        paths += [Path(r) / n for n in dirs + files]
    # Loosen parents before children, tighten children before parents.
    for p in paths if writable else reversed(paths):
        try:
            st = p.lstat()
            if stat.S_ISLNK(st.st_mode):
                continue
            mode = (st.st_mode | stat.S_IWUSR) if writable else (st.st_mode & ~mask)
            os.chmod(p, stat.S_IMODE(mode))
        except OSError:
            pass


def _make_writable(p: Path) -> None:
    try:
        os.chmod(p, stat.S_IMODE(p.lstat().st_mode) | stat.S_IWUSR)
    except OSError:
        pass


# --- apply ---------------------------------------------------------------------


@dataclass
class ApplyResult:
    depot_id: str
    manifest: str
    files_moved: int
    files_deleted: int
    missing: list[str]


def apply_depot(
    steam: Steam,
    state: State,
    game: Game,
    st: Staged,
    move: bool = True,
    delete_extra: bool = True,
    progress: Progress = _noop,
) -> ApplyResult:
    """Put one staged depot into the install. Caller locks afterwards."""
    _require_steam_closed(steam)
    if not st.manifest:
        raise OpError(f"Депо {st.depot_id}: неизвестен манифест загрузки (загрузка не завершена?).")
    if st.depot_id not in game.depots:
        raise OpError(f"Депо {st.depot_id} не установлено у {game.name}.")

    lock = state.lock_for(game.app_id)
    if lock and lock.get("strong"):
        _set_tree_writable(game.install_dir, True)

    target, _from_manifest = staged_file_list(steam, st)
    target_names = {rel.lower() for rel, _ in target}

    # Files the currently installed build of this depot owns but the target
    # build doesn't. Only ever delete what the depot manifest lists, so saves
    # and configs the game keeps in its folder survive.
    deleted = 0
    current = actual_depots(state, game).get(st.depot_id)
    cur_path = depotcache_manifest(steam, st.depot_id, current) if current else None
    if delete_extra and cur_path and current != st.manifest:
        try:
            cur = read_manifest(cur_path)
        except (OSError, ValueError):
            cur = None
        if cur and not cur.filenames_encrypted:
            extra = [f.name for f in cur.files if not f.is_dir and f.name.lower() not in target_names]
            for i, rel in enumerate(extra):
                p = game.install_dir / rel
                progress(f"Удаление {rel}", i, len(extra))
                if p.is_file() or p.is_symlink():
                    _make_writable(p.parent)
                    p.unlink()
                    deleted += 1
            # Directories that became empty and belonged to the old build.
            target_dirs = {str(Path(rel).parent).lower() for rel, _ in target}
            for f in sorted((f for f in cur.files if f.is_dir), key=lambda f: -len(f.name)):
                p = game.install_dir / f.name
                if f.name.lower() not in target_dirs and p.is_dir() and not any(p.iterdir()):
                    p.rmdir()

    moved = 0
    missing: list[str] = []
    same_fs = _same_fs(st.path, game.install_dir)
    for i, (rel, _size) in enumerate(target):
        src = st.path / rel
        dst = game.install_dir / rel
        progress(f"{'Перенос' if move else 'Копирование'} {rel}", i, len(target))
        if not src.is_file():
            missing.append(rel)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            _make_writable(dst)
        if move and same_fs:
            os.replace(src, dst)
        else:
            tmp = dst.with_name(dst.name + ".sdtmp")
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            if move:
                src.unlink()
        moved += 1
    progress("Готово", len(target), len(target))

    if missing:
        raise OpError(
            f"Депо {st.depot_id}: в загрузке нет {len(missing)} файлов (первый: {missing[0]}). "
            "Установка частично обновлена — докачайте депо и примените снова."
        )

    if move:
        shutil.rmtree(st.path, ignore_errors=True)
        _prune_empty(st.path.parent)
    return ApplyResult(st.depot_id, st.manifest, moved, deleted, missing)


def _same_fs(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False


def _prune_empty(d: Path) -> None:
    try:
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    except OSError:
        pass


# --- lock ----------------------------------------------------------------------


def _spoof_target(info_cache: AppInfoCache, game: Game) -> tuple[int, dict[str, tuple[str, int]]]:
    """Latest buildid and depot manifests of the game's branch, per appinfo."""
    info = info_cache.get(game.app_id)
    if not info:
        raise OpError(f"В appinfo.vdf нет данных для {game.app_id} — запустите Steam, чтобы он их обновил.")
    branch = info.branches.get(game.branch) or info.branches.get("public")
    if not branch:
        raise OpError(f"В appinfo.vdf нет ветки {game.branch} для {game.name}.")
    manifests = {}
    for d in game.depots:
        di = info.depots.get(d)
        if di:
            m = di.manifests.get(branch.name) or di.manifests.get("public")
            if m:
                manifests[d] = m
    return branch.buildid, manifests


def _write_spoof(game: Game, buildid: int, manifests: dict[str, tuple[str, int]]) -> bool:
    data = read_acf(game.acf_path)
    st = data["AppState"]
    before = repr(data)
    st["buildid"] = str(buildid)
    st["TargetBuildID"] = "0"
    st["StateFlags"] = "4"
    st["UpdateResult"] = "0"
    st["AutoUpdateBehavior"] = "1"
    st["ScheduledAutoUpdate"] = "0"
    for key in ("BytesToDownload", "BytesDownloaded", "BytesToStage", "BytesStaged"):
        if key in st:
            st[key] = "0"
    for d, dep in (st.get("InstalledDepots") or {}).items():
        if d in manifests and isinstance(dep, dict):
            dep["manifest"], size = manifests[d][0], manifests[d][1]
            if size:
                dep["size"] = str(size)
    changed = repr(data) != before
    if changed:
        write_acf(game.acf_path, data)
    os.chmod(game.acf_path, 0o444)
    return changed


def lock(
    steam: Steam,
    state: State,
    info_cache: AppInfoCache,
    game: Game,
    real_depots: dict[str, str] | None = None,
    real_buildid: int | None = None,
    strong: bool = False,
) -> None:
    """Protect the current (rolled back) install from Steam updates.

    ``real_depots``: manifests actually on disk now (after apply). Defaults to
    whatever we already know.
    """
    _require_steam_closed(steam)
    state.load()
    prev = state.lock_for(game.app_id) or {}
    actual = actual_depots(state, game)
    if real_depots:
        actual.update(real_depots)
    # Locking an up-to-date game is fine too: it freezes the current build.
    buildid, manifests = _spoof_target(info_cache, game)
    if real_buildid is None and not prev and actual == {d: m.manifest for d, m in game.depots.items()}:
        real_buildid = game.buildid

    _write_spoof(game, buildid, manifests)
    if strong:
        _set_tree_writable(game.install_dir, False)
    elif prev.get("strong"):
        _set_tree_writable(game.install_dir, True)

    state.locks[game.app_id] = {
        "name": game.name,
        "locked_at": prev.get("locked_at", int(time.time())),
        "actual_depots": actual,
        "actual_buildid": real_buildid if real_buildid is not None else prev.get("actual_buildid"),
        "orig_auto_update": prev.get("orig_auto_update", game.auto_update if not prev else 0),
        "strong": strong,
        "spoofed_buildid": buildid,
    }
    state.save()


def relock(steam: Steam, state: State, info_cache: AppInfoCache) -> list[tuple[str, int, int]]:
    """Bring every lock's spoofed build up to the latest one Steam knows.
    Safe with Steam running: the client only reads appmanifests at startup and
    can't write the read-only file. Returns (name, old buildid, new buildid)."""
    state.load()
    changes = []
    for app_id, lk in list(state.locks.items()):
        game = steam.game(app_id)
        if not game:
            continue
        try:
            buildid, manifests = _spoof_target(info_cache, game)
        except OpError:
            continue
        if _write_spoof(game, buildid, manifests):
            changes.append((game.name, lk.get("spoofed_buildid", 0), buildid))
            lk["spoofed_buildid"] = buildid
        if lk.get("strong"):
            _set_tree_writable(game.install_dir, False)
    state.save()
    return changes


def unlock(steam: Steam, state: State, game: Game, validate: bool = False) -> None:
    """Drop the protection and tell Steam the truth, so its next update is a
    normal delta from the real old build. ``validate`` asks Steam to verify
    files right away, which restores the latest version."""
    _require_steam_closed(steam)
    state.load()
    lk = state.lock_for(game.app_id)
    if lk and lk.get("strong"):
        _set_tree_writable(game.install_dir, True)
    os.chmod(game.acf_path, 0o644)
    if lk:
        data = read_acf(game.acf_path)
        st = data["AppState"]
        for d, dep in (st.get("InstalledDepots") or {}).items():
            real = lk.get("actual_depots", {}).get(d)
            if real and isinstance(dep, dict):
                dep["manifest"] = real
        if lk.get("actual_buildid"):
            st["buildid"] = str(lk["actual_buildid"])
        st["AutoUpdateBehavior"] = str(lk.get("orig_auto_update", 0))
        write_acf(game.acf_path, data)
        del state.locks[game.app_id]
        state.save()
    if validate:
        steam.start()
        steam.open_url(f"steam://validate/{game.app_id}")
