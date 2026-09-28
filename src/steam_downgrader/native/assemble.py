"""Build one depot version in a staging folder from as little network as possible.

For every file of the target manifest, in order of preference:

1. **unchanged** -- the installed file already is the target (same SHA-1 in both
   manifests, same size, and its bytes hash to it). Nothing is written; apply
   leaves the installed file in place.
2. **resumed** -- a previous, interrupted run already wrote this chunk into the
   staging file (verified by hash).
3. **reused** -- the chunk exists in the installed build (any file, any offset;
   game patches usually rewrite parts of big archives) and is copied locally.
4. **downloaded** from the CDN.

Every chunk is checked against its SHA-1 before it is written. The network is
reached only through ``fetch(sha) -> bytes``, which keeps this testable.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

FLAG_EXECUTABLE = 32
FLAG_DIRECTORY = 64

FILES_LIST = ".sd_files.json"
COMPLETE = ".complete"


@dataclass
class TChunk:
    sha: bytes
    offset: int
    size: int


@dataclass
class TFile:
    name: str  # forward slashes
    size: int
    flags: int
    sha: bytes
    chunks: list[TChunk]
    link: str = ""

    @property
    def is_dir(self) -> bool:
        return bool(self.flags & FLAG_DIRECTORY)


@dataclass
class Stats:
    total: int = 0  # bytes of all target files
    unchanged: int = 0
    resumed: int = 0
    reused: int = 0
    downloaded: int = 0
    to_download: int = 0

    @property
    def done(self) -> int:
        return self.unchanged + self.resumed + self.reused + self.downloaded


@dataclass
class Plan:
    unchanged: set[str] = field(default_factory=set)
    # (file, chunk) pairs still to fetch, grouped by file for fd reuse
    download: list[tuple[TFile, TChunk]] = field(default_factory=list)


class ChunkError(RuntimeError):
    pass


def _sha1(b: bytes) -> bytes:
    return hashlib.sha1(b).digest()


class Assembler:
    def __init__(
        self,
        files: Iterable[TFile],
        out_dir: Path,
        base_files: Iterable[TFile] = (),
        base_dir: Path | None = None,
        progress: Callable[[str, Stats], None] = lambda phase, s: None,
    ):
        self.files = [f for f in files]
        self.out = out_dir
        self.base_dir = base_dir
        self.progress = progress
        self.stats = Stats(total=sum(f.size for f in self.files if not f.is_dir))
        self.base_by_name = {f.name.lower(): f for f in base_files if not f.is_dir}
        self.base_chunks: dict[bytes, tuple[str, int, int]] = {}
        for f in self.base_by_name.values():
            for c in f.chunks:
                self.base_chunks.setdefault(c.sha, (f.name, c.offset, c.size))
        self._fds: dict[Path, int] = {}

    # --- helpers ---------------------------------------------------------------

    def _fd(self, path: Path, write: bool) -> int:
        fd = self._fds.get(path)
        if fd is None:
            if write:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
            else:
                fd = os.open(path, os.O_RDONLY)
            self._fds[path] = fd
        return fd

    def close(self) -> None:
        for fd in self._fds.values():
            os.close(fd)
        self._fds.clear()

    def _installed_matches(self, f: TFile) -> bool:
        base = self.base_by_name.get(f.name.lower())
        if not (self.base_dir and base and base.sha == f.sha and base.size == f.size):
            return False
        p = self.base_dir / f.name
        try:
            if p.stat().st_size != f.size:
                return False
        except OSError:
            return False
        h = hashlib.sha1()
        with open(p, "rb") as fh:
            while block := fh.read(8 << 20):
                h.update(block)
                self.stats.unchanged += len(block)
                self.progress("scan", self.stats)
        if h.digest() != f.sha:
            self.stats.unchanged -= f.size
            return False
        return True

    # --- phase 1: everything that doesn't need the network ------------------------

    def plan(self) -> Plan:
        plan = Plan()
        self.out.mkdir(parents=True, exist_ok=True)
        for f in self.files:
            if f.is_dir or f.link:
                continue
            if self._installed_matches(f):
                plan.unchanged.add(f.name)
                continue
            out = self.out / f.name
            existed = out.exists() and out.stat().st_size == f.size
            fd = self._fd(out, write=True)
            if not existed:
                os.ftruncate(fd, f.size)
            for c in f.chunks:
                if existed:
                    data = os.pread(fd, c.size, c.offset)
                    if _sha1(data) == c.sha:
                        self.stats.resumed += c.size
                        continue
                src = self.base_chunks.get(c.sha)
                if src and self.base_dir:
                    try:
                        data = os.pread(self._fd(self.base_dir / src[0], write=False), src[2], src[1])
                    except OSError:
                        data = b""
                    if _sha1(data) == c.sha:
                        os.pwrite(fd, data, c.offset)
                        self.stats.reused += c.size
                        self.progress("scan", self.stats)
                        continue
                plan.download.append((f, c))
                self.stats.to_download += c.size
            self.progress("scan", self.stats)
        return plan

    # --- phase 2: network ------------------------------------------------------------

    def store(self, f: TFile, c: TChunk, data: bytes) -> None:
        if _sha1(data) != c.sha:
            raise ChunkError(f"чанк {c.sha.hex()} файла {f.name} повреждён")
        os.pwrite(self._fd(self.out / f.name, write=True), data, c.offset)
        self.stats.downloaded += c.size

    def fetch_all(self, plan: Plan, fetch: Callable[[bytes], bytes], attempts: int = 4) -> None:
        """Sequential variant (tests); the worker runs store() from a gevent pool."""
        for f, c in plan.download:
            for i in range(attempts):
                try:
                    self.store(f, c, fetch(c.sha))
                    break
                except ChunkError:
                    if i == attempts - 1:
                        raise
            self.progress("download", self.stats)

    # --- phase 3 -----------------------------------------------------------------------

    def finish(self, plan: Plan, manifest_gid: str, base_gid: str) -> None:
        self.close()
        entries = []
        for f in self.files:
            p = self.out / f.name
            if f.is_dir:
                p.mkdir(parents=True, exist_ok=True)
                continue
            if f.link:
                p.parent.mkdir(parents=True, exist_ok=True)
                if not p.is_symlink():
                    p.symlink_to(f.link)
                continue
            unchanged = f.name in plan.unchanged
            if not unchanged:
                if p.stat().st_size != f.size:
                    raise ChunkError(f"{f.name}: размер {p.stat().st_size} вместо {f.size}")
                if f.flags & FLAG_EXECUTABLE:
                    os.chmod(p, 0o755)
            entries.append([f.name, f.size, unchanged])
        (self.out / FILES_LIST).write_text(
            json.dumps({"manifest": manifest_gid, "base_manifest": base_gid, "files": entries}, ensure_ascii=False)
        )
        (self.out / COMPLETE).touch()


def read_files_list(staged_dir: Path) -> dict | None:
    try:
        return json.loads((staged_dir / FILES_LIST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
