"""Finding and clearing a game's shader caches.

After a version change, pipeline caches built for the other build are stale at
best. Everything here is regenerated automatically (and Steam re-downloads its
pre-caching data), so clearing only costs recompilation time. Only entries
matched by name are touched -- never saves, configs or Steam's video transcodes.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from .history import depotcache_manifest
from .manifest import read_manifest
from .steam import Game, Steam

# steamapps/shadercache/<appid>/…  (fozmediav1, transcoded_video.foz, *-used are video, not shaders)
_STEAM_CACHE_PREFIXES = ("fozpipelines", "DXVK_state_cache", "nvidiav", "mesa_shader_cache", "radv", "vkd3d", "gl_cache")

# Driver/D3D caches inside the Proton prefix, relative to AppData/Local.
_PREFIX_CACHES = ("NVIDIA/DXCache", "NVIDIA/GLCache", "AMD/DxCache", "AMD/DxcCache", "AMD/VkCache", "D3DSCache")

# Translation layer caches that DXVK / vkd3d-proton drop next to the game exe.
_INSTALL_SUFFIXES = (".dxvk-cache", "vkd3d-proton.cache", "vkd3d-proton.cache.write")


@dataclass
class CacheItem:
    path: Path
    size: int
    kind: str  # human label


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    total = 0
    for root, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _depot_files(steam: Steam, depots: dict[str, str]) -> set[str]:
    """Lower-cased paths the game's own depots ship, so we never delete a
    cache file the developer put there on purpose."""
    names: set[str] = set()
    for d, gid in depots.items():
        mp = depotcache_manifest(steam, d, gid)
        if not mp:
            continue
        try:
            names |= {f.name.lower() for f in read_manifest(mp).files}
        except (OSError, ValueError):
            pass
    return names


def find_shader_caches(steam: Steam, game: Game, real_depots: dict[str, str]) -> list[CacheItem]:
    items: list[CacheItem] = []

    shipped = _depot_files(steam, real_depots)
    if game.install_dir.is_dir():
        for root, _dirs, files in os.walk(game.install_dir):
            for f in files:
                if f.endswith(_INSTALL_SUFFIXES):
                    p = Path(root) / f
                    if p.relative_to(game.install_dir).as_posix().lower() not in shipped:
                        kind = "vkd3d-proton" if "vkd3d" in f else "DXVK"
                        items.append(CacheItem(p, p.stat().st_size, f"кэш {kind} в папке игры"))

    libs = [game.library] + [lib for lib in (steam.root,) if lib != game.library]
    for lib in libs:
        sc = lib / "steamapps" / "shadercache" / game.app_id
        if sc.is_dir():
            for p in sorted(sc.iterdir()):
                if p.name.startswith(_STEAM_CACHE_PREFIXES):
                    items.append(CacheItem(p, _size(p), f"кэш шейдеров Steam: {p.name}"))
        local = lib / "steamapps" / "compatdata" / game.app_id / "pfx" / "drive_c" / "users" / "steamuser" / "AppData" / "Local"
        for rel in _PREFIX_CACHES:
            p = local / rel
            if p.exists():
                items.append(CacheItem(p, _size(p), f"кэш драйвера в префиксе Proton: {rel}"))
    return items


def clear_shader_caches(items: list[CacheItem]) -> int:
    freed = 0
    for it in items:
        parent = it.path.parent
        mode = parent.stat().st_mode if parent.exists() else None
        # A strong lock leaves the install read-only; lend write access briefly.
        relax = mode is not None and not os.access(parent, os.W_OK)
        try:
            if relax:
                os.chmod(parent, stat.S_IMODE(mode) | stat.S_IWUSR)
            if it.path.is_dir() and not it.path.is_symlink():
                shutil.rmtree(it.path)
            else:
                it.path.unlink()
            freed += it.size
        except FileNotFoundError:
            pass
        finally:
            if relax:
                os.chmod(parent, stat.S_IMODE(mode))
    return freed


def human(n: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)
