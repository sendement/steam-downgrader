"""Reader for Steam's appcache/appinfo.vdf (PICS cache).

It holds, for every app the client has seen, the current branches (buildid +
update time) and each depot's current manifest per branch. We use it to learn
what "latest" looks like, which is what the lock has to pretend is installed.

Only formats v28 (0x07564428) and v29 (0x07564429, key string table) are
supported; the file is indexed once and entries are decoded on demand.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from .vdf import parse_binary_kv

_V28 = 0x07564428
_V29 = 0x07564429
_ENTRY_HEADER = 60  # infostate..binary_sha1, after appid+size


@dataclass
class Branch:
    name: str
    buildid: int
    time_updated: int
    description: str = ""
    password: bool = False


@dataclass
class DepotInfo:
    depot_id: str
    name: str = ""
    oslist: str = ""
    language: str = ""
    dlc_app_id: str = ""
    shared_from: str = ""  # depotfromapp: redistributables living in another app
    manifests: dict[str, tuple[str, int]] = field(default_factory=dict)  # branch -> (gid, size)


@dataclass
class AppInfo:
    app_id: str
    name: str
    branches: dict[str, Branch]
    depots: dict[str, DepotInfo]


class AppInfoCache:
    def __init__(self, path: Path):
        self.path = path
        self._buf = path.read_bytes()
        magic, _universe = struct.unpack_from("<II", self._buf, 0)
        if magic == _V29:
            (table_off,) = struct.unpack_from("<q", self._buf, 8)
            (count,) = struct.unpack_from("<I", self._buf, table_off)
            raw = self._buf[table_off + 4 :].split(b"\0", count)[:count]
            self._strings: list[str] | None = [s.decode("utf-8", "replace") for s in raw]
            pos = 16
        elif magic == _V28:
            self._strings = None
            pos = 8
        else:
            raise ValueError(f"unsupported appinfo.vdf format 0x{magic:08x}")

        self._index: dict[int, int] = {}
        while pos + 8 <= len(self._buf):
            app_id, size = struct.unpack_from("<II", self._buf, pos)
            if app_id == 0:
                break
            self._index[app_id] = pos + 8 + _ENTRY_HEADER
            pos += 8 + size
        self._cache: dict[int, AppInfo | None] = {}

    def raw(self, app_id: int | str) -> dict | None:
        off = self._index.get(int(app_id))
        if off is None:
            return None
        data, _ = parse_binary_kv(self._buf, off, self._strings)
        return data.get("appinfo", data)

    def get(self, app_id: int | str) -> AppInfo | None:
        key = int(app_id)
        if key not in self._cache:
            self._cache[key] = self._decode(key)
        return self._cache[key]

    def _decode(self, app_id: int) -> AppInfo | None:
        raw = self.raw(app_id)
        if raw is None:
            return None
        common = raw.get("common", {}) or {}
        depots_raw = raw.get("depots", {}) or {}

        branches: dict[str, Branch] = {}
        for name, b in (depots_raw.get("branches") or {}).items():
            if not isinstance(b, dict):
                continue
            branches[name] = Branch(
                name=name,
                buildid=int(b.get("buildid", 0) or 0),
                time_updated=int(b.get("timeupdated", 0) or 0),
                description=str(b.get("description", "") or ""),
                password=bool(int(b.get("pwdrequired", 0) or 0)),
            )

        depots: dict[str, DepotInfo] = {}
        for did, d in depots_raw.items():
            if not did.isdigit() or not isinstance(d, dict):
                continue
            cfg = d.get("config", {}) or {}
            info = DepotInfo(
                depot_id=did,
                name=str(d.get("name", "") or ""),
                oslist=str(cfg.get("oslist", "") or ""),
                language=str(cfg.get("language", "") or ""),
                dlc_app_id=str(d.get("dlcappid", "") or ""),
                shared_from=str(d.get("depotfromapp", "") or ""),
            )
            for branch, m in (d.get("manifests") or {}).items():
                if isinstance(m, dict) and m.get("gid"):
                    info.manifests[branch] = (str(m["gid"]), int(m.get("size", 0) or 0))
            depots[did] = info

        return AppInfo(
            app_id=str(app_id),
            name=str(common.get("name", "") or ""),
            branches=branches,
            depots=depots,
        )
