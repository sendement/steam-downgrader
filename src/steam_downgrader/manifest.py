"""Reader for Steam depot manifests (depotcache/<depot>_<gid>.manifest).

The on-disk format is a sequence of ``[magic u32][length u32][protobuf]``
sections followed by an end marker. We decode just enough protobuf by hand to
get the file list (payload) and the build timestamp (metadata) -- no
protobuf dependency needed for two messages.

Steam keeps manifests of builds it installed or downloaded (including via
``download_depot``), so depotcache doubles as a local version history.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

_PAYLOAD = 0x71F617D0
_METADATA = 0x1F4812BE
_SIGNATURE = 0x1B81B817
_END = 0x32C415AB

FLAG_DIRECTORY = 64


@dataclass
class ManifestFile:
    name: str  # relative path, forward slashes
    size: int
    flags: int

    @property
    def is_dir(self) -> bool:
        return bool(self.flags & FLAG_DIRECTORY)


@dataclass
class DepotManifest:
    depot_id: str
    gid: str
    created: int  # unix time the build was made
    size_original: int
    filenames_encrypted: bool
    files: list[ManifestFile] = field(default_factory=list)


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def _fields(buf: bytes):
    """Yield (field_number, wire_type, value) for a protobuf message."""
    pos = 0
    n = len(buf)
    while pos < n:
        key, pos = _varint(buf, pos)
        fno, wt = key >> 3, key & 7
        if wt == 0:
            val, pos = _varint(buf, pos)
        elif wt == 1:
            val = struct.unpack_from("<Q", buf, pos)[0]
            pos += 8
        elif wt == 2:
            ln, pos = _varint(buf, pos)
            val = buf[pos : pos + ln]
            pos += ln
        elif wt == 5:
            val = struct.unpack_from("<I", buf, pos)[0]
            pos += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wt}")
        yield fno, wt, val


def _sections(data: bytes) -> dict[int, bytes]:
    out: dict[int, bytes] = {}
    pos = 0
    while pos + 4 <= len(data):
        (magic,) = struct.unpack_from("<I", data, pos)
        pos += 4
        if magic == _END:
            break
        (ln,) = struct.unpack_from("<I", data, pos)
        pos += 4
        out[magic] = data[pos : pos + ln]
        pos += ln
    return out


def read_manifest(path: Path, with_files: bool = True) -> DepotManifest:
    data = path.read_bytes()
    secs = _sections(data)
    meta = secs.get(_METADATA)
    if meta is None:
        raise ValueError(f"{path.name}: no metadata section")

    depot = gid = created = size_orig = 0
    encrypted = False
    for fno, _wt, val in _fields(meta):
        if fno == 1:
            depot = val
        elif fno == 2:
            gid = val
        elif fno == 3:
            created = val
        elif fno == 4:
            encrypted = bool(val)
        elif fno == 5:
            size_orig = val

    m = DepotManifest(str(depot), str(gid), created, size_orig, encrypted)
    if with_files and not encrypted and _PAYLOAD in secs:
        for fno, _wt, mapping in _fields(secs[_PAYLOAD]):
            if fno != 1:
                continue
            name, size, flags = "", 0, 0
            for f2, _w2, v2 in _fields(mapping):
                if f2 == 1:
                    name = v2.decode("utf-8", "replace").replace("\\", "/")
                elif f2 == 2:
                    size = v2
                elif f2 == 3:
                    flags = v2
            m.files.append(ManifestFile(name, size, flags))
    return m
