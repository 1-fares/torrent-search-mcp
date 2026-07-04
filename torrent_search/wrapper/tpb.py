"""Direct HTTP scraper for thepiratebay.org via the apibay.org JSON API.

Bypasses Chromium entirely: thepiratebay.org's HTML response is just a JS shell
that calls apibay.org/q.php?q=... and renders the JSON. We hit that API directly
and assemble the same Torrent records, including a magnet link built from the
info_hash plus TPB's published tracker set.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from .models import Torrent

logger = logging.getLogger(__name__)

APIBAY_URL = "https://apibay.org/q.php"
SOURCE = "thepiratebay.org"

# Tracker list that thepiratebay.org's static main.js embeds in every magnet link.
# Keeping it identical to TPB's own output so links from this scraper are
# indistinguishable from links the official site hands out.
TPB_TRACKERS: tuple[str, ...] = (
    "udp://tracker.opentrackr.org:1337",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.bittor.pw:1337/announce",
    "udp://public.popcorn-tracker.org:6969/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://exodus.desync.com:6969",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker2.dler.com/announce",
    "udp://tracker3.dler.com:2710/announce",
    "udp://glotorrents.pw:6969/announce",
    "udp://tracker.coppersurfer.tk:6969",
    "udp://torrent.gresille.org:80/announce",
    "udp://p4p.arenabg.com:1337",
    "udp://tracker.internetwarriors.net:1337",
)

# Top-level category groups (the hundreds digit of the cat code).
_TPB_TOP_CATEGORIES: dict[int, str] = {
    1: "Audio",
    2: "Video",
    3: "Applications",
    4: "Games",
    5: "Porn",
    6: "Other",
}

# Subcategory map sourced from thepiratebay.org/static/main.js.
_TPB_SUBCATEGORIES: dict[int, str] = {
    101: "Music",
    102: "Audio books",
    103: "Sound clips",
    104: "FLAC",
    199: "Other",
    201: "Movies",
    202: "Movies DVDR",
    203: "Music videos",
    204: "Movie clips",
    205: "TV shows",
    206: "Handheld",
    207: "HD - Movies",
    208: "HD - TV shows",
    209: "3D",
    210: "CAM/TS",
    211: "UHD/4k - Movies",
    212: "UHD/4k - TV shows",
    299: "Other",
    301: "Windows",
    302: "Mac",
    303: "UNIX",
    304: "Handheld",
    305: "IOS (iPad/iPhone)",
    306: "Android",
    399: "Other OS",
    401: "PC",
    402: "Mac",
    403: "PSx",
    404: "XBOX360",
    405: "Wii",
    406: "Handheld",
    407: "IOS (iPad/iPhone)",
    408: "Android",
    499: "Other",
    501: "Movies",
    502: "Movies DVDR",
    503: "Pictures",
    504: "Games",
    505: "HD - Movies",
    506: "Movie clips",
    507: "UHD/4k - Movies",
    599: "Other",
    601: "E-books",
    602: "Comics",
}


def _category_label(code: str | int) -> str:
    """Translate apibay's numeric category to TPB's "Group - Subgroup" label."""
    try:
        cat = int(code)
    except (TypeError, ValueError):
        return ""
    if cat == 0:
        return ""
    top = _TPB_TOP_CATEGORIES.get(cat // 100, "")
    sub = _TPB_SUBCATEGORIES.get(cat, "")
    if top and sub:
        return f"{top} - {sub}"
    return top or sub


def _humanize_size(byte_count: str | int) -> str:
    """Format a byte count the same way TPB's UI does ('1.5 GiB' style)."""
    try:
        n = float(byte_count)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if n < 1024.0:
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.2f} EiB"


def _format_date(unix_ts: str | int) -> str:
    try:
        ts = int(unix_ts)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _build_magnet(info_hash: str, name: str) -> str:
    parts = [f"magnet:?xt=urn:btih:{info_hash.upper()}", f"dn={quote(name)}"]
    parts.extend(f"tr={quote(t, safe='')}" for t in TPB_TRACKERS)
    return "&".join(parts)


def _is_empty_sentinel(entries: list[dict[str, Any]]) -> bool:
    """apibay returns a single 'No results returned' row instead of an empty list."""
    return len(entries) == 1 and entries[0].get("id") in ("0", 0)


async def fetch_tpb(query: str, client: httpx.AsyncClient) -> list[Torrent]:
    """Fetch and parse a TPB search via apibay.org. Returns [] on any failure."""
    params = {"q": query, "cat": "0"}
    try:
        response = await client.get(APIBAY_URL, params=params)
        response.raise_for_status()
        entries: list[dict[str, Any]] = response.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("apibay request failed for %r: %s", query, e)
        return []

    if not isinstance(entries, list) or not entries or _is_empty_sentinel(entries):
        return []

    torrents: list[Torrent] = []
    for entry in entries:
        try:
            info_hash = str(entry.get("info_hash", "")).strip()
            name = str(entry.get("name", "")).strip()
            if not info_hash or not name:
                continue
            torrents.append(
                Torrent.format(
                    id=str(entry.get("id") or ""),
                    filename=name,
                    category=_category_label(entry.get("category", 0)),
                    size=_humanize_size(entry.get("size", 0)),
                    seeders=entry.get("seeders", 0),
                    leechers=entry.get("leechers", 0),
                    downloads=None,  # apibay doesn't expose this
                    date=_format_date(entry.get("added", 0)),
                    magnet_link=_build_magnet(info_hash, name),
                    uploader=entry.get("username") or None,
                    source=SOURCE,
                )
            )
        except ValidationError as e:
            logger.debug("Skipped TPB entry (validation): %s | row=%r", e, entry)
        except Exception as e:
            logger.debug("Skipped TPB entry (parse): %s | row=%r", e, entry)
    return torrents
