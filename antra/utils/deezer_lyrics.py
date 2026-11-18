"""Time-synced Deezer lyrics, through our Deezer mirror's `/api/lyrics/{deezer_track_id}`.

Deezer only hands lyrics to an ACCOUNT session (an anonymous gw-light session is never given a
lyrics id: 0 of 16 tracks, measured from India and Finland on 2026-11-13), so the mirror answers
from a pooled account and this module is just its client. Measured with one pool account (BR) on
15 real tracks: 5 synced, 5 plain only, 5 no lyrics id for that country; line times within about
0.4 s of LRCLIB / lrc.red. Coverage is below LRCLIB's, so it ranks beside the Apple source, not
above LRCLIB.

Self-contained on purpose (no imports from the rest of `antra`), so the desktop tree can take this
file as it is and only add the same three-line hook to its LyricsFetcher.
"""
import logging
import re
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

PUBLIC_API = "https://api.deezer.com"
# ISRCs are shared across mixes and editions (BUG-MIX-1), so an id found by ISRC is only trusted
# when the recording's length agrees; the title check catches an outright different song.
MAX_DURATION_DIFF_MS = 5000
MIN_TITLE_OVERLAP = 0.7


def _norm(text: str) -> str:
    text = (text or "").lower()
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _title_overlap(a: str, b: str) -> float:
    ta, tb = set(_norm(a).split()), set(_norm(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, min(len(ta), len(tb)))


def _matches(track, title: str, duration_ms) -> bool:
    """Is the Deezer recording found by ISRC the one the track is?"""
    want_title = getattr(track, "title", "") or ""
    # An empty title on either side has overlap 0, so a match that cannot be verified is refused.
    if _title_overlap(want_title, title) < MIN_TITLE_OVERLAP:
        return False
    want_ms = getattr(track, "duration_ms", None) or 0
    try:
        got_ms = int(duration_ms or 0)
    except (TypeError, ValueError):
        got_ms = 0
    if want_ms and got_ms:
        return abs(want_ms - got_ms) <= MAX_DURATION_DIFF_MS
    return True                          # length unknown on one side: the title match has to do


def resolve_deezer_id(session, mirrors, api_key: str, track, timeout: int = 12) -> Optional[str]:
    """The Deezer track id for `track`: the id it already carries, else its ISRC resolved through our
    mirror (authenticated, proxied) and, if no mirror answers, the public API, verified either way."""
    carried = str(getattr(track, "deezer_track_id", "") or "").strip()
    if carried.isdigit():
        return carried
    isrc = (getattr(track, "isrc", "") or "").strip().upper()
    if not isrc:
        return None
    headers = {"X-API-Key": api_key} if api_key else {}
    for base in mirrors:
        try:
            r = session.get(f"{base}/api/search/isrc/{isrc}", headers=headers, timeout=timeout)
        except Exception as e:
            logger.debug("[Lyrics] deezer mirror %s isrc lookup unreachable: %s", base, e)
            continue
        if r.status_code == 404:
            return None                  # Deezer has no track for this ISRC
        if r.status_code != 200:
            continue
        try:
            data = r.json() or {}
        except Exception:
            continue
        found = str(data.get("track_id") or "").strip()
        # An answer that does not match is final: the public API would return the same recording.
        if found.isdigit() and _matches(track, data.get("title", ""), data.get("duration_ms")):
            return found
        return None
    try:
        r = session.get(f"{PUBLIC_API}/track/isrc:{isrc}", timeout=timeout)
        data = r.json() if r.status_code == 200 else {}
    except Exception as e:
        logger.debug("[Lyrics] deezer public isrc lookup failed: %s", e)
        return None
    if not isinstance(data, dict) or "error" in data or not data.get("id"):
        return None
    try:
        duration_ms = int(data.get("duration") or 0) * 1000
    except (TypeError, ValueError):
        duration_ms = 0
    if _matches(track, data.get("title", ""), duration_ms):
        return str(data["id"])
    return None


def fetch_deezer_lyrics(session, mirrors, api_key: str, track,
                        timeout: int = 25) -> Optional[Tuple[str, str]]:
    """(plain, synced_lrc) from the Deezer mirror, or None. Same return shape as the Apple source."""
    if not mirrors:
        return None
    deezer_id = resolve_deezer_id(session, mirrors, api_key, track)
    if not deezer_id:
        return None
    headers = {"X-API-Key": api_key} if api_key else {}
    for base in mirrors:
        try:
            r = session.get(f"{base}/api/lyrics/{deezer_id}", headers=headers, timeout=timeout)
        except Exception as e:
            logger.debug("[Lyrics] deezer mirror %s unreachable: %s", base, e)
            continue
        if r.status_code == 404:
            return None                  # no account's country has lyrics for this track
        if r.status_code != 200:
            logger.debug("[Lyrics] deezer mirror %s returned %s", base, r.status_code)
            continue
        try:
            data = r.json() or {}
        except Exception:
            continue
        plain = (data.get("plain") or "").strip()
        synced = (data.get("synced") or "").strip()
        if not plain and not synced:
            return None
        return plain, synced
    return None
