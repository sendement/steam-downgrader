"""Valve KeyValues in both flavours Steam uses on disk.

* Text VDF (appmanifest_*.acf, libraryfolders.vdf) -- parsed *and* written
  back, since locking a game means editing its appmanifest. Dicts keep
  insertion order, so a parse/dump round trip preserves Steam's layout.
* Binary VDF as embedded in appcache/appinfo.vdf (read-only).
"""

from __future__ import annotations

import re
import struct

_TOKEN_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\\\", "\\")


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def parse_vdf(text: str) -> dict:
    tokens: list[tuple[str, bool]] = []  # (value, is_brace)
    for m in _TOKEN_RE.finditer(text):
        if m.group(1) is not None:
            tokens.append((_unescape(m.group(1)), False))
        else:
            tokens.append((m.group(2), True))
    pos = 0

    def parse_object(top: bool) -> dict:
        nonlocal pos
        obj: dict = {}
        while pos < len(tokens):
            tok, brace = tokens[pos]
            pos += 1
            if brace and tok == "}":
                if top:
                    continue
                return obj
            if pos >= len(tokens):
                obj[tok] = ""
                break
            nxt, nbrace = tokens[pos]
            if nbrace and nxt == "{":
                pos += 1
                obj[tok] = parse_object(False)
            else:
                obj[tok] = nxt
                pos += 1
        return obj

    return parse_object(True)


def dump_vdf(data: dict) -> str:
    out: list[str] = []

    def emit(obj: dict, depth: int) -> None:
        ind = "\t" * depth
        for k, v in obj.items():
            if isinstance(v, dict):
                out.append(f'{ind}"{_escape(k)}"\n{ind}{{\n')
                emit(v, depth + 1)
                out.append(f"{ind}}}\n")
            else:
                out.append(f'{ind}"{_escape(k)}"\t\t"{_escape(str(v))}"\n')

    emit(data, 0)
    return "".join(out)


# --- binary KeyValues -------------------------------------------------------

_BIN_NONE, _BIN_STRING, _BIN_INT32, _BIN_FLOAT32 = 0x00, 0x01, 0x02, 0x03
_BIN_UINT64, _BIN_END, _BIN_INT64 = 0x07, 0x08, 0x0A


def parse_binary_kv(buf: bytes, pos: int, strings: list[str] | None) -> tuple[dict, int]:
    """Parse one binary KV object starting at ``pos``. ``strings`` is the
    appinfo v29 key table; ``None`` means keys are inline C strings (v28)."""
    obj: dict = {}
    while True:
        t = buf[pos]
        pos += 1
        if t == _BIN_END:
            return obj, pos
        if strings is not None:
            key = strings[struct.unpack_from("<I", buf, pos)[0]]
            pos += 4
        else:
            end = buf.index(b"\0", pos)
            key = buf[pos:end].decode("utf-8", "replace")
            pos = end + 1
        if t == _BIN_NONE:
            val, pos = parse_binary_kv(buf, pos, strings)
        elif t == _BIN_STRING:
            end = buf.index(b"\0", pos)
            val = buf[pos:end].decode("utf-8", "replace")
            pos = end + 1
        elif t == _BIN_INT32:
            val = struct.unpack_from("<i", buf, pos)[0]
            pos += 4
        elif t == _BIN_FLOAT32:
            val = struct.unpack_from("<f", buf, pos)[0]
            pos += 4
        elif t == _BIN_UINT64:
            val = struct.unpack_from("<Q", buf, pos)[0]
            pos += 8
        elif t == _BIN_INT64:
            val = struct.unpack_from("<q", buf, pos)[0]
            pos += 8
        else:
            raise ValueError(f"unknown binary KV type 0x{t:02x} at offset {pos - 1}")
        obj[key] = val
