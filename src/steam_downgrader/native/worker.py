"""Download one depot manifest into a staging folder. Runs as its own process
(``python -m steam_downgrader.native.worker``): the Steam library is gevent
based, which doesn't mix with Qt.

Talks JSON lines on stdout: ``{"ev": ...}``. Diagnostics go to stderr.
Exit codes: 0 done, 1 error, 3 sign-in required (no/expired token).
"""

from steam.monkey import patch_minimal  # isort: skip

patch_minimal()  # before anything opens a socket

import argparse  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
from pathlib import Path  # noqa: E402

import gevent  # noqa: E402
from gevent.pool import Pool  # noqa: E402

EXIT_OK, EXIT_ERROR, EXIT_AUTH = 0, 1, 3
# EResults that mean "the stored token is no good": InvalidPassword,
# AccessDenied, Revoked, Expired, InvalidSignature, AccountLoginDeniedNeedTwoFactor…
AUTH_ERESULTS = {5, 15, 26, 27, 63, 65, 85, 88, 96}


def emit(ev: str, **kw) -> None:
    sys.stdout.write(json.dumps({"ev": ev, **kw}, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _tfiles(mappings):
    from .assemble import TChunk, TFile

    out = []
    for m in mappings:
        out.append(TFile(
            name=m.filename.rstrip("\x00").replace("\\", "/"),
            size=m.size,
            flags=m.flags,
            sha=bytes(m.sha_content),
            chunks=sorted((TChunk(bytes(c.sha), c.offset, c.cb_original) for c in m.chunks), key=lambda c: c.offset),
            link=(getattr(m, "linktarget", "") or "").rstrip("\x00"),
        ))
    return out


def _local_base(depotcache: Path, depot: int, gid: str):
    from ..manifest import read_manifest
    from .assemble import TChunk, TFile

    p = depotcache / f"{depot}_{gid}.manifest"
    if not p.is_file():
        return None
    m = read_manifest(p, with_chunks=True)
    if m.filenames_encrypted:
        return None
    return [TFile(f.name, f.size, f.flags, f.sha, [TChunk(c.sha, c.offset, c.size) for c in f.chunks]) for f in m.files]


def run(args) -> int:
    from steam.client import SteamClient
    from steam.client.cdn import CDNClient
    from steam.exceptions import SteamError

    from ..auth import clear_token, load_token
    from .assemble import Assembler, ChunkError

    client = SteamClient()
    if args.anonymous:
        res = client.anonymous_login()
    else:
        tok = load_token()
        if not tok:
            emit("auth_required", reason="нет сохранённого входа")
            return EXIT_AUTH
        emit("status", text=f"Вход в Steam ({tok.account_name})…")
        res = client.login(tok.account_name, access_token=tok.refresh_token)
        if int(res) in AUTH_ERESULTS:
            clear_token()
            emit("auth_required", reason=f"сохранённый вход больше не действует ({res!r})")
            return EXIT_AUTH
    if int(res) != 1:
        emit("error", message=f"Не удалось войти в Steam: {res!r}")
        return EXIT_ERROR

    app, depot = int(args.app), int(args.depot)
    cdn = CDNClient(client)

    def manifest(gid: str):
        code = cdn.get_manifest_request_code(app, depot, int(gid))
        return cdn.get_manifest(app, depot, int(gid), decrypt=True, manifest_request_code=code)

    emit("status", text="Загрузка манифеста…")
    try:
        target = _tfiles(manifest(args.manifest).payload.mappings)
    except SteamError as e:
        msg = str(e)
        if "AccessDenied" in msg or "403" in msg or "401" in msg:
            msg = f"Нет доступа к депо {depot} (аккаунт не владеет им или манифест недоступен): {msg}"
        emit("error", message=msg)
        return EXIT_ERROR

    base = None
    if args.base_manifest and args.base_dir:
        base = _local_base(Path(args.depotcache), depot, args.base_manifest) if args.depotcache else None
        if base is None:
            try:
                emit("status", text="Загрузка манифеста установленной версии…")
                base = _tfiles(manifest(args.base_manifest).payload.mappings)
            except SteamError as e:
                emit("log", msg=f"Манифест установленной версии недоступен ({e}) — без дельты")

    last = [0.0]
    speed = [0.0, time.monotonic(), 0]  # ema, t, bytes

    def progress(phase, s):
        now = time.monotonic()
        if now - last[0] < 0.3:
            return
        last[0] = now
        dt = now - speed[1]
        if phase == "download" and dt > 0:
            inst = (s.downloaded - speed[2]) / dt
            speed[0] = inst if speed[0] == 0 else 0.7 * speed[0] + 0.3 * inst
        speed[1], speed[2] = now, s.downloaded
        emit("progress", phase=phase, done=s.done, total=s.total, downloaded=s.downloaded,
             to_download=s.to_download, reused=s.reused, unchanged=s.unchanged, speed=int(speed[0]))

    out = Path(args.out)
    asm = Assembler(target, out, base or (), Path(args.base_dir) if base else None, progress)
    emit("manifest", files=sum(not f.is_dir for f in target), total=asm.stats.total, delta=base is not None)
    emit("status", text="Проверка установленных файлов…" if base else "Подготовка…")
    plan = asm.plan()
    emit("plan", total=asm.stats.total, to_download=asm.stats.to_download, reused=asm.stats.reused,
         unchanged=asm.stats.unchanged, resumed=asm.stats.resumed, chunks=len(plan.download))

    emit("status", text="Загрузка…")
    speed[1], speed[2] = time.monotonic(), 0
    failed: list[str] = []

    def job(f, c):
        for attempt in range(5):
            try:
                asm.store(f, c, cdn.get_chunk(app, depot, c.sha.hex()))
                progress("download", asm.stats)
                return
            except (SteamError, ChunkError, OSError) as e:
                if attempt == 4:
                    failed.append(f"{f.name}: {e}")
                gevent.sleep(1 + attempt)

    pool = Pool(args.threads)
    for f, c in plan.download:
        if failed:
            break
        pool.spawn(job, f, c)
    pool.join()
    if failed:
        asm.close()
        emit("error", message=f"Не удалось скачать {len(failed)} чанков, первый: {failed[0]}")
        return EXIT_ERROR

    asm.finish(plan, args.manifest, args.base_manifest or "")
    emit("done", downloaded=asm.stats.downloaded, reused=asm.stats.reused,
         unchanged=asm.stats.unchanged, resumed=asm.stats.resumed, total=asm.stats.total)
    client.logout()
    return EXIT_OK


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--app", required=True)
    p.add_argument("--depot", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--base-dir", default="")
    p.add_argument("--base-manifest", default="")
    p.add_argument("--depotcache", default="")
    p.add_argument("--threads", type=int, default=12)
    p.add_argument("--anonymous", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args()
    try:
        code = run(args)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        emit("error", message=f"{type(e).__name__}: {e}")
        code = EXIT_ERROR
    sys.stdout.flush()
    sys.exit(code)


if __name__ == "__main__":
    main()
