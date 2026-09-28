#!/usr/bin/env python3
"""Stands in for steam_downgrader.native.worker: same CLI, same JSON events.
Without a stored token it asks for sign-in (exit 3), like the real one."""
import argparse
import json
import sys
from pathlib import Path

from steam_downgrader.auth import load_token
from steam_downgrader.native.assemble import COMPLETE, FILES_LIST


def emit(ev, **kw):
    print(json.dumps({"ev": ev, **kw}), flush=True)


p = argparse.ArgumentParser()
for a in ("--app", "--depot", "--manifest", "--out", "--base-dir", "--base-manifest", "--depotcache"):
    p.add_argument(a, default="")
args = p.parse_args()
if not load_token():
    emit("auth_required", reason="нет сохранённого входа")
    sys.exit(3)
print("worker diagnostics on stderr", file=sys.stderr)
if args.manifest == "999":
    emit("error", message="Нет доступа к депо (манифест недоступен)")
    sys.exit(1)
emit("status", text="Загрузка манифеста…")
emit("plan", total=15, to_download=9, reused=0, unchanged=6, resumed=0, chunks=2)
emit("progress", phase="download", done=15, total=15, downloaded=9, to_download=9, reused=0, unchanged=6, speed=1000)
out = Path(args.out)
(out / "bin").mkdir(parents=True, exist_ok=True)
(out / "bin" / "game.exe").write_bytes(b"native-exe")
# data/a.pak is "unchanged": stays as installed, not staged
(out / FILES_LIST).write_text(json.dumps({
    "manifest": args.manifest, "base_manifest": args.base_manifest,
    "files": [["bin/game.exe", 10, False], ["data/a.pak", 5, True]],
}))
(out / COMPLETE).touch()
emit("done", downloaded=9, reused=0, unchanged=6, resumed=0, total=15)
