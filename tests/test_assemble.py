"""Delta assembly on synthetic depots: unchanged / reused / resumed / downloaded."""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from steam_downgrader.native.assemble import Assembler, TChunk, TFile, read_files_list  # noqa: E402

CH = 4  # tiny chunk size


def mkfile(name: str, data: bytes) -> TFile:
    chunks = [TChunk(hashlib.sha1(data[o:o + CH]).digest(), o, len(data[o:o + CH])) for o in range(0, len(data), CH)]
    return TFile(name, len(data), 0, hashlib.sha1(data).digest(), chunks)


def test_delta() -> None:
    tmp = Path(tempfile.mkdtemp())
    inst, out = tmp / "install", tmp / "staging"
    old = {"same.bin": b"AAAABBBBCCCC", "pak.bin": b"1111222233334444", "gone.bin": b"xxxx"}
    new = {"same.bin": b"AAAABBBBCCCC", "pak.bin": b"1111ZZZZ33334444YYYY", "fresh.bin": b"BBBBnewN"}
    for n, d in old.items():
        (inst / n).parent.mkdir(parents=True, exist_ok=True)
        (inst / n).write_bytes(d)
    base = [mkfile(n, d) for n, d in old.items()]
    target = [mkfile(n, d) for n, d in new.items()] + [TFile("emptydir", 0, 64, b"", [])]
    store = {c.sha: new[f.name][c.offset:c.offset + c.size] for f in target for c in f.chunks}
    fetched: list[bytes] = []

    def fetch(sha: bytes) -> bytes:
        fetched.append(sha)
        return store[sha]

    a = Assembler(target, out, base, inst)
    plan = a.plan()
    assert plan.unchanged == {"same.bin"}
    # pak: 1111/3333/4444 reused from the old pak, ZZZZ/YYYY downloaded;
    # fresh: BBBB reused from same.bin, newN downloaded
    assert sorted(store[s] for s in (c.sha for _, c in plan.download)) == [b"YYYY", b"ZZZZ", b"newN"]
    a.fetch_all(plan, fetch)
    a.finish(plan, "222", "111")
    assert (out / "pak.bin").read_bytes() == new["pak.bin"]
    assert (out / "fresh.bin").read_bytes() == new["fresh.bin"]
    assert not (out / "same.bin").exists(), "unchanged files are not staged"
    assert (out / "emptydir").is_dir()
    assert a.stats.downloaded == 12 and a.stats.reused == 16 and a.stats.unchanged == 12, a.stats
    lst = read_files_list(out)
    assert lst["base_manifest"] == "111" and ["same.bin", 12, True] in lst["files"]

    # Resume: corrupt one chunk of a finished file, drop the marker, run again.
    os.remove(out / ".complete")
    with open(out / "pak.bin", "r+b") as fh:
        fh.seek(4)
        fh.write(b"????")
    fetched.clear()
    a2 = Assembler(target, out, base, inst)
    p2 = a2.plan()
    assert [store[c.sha] for _, c in p2.download] == [b"ZZZZ"], "only the damaged chunk is fetched again"
    a2.fetch_all(p2, fetch)
    a2.finish(p2, "222", "111")
    assert (out / "pak.bin").read_bytes() == new["pak.bin"]
    print("assemble ok")


if __name__ == "__main__":
    test_delta()
