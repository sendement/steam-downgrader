"""Command line interface. Without a subcommand the GUI starts."""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

from .appinfo import AppInfoCache
from .history import History, fmt_time
from .ops import OpError, actual_depots, relock, unlock
from .staging import check_staged, find_staged
from .state import State
from .steam import Steam, default_steam_root, is_tool


def _steam() -> Steam:
    root = default_steam_root()
    if not root:
        sys.exit("Steam не найден")
    return Steam(root)


def cmd_list(args) -> None:
    steam, state = _steam(), State()
    for g in steam.games():
        if is_tool(g):
            continue
        mark = "🔒" if state.lock_for(g.app_id) else "  "
        print(f"{mark} {g.app_id:>8}  build {g.buildid:<10} {g.name}")


def cmd_versions(args) -> None:
    steam, state = _steam(), State()
    game = steam.game(args.appid) or sys.exit("игра не установлена")
    info = AppInfoCache(steam.appinfo_path).get(game.app_id)
    hist = History(steam, state)
    actual = actual_depots(state, game)
    cands = hist.candidates(game, info, actual)
    print(f"{game.name} — установлено: {actual}")
    for v in hist.versions(game, info, cands):
        cur = " (установлена)" if all(v.depots.get(d) in (None, m) for d, m in actual.items()) else ""
        print(f"\n{fmt_time(v.time)}  {v.title}  [{v.source}{'' if v.exact else ', восстановлено'}]{cur}")
        for d, m in v.depots.items():
            print(f"    {d}: {m or '?'}")
    for st in find_staged(steam, hist, game.app_id):
        chk = check_staged(steam, st)
        print(f"\nЗагружено: депо {st.depot_id} манифест {st.manifest or '?'} ({st.source}) — {chk.files_ok}/{chk.files_expected} файлов OK")


def cmd_relock(args) -> None:
    steam, state = _steam(), State()
    for name, old, new in relock(steam, state, AppInfoCache(steam.appinfo_path)):
        print(f"{name}: подменённая сборка {old} → {new}")
        if steam.is_running():
            print("  Steam запущен — перезапустите его, чтобы он перечитал appmanifest.")


def cmd_unlock(args) -> None:
    steam, state = _steam(), State()
    game = steam.game(args.appid) or sys.exit("игра не установлена")
    try:
        unlock(steam, state, game, validate=args.validate)
    except OpError as e:
        sys.exit(str(e))
    print(f"{game.name}: защита снята")


SERVICE_NAME = "steam-downgrader-relock"


def install_relock_units(steam: Steam) -> Path:
    """systemd --user path unit: re-run relock whenever Steam refreshes appinfo.vdf."""
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    exec_line = " ".join(shlex.quote(a) for a in [sys.executable, "-m", "steam_downgrader", "relock"])
    (unit_dir / f"{SERVICE_NAME}.service").write_text(
        "[Unit]\nDescription=Keep steam-downgrader locks ahead of new Steam builds\n\n"
        f"[Service]\nType=oneshot\nExecStart={exec_line}\n"
    )
    (unit_dir / f"{SERVICE_NAME}.path").write_text(
        "[Unit]\nDescription=Watch Steam appinfo.vdf for new builds\n\n"
        f"[Path]\nPathChanged={steam.appinfo_path}\n\n[Install]\nWantedBy=default.target\n"
    )
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", f"{SERVICE_NAME}.path"], check=True)
    return unit_dir


def remove_relock_units() -> None:
    subprocess.run(["systemctl", "--user", "disable", "--now", f"{SERVICE_NAME}.path"], check=False)
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    for ext in ("path", "service"):
        (unit_dir / f"{SERVICE_NAME}.{ext}").unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)


def relock_units_installed() -> bool:
    r = subprocess.run(["systemctl", "--user", "is-enabled", f"{SERVICE_NAME}.path"], capture_output=True, text=True)
    return r.stdout.strip() == "enabled"


def cmd_service(args) -> None:
    if args.action == "install":
        print("Установлено в", install_relock_units(_steam()))
    else:
        remove_relock_units()
        print("Удалено")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="steam-downgrader", description="Откат игр Steam и защита от обновлений")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("list", help="установленные игры").set_defaults(func=cmd_list)
    v = sub.add_parser("versions", help="известные версии игры")
    v.add_argument("appid")
    v.set_defaults(func=cmd_versions)
    sub.add_parser("relock", help="подтянуть подмену к новой сборке Steam").set_defaults(func=cmd_relock)
    u = sub.add_parser("unlock", help="снять защиту")
    u.add_argument("appid")
    u.add_argument("--validate", action="store_true", help="сразу проверить файлы (вернуть актуальную версию)")
    u.set_defaults(func=cmd_unlock)
    s = sub.add_parser("service", help="systemd-служба авто-relock")
    s.add_argument("action", choices=["install", "remove"])
    s.set_defaults(func=cmd_service)

    args = p.parse_args(argv)
    if not args.cmd:
        from .gui.app import run

        sys.exit(run())
    args.func(args)
