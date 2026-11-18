"""
Lyrics fetching — synced-first waterfall across multiple providers (v1.1.8 FEAT-9).

The previous implementation had two sources and one controlling bug:

    lrc = self._fetch_lrclib(track)
    if lrc:
        plain, synced = lrc
        if plain or synced:        # <-- `plain` alone satisfied this
            return plain, synced   # <-- the synced source was NEVER tried

LRCLIB has very large *plain-only* coverage, so any track where it had text but
no timings returned immediately and never consulted a provider that might have
had synced lyrics. That single `or` is the mechanical explanation for
"most lyrics are not synced" — it was a priority bug, not a coverage problem.

This module inverts the rule: **never settle for unsynced lyrics while an
unqueried provider might have synced ones.** The best result so far is kept and
only returned once the whole chain is exhausted.

Provider order (best synced coverage first), measured 2026-07-30:

  1. Apple Music (via our own mirror) — by far the best, and the only one with
     meaningful non-Western coverage. On a Hindi sample Apple returned synced
     lyrics for Kesariya (44 lines), Chaiyya Chaiyya (94) and Tum Hi Ho (36),
     while LRCLIB found *nothing at all* for any of them.
  2. LRCLIB — excellent where it has data, free, no key. Queried with several
     phrasings (with album, without album, search, simplified title), because
     any single phrasing misses rows the others find.
  3. lrc.red (ISRC-keyed), LyricsPlus and BetterLyrics' boidu.dev API — word-level
     sources. Paxsenix was removed: its endpoint answers 403 "no longer available"
     (LYR-1, measured 2026-10-26).

Word-level timings (`<mm:ss.xx>` tags) are flattened to line-level LRC before
anything is returned: SYLT frames, the tag fields and the sidecar are all
line-level, and a stray tag would be printed as lyric text.

Every candidate is verified before it is accepted (see `_candidate_is_plausible`).
Attaching a *different song's* lyrics is the lyrics-shaped version of BUG-1, and
the old `results[0]` search fallback could do exactly that.
"""
import logging
import re
from typing import Optional, Tuple

import requests

from antra import __version__

logger = logging.getLogger(__name__)

# A synced candidate whose final timestamp runs far past the track is a
# different (usually longer) recording.
_MAX_OVERRUN_SECONDS = 30
# Fewer than this many timed lines for a full-length track is a placeholder or a
# mis-parse, not real synced lyrics.
_MIN_SYNCED_LINES = 3


def _norm(text: str) -> str:
    """Loose normalisation for comparing titles/artists across providers."""
    text = (text or "").lower()
    text = re.sub(r"\(.*?\)|\[.*?\]", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens_overlap(a: str, b: str) -> float:
    ta, tb = set(_norm(a).split()), set(_norm(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, min(len(ta), len(tb)))


def last_lrc_timestamp_seconds(lrc: str) -> Optional[float]:
    """Seconds of the final timestamp in an LRC block, or None."""
    stamps = re.findall(r"\[(\d+):(\d+(?:\.\d+)?)\]", lrc or "")
    if not stamps:
        return None
    try:
        return max(int(m) * 60 + float(s) for m, s in stamps)
    except Exception:
        return None


_WORD_TAG = re.compile(r"<\d{1,3}:\d{2}(?:[.:]\d{1,3})?>")


def strip_word_tags(lrc: str) -> str:
    """Enhanced (word-level) LRC -> plain line-level LRC."""
    if not lrc or "<" not in lrc:
        return lrc
    return "\n".join(re.sub(r"[ \t]{2,}", " ", _WORD_TAG.sub("", ln)).rstrip()
                     for ln in lrc.splitlines())


def _stamp(ms: float) -> str:
    cs = int(round(ms / 10))
    return "[%02d:%05.2f]" % (cs // 6000, (cs % 6000) / 100)


def _lines_to_lrc(lines) -> Tuple[str, str]:
    """[(start_ms, text)] -> (plain, line-level LRC); empty lines are dropped."""
    lines = sorted((ms, " ".join(t.split())) for ms, t in lines if t and t.strip())
    return "\n".join(t for _, t in lines), "\n".join(_stamp(ms) + t for ms, t in lines)


def _ttml_seconds(v: str) -> float:
    secs = 0.0
    for part in (v or "0").split(":"):
        secs = secs * 60 + float(part)
    return secs


def ttml_to_lines(ttml: str):
    """Line start (ms) and text from every <p> of an Apple-style TTML document."""
    import xml.etree.ElementTree as ET
    out = []
    for el in ET.fromstring(ttml).iter():
        if el.tag.rsplit("}", 1)[-1] == "p" and el.get("begin"):
            out.append((int(_ttml_seconds(el.get("begin")) * 1000), "".join(el.itertext())))
    return out


def count_synced_lines(lrc: str) -> int:
    return len([ln for ln in (lrc or "").splitlines() if re.match(r"\s*\[\d+:\d+", ln)])


class LyricsFetcher:
    """Multi-provider, synced-first lyrics resolution."""

    def __init__(
        self,
        musixmatch_api_key: Optional[str] = None,
        genius_api_key: Optional[str] = None,
        apple_mirrors: Optional[list] = None,
        mirror_api_key: str = "",
        deezer_mirrors: Optional[list] = None,
        use_cache: bool = True,
    ):
        self.musixmatch_key = musixmatch_api_key
        self.genius_key = genius_api_key
        self._apple_mirrors = [m.rstrip("/") for m in (apple_mirrors or []) if m]
        self._mirror_api_key = mirror_api_key or ""
        self._deezer_mirrors = [m.rstrip("/") for m in (deezer_mirrors or []) if m]
        self._use_cache = use_cache
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Antra/" + __version__})

    # ── public API ─────────────────────────────────────────────────────────
    def fetch(self, track) -> Tuple[Optional[str], Optional[str]]:
        """Return (plain_text, synced_lrc). Either may be None."""
        cache = self._cache()
        if cache:
            hit = cache.get(track)
            if hit is not None:
                plain, synced, provider = hit
                # The key is the ISRC, and one ISRC can cover mixes of different
                # lengths, so a hit must still be plausible for THIS track.
                if not synced or self._candidate_is_plausible(track, plain, synced, "cache"):
                    if plain or synced:
                        logger.debug(
                            "[Lyrics] cache hit for '%s' (provider=%s, synced=%s)",
                            track.title, provider, bool(synced),
                        )
                    return (plain or None), (synced or None)

        best_plain: Optional[str] = None
        best_provider = ""
        # A host that is unreachable stays unreachable for the rest of the run.
        # Several providers share one host (four LRCLIB query shapes), so without
        # this a dead host costs 4 x timeout *per track* — measured at 71s for a
        # single track when LRCLIB was unreachable. Per-fetch, not persistent:
        # a host that recovers mid-library is picked up on the next track.
        dead_hosts: set = set()
        # Any provider that RAISED (offline, 5xx, bad JSON) means "no answer" is
        # not the same as "answered none": nothing is cached as a negative or as
        # a plain-only settlement in that case, or one outage would suppress
        # lookups for days.
        errored = False

        for name, provider in self._providers(track):
            host = name.split("-", 1)[0]
            if host in dead_hosts:
                logger.debug("[Lyrics] skipping %s — %s unreachable this run", name, host)
                continue
            try:
                result = provider()
            except Exception as e:
                errored = True
                if self._is_unreachable(e):
                    dead_hosts.add(host)
                    logger.warning(
                        "[Lyrics] %s unreachable (%s) — skipping its remaining "
                        "queries for this track", host, type(e).__name__,
                    )
                else:
                    logger.debug("[Lyrics] %s failed: %s", name, e)
                continue
            if not result:
                continue
            plain, synced = result
            plain = (plain or "").strip()
            synced = strip_word_tags((synced or "").strip())
            if not plain and not synced:
                continue

            if not self._candidate_is_plausible(track, plain, synced, name):
                continue

            if synced:
                logger.info(
                    "[Lyrics] %s — SYNCED via %s (%d lines)",
                    track.title, name, count_synced_lines(synced),
                )
                if cache:
                    cache.put(track, plain, synced, name)
                return (plain or None), synced

            # Plain only: remember it, but keep looking for synced. This is the
            # inversion of the old behaviour and the core of the fix.
            if best_plain is None:
                best_plain, best_provider = plain, name
                logger.debug(
                    "[Lyrics] %s — plain-only via %s, continuing to look for synced",
                    track.title, name,
                )

        if best_plain:
            logger.info("[Lyrics] %s — plain only (via %s); no synced found anywhere",
                        track.title, best_provider)
            if cache and not errored:
                cache.put(track, best_plain, "", best_provider)
            return best_plain, None

        logger.debug("[Lyrics] %s — no lyrics found in any provider", track.title)
        if cache and not errored:
            cache.put(track, "", "", "")
        return None, None

    # ── provider chain ─────────────────────────────────────────────────────
    def _providers(self, track):
        """Ordered (name, callable) pairs — cheapest good answer first.

        **LRCLIB leads, Apple follows.** The first cut had Apple first, on an
        investigation that reported LRCLIB finding nothing for regional
        catalogues. Measuring from the VPS disproved that: LRCLIB answered every
        test track with synced lyrics **in 0.2-0.3s**, while an Apple lookup
        costs an ISRC resolution against a pool that rotates ~15 accounts with
        waits between them — 5.9s on a good run, a 10s timeout on a bad one.
        Apple-first therefore added minutes per album to reach an answer LRCLIB
        already had.

        Ordering by cost does NOT weaken the synced-first rule, which is the
        actual fix in this feature: a plain-only LRCLIB hit still falls through
        to Apple, because `fetch()` only stops early on *synced* lyrics. The
        single thing given up is preferring Apple's timings when both have
        synced — not worth a 30x latency cost.

        On the desktop the order is self-correcting: LRCLIB is unreachable on
        some ISPs, the unreachable-host breaker skips its remaining query shapes
        after the first transport failure, and Apple runs anyway.
        """
        chain = []
        chain.append(("lrclib", lambda: self._fetch_lrclib_exact(track, with_album=True)))
        if track.album:
            chain.append(("lrclib-noalbum", lambda: self._fetch_lrclib_exact(track, with_album=False)))
        chain.append(("lrclib-search", lambda: self._fetch_lrclib_search(track, track.title)))
        simplified = self._simplify_title(track.title)
        if simplified and simplified.lower() != (track.title or "").lower():
            chain.append(("lrclib-simplified", lambda: self._fetch_lrclib_search(track, simplified)))
        if getattr(track, "isrc", None):
            chain.append(("lrcred", lambda: self._fetch_lrcred(track)))
        chain.append(("lyricsplus", lambda: self._fetch_lyricsplus(track)))
        if self._apple_mirrors:
            chain.append(("apple", lambda: self._fetch_apple(track)))
        if self._deezer_mirrors:
            # Fills gaps LRCLIB has, via a pooled Deezer account on our mirror (LYR-3).
            chain.append(("deezer", lambda: self._fetch_deezer(track)))
        chain.append(("boidu", lambda: self._fetch_boidu(track)))
        return chain

    @staticmethod
    def _is_unreachable(exc: Exception) -> bool:
        """True when the host could not be reached at all, as opposed to
        answering with 'no lyrics'. Only a transport failure justifies skipping
        a provider's remaining queries — an HTTP 404 says nothing about whether
        a *different* query shape against the same host would succeed.
        """
        try:
            from requests.exceptions import (ConnectionError as ReqConnErr,
                                             RequestException, Timeout)
            if isinstance(exc, (Timeout, ReqConnErr)):
                return True
            # Every requests error subclasses OSError, so the fallback below
            # would otherwise treat an HTTP 500 as "host unreachable" and
            # disable the remaining query shapes against a host that answered.
            if isinstance(exc, RequestException):
                return False
        except Exception:  # pragma: no cover - requests always present in practice
            pass
        return isinstance(exc, (OSError, TimeoutError))

    # ── quality gates ──────────────────────────────────────────────────────
    def _candidate_is_plausible(self, track, plain: str, synced: str, provider: str) -> bool:
        """Reject lyrics that are probably for a different recording.

        This is the lyrics-shaped version of BUG-1's identity gate. The old
        implementation took `results[0]` from an LRCLIB search with no checks at
        all, which could attach a completely different song's words to a track —
        invisible in the file browser, obvious the moment you play it.
        """
        if synced:
            lines = count_synced_lines(synced)
            if lines < _MIN_SYNCED_LINES:
                logger.debug("[Lyrics] rejected %s candidate — only %d timed lines",
                             provider, lines)
                return False
            last = last_lrc_timestamp_seconds(synced)
            dur = (getattr(track, "duration_ms", None) or 0) / 1000.0
            if last is not None and dur > 0 and last > dur + _MAX_OVERRUN_SECONDS:
                logger.info(
                    "[Lyrics] rejected %s candidate for '%s' — lyrics run to %.0fs "
                    "but the track is %.0fs (different recording)",
                    provider, track.title, last, dur,
                )
                return False
        return True

    @staticmethod
    def _simplify_title(title: str) -> str:
        """Drop decorations that stop a catalogue matching: feat credits,
        version and remaster tags."""
        t = re.sub(r"\s*[\(\[](feat\.?|ft\.?|with|featuring)[^\)\]]*[\)\]]", "", title or "",
                   flags=re.IGNORECASE)
        # The year is optional and can sit on either side of the tag — Spotify
        # writes "- 2011 Remaster", Apple writes "- Remastered 2011".
        t = re.sub(
            r"\s*-\s*(\d{4}\s+)?(remaster(ed)?|single version|album version|radio edit)\b.*$",
            "", t, flags=re.IGNORECASE)
        t = re.sub(r"\s*[\(\[][^\)\]]*(remaster|version|edit)[^\)\]]*[\)\]]", "", t,
                   flags=re.IGNORECASE)
        return t.strip()

    # ── providers ──────────────────────────────────────────────────────────
    def _resolve_apple_id_by_isrc(self, isrc: str) -> Optional[str]:
        """Apple catalog id for an ISRC, via our authenticated mirror."""
        if not self._apple_mirrors:
            return None
        headers = {"X-API-Key": self._mirror_api_key} if self._mirror_api_key else {}
        for base in self._apple_mirrors:
            try:
                r = self.session.get(
                    f"{base.rstrip('/')}/api/search/isrc/{isrc}",
                    headers=headers,
                    # This pool rotates through ~15 accounts with 2-5s waits, so
                    # 10s was too tight — measured 5.9s on a good run and a
                    # timeout on a slow one. Affordable now only because Apple
                    # sits AFTER LRCLIB: it is reached for the minority of
                    # tracks LRCLIB could not answer, not for every track.
                    timeout=20,
                )
            except Exception as e:
                logger.debug("[Lyrics] apple isrc lookup unreachable (%s): %s", base, e)
                continue
            if r.status_code == 404:
                # No Apple track for THIS ISRC. Deliberately falls through to the
                # text search rather than giving up: a track's metadata ISRC is
                # often for a different release of the same recording, and the
                # text path is verified on title + artist + duration before it is
                # trusted, so the "wrong song's lyrics" risk is already covered.
                # Treating this as authoritative would cost coverage for nothing.
                return None
            if r.status_code != 200:
                logger.debug("[Lyrics] apple isrc lookup %s returned %s", base, r.status_code)
                continue
            try:
                track_id = str((r.json() or {}).get("track_id") or "").strip()
            except Exception:
                continue
            if track_id:
                logger.debug("[Lyrics] apple id %s resolved from ISRC %s", track_id, isrc)
                return track_id
        return None

    def _fetch_apple(self, track) -> Optional[Tuple[str, str]]:
        """Apple Music lyrics via our mirror's /api/lyrics endpoint.

        Needs an Apple catalog track id. When the track did not come from an
        Apple URL we resolve one from the public iTunes Search API, and verify
        the match before trusting it — an unverified id would attach the wrong
        song's lyrics.
        """
        track_id = getattr(track, "apple_music_id", None)
        if not track_id:
            track_id = self._resolve_apple_id(track)
        if not track_id:
            return None

        headers = {"X-API-Key": self._mirror_api_key} if self._mirror_api_key else {}
        for base in self._apple_mirrors:
            try:
                r = self.session.get(
                    f"{base}/api/lyrics/{track_id}", headers=headers, timeout=25,
                )
            except Exception as e:
                logger.debug("[Lyrics] apple mirror %s unreachable: %s", base, e)
                continue
            if r.status_code == 404:
                return None          # Apple genuinely has no lyrics for this track
            if r.status_code != 200:
                logger.debug("[Lyrics] apple mirror %s returned %s", base, r.status_code)
                continue
            try:
                data = r.json()
            except Exception:
                continue
            return data.get("plain") or "", data.get("synced") or ""
        return None

    def _fetch_deezer(self, track) -> Optional[Tuple[str, str]]:
        """Deezer synced lyrics via our mirror (see antra/utils/deezer_lyrics.py)."""
        from antra.utils.deezer_lyrics import fetch_deezer_lyrics
        return fetch_deezer_lyrics(self.session, self._deezer_mirrors, self._mirror_api_key, track)

    def _resolve_apple_id(self, track) -> Optional[str]:
        """Find the Apple catalog id for a track.

        ISRC through our own mirror first, then the public iTunes Search API.
        The order matters and is not just a preference:

        * iTunes Search rate-limits **per IP in bursts** and answers `403` with
          an empty body once tripped. One lookup per album is fine; one per
          track is not — that is precisely the bug the v1.1.7 artwork upgrade
          hit, where a 20-track album silently lost its hi-res covers partway
          through. Lyrics are per-track, so leading with iTunes would reproduce
          it exactly.
        * The mirror's `/api/search/isrc/{isrc}` is authenticated with real
          account credentials and is therefore immune to that ban — the same
          fallback the artwork path already relies on.
        * An ISRC match needs no fuzzy verification: it identifies the exact
          recording, so unlike the text-search path there is no risk of
          attaching another song's lyrics.
        """
        isrc = (getattr(track, "isrc", "") or "").strip().upper()
        if isrc:
            found = self._resolve_apple_id_by_isrc(isrc)
            if found:
                return found

        title = (track.title or "").strip()
        artist = (getattr(track, "primary_artist", "") or "").strip()
        if not title:
            return None
        try:
            r = self.session.get(
                "https://itunes.apple.com/search",
                params={"term": f"{title} {artist}".strip(), "entity": "song", "limit": 5},
                timeout=15,
            )
            if r.status_code != 200:
                return None
            results = r.json().get("results") or []
        except Exception:
            return None

        for item in results:
            got_title = item.get("trackName") or ""
            got_artist = item.get("artistName") or ""
            if _tokens_overlap(title, got_title) < 0.6:
                continue
            if artist and _tokens_overlap(artist, got_artist) < 0.4:
                continue
            dur = (getattr(track, "duration_ms", None) or 0)
            got_dur = item.get("trackTimeMillis") or 0
            if dur and got_dur and abs(dur - got_dur) > 15000:
                continue
            tid = item.get("trackId")
            if tid:
                return str(tid)
        return None

    def _fetch_lrclib_exact(self, track, with_album: bool) -> Optional[Tuple[str, str]]:
        params = {
            "artist_name": getattr(track, "primary_artist", None),
            "track_name": track.title,
            "duration": getattr(track, "duration_seconds", None),
        }
        if with_album and track.album:
            params["album_name"] = track.album
        params = {k: v for k, v in params.items() if v}
        r = self.session.get("https://lrclib.net/api/get", params=params, timeout=15)
        if r.status_code != 200:
            return None
        data = r.json()
        return data.get("plainLyrics") or "", data.get("syncedLyrics") or ""

    def _fetch_lrclib_search(self, track, title: str) -> Optional[Tuple[str, str]]:
        """Search LRCLIB, preferring a SYNCED result and verifying the match.

        The old code took `results[0]` blindly. Here every candidate is checked
        against title/artist/duration, and a synced hit is preferred over the
        first hit.
        """
        artist = getattr(track, "primary_artist", "") or ""
        r = self.session.get(
            "https://lrclib.net/api/search",
            params={"q": f"{artist} {title}".strip()},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        try:
            results = r.json() or []
        except Exception:
            return None

        dur = (getattr(track, "duration_ms", None) or 0) / 1000.0
        plain_fallback = None
        for item in results[:10]:
            if _tokens_overlap(title, item.get("trackName") or "") < 0.6:
                continue
            if artist and _tokens_overlap(artist, item.get("artistName") or "") < 0.4:
                continue
            item_dur = item.get("duration") or 0
            if dur and item_dur and abs(dur - item_dur) > 15:
                continue
            synced = item.get("syncedLyrics") or ""
            plain = item.get("plainLyrics") or ""
            if synced:
                return plain, synced
            if plain and plain_fallback is None:
                plain_fallback = plain
        return (plain_fallback, "") if plain_fallback else None

    def _fetch_lrcred(self, track) -> Optional[Tuple[str, str]]:
        """lrc.red serves word-level LRC by ISRC (404 when it has none)."""
        r = self.session.get(f"https://lrc.red/s/{track.isrc.strip().upper()}.lrc", timeout=15)
        if r.status_code != 200 or not r.text.lstrip().startswith("["):
            return None
        synced = strip_word_tags(r.text)
        leads = (_LRC_LEAD.match(ln.strip()) for ln in synced.splitlines())
        return "\n".join(m.group(2).strip() for m in leads if m and m.group(2).strip()), synced

    def _fetch_lyricsplus(self, track) -> Optional[Tuple[str, str]]:
        params = {"title": track.title, "artist": getattr(track, "primary_artist", "") or ""}
        if getattr(track, "isrc", None):
            params["isrc"] = track.isrc
        if getattr(track, "duration_seconds", None):
            params["duration"] = track.duration_seconds
        err = None
        for host in ("lyricsplus.binimum.org", "lyricsplus.prjktla.my.id"):
            try:
                r = self.session.get(f"https://{host}/v2/lyrics/get", params=params, timeout=15)
            except requests.RequestException as e:
                err = e
                continue
            if r.status_code != 200:
                continue
            data = r.json()
            # The lookup is fuzzy server-side; refuse a different song.
            if _tokens_overlap(track.title, (data.get("metadata") or {}).get("title") or track.title) < 0.6:
                continue
            plain, synced = _lines_to_lrc((ln.get("time") or 0, ln.get("text") or "")
                                          for ln in data.get("lyrics") or [])
            return (plain, synced) if synced else None
        if err:
            raise err
        return None

    def _fetch_boidu(self, track) -> Optional[Tuple[str, str]]:
        """BetterLyrics' backend (TTML). Uncached queries need a key, so most
        misses are a 401 — treated as 'none'."""
        params = {"s": track.title, "a": getattr(track, "primary_artist", "") or ""}
        if getattr(track, "duration_seconds", None):
            params["d"] = track.duration_seconds
        r = self.session.get("https://lyrics-api.boidu.dev/getLyrics", params=params, timeout=15)
        if r.status_code != 200:
            return None
        ttml = r.json().get("ttml")
        if not ttml:
            return None
        plain, synced = _lines_to_lrc(ttml_to_lines(ttml))
        return (plain, synced) if synced else None

    # ── cache ──────────────────────────────────────────────────────────────
    def _cache(self):
        if not self._use_cache:
            return None
        try:
            from antra.utils.lyrics_cache import get_lyrics_cache
            return get_lyrics_cache()
        except Exception:
            return None


# Every LRC timestamp form seen in the wild: [mm:ss], [mm:ss.x], [mm:ss.xx],
# [mm:ss.xxx] (and a ':' before the fraction). A line may start with several.
_LRC_STAMP = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
_LRC_LEAD = re.compile(r"^((?:\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\])+)(.*)$")


def _stamp_ms(m: "re.Match") -> int:
    minutes, seconds, frac = m.groups()
    ms = (int(minutes) * 60 + int(seconds)) * 1000
    if frac:
        ms += int(frac.ljust(3, "0")[:3])   # '5' -> 500, '05' -> 50, '050' -> 50
    return ms


def validate_and_strip_lrc(lrc_text: str, duration_ms: int) -> str:
    """
    Parse LRC timestamps and strip any lines whose timestamp exceeds
    the track duration. Prevents player desyncs.
    Returns cleaned LRC string.
    """
    if not duration_ms or duration_ms <= 0 or not lrc_text:
        return lrc_text

    output_lines = []

    for line in lrc_text.splitlines():
        lead = _LRC_LEAD.match(line)
        if lead:
            stamps = [_stamp_ms(m) for m in _LRC_STAMP.finditer(lead.group(1))]
            # Drop the line only when EVERY timestamp is past the end.
            if stamps and min(stamps) > duration_ms:
                logger.debug(
                    f"[Lyrics] Stripping out-of-range line at {min(stamps)}ms "
                    f"(track duration: {duration_ms}ms): {line[:60]}"
                )
                continue
        output_lines.append(line)

    return "\n".join(output_lines)


def lrc_to_sylt_frames(lrc_text: str) -> list[tuple[str, int]]:
    """
    Convert LRC text to mutagen SYLT format: list of (text, timestamp_ms).
    Strips timestamp prefix from each line's text.
    """
    if not lrc_text:
        return []
        
    pattern = re.compile(r'^\[(\d{1,2}):(\d{2})\.(\d{2,3})\](.*)')
    frames: list[tuple[str, int]] = []

    for line in lrc_text.splitlines():
        lead = _LRC_LEAD.match(line.strip())
        if not lead:
            continue  # metadata tags ([ar:...]) and untimed lines
        text = lead.group(2).strip()
        for m in _LRC_STAMP.finditer(lead.group(1)):
            frames.append((text, _stamp_ms(m)))

    # SYLT frames must be in time order; multi-timestamp lines are not.
    frames.sort(key=lambda f: f[1])
    return frames
