"""
Extra cross-platform link resolvers for OdesliEnricher (AUDIT-API-1).

Squigly, MusicBrainz URL relations and Unitune each turn one track into the
streaming-service URLs for it. They run only after song.link / Songwhip /
Songstats found no Amazon id, and only ever contribute ids parsed out of REAL
track URLs: Unitune answers with search-page URLs for services it could not
match (`music.amazon.com/search/...`), and an id guessed from those is the
BUG-ASIN-1 failure (a toy or a book downloaded as a song).
"""
import json
import logging
import re
from typing import Optional

import requests

from antra.utils.matching import has_severe_duration_mismatch, score_similarity

logger = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
_TIMEOUT = 10
_SQUIGLY_MARKER = "window.__SQUIGLY_LINK__ ="
# MusicBrainz's own search score, plus our title/artist check on top of it.
_MB_MIN_SCORE = 90
_MB_MIN_SIMILARITY = 0.9


def ids_from_links(urls) -> dict[str, str]:
    """Platform ids from track URLs, keyed like OdesliEnricher's other resolvers.

    Strict on purpose: an Amazon link counts only if it names a track
    (`trackAsin=` or `/tracks/`), never a bare album, search or product page.
    """
    out: dict[str, str] = {}
    for raw in urls:
        link = (raw or "").strip()
        if not link:
            continue
        m = None
        if "music.amazon." in link:
            m = re.search(r"trackAsin=([A-Z0-9]{10})|/tracks/([A-Z0-9]{10})", link)
            if m:
                out.setdefault("amazonMusic", m.group(1) or m.group(2))
        elif "tidal.com" in link:
            m = re.search(r"/track/(\d+)", link)
            if m:
                out.setdefault("tidal", m.group(1))
        elif "deezer.com" in link:
            m = re.search(r"/track/(\d+)", link)
            if m:
                out.setdefault("deezer", m.group(1))
        elif "qobuz.com" in link:
            m = re.search(r"/track/(\d+)", link)
            if m:
                out.setdefault("qobuz", m.group(1))
        elif "music.apple.com" in link or "itunes.apple.com" in link:
            m = re.search(r"[?&]i=(\d+)", link)
            if m:
                out.setdefault("appleMusic", m.group(1))
    return out


def _spotify_url(track) -> Optional[str]:
    sid = getattr(track, "spotify_id", None)
    return f"https://open.spotify.com/track/{sid}" if sid else None


def squigly_links(session, track) -> dict[str, str]:
    url = _spotify_url(track)
    if not url:
        return {}
    r = session.post("https://squigly.link/api/create", json={"url": url}, timeout=_TIMEOUT)
    if not r.ok:
        return {}
    page = (r.json() or {}).get("full_url") or ""
    # Only fetch a page on the host we asked; the reply is third-party data.
    if not page.startswith("https://squigly.link/"):
        return {}
    html = session.get(page, timeout=_TIMEOUT).text
    at = html.find(_SQUIGLY_MARKER)
    if at < 0:
        return {}
    payload, _ = json.JSONDecoder().raw_decode(html[at + len(_SQUIGLY_MARKER):].lstrip())
    services = ((payload or {}).get("data") or {}).get("services") or {}
    return ids_from_links((s or {}).get("url") for s in services.values())


def unitune_links(session, track) -> dict[str, str]:
    url = _spotify_url(track)
    if not url:
        return {}
    r = session.get("https://api.unitune.art/v1-alpha.1/links", params={"url": url}, timeout=_TIMEOUT)
    if not r.ok:
        return {}
    by_platform = (r.json() or {}).get("linksByPlatform") or {}
    return ids_from_links((v or {}).get("url") for v in by_platform.values())


def musicbrainz_links(session, track) -> dict[str, str]:
    isrc = str(getattr(track, "isrc", "") or "").strip().upper()
    if not isrc:
        return {}
    from antra.utils.musicbrainz import _MB_API, _USER_AGENT, _throttle
    headers = {"User-Agent": _USER_AGENT}
    _throttle()
    r = session.get(f"{_MB_API}/recording", params={"query": f"isrc:{isrc}", "fmt": "json", "limit": 5},
                    headers=headers, timeout=_TIMEOUT)
    if not r.ok:
        return {}
    for rec in (r.json() or {}).get("recordings") or []:
        credit = " ".join(c.get("name", "") for c in rec.get("artist-credit") or [])
        # An ISRC can be shared by two mixes, so the ISRC alone is not identity.
        if (rec.get("score") or 0) < _MB_MIN_SCORE:
            continue
        if score_similarity(track.title, track.artists, rec.get("title") or "", credit) < _MB_MIN_SIMILARITY:
            continue
        if has_severe_duration_mismatch(getattr(track, "duration_ms", None), rec.get("length")):
            continue
        _throttle()
        rel = session.get(f"{_MB_API}/recording/{rec['id']}", params={"fmt": "json", "inc": "url-rels"},
                          headers=headers, timeout=_TIMEOUT)
        if not rel.ok:
            return {}
        return ids_from_links(((x or {}).get("url") or {}).get("resource")
                              for x in (rel.json() or {}).get("relations") or [])
    return {}


def resolve_extra(track, session=None) -> dict[str, str]:
    """Squigly -> MusicBrainz -> Unitune, stopping once an Amazon id is found.

    Each source is optional: a failure is logged and the next one still runs.
    """
    if session is None:
        session = requests.Session()
        session.headers.update(_UA)
    result: dict[str, str] = {}
    for name, fn in (("Squigly", squigly_links), ("MusicBrainz", musicbrainz_links),
                     ("Unitune", unitune_links)):
        try:
            for k, v in fn(session, track).items():
                result.setdefault(k, v)
        except Exception as e:
            logger.debug("[LinkResolver] %s failed: %s", name, e)
        if "amazonMusic" in result:
            break
    return result
