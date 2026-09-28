"""Human version numbers ("1.16.1") for builds.

Steam itself only knows build ids. Version numbers live in patch note titles:

* SteamDB patch notes (imported by the user): title belongs to that exact build;
* Steam news (ISteamNews/GetNewsForApp, public, no key): patch note posts,
  matched to builds by date -- approximate, shown with "≈".
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass

NEWS_URL = (
    "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
    "?appid={app}&count=500&maxlength=1&feeds=steam_community_announcements"
)
NEWS_TTL = 12 * 3600

_KEYWORD_RE = re.compile(r"(?i)\b(?:patch|update|version|ver|hotfix|release|notes?|v)\b\.?")
_VERSION_RE = re.compile(r"(?<![\w.])v?(\d{1,4}(?:\.\d{1,5}){1,3}[a-z]?)(?![\w.]*\d)", re.I)
# Things that look like x.y but aren't versions: "4.5 million", "2.0x speed".
_NOT_VERSION_AFTER = re.compile(r"^\s*(?:million|billion|k\b|m\b|%|x\b|gb|mb|fps|hz)", re.I)


@dataclass
class PatchNote:
    time: int
    version: str
    title: str
    url: str = ""


def extract_version(title: str, game_name: str = "") -> str | None:
    """Version number mentioned in a patch note title, or None."""
    t = title
    if game_name:  # "NieR Replicant ver.1.22474487139..." is a name, not a version
        t = t.replace(game_name, " ")
    candidates = []
    for m in _VERSION_RE.finditer(t):
        if _NOT_VERSION_AFTER.match(t[m.end():]):
            continue
        # Prefer numbers right after "patch"/"version"/…
        before = t[max(0, m.start() - 12) : m.start()]
        candidates.append((0 if _KEYWORD_RE.search(before) else 1, m.group(1)))
    if not candidates:
        return None
    return min(candidates, key=lambda c: c[0])[1]


def fetch_patch_notes(app_id: str, game_name: str = "") -> list[PatchNote]:
    req = urllib.request.Request(NEWS_URL.format(app=app_id), headers={"User-Agent": "steam-downgrader"})
    with urllib.request.urlopen(req, timeout=15) as r:
        items = json.load(r).get("appnews", {}).get("newsitems", [])
    out = []
    for n in items:
        title = n.get("title", "")
        tags = n.get("tags") or []
        v = extract_version(title, game_name)
        if not v:
            continue
        # Keep patch-note posts, titled updates, or unmistakable x.y.z numbers.
        if "patchnotes" not in tags and v.count(".") < 2 and not _KEYWORD_RE.search(title.replace(game_name, "")):
            continue
        out.append(PatchNote(int(n.get("date", 0)), v, title, n.get("url", "")))
    return sorted(out, key=lambda p: p.time)


def cached_patch_notes(state, app_id: str, game_name: str, max_age: int = NEWS_TTL) -> list[PatchNote] | None:
    """From state if fresh enough, else None (caller fetches in background)."""
    c = state.data.setdefault("news", {}).get(app_id)
    if not c or time.time() - c.get("fetched", 0) > max_age:
        return None
    return [PatchNote(**p) for p in c["items"]]


def store_patch_notes(state, app_id: str, notes: list[PatchNote]) -> None:
    state.data.setdefault("news", {})[app_id] = {
        "fetched": int(time.time()),
        "items": [n.__dict__ for n in notes],
    }


# A build is usually made hours to days before its patch notes go out.
_LEAD = 3 * 86400


def version_at(t: int, notes: list[PatchNote], lead: int = _LEAD) -> PatchNote | None:
    """Newest patch note published by ``t`` (+``lead``)."""
    if not t:
        return None
    best = None
    for n in notes:
        if n.time <= t + lead:
            best = n
        else:
            break
    return best
