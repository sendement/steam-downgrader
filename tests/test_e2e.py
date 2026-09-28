"""End-to-end test of apply/lock/relock/unlock on a synthetic Steam tree.

Run: uv run python -m pytest tests  (or plain: uv run python tests/test_e2e.py)
"""

from __future__ import annotations

import os
import stat
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from steam_downgrader.appinfo import AppInfoCache  # noqa: E402
from steam_downgrader.history import History  # noqa: E402
from steam_downgrader.ops import actual_depots, apply_depot, lock, relock, unlock  # noqa: E402
from steam_downgrader.staging import check_staged, find_staged  # noqa: E402
from steam_downgrader.state import State  # noqa: E402
from steam_downgrader.steam import Steam, read_acf  # noqa: E402
from steam_downgrader.vdf import dump_vdf  # noqa: E402

APP, DEPOT = "4242", "4243"
OLD, NEW, NEWER = "1111111111111111111", "2222222222222222222", "3333333333333333333"


# --- tiny encoders -----------------------------------------------------------


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(no: int, val) -> bytes:
    if isinstance(val, int):
        return _varint(no << 3) + _varint(val)
    if isinstance(val, str):
        val = val.encode()
    return _varint(no << 3 | 2) + _varint(len(val)) + val


def write_manifest(path: Path, depot: str, gid: str, created: int, files: dict[str, bytes]) -> None:
    payload = b""
    dirs = {str(Path(f).parent) for f in files} - {"."}
    for d in sorted(dirs):
        payload += _field(1, _field(1, d.replace("/", "\\")) + _field(2, 0) + _field(3, 64))
    for name, data in files.items():
        payload += _field(1, _field(1, name.replace("/", "\\")) + _field(2, len(data)) + _field(3, 0))
    meta = _field(1, int(depot)) + _field(2, int(gid)) + _field(3, created) + _field(4, 0) + _field(5, sum(map(len, files.values())))
    blob = struct.pack("<II", 0x71F617D0, len(payload)) + payload
    blob += struct.pack("<II", 0x1F4812BE, len(meta)) + meta
    blob += struct.pack("<I", 0x32C415AB)
    path.write_bytes(blob)


def write_appinfo(path: Path, apps: dict[int, dict]) -> None:
    strings: list[str] = []

    def key(s: str) -> bytes:
        if s not in strings:
            strings.append(s)
        return struct.pack("<I", strings.index(s))

    def kv(obj: dict) -> bytes:
        out = b""
        for k, v in obj.items():
            if isinstance(v, dict):
                out += b"\x00" + key(k) + kv(v)
            elif isinstance(v, int):
                out += b"\x02" + key(k) + struct.pack("<i", v)
            else:
                out += b"\x01" + key(k) + str(v).encode() + b"\0"
        return out + b"\x08"

    body = b""
    for app_id, data in apps.items():
        entry = b"\0" * 60 + kv({"appinfo": data})
        body += struct.pack("<II", app_id, len(entry)) + entry
    body += struct.pack("<I", 0)
    table_off = 16 + len(body)
    table = struct.pack("<I", len(strings)) + b"".join(s.encode() + b"\0" for s in strings)
    path.write_bytes(struct.pack("<IIq", 0x07564429, 1, table_off) + body + table)


def appinfo_for(buildid: int, gid: str) -> dict:
    return {
        "common": {"name": "Fake Game"},
        "depots": {
            DEPOT: {"manifests": {"public": {"gid": gid, "size": "100"}}},
            "branches": {"public": {"buildid": buildid, "timeupdated": 1700000000}},
        },
    }


class FakeSteam(Steam):
    running = False

    def is_running(self) -> bool:
        return self.running

    def start(self) -> None:
        pass

    def open_url(self, url: str) -> None:
        self.opened = url


# --- the test ----------------------------------------------------------------


def build_fake_steam(tmp: Path) -> tuple[Path, Path, Path, Path]:
    """-> (steam root, game dir, appmanifest, download_depot folder)"""
    os.environ["XDG_DATA_HOME"] = str(tmp / "xdg")
    root = tmp / "Steam"
    (root / "steamapps" / "common" / "Fake").mkdir(parents=True)
    (root / "depotcache").mkdir()
    (root / "appcache").mkdir()
    (root / "logs").mkdir()
    game_dir = root / "steamapps" / "common" / "Fake"

    old_files = {"bin/game.exe": b"old-exe", "data/a.pak": b"old-a"}
    new_files = {"bin/game.exe": b"new-exe!!", "data/a.pak": b"new-a", "data/b_new.pak": b"only-in-new"}
    for rel, data in new_files.items():
        (game_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (game_dir / rel).write_bytes(data)
    (game_dir / "user.cfg").write_bytes(b"user settings")  # not in any manifest

    write_manifest(root / "depotcache" / f"{DEPOT}_{OLD}.manifest", DEPOT, OLD, 1600000000, old_files)
    write_manifest(root / "depotcache" / f"{DEPOT}_{NEW}.manifest", DEPOT, NEW, 1700000000, new_files)
    write_appinfo(root / "appcache" / "appinfo.vdf", {int(APP): appinfo_for(200, NEW)})

    acf = root / "steamapps" / f"appmanifest_{APP}.acf"
    acf.write_text(dump_vdf({"AppState": {
        "appid": APP, "name": "Fake Game", "StateFlags": "4", "installdir": "Fake", "buildid": "200",
        "AutoUpdateBehavior": "0",
        "InstalledDepots": {DEPOT: {"manifest": NEW, "size": "100"}},
    }}))

    # download_depot result + its console_log line
    content = root / "ubuntu12_32" / "steamapps" / "content" / f"app_{APP}" / f"depot_{DEPOT}"
    for rel, data in old_files.items():
        (content / rel).parent.mkdir(parents=True, exist_ok=True)
        (content / rel).write_bytes(data)
    (content / "leftover_from_other_download.bin").write_bytes(b"junk")
    (root / "logs" / "console_log.txt").write_text(
        f'[2026-09-28 02:29:59] Depot download complete : "{root}/ubuntu12_32\\steamapps\\content\\app_{APP}\\depot_{DEPOT}" (manifest {OLD})\n'
    )
    return root, game_dir, acf, content


OLD_FILES = {"bin/game.exe": b"old-exe", "data/a.pak": b"old-a"}


def test_full_cycle(tmp_path: Path | None = None) -> None:
    root, game_dir, acf, content = build_fake_steam(tmp_path or Path(tempfile.mkdtemp()))
    old_files = OLD_FILES
    steam, state = FakeSteam(root), State()
    game = steam.game(APP)
    hist = History(steam, state)
    info = AppInfoCache(steam.appinfo_path)

    versions = hist.versions(game, info.get(APP), hist.candidates(game, info.get(APP), actual_depots(state, game)))
    assert [v.depots[DEPOT] for v in versions] == [NEW, OLD], versions

    staged = find_staged(steam, hist, APP)
    assert len(staged) == 1 and staged[0].manifest == OLD
    assert check_staged(steam, staged[0]).ok

    # Steam running -> refuses
    steam.running = True
    try:
        apply_depot(steam, state, game, staged[0])
        raise AssertionError("apply must refuse while Steam runs")
    except RuntimeError:
        pass
    steam.running = False

    r = apply_depot(steam, state, game, staged[0])
    assert r.files_moved == 2 and r.files_deleted == 1
    assert (game_dir / "bin/game.exe").read_bytes() == b"old-exe"
    assert not (game_dir / "data/b_new.pak").exists()
    assert (game_dir / "user.cfg").read_bytes() == b"user settings"
    assert not (game_dir / "leftover_from_other_download.bin").exists()
    assert not content.exists()

    lock(steam, state, info, steam.game(APP), real_depots={DEPOT: OLD}, real_buildid=100, strong=True)
    st = read_acf(acf)["AppState"]
    assert st["buildid"] == "200" and st["InstalledDepots"][DEPOT]["manifest"] == NEW
    assert st["AutoUpdateBehavior"] == "1"
    assert not os.access(acf, os.W_OK)
    assert not (game_dir / "bin" / "game.exe").stat().st_mode & stat.S_IWUSR
    assert actual_depots(State(), steam.game(APP)) == {DEPOT: OLD}

    # A new build ships -> relock follows it
    write_appinfo(root / "appcache" / "appinfo.vdf", {int(APP): appinfo_for(300, NEWER)})
    changes = relock(steam, state, AppInfoCache(steam.appinfo_path))
    assert changes == [("Fake Game", 200, 300)], changes
    st = read_acf(acf)["AppState"]
    assert st["buildid"] == "300" and st["InstalledDepots"][DEPOT]["manifest"] == NEWER
    assert relock(steam, state, AppInfoCache(steam.appinfo_path)) == []

    unlock(steam, state, steam.game(APP))
    st = read_acf(acf)["AppState"]
    assert st["buildid"] == "100" and st["InstalledDepots"][DEPOT]["manifest"] == OLD
    assert st["AutoUpdateBehavior"] == "0"
    assert os.access(acf, os.W_OK)
    assert (game_dir / "bin" / "game.exe").stat().st_mode & stat.S_IWUSR
    assert not State().locks
    print("ok")


if __name__ == "__main__":
    test_full_cycle()
