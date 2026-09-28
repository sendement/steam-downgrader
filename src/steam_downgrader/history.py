"""Collect the versions of a game we can roll back to, from local sources only.

Valve offers no public API for historic manifests (SteamDB has them but sits
behind Cloudflare), so we combine what the machine already knows:

* appinfo.vdf branches -- the current build of every branch (public, betas,
  sometimes publisher-provided "previous_version" branches);
* content_log "finished update" lines -- complete builds this client installed,
  with buildid and every depot's manifest;
* our own snapshots of appmanifests, recorded each time the tool runs;
* depotcache/*.manifest -- every manifest Steam has downloaded, with the date
  the build was made (used to reconstruct older builds);
* console_log "Depot download complete" lines from download_depot;
* manifest ids the user typed in (e.g. copied from SteamDB).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .appinfo import AppInfo
from .manifest import read_manifest
from .state import State
from .steam import Game, Steam, parse_download_log_line

SRC_BRANCH = "ветка"
SRC_LOG = "журнал обновлений"
SRC_SNAPSHOT = "снимок"
SRC_CACHE = "depotcache"
SRC_DOWNLOAD = "download_depot"
SRC_MANUAL = "вручную"
SRC_STEAMDB = "SteamDB"
SRC_INSTALLED = "установлено"


@dataclass
class Candidate:
    """One known manifest of one depot."""

    manifest: str
    created: int = 0  # build creation time, 0 if unknown
    buildid: int = 0
    sources: set[str] = field(default_factory=set)
    label: str = ""


@dataclass
class Version:
    """A whole-game version: a manifest for each installed depot. Depots
    mapped to ``None`` are unknown for that version (kept as installed)."""

    title: str
    buildid: int
    time: int
    depots: dict[str, str | None]
    source: str
    exact: bool  # True if the depot set is recorded, not reconstructed


_FINISHED_RE = re.compile(
    r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\] AppID (\d+) finished update, \d+ mounted depots "
    r"\(BuildID (\d+)\) : (.*)$"
)
_PAIR_RE = re.compile(r"(\d+) \((\d+)\)")
_TS_RE = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\]")


def _ts(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp())


class _ManifestIndex:
    """Metadata for every depotcache manifest, cached by (path, mtime)."""

    def __init__(self):
        self._cache: dict[Path, tuple[float, int]] = {}

    def created(self, path: Path) -> int:
        try:
            mt = path.stat().st_mtime
        except OSError:
            return 0
        hit = self._cache.get(path)
        if hit and hit[0] == mt:
            return hit[1]
        try:
            created = read_manifest(path, with_files=False).created
        except (OSError, ValueError, IndexError):
            created = 0
        self._cache[path] = (mt, created)
        return created


_manifest_index = _ManifestIndex()


class History:
    def __init__(self, steam: Steam, state: State):
        self.steam = steam
        self.state = state
        self._log_builds: dict[str, list[tuple[int, int, dict[str, str]]]] | None = None
        self._downloads: dict[tuple[str, str], list[tuple[str, int]]] | None = None

    # --- log scraping (once per History instance) ----------------------------

    def _scan_logs(self) -> None:
        self._log_builds = {}
        for f in self.steam.log_files("content_log"):
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                m = _FINISHED_RE.match(line)
                if m:
                    depots = dict(_PAIR_RE.findall(m.group(4)))
                    self._log_builds.setdefault(m.group(2), []).append((_ts(m.group(1)), int(m.group(3)), depots))
        self._downloads = {}
        for f in self.steam.log_files("console_log"):
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                r = parse_download_log_line(line)
                if r:
                    tm = _TS_RE.match(line)
                    self._downloads.setdefault((r[0], r[1]), []).append((r[2], _ts(tm.group(1)) if tm else 0))

    def log_builds(self, app_id: str) -> list[tuple[int, int, dict[str, str]]]:
        if self._log_builds is None:
            self._scan_logs()
        return self._log_builds.get(app_id, [])

    def downloads(self, app_id: str, depot_id: str) -> list[tuple[str, int]]:
        if self._downloads is None:
            self._scan_logs()
        return self._downloads.get((app_id, depot_id), [])

    # --- per-depot candidates -------------------------------------------------

    def candidates(self, game: Game, info: AppInfo | None, actual: dict[str, str]) -> dict[str, list[Candidate]]:
        out: dict[str, dict[str, Candidate]] = {d: {} for d in game.depots}

        def add(depot: str, gid: str, src: str, created: int = 0, buildid: int = 0, label: str = "") -> None:
            if depot not in out or not gid or gid == "0":
                return
            c = out[depot].setdefault(gid, Candidate(gid))
            c.sources.add(src)
            c.created = c.created or created
            c.buildid = c.buildid or buildid
            c.label = c.label or label

        for d, gid in actual.items():
            add(d, gid, SRC_INSTALLED)

        for d in game.depots:
            for p in self.steam.depotcache.glob(f"{d}_*.manifest"):
                gid = p.stem.split("_", 1)[1]
                add(d, gid, SRC_CACHE, created=_manifest_index.created(p))
            for gid, _t in self.downloads(game.app_id, d):
                add(d, gid, SRC_DOWNLOAD)
            for gid, label in self.state.manual(d).items():
                add(d, gid, SRC_MANUAL, label=label)
            for gid, when in self.state.steamdb(d).items():
                add(d, gid, SRC_STEAMDB, created=when)

        if info:
            for d, di in info.depots.items():
                for branch, (gid, _size) in di.manifests.items():
                    b = info.branches.get(branch)
                    add(d, gid, SRC_BRANCH, buildid=b.buildid if b else 0, label=f"ветка {branch}")

        for _t, buildid, depots in self.log_builds(game.app_id):
            for d, gid in depots.items():
                add(d, gid, SRC_LOG, buildid=buildid)
        for s in self.state.snapshots(game.app_id):
            for d, gid in s["depots"].items():
                add(d, gid, SRC_SNAPSHOT, buildid=s["buildid"])

        return {
            d: sorted(cs.values(), key=lambda c: (c.created, c.buildid), reverse=True)
            for d, cs in out.items()
        }

    # --- whole-game versions -------------------------------------------------

    def versions(self, game: Game, info: AppInfo | None, cands: dict[str, list[Candidate]]) -> list[Version]:
        depot_ids = list(game.depots)
        found: list[Version] = []

        def created_of(depot: str, gid: str) -> int:
            for c in cands.get(depot, []):
                if c.manifest == gid:
                    return c.created
            return 0

        if info:
            for name, b in info.branches.items():
                depots = {d: (info.depots[d].manifests.get(name, (None,))[0] if d in info.depots else None) for d in depot_ids}
                if not any(depots.values()):
                    continue
                title = "Последняя (public)" if name == "public" else f"Ветка «{name}»"
                if b.description:
                    title += f" — {b.description}"
                found.append(Version(title, b.buildid, b.time_updated, depots, SRC_BRANCH, True))

        for t, buildid, depots in self.log_builds(game.app_id):
            found.append(Version(f"Сборка {buildid}", buildid, t, {d: depots.get(d) for d in depot_ids}, SRC_LOG, True))
        for s in self.state.snapshots(game.app_id):
            found.append(
                Version(f"Сборка {s['buildid']}", s["buildid"], s["time"], {d: s["depots"].get(d) for d in depot_ids}, SRC_SNAPSHOT, True)
            )

        # Reconstruct builds from dated manifests (depotcache, SteamDB): anchor on the biggest depot and,
        # for every other depot, take its newest manifest not newer than the
        # anchor (+2h slack -- depots of one build are made minutes apart).
        main = max(depot_ids, key=lambda d: game.depots[d].size, default=None)
        if main:
            known = {v.depots.get(main) for v in found}
            for c in cands.get(main, []):
                if c.manifest in known or not c.created:
                    continue
                depots: dict[str, str | None] = {main: c.manifest}
                for d in depot_ids:
                    if d == main:
                        continue
                    pick = next((o for o in cands.get(d, []) if o.created and o.created <= c.created + 7200), None)
                    depots[d] = pick.manifest if pick else None
                bid = c.buildid
                title = f"Сборка {bid}" if bid else "Сборка от " + datetime.fromtimestamp(c.created).strftime("%d.%m.%Y")
                src = SRC_CACHE if SRC_CACHE in c.sources else SRC_STEAMDB if SRC_STEAMDB in c.sources else next(iter(c.sources))
                found.append(Version(title, bid, c.created, depots, src, False))

        # Dedupe on the depot tuple; keep the richest entry.
        merged: dict[tuple, Version] = {}
        for v in found:
            key = tuple(v.depots.get(d) for d in depot_ids)
            if key in merged:
                old = merged[key]
                if v.source == SRC_BRANCH and old.source != SRC_BRANCH:
                    v.time = v.time or old.time
                    merged[key] = v
                else:
                    old.buildid = old.buildid or v.buildid
                continue
            merged[key] = v
        # Fill build dates from manifest creation times where we only have a log time.
        for v in merged.values():
            if main and v.depots.get(main):
                ct = created_of(main, v.depots[main])
                if ct:
                    v.time = ct
        return sorted(merged.values(), key=lambda v: (v.time, v.buildid), reverse=True)


def fmt_time(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else "—"


def now() -> int:
    return int(time.time())


def steamdb_manifests_url(depot_id: str) -> str:
    return f"https://steamdb.info/depot/{depot_id}/manifests/"


def steamdb_patchnotes_url(app_id: str) -> str:
    return f"https://steamdb.info/app/{app_id}/patchnotes/"


def depotcache_manifest(steam: Steam, depot_id: str, gid: str) -> Path | None:
    p = steam.depotcache / f"{depot_id}_{gid}.manifest"
    return p if p.is_file() else None
