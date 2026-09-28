"""Steam install discovery: libraries, installed games, client process, logs."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from .vdf import dump_vdf, parse_vdf


@dataclass
class InstalledDepot:
    depot_id: str
    manifest: str
    size: int
    dlc_app_id: str = ""


@dataclass
class Game:
    app_id: str
    name: str
    library: Path
    acf_path: Path
    install_dir: Path
    buildid: int
    beta_key: str  # "" means the public branch
    auto_update: int  # AutoUpdateBehavior: 0 always, 1 on launch, 2 high priority
    state_flags: int
    depots: dict[str, InstalledDepot] = field(default_factory=dict)

    @property
    def branch(self) -> str:
        return self.beta_key or "public"


def default_steam_root() -> Path | None:
    home = Path.home()
    if sys.platform.startswith("win"):
        cands = [Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Steam"]
    else:
        cands = [
            home / ".local/share/Steam",
            home / ".steam/steam",
            home / ".steam/root",
            home / ".var/app/com.valvesoftware.Steam/data/Steam",
            home / "snap/steam/common/.local/share/Steam",
        ]
    for c in cands:
        if (c / "steamapps").is_dir():
            return c.resolve()
    return None


class Steam:
    def __init__(self, root: Path):
        self.root = root

    # --- paths ---------------------------------------------------------------

    @property
    def appinfo_path(self) -> Path:
        return self.root / "appcache" / "appinfo.vdf"

    @property
    def depotcache(self) -> Path:
        return self.root / "depotcache"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    def content_dirs(self) -> list[Path]:
        """Where the ``download_depot`` console command drops files. On Linux
        the client resolves it relative to ubuntu12_32/ (a Windows-ism in
        Valve's path handling), elsewhere it's steamapps/content."""
        dirs = [self.root / "ubuntu12_32" / "steamapps" / "content", self.root / "steamapps" / "content"]
        return [d for d in dirs if d.is_dir()]

    def library_image(self, app_id: str) -> Path | None:
        """The 460x215 header from the client's image cache. Old clients store
        it flat, newer ones under a content hash with a localized name."""
        base = self.root / "appcache" / "librarycache"
        for p in (base / app_id / "header.jpg", base / f"{app_id}_header.jpg"):
            if p.is_file():
                return p
        d = base / app_id
        if d.is_dir():
            for pattern in ("*/header*.jpg", "*/library_header.jpg", "*/library_header_*.jpg"):
                hits = sorted(p for p in d.glob(pattern) if "_2x" not in p.name)
                if hits:
                    return hits[0]
        return None

    def libraries(self) -> list[Path]:
        libs = [self.root]
        vdf_path = self.root / "steamapps" / "libraryfolders.vdf"
        try:
            data = parse_vdf(vdf_path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            data = {}
        for entry in (data.get("libraryfolders") or {}).values():
            if isinstance(entry, dict) and entry.get("path"):
                p = Path(entry["path"])
                if (p / "steamapps").is_dir() and p.resolve() not in {l.resolve() for l in libs}:
                    libs.append(p)
        return libs

    # --- games ---------------------------------------------------------------

    def games(self) -> list[Game]:
        out: dict[str, Game] = {}
        for lib in self.libraries():
            for acf in sorted((lib / "steamapps").glob("appmanifest_*.acf")):
                g = load_game(acf, lib)
                if g and g.app_id not in out:
                    out[g.app_id] = g
        return sorted(out.values(), key=lambda g: g.name.lower())

    def game(self, app_id: str) -> Game | None:
        for lib in self.libraries():
            acf = lib / "steamapps" / f"appmanifest_{app_id}.acf"
            if acf.is_file():
                return load_game(acf, lib)
        return None

    # --- client process ------------------------------------------------------

    def is_running(self) -> bool:
        if sys.platform.startswith("win"):
            try:
                out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq steam.exe"], capture_output=True, text=True).stdout
                return "steam.exe" in out.lower()
            except OSError:
                return False
        pid_file = Path.home() / ".steam" / "steam.pid"
        try:
            pid = int(pid_file.read_text().strip())
            if Path(f"/proc/{pid}").exists():
                return True
        except (OSError, ValueError):
            pass
        for proc in Path("/proc").glob("[0-9]*"):
            try:
                comm = (proc / "comm").read_text().strip()
            except OSError:
                continue
            if comm in ("steam", "steamwebhelper"):
                return True
        return False

    def shutdown(self, timeout: float = 30.0) -> bool:
        self._launch("-shutdown")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_running():
                return True
            time.sleep(0.5)
        return False

    def start(self) -> None:
        self._launch()

    def open_url(self, url: str) -> None:
        """steam:// URLs (console, validate, ...) go through the client."""
        if self.is_running() or not url.startswith("steam://"):
            if shutil.which("xdg-open") and not sys.platform.startswith("win"):
                subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                webbrowser.open(url)
        else:
            self._launch(url)

    def _launch(self, *args: str) -> None:
        exe = shutil.which("steam") or "steam"
        subprocess.Popen(
            [exe, *args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    # --- logs ----------------------------------------------------------------

    def log_files(self, stem: str) -> list[Path]:
        """Older rotation first, so later lines win."""
        files = [self.logs / f"{stem}.previous.txt", self.logs / f"{stem}.txt"]
        return [f for f in files if f.is_file()]


def load_game(acf: Path, library: Path) -> Game | None:
    try:
        data = parse_vdf(acf.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    st = data.get("AppState") or {}
    app_id, name, installdir = st.get("appid"), st.get("name"), st.get("installdir")
    if not (app_id and name and installdir):
        return None
    depots = {}
    for did, d in (st.get("InstalledDepots") or {}).items():
        if isinstance(d, dict):
            depots[did] = InstalledDepot(did, d.get("manifest", ""), int(d.get("size", 0) or 0), d.get("dlcappid", ""))
    return Game(
        app_id=app_id,
        name=name,
        library=library,
        acf_path=acf,
        install_dir=library / "steamapps" / "common" / installdir,
        buildid=int(st.get("buildid", 0) or 0),
        beta_key=((st.get("UserConfig") or {}).get("BetaKey") or ""),
        auto_update=int(st.get("AutoUpdateBehavior", 0) or 0),
        state_flags=int(st.get("StateFlags", 0) or 0),
        depots=depots,
    )


def read_acf(path: Path) -> dict:
    return parse_vdf(path.read_text(encoding="utf-8", errors="replace"))


def write_acf(path: Path, data: dict) -> None:
    """Atomic write that tolerates (and preserves) a read-only file."""
    was_ro = not os.access(path, os.W_OK)
    tmp = path.with_name(path.name + ".sdtmp")
    tmp.write_text(dump_vdf(data), encoding="utf-8")
    if was_ro:
        os.chmod(path, 0o644)
    os.replace(tmp, path)
    if was_ro:
        os.chmod(path, 0o444)


_TOOL_PREFIXES = ("Proton ", "Proton-", "SteamLinuxRuntime", "Steamworks Shared", "Steam Linux Runtime")


def is_tool(game: Game) -> bool:
    return game.name.startswith(_TOOL_PREFIXES) or game.install_dir.name.startswith(_TOOL_PREFIXES)


# "[2026-09-28 02:29:59] Depot download complete : "<path>" (manifest 666...)"
_DL_COMPLETE_RE = re.compile(r'Depot download complete : "([^"]+)" \(manifest (\d+)\)')
_DL_FAILED_RE = re.compile(r"Depot download failed : (.*)")


def parse_download_log_line(line: str) -> tuple[str, str, str] | None:
    """-> (app_id, depot_id, manifest) for a finished download_depot."""
    m = _DL_COMPLETE_RE.search(line)
    if not m:
        return None
    path = m.group(1).replace("\\", "/")
    pm = re.search(r"app_(\d+)/depot_(\d+)", path)
    if not pm:
        return None
    return pm.group(1), pm.group(2), m.group(2)
