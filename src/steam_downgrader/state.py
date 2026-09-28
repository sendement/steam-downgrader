"""Persistent tool state: locks, observed build snapshots, manual manifests, settings."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    d = Path(base) / "steam-downgrader"
    d.mkdir(parents=True, exist_ok=True)
    return d


def staging_dir() -> Path:
    d = data_dir() / "staging"
    d.mkdir(parents=True, exist_ok=True)
    return d


class State:
    """Plain JSON; re-read on every load so the CLI relock and the GUI
    don't clobber each other's changes."""

    def __init__(self, path: Path | None = None):
        self.path = path or data_dir() / "state.json"
        self.data: dict = {}
        self.load()

    def load(self) -> None:
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        for k in ("locks", "snapshots", "manual", "settings", "steamdb", "steamdb_prompted"):
            self.data.setdefault(k, {})

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    # --- locks ---------------------------------------------------------------
    # lock = {
    #   "locked_at": ts, "actual_depots": {depot: manifest},
    #   "actual_buildid": int | None, "orig_auto_update": int,
    #   "strong": bool, "spoofed_buildid": int,
    # }

    @property
    def locks(self) -> dict[str, dict]:
        return self.data["locks"]

    def lock_for(self, app_id: str) -> dict | None:
        return self.locks.get(app_id)

    # --- snapshots of real installs (history we saw ourselves) ---------------

    def record_snapshot(self, app_id: str, buildid: int, depots: dict[str, str]) -> bool:
        snaps = self.data["snapshots"].setdefault(app_id, [])
        if any(s["buildid"] == buildid and s["depots"] == depots for s in snaps):
            return False
        snaps.append({"buildid": buildid, "time": int(time.time()), "depots": depots})
        return True

    def snapshots(self, app_id: str) -> list[dict]:
        return self.data["snapshots"].get(app_id, [])

    # --- manually entered manifest ids ---------------------------------------

    def add_manual(self, depot_id: str, manifest: str, label: str = "") -> None:
        self.data["manual"].setdefault(depot_id, {})[manifest] = label

    def manual(self, depot_id: str) -> dict[str, str]:
        return self.data["manual"].get(depot_id, {})

    # --- manifest lists imported from SteamDB ---------------------------------

    def add_steamdb(self, depot_id: str, manifest: str, when: int) -> bool:
        d = self.data["steamdb"].setdefault(depot_id, {})
        if d.get(manifest) == when or (manifest in d and not when):
            return False
        d[manifest] = when or d.get(manifest, 0)
        return True

    def steamdb(self, depot_id: str) -> dict[str, int]:
        return self.data["steamdb"].get(depot_id, {})

    def steamdb_prompted(self, app_id: str) -> bool:
        return app_id in self.data["steamdb_prompted"]

    def mark_steamdb_prompted(self, app_id: str) -> None:
        self.data["steamdb_prompted"][app_id] = int(time.time())

    # --- settings ------------------------------------------------------------

    def setting(self, key: str, default=None):
        return self.data["settings"].get(key, default)

    def set_setting(self, key: str, value) -> None:
        self.data["settings"][key] = value
