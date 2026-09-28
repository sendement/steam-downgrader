"""Parse manifest lists pasted from SteamDB.

SteamDB won't talk to scripts, so the user copies from the browser. Accepted:

* the "Previously seen manifests" table of steamdb.info/depot/<id>/manifests/
  (a date and a manifest id per row; rows may wrap over several lines);
* SteamDB's copy formats: ``-app A -depot D -manifest M`` (DepotDownloader)
  and ``download_depot A D M`` (Steam console);
* bare manifest ids, one per line.

Dates are read in the formats SteamDB uses and treated as UTC.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        ["january", "february", "march", "april", "may", "june",
         "july", "august", "september", "october", "november", "december"]
    )
}
_MON = "|".join(_MONTHS) + "|" + "|".join(m[:3] for m in _MONTHS)

_DATE_RES = [
    # 5 September 2025 – 18:23:11 UTC
    re.compile(rf"\b(?P<d>\d{{1,2}})\s+(?P<mon>{_MON})\.?\s+(?P<y>\d{{4}})(?:\D{{1,5}}(?P<H>\d{{1,2}}):(?P<M>\d\d)(?::(?P<S>\d\d))?)?", re.I),
    # September 5, 2025 18:23:11
    re.compile(rf"\b(?P<mon>{_MON})\.?\s+(?P<d>\d{{1,2}}),?\s+(?P<y>\d{{4}})(?:\D{{1,5}}(?P<H>\d{{1,2}}):(?P<M>\d\d)(?::(?P<S>\d\d))?)?", re.I),
    # 2025-09-05 18:23:11 / 2025-09-05T18:23:11Z
    re.compile(r"\b(?P<y>\d{4})-(?P<m>\d\d)-(?P<d>\d\d)(?:[ T](?P<H>\d\d):(?P<M>\d\d)(?::(?P<S>\d\d))?)?"),
]
_DD_RE = re.compile(r"-app\s+(\d+).*?-depot\s+(\d+).*?-manifest\s+(\d+)", re.I)
_CONSOLE_RE = re.compile(r"download_depot\s+(\d+)\s+(\d+)\s+(\d+)", re.I)
_MANIFEST_RE = re.compile(r"(?<![\d.:-])(\d{12,20})(?![\d.:])")


@dataclass
class Entry:
    depot_id: str
    manifest: str
    time: int  # 0 if the paste had no date for it


def _date(line: str) -> tuple[int, tuple[int, int]] | None:
    for rx in _DATE_RES:
        m = rx.search(line)
        if not m:
            continue
        g = m.groupdict()
        month = int(g["m"]) if g.get("m") else _MONTHS.get(g["mon"].lower()) or next(
            v for k, v in _MONTHS.items() if k.startswith(g["mon"].lower()[:3])
        )
        try:
            dt = datetime(
                int(g["y"]), month, int(g["d"]),
                int(g["H"] or 0), int(g["M"] or 0), int(g["S"] or 0),
                tzinfo=timezone.utc,
            )
        except ValueError:
            continue
        return int(dt.timestamp()), m.span()
    return None


def parse(text: str, default_depot: str) -> list[Entry]:
    lines = [ln.strip() for ln in text.splitlines()]
    rows: list[tuple[str, str | None, int | None]] = []  # (manifest, depot, date) per line
    dates: list[int | None] = []
    for ln in lines:
        d = _date(ln)
        stripped = ln
        if d:
            a, b = d[1]
            stripped = ln[:a] + " " + ln[b:]  # keep date digits out of the id search
        dates.append(d[0] if d else None)
        m = _DD_RE.search(ln) or _CONSOLE_RE.search(ln)
        if m:
            rows.append((m.group(3), m.group(2), d[0] if d else None))
            continue
        ids = _MANIFEST_RE.findall(stripped)
        rows.append((ids[0], None, d[0] if d else None) if ids else ("", None, None))

    out: list[Entry] = []
    seen: set[tuple[str, str]] = set()
    for i, (manifest, depot, when) in enumerate(rows):
        if not manifest:
            continue
        if when is None:
            # Table cells that wrapped: take a date-only line right above or below.
            for j in (i - 1, i + 1, i - 2, i + 2):
                if 0 <= j < len(rows) and dates[j] is not None and not rows[j][0]:
                    when = dates[j]
                    break
        key = (depot or default_depot, manifest)
        if key in seen:
            continue
        seen.add(key)
        out.append(Entry(key[0], manifest, when or 0))
    return out


# --- patch notes page: builds ------------------------------------------------

# A bare build id: 5..10 digits (4 would catch years); dots/colons around it
# mean a version or a time.
_BUILD_RE = re.compile(r"(?<![\d.:,/-])(\d{5,10})(?![\d.:,/])")
_AGO_RE = re.compile(r"^\s*(?:a|an|\d+)\s+\w+\s+ago\b\s*", re.I)


@dataclass
class BuildEntry:
    buildid: int
    time: int
    title: str


def parse_builds(text: str) -> list[BuildEntry]:
    """Rows of steamdb.info/app/<id>/patchnotes/: date, build id, title.
    Lines that carry a manifest id are left to ``parse``."""
    out: dict[int, BuildEntry] = {}
    lines = [ln.strip() for ln in text.splitlines()]
    for i, ln in enumerate(lines):
        if not ln or _DD_RE.search(ln) or _CONSOLE_RE.search(ln):
            continue
        d = _date(ln)
        rest = ln
        if d:
            a, b = d[1]
            rest = ln[:a] + "\t" + ln[b:]
        if _MANIFEST_RE.search(rest):
            continue
        m = re.search(r"(?i)build(?:\s*id)?\s*[:#]?\s*(\d{4,10})", rest) or _BUILD_RE.search(rest)
        if not m:
            continue
        buildid = int(m.group(1))
        when = d[0] if d else 0
        if not when:  # wrapped row: date on a neighbouring line
            for j in (i - 1, i - 2, i + 1, i + 2):
                if 0 <= j < len(lines) and (dj := _date(lines[j])) and not _BUILD_RE.search(lines[j][: dj[1][0]] + lines[j][dj[1][1] :]):
                    when = dj[0]
                    break
        title = _AGO_RE.sub("", rest[m.end():].strip(" \t–—-|")).strip(" \t–—-|")
        if buildid not in out:
            out[buildid] = BuildEntry(buildid, when, title)
    return list(out.values())
