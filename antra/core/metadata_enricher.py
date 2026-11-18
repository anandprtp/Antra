"""
Centralized metadata enrichment pipeline.

Fills every available TrackMetadata field by consulting sources in priority order:
  1. source_metadata from the winning adapter (already fetched with the download —
     most authoritative, zero extra API calls)
  2. Deezer free API (ISRC, track#, disc#, release date, 1000px artwork)
  3. iTunes Search API (genre, composer, track#, disc#, year, 3000px artwork)
  4. MusicBrainz (genre tags, ISWC, label — by ISRC or title+artist fallback)
  5. Lyrics: LRCLIB → lrc.red → LyricsPlus → Apple → boidu (synced + plain)
  6. Artwork upgrade to highest resolution available

All API calls are fully wrapped in try/except — enrichment is strictly best-effort.
A failure in any source silently falls through to the next.
"""
from __future__ import annotations

import logging
import re as _re
import threading
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from antra.core.models import TrackMetadata, SearchResult

logger = logging.getLogger(__name__)

# ── Cache ────────────────────────────────────────────────────────────────────

_enrich_cache: dict[str, dict[str, Any]] = {}
_enrich_cache_lock = threading.Lock()

# Parenthetical/bracketed version tags (e.g. "(Atmos Mix)", "(Dolby Atmos)",
# "(2019 Remaster)", "(Radio Edit)") break Deezer/iTunes/MusicBrainz lookups —
# those catalogs index the base recording, not the spatial/remaster variant.
# Strip them from the *search query* only; the real title on the track is kept.
_VERSION_SUFFIX_RE = _re.compile(
    r"\s*[\(\[][^\(\)\[\]]*\b"
    r"(?:atmos|dolby|spatial|mix|version|remaster(?:ed)?|edit|mono|stereo|remix)\b"
    r"[^\(\)\[\]]*[\)\]]",
    _re.IGNORECASE,
)


def _clean_search_title(title: str) -> str:
    """Drop version/mix/remaster suffixes so catalog lookups hit the base track."""
    if not title:
        return title
    cleaned = _VERSION_SUFFIX_RE.sub("", title).strip()
    cleaned = _re.sub(r"\s{2,}", " ", cleaned).strip(" -–—")
    return cleaned or title


def _cache_key(track: "TrackMetadata", has_source_meta: bool = False) -> str:
    isrc = (track.isrc or "").strip().upper()
    if isrc:
        return f"isrc:{isrc}:sm:{int(has_source_meta)}"
    title = (track.title or "").strip().lower()
    artist = (track.primary_artist or "").strip().lower()
    album = (track.album or "").strip().lower()
    return f"taa:{title}|{artist}|{album}:sm:{int(has_source_meta)}"


# ── Public API ────────────────────────────────────────────────────────────────


class MetadataEnricher:
    """Fill TrackMetadata gaps from adapters + free external APIs."""

    @staticmethod
    def enrich(
        track: "TrackMetadata",
        result: Optional["SearchResult"] = None,
        fetch_lyrics: bool = True,
    ) -> None:
        """Enrich track in-place.  Never raises — silently degrades on any failure.

        Call AFTER download succeeds and BEFORE the tagger runs, so all enriched
        fields are available for embedding into the file tags.
        """
        has_sm = bool(result and result.source_metadata)
        ck = _cache_key(track, has_sm)
        with _enrich_cache_lock:
            if ck in _enrich_cache:
                _apply_cache(track, _enrich_cache[ck])
                setattr(track, "_antra_meta_diag", {"cache_hit": True})
                return

        enriched: dict[str, Any] = {}
        diagnostics: dict[str, Any] = {
            "cache_hit": False,
            "source_meta_isrc": ((getattr(result, "source_metadata", None) or {}).get("isrc") or ""),
            "deezer": {},
            "itunes": {},
            "musicbrainz": {},
        }

        try:
            # 1 ── Source-authoritative metadata (already fetched by adapter) ──
            if result and result.source_metadata:
                _merge_source_meta(track, result.source_metadata, enriched)

            # 2 ── Free external APIs ──────────────────────────────────────
            _enrich_from_deezer(track, enriched, diagnostics["deezer"])
            _enrich_from_itunes(track, enriched, diagnostics["itunes"])
            _enrich_from_musicbrainz(track, enriched, diagnostics["musicbrainz"])
            # Year is often still missing even after the above (e.g. the Deezer
            # text-search hit carried no album release_date). If we now have an
            # ISRC, do one authoritative Deezer-by-ISRC lookup just for the year.
            _ensure_release_year(track, enriched)

            # 3 ── Lyrics ──────────────────────────────────────────────────
            if fetch_lyrics:
                _enrich_lyrics(track, enriched)

            # 4 ── Artwork upgrade ─────────────────────────────────────────
            _upgrade_artwork(track, enriched)

        except Exception as exc:
            logger.debug("[MetadataEnricher] enrich() failed: %s", exc)

        with _enrich_cache_lock:
            _enrich_cache[ck] = enriched
        setattr(track, "_antra_meta_diag", diagnostics)


def _apply_cache(track: "TrackMetadata", data: dict[str, Any]) -> None:
    for key, val in data.items():
        if val is not None and val != "" and val != []:
            setattr(track, key, val)


# ── 1. Source metadata ──────────────────────────────────────────────────────


def _merge_source_meta(
    track: "TrackMetadata",
    source: dict[str, Any],
    out: dict[str, Any],
) -> None:
    """Apply metadata from the winning adapter's source API response."""
    field_map = {
        "isrc": "isrc",
        "track_number": "track_number",
        "disc_number": "disc_number",
        "release_date": "release_date",
        "release_year": "release_year",
        "genres": "genres",
        "composer": "composer",
        "label": "label",
        "iswc": "iswc",
        "upc": "upc",
        "is_explicit": "is_explicit",
        "artwork_url": "artwork_url",
    }
    _BOOL_FIELDS = {"is_explicit"}
    for src_key, track_attr in field_map.items():
        val = source.get(src_key)
        if val is not None and val != "" and val != []:
            current = getattr(track, track_attr, None)
            if track_attr in _BOOL_FIELDS:
                if current is None:
                    setattr(track, track_attr, val)
                    out[track_attr] = val
            elif not current or current == [] or current is None:
                setattr(track, track_attr, val)
                out[track_attr] = val


# ── Album-level Deezer cache (by Deezer album ID) ──────────────────────────

_deezer_album_cache: dict[str, dict[str, Any]] = {}
_deezer_album_cache_lock = threading.Lock()

# ── 2. Deezer free API ──────────────────────────────────────────────────────


def _enrich_from_deezer(track: "TrackMetadata", out: dict[str, Any], diag: Optional[dict[str, Any]] = None) -> None:
    diag = diag if diag is not None else {}
    diag["started"] = True
    needs_isrc = not track.isrc
    needs_track_num = not track.track_number
    needs_disc = not track.disc_number
    needs_date = not track.release_date
    needs_genre = not track.genres
    needs_label = not getattr(track, "label", None)
    needs_art = not track.artwork_url or _is_spotify_art(track.artwork_url)

    # ── ISRC-based exact lookup (works for niche artists too) ─────────────────
    isrc = track.isrc
    if isrc and any([needs_isrc, needs_track_num, needs_disc, needs_date, needs_genre, needs_label, needs_art]):
        diag["isrc_lookup_attempted"] = True
        _deezer_isrc_enrich(track, isrc, needs_track_num, needs_disc, needs_date, needs_genre, needs_label, needs_art, out, diag)
        needs_isrc = not track.isrc
        needs_track_num = not track.track_number
        needs_disc = not track.disc_number
        needs_date = not track.release_date
        needs_genre = not track.genres
        needs_label = not getattr(track, "label", None)
        needs_art = not track.artwork_url or _is_spotify_art(track.artwork_url)

    if not any([needs_isrc, needs_track_num, needs_disc, needs_date, needs_art]):
        return
    if not track.title or not track.artists:
        return

    try:
        import requests as _req
    except ImportError:
        return

    artist = track.artists[0]
    title = track.title
    search_title = _clean_search_title(title)

    try:
        resp = _req.get(
            "https://api.deezer.com/search",
            params={"q": f'artist:"{artist}" track:"{search_title}"', "limit": 5},
            timeout=8,
        )
        diag["search_status"] = resp.status_code
        if resp.status_code != 200:
            return
        for hit in resp.json().get("data") or []:
            hit_title = hit.get("title") or ""
            hit_artist = (hit.get("artist") or {}).get("name") or ""
            if _score_similarity(search_title, track.artists, hit_title, hit_artist) < 0.60:
                continue
            if needs_isrc and hit.get("isrc"):
                track.isrc = hit["isrc"]
                needs_isrc = False
                out["isrc"] = hit["isrc"]
                logger.debug("[MetaEnrich] ISRC from Deezer: %s", title)
            if needs_track_num and hit.get("track_position"):
                track.track_number = int(hit["track_position"])
                needs_track_num = False
                out["track_number"] = track.track_number
            if needs_disc and hit.get("disk_number"):
                track.disc_number = int(hit["disk_number"])
                needs_disc = False
                out["disc_number"] = track.disc_number
            if needs_date:
                rd = (hit.get("album") or {}).get("release_date") or ""
                if rd:
                    track.release_date = rd
                    try:
                        track.release_year = int(rd[:4])
                    except (ValueError, TypeError):
                        pass
                    needs_date = False
                    out["release_date"] = rd
                    out["release_year"] = track.release_year
            if needs_art:
                album_title = (hit.get("album") or {}).get("title") or ""
                cover_xl = (hit.get("album") or {}).get("cover_xl") or ""
                if cover_xl and _album_titles_match(track.album, album_title):
                    track.artwork_url = cover_xl
                    needs_art = False
                    out["artwork_url"] = cover_xl
            break
    except Exception as e:
        diag["search_error"] = str(e)
        logger.debug("[MetaEnrich] Deezer failed for %r: %s", title, e)


def _deezer_isrc_enrich(
    track: "TrackMetadata",
    isrc: str,
    needs_track_num: bool,
    needs_disc: bool,
    needs_date: bool,
    needs_genre: bool,
    needs_label: bool,
    needs_art: bool,
    out: dict[str, Any],
    diag: Optional[dict[str, Any]] = None,
) -> None:
    """Look up a track by ISRC on Deezer, then fetch the album for genre/label/art."""
    diag = diag if diag is not None else {}
    try:
        import requests as _req
    except ImportError:
        return

    try:
        resp = _req.get(
            f"https://api.deezer.com/track/isrc:{isrc}",
            timeout=10,
        )
        diag["track_status"] = resp.status_code
        if resp.status_code != 200:
            return
        data = resp.json()
        if not data.get("id"):
            diag["track_found"] = False
            return
        diag["track_found"] = True
        diag["track_id"] = data.get("id")

        # One ISRC sits on many releases (compilations) and Deezer hands back just
        # one. Position, disc, date, genre, label and cover all describe THAT
        # release, so take them only when it is our album (WEB-ART-1).
        _hit_album = (data.get("album") or {}).get("title")
        if _hit_album and not _album_titles_match(track.album, _hit_album):
            diag["other_release"] = _hit_album
            logger.debug("[MetaEnrich] Deezer ISRC hit is on another release (%r), ignored for %s",
                         _hit_album, track.title)
            return

        # Fill per-track fields from the ISRC response
        if needs_track_num and data.get("track_position"):
            track.track_number = int(data["track_position"])
            out["track_number"] = track.track_number
            logger.debug("[MetaEnrich] Track# from Deezer ISRC: %s -> %s", track.title, track.track_number)
        if needs_disc and data.get("disk_number"):
            track.disc_number = int(data["disk_number"])
            out["disc_number"] = track.disc_number
        if needs_date:
            rd = (data.get("album") or {}).get("release_date") or ""
            if rd:
                track.release_date = rd
                try:
                    track.release_year = int(rd[:4])
                except (ValueError, TypeError):
                    pass
                out["release_date"] = rd
                out["release_year"] = track.release_year
                logger.debug("[MetaEnrich] Release date from Deezer ISRC: %s -> %s", track.title, rd)

        # Fetch album-level metadata (genre, label, artwork) from Deezer album API
        album = data.get("album") or {}
        album_id = album.get("id")
        diag["album_id"] = album_id
        if album_id and (needs_genre or needs_label or needs_art):
            _deezer_album_enrich(track, str(album_id), needs_genre, needs_label, needs_art, out, diag)

    except Exception as e:
        diag["track_error"] = str(e)
        logger.debug("[MetaEnrich] Deezer ISRC lookup failed for %s: %s", track.isrc, e)


def _deezer_album_enrich(
    track: "TrackMetadata",
    album_id: str,
    needs_genre: bool,
    needs_label: bool,
    needs_art: bool,
    out: dict[str, Any],
    diag: Optional[dict[str, Any]] = None,
) -> None:
    """Fetch Deezer album by ID and extract genre, label, hi-res artwork."""
    diag = diag if diag is not None else {}
    ck = f"dza:{album_id}"
    with _deezer_album_cache_lock:
        cached = _deezer_album_cache.get(ck)
    cache_missing_requested_field = bool(
        cached
        and (
            (needs_genre and not cached.get("genres"))
            or (needs_label and not cached.get("label"))
            or (needs_art and not cached.get("artwork_url"))
        )
    )
    if cached and not cache_missing_requested_field:
        diag["album_cache_hit"] = True
        _apply_album_cache(track, cached, needs_genre, needs_label, needs_art, out)
        return
    if cached and cache_missing_requested_field:
        diag["album_cache_incomplete"] = True

    try:
        import requests as _req
        resp = _req.get(f"https://api.deezer.com/album/{album_id}", timeout=10)
        diag["album_status"] = resp.status_code
        if resp.status_code != 200:
            return
        data = resp.json()
    except Exception as e:
        diag["album_error"] = str(e)
        logger.debug("[MetaEnrich] Deezer album lookup failed for album %s: %s", album_id, e)
        return

    entry: dict[str, Any] = {
        "album_title": str(data.get("title") or ""),
    }
    title = track.title

    # Always cache the full album metadata so later tracks do not inherit a
    # partial cache entry just because the first caller only needed label/art.
    genres_data = data.get("genres", {})
    genre_list = (genres_data or {}).get("data") if isinstance(genres_data, dict) else []
    if genre_list and isinstance(genre_list, list):
        gn = genre_list[0].get("name")
        if gn:
            entry["genres"] = [str(gn)]

    lbl = data.get("label")
    if lbl and isinstance(lbl, str) and lbl.strip():
        entry["label"] = lbl.strip()

    cover_xl = data.get("cover_xl") or ""
    if cover_xl and _album_titles_match(track.album, entry.get("album_title")):
        entry["artwork_url"] = cover_xl.replace("/1000x1000-", "/1800x1800-")

    if needs_genre and entry.get("genres"):
        track.genres = list(entry["genres"])
        out["genres"] = list(entry["genres"])
        diag["genre_found"] = entry["genres"][0]
        logger.debug("[MetaEnrich] Genre from Deezer album: %s -> %s", title, track.genres)

    if needs_label and entry.get("label"):
        setattr(track, "label", entry["label"])
        out["label"] = entry["label"]
        logger.debug("[MetaEnrich] Label from Deezer album: %s -> %s", title, entry["label"])

    if needs_art and entry.get("artwork_url"):
        track.artwork_url = entry["artwork_url"]
        out["artwork_url"] = entry["artwork_url"]
        logger.debug("[MetaEnrich] Art from Deezer album (1800px): %s", title)

    with _deezer_album_cache_lock:
        _deezer_album_cache[ck] = entry


def _apply_album_cache(
    track: "TrackMetadata",
    cached: dict[str, Any],
    needs_genre: bool,
    needs_label: bool,
    needs_art: bool,
    out: dict[str, Any],
) -> None:
    if needs_genre and cached.get("genres"):
        track.genres = list(cached["genres"])
        out["genres"] = list(cached["genres"])
    if needs_label and cached.get("label"):
        setattr(track, "label", cached["label"])
        out["label"] = cached["label"]
    if needs_art and cached.get("artwork_url") and _album_titles_match(track.album, cached.get("album_title")):
        track.artwork_url = cached["artwork_url"]
        out["artwork_url"] = cached["artwork_url"]


# ── Year backfill (Deezer by ISRC) ──────────────────────────────────────────


def _ensure_release_year(track: "TrackMetadata", out: dict[str, Any]) -> None:
    """Last-resort year fill: Deezer track-by-ISRC carries a reliable release_date.

    Tidal supplies no genre/year, and the earlier passes can discover an ISRC
    (via Deezer/iTunes text search) without ever landing a year — the search hit
    may omit album.release_date. This single ISRC lookup closes that gap.
    """
    if track.release_year:
        return
    isrc = (track.isrc or "").strip()
    if not isrc:
        return
    try:
        import requests as _req
        resp = _req.get(f"https://api.deezer.com/track/isrc:{isrc}", timeout=8)
        if resp.status_code != 200:
            return
        data = resp.json()
        rd = data.get("release_date") or (data.get("album") or {}).get("release_date") or ""
        if rd and len(rd) >= 4 and rd[:4].isdigit():
            track.release_year = int(rd[:4])
            if not track.release_date:
                track.release_date = rd
            out["release_year"] = track.release_year
            out["release_date"] = track.release_date
            logger.debug("[MetaEnrich] Year backfill via Deezer ISRC: %s -> %s", track.title, track.release_year)
    except Exception as e:
        logger.debug("[MetaEnrich] Year backfill failed for %s: %s", isrc, e)


# ── 3. iTunes Search API ────────────────────────────────────────────────────


def _enrich_from_itunes(track: "TrackMetadata", out: dict[str, Any], diag: Optional[dict[str, Any]] = None) -> None:
    diag = diag if diag is not None else {}
    needs_track_num = not track.track_number
    needs_disc = not track.disc_number
    needs_date = not track.release_date
    needs_genre = not track.genres
    needs_composer = not track.composer
    needs_art = not track.artwork_url or _is_spotify_art(track.artwork_url)

    if not any([needs_track_num, needs_disc, needs_date, needs_genre, needs_art, needs_composer]):
        return
    if not track.title or not track.artists:
        return

    try:
        import requests as _req
    except ImportError:
        return

    artist = track.artists[0]
    title = track.title
    search_title = _clean_search_title(title)

    try:
        resp = _req.get(
            "https://itunes.apple.com/search",
            params={"term": f"{artist} {search_title}", "entity": "song", "limit": 10, "country": "us"},
            timeout=10,
        )
        diag["status"] = resp.status_code
        if resp.status_code != 200:
            return
        hits = resp.json().get("results") or []
        diag["results"] = len(hits)
        for hit in hits:
            if hit.get("wrapperType") != "track":
                continue
            hit_title = hit.get("trackName") or ""
            hit_artist = hit.get("artistName") or ""
            sim = _score_similarity(search_title, track.artists, hit_title, hit_artist)
            if sim < 0.60:
                continue
            diag["matched_title"] = hit_title
            diag["matched_artist"] = hit_artist
            diag["match_score"] = round(sim, 3)
            if needs_track_num and hit.get("trackNumber"):
                track.track_number = int(hit["trackNumber"])
                needs_track_num = False
                out["track_number"] = track.track_number
                logger.debug("[MetaEnrich] Track# from iTunes: %s -> %s", title, track.track_number)
            if needs_disc and hit.get("discNumber"):
                track.disc_number = int(hit["discNumber"])
                needs_disc = False
                out["disc_number"] = track.disc_number
            if needs_date and not track.release_year:
                rd = hit.get("releaseDate") or ""
                if rd and len(rd) >= 4 and rd[:4].isdigit():
                    track.release_year = int(rd[:4])
                    track.release_date = rd[:10] if len(rd) >= 10 else rd
                    needs_date = False
                    out["release_year"] = track.release_year
                    logger.debug("[MetaEnrich] Year from iTunes: %s -> %s", title, track.release_year)
            if needs_genre and hit.get("primaryGenreName"):
                track.genres = [hit["primaryGenreName"]]
                needs_genre = False
                out["genres"] = track.genres[:]
                logger.debug("[MetaEnrich] Genre from iTunes: %s -> %s", title, track.genres)
            if needs_composer and hit.get("composerName"):
                track.composer = hit["composerName"]
                needs_composer = False
                out["composer"] = track.composer
            if needs_art and hit.get("artworkUrl100") and _album_titles_match(track.album, hit.get("collectionName") or ""):
                url = _re.sub(r"\d+x\d+bb", "3000x3000bb", hit["artworkUrl100"])
                track.artwork_url = url
                needs_art = False
                out["artwork_url"] = url
                logger.debug("[MetaEnrich] Art from iTunes (3000px): %s", title)
            break
    except Exception as e:
        diag["error"] = str(e)
        logger.debug("[MetaEnrich] iTunes failed for %r: %s", title, e)


# ── 4. MusicBrainz ───────────────────────────────────────────────────────────


def _enrich_from_musicbrainz(track: "TrackMetadata", out: dict[str, Any], diag: Optional[dict[str, Any]] = None) -> None:
    diag = diag if diag is not None else {}
    needs_genre = not track.genres
    needs_iswc = not track.iswc
    needs_label = not getattr(track, "label", None)

    if not any([needs_genre, needs_iswc, needs_label]):
        return

    # Try ISRC-based genre lookup via local throttled helper
    if track.isrc and needs_genre:
        try:
            from antra.utils.musicbrainz import fetch_genres
            genres = fetch_genres(track.isrc)
            diag["isrc_lookup_attempted"] = True
            if genres:
                track.genres = genres
                needs_genre = False
                out["genres"] = genres[:]
                diag["genres_found"] = genres[:]
                logger.debug("[MetaEnrich] Genres from MB (ISRC): %s -> %s", track.title, genres)
        except ImportError:
            pass
        except Exception as e:
            diag["isrc_error"] = str(e)
            logger.debug("[MetaEnrich] MB genre lookup failed: %s", e)

    # Fill ISWC and label via MusicBrainz recording API (by ISRC)
    if track.isrc and (needs_iswc or needs_label):
        _mb_label_iswc_lookup(track, needs_iswc, needs_label, out)

    # Fallback: title+artist MusicBrainz search for genre (when ISRC is missing)
    if needs_genre and not track.isrc and track.title and track.artists:
        _mb_text_search_genre(track, out)


def _mb_label_iswc_lookup(
    track: "TrackMetadata",
    needs_iswc: bool,
    needs_label: bool,
    out: dict[str, Any],
) -> None:
    try:
        import requests as _req
        resp = _req.get(
            "https://musicbrainz.org/ws/2/recording/",
            params={"query": f"isrc:{track.isrc}", "fmt": "json", "inc": "labels+iswcs"},
            headers={"User-Agent": "AntraMusic/1.0 ( https://github.com/antra-music/antra )"},
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            recordings = data.get("recordings") or []
            if recordings:
                rec = recordings[0]
                if needs_iswc:
                    iswcs = rec.get("iswcs") or []
                    if iswcs:
                        track.iswc = iswcs[0]
                        out["iswc"] = track.iswc
                if needs_label:
                    labels = (rec.get("labels") or []) or (rec.get("label-info") or [])
                    if labels:
                        lbl = labels[0]
                        lbl_name = lbl.get("label", {}).get("name") if isinstance(lbl.get("label"), dict) else lbl.get("name")
                        if lbl_name:
                            setattr(track, "label", str(lbl_name))
                            out["label"] = str(lbl_name)
    except Exception as e:
        logger.debug("[MetaEnrich] MB label/ISWC lookup failed: %s", e)


def _mb_text_search_genre(track: "TrackMetadata", out: dict[str, Any]) -> None:
    try:
        import requests as _req
        resp = _req.get(
            "https://musicbrainz.org/ws/2/recording/",
            params={
                "query": f"{track.title} {track.primary_artist}",
                "fmt": "json",
                "inc": "tags",
                "limit": 3,
            },
            headers={"User-Agent": "AntraMusic/1.0 ( https://github.com/antra-music/antra )"},
            timeout=10,
        )
        if resp.ok:
            data = resp.json()
            recordings = data.get("recordings") or []
            # A free-text search ranks by text score, so recordings[0] can be a
            # cover or an unrelated song; its genres were written to the file
            # unchecked. Take the best title+artist match, and none if none match.
            from antra.utils.matching import score_similarity
            best, best_score = None, 0.0
            for cand in recordings:
                if not isinstance(cand, dict):
                    continue
                credit = cand.get("artist-credit") or []
                names = [
                    (c.get("name") or (c.get("artist") or {}).get("name") or "")
                    for c in credit if isinstance(c, dict)
                ]
                s = score_similarity(
                    track.title, list(track.artists or []),
                    cand.get("title") or "", ", ".join(n for n in names if n),
                )
                if s > best_score:
                    best, best_score = cand, s
            if best is not None and best_score >= 0.80:
                tags = best.get("tags") or []
                tags_sorted = sorted(tags, key=lambda t: t.get("count", 0), reverse=True)
                genres = [t["name"].title() for t in tags_sorted[:3] if t.get("name")]
                if genres:
                    track.genres = genres
                    out["genres"] = genres[:]
                    logger.debug("[MetaEnrich] Genres from MB (text): %s -> %s", track.title, genres)
    except Exception as e:
        logger.debug("[MetaEnrich] MB text search failed: %s", e)


# ── 5. Lyrics ────────────────────────────────────────────────────────────────


def _lyrics_apple_mirrors() -> list:
    """Apple mirror pool for the lyrics fetcher (v1.1.8 FEAT-9).

    Goes through service._resolve_apple_mirrors because the pool lives in the
    remote manifest, not in config — reading cfg.apple_mirrors directly yields
    an empty list on a default install and silently disables Apple lyrics.
    """
    try:
        from antra.core.config import load_config
        from antra.core.service import _resolve_apple_mirrors
        return list(_resolve_apple_mirrors(load_config()) or [])
    except Exception:
        return []


def _lyrics_mirror_key() -> str:
    try:
        from antra.core.service import _resolve_mirror_api_key
        from antra.core.config import load_config
        return _resolve_mirror_api_key(load_config())
    except Exception:
        return ""


def _enrich_lyrics(track: "TrackMetadata", out: dict[str, Any]) -> None:
    if track.lyrics or track.synced_lyrics:
        return
    if not track.title or not track.artists:
        return

    try:
        from antra.utils.lyrics import LyricsFetcher
        # v1.1.8 FEAT-9: this enricher path runs for every track, so it must get
        # the Apple mirror too — a bare LyricsFetcher() would silently skip the
        # strongest synced-lyrics provider and quietly undo the feature.
        fetcher = LyricsFetcher(
            apple_mirrors=_lyrics_apple_mirrors(),
            mirror_api_key=_lyrics_mirror_key(),
        )
        plain, synced = fetcher.fetch(track)
        if plain:
            track.lyrics = plain
            out["lyrics"] = plain
        if synced:
            track.synced_lyrics = synced
            out["synced_lyrics"] = synced
        if plain or synced:
            logger.debug("[MetaEnrich] Lyrics for '%s': plain=%s synced=%s", track.title, bool(plain), bool(synced))
    except Exception as e:
        logger.debug("[MetaEnrich] Lyrics fetch failed: %s", e)


# ── 6. Artwork upgrade ──────────────────────────────────────────────────────


def _upgrade_artwork(track: "TrackMetadata", out: dict[str, Any]) -> None:
    """Always attempt iTunes 3000×3000 — the highest resolution free art source.

    iTunes is tried unconditionally regardless of what the adapter or Deezer
    already set, because it serves the largest publicly available images.
    Only skips when the existing artwork is already 3000×3000.
    """
    if track.artwork_url and "3000x3000" in track.artwork_url:
        return
    artist = track.artists[0] if track.artists else ""
    title = track.title or ""
    if not artist or not title:
        return
    search_title = _clean_search_title(title)

    try:
        import requests as _req
    except ImportError:
        return

    try:
        resp = _req.get(
            "https://itunes.apple.com/search",
            params={"term": f"{artist} {search_title}", "entity": "song", "limit": 5, "country": "us"},
            timeout=8,
        )
        if resp.status_code != 200:
            return
        our_album = (getattr(track, "album", "") or "").strip()
        scored_fallback = None
        for hit in resp.json().get("results") or []:
            if hit.get("wrapperType") != "track":
                continue
            art = hit.get("artworkUrl100") or ""
            if not art:
                continue
            hit_title = hit.get("trackName") or ""
            hit_artist = hit.get("artistName") or ""
            hit_album = hit.get("collectionName") or ""

            # ALBUM FIRST. Cover art is an album property, so a hit whose album is
            # ours is the answer regardless of how the artist string scores -
            # score_similarity matches each of OUR artists against the result's
            # COMBINED artist string, so a hit that correctly lists every
            # collaborator can score BELOW a wrong hit with one coincidentally
            # similar name. Measured live on "Type Shit" through THIS tree's
            # score_similarity: the correct WE DON'T TRUST YOU hit scores 0.776
            # and Migos' Culture III scores 0.785 - the right answer loses by
            # 0.009. (The 0.500 recorded elsewhere is the WEB copy's number; its
            # score_similarity lacks desktop's title-trust bypass and softer cap,
            # so it caps that hit where desktop does not. Do not copy it here.)
            #
            # The ordering-independence this buys is what actually fixes desktop.
            # Measured on live responses: for "Bohemian Rhapsody"/A Night at the
            # Opera and "Dreams"/Rumours a non-matching compilation is hit [0],
            # clears the 0.60 gate, and the old unconditional `break` stopped
            # there - throwing away the matching release further down the list.
            album_hit = bool(our_album and hit_album
                             and _album_titles_match(our_album, hit_album))
            if album_hit and _artist_plausible(track.artists, hit_artist):
                url = _re.sub(r"\d+x\d+bb", "3000x3000bb", art)
                track.artwork_url = url
                out["artwork_url"] = url
                logger.debug("[MetaEnrich] Art upgraded to iTunes 3000px (album match): %s", title)
                return

            # Otherwise keep the old title+artist rule as a fallback, but REMEMBER it
            # instead of breaking: the previous code took the first score-passing hit
            # and stopped, so a wrong-album hit early in the list threw away a correct
            # one further down.
            #
            # The same artist guard applies here. It is not redundant with the 0.60
            # gate: this tree's score_similarity has a title-trust bypass, so a hit
            # whose title matches ours exactly clears the gate even when the artist
            # is a stranger -- measured, ('Greatest Hits', ['Various Artists']) vs
            # ('Greatest Hits', 'Queen & David Bowie') scores 0.714. Embedding that
            # cover is the exact failure this whole routine exists to prevent.
            if scored_fallback is None and _album_titles_match(our_album, hit_album) \
                    and _artist_plausible(track.artists, hit_artist) \
                    and _score_similarity(search_title, track.artists, hit_title, hit_artist) >= 0.60:
                scored_fallback = art

        if scored_fallback:
            url = _re.sub(r"\d+x\d+bb", "3000x3000bb", scored_fallback)
            track.artwork_url = url
            out["artwork_url"] = url
            logger.debug("[MetaEnrich] Art upgraded to iTunes 3000px: %s", title)
    except Exception as e:
        logger.debug("[MetaEnrich] Art upgrade (iTunes) failed: %s", e)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _is_spotify_art(url: str) -> bool:
    return "i.scdn.co" in url if url else False


def _artist_plausible(our_artists: Optional[list], candidate_artist: Optional[str]) -> bool:
    """Is `candidate_artist` plausibly the same act as ours?

    Album-first artwork selection deliberately drops the composite score gate,
    because score_similarity matches each of OUR artists against the result's
    COMBINED artist string and therefore under-scores correct multi-artist hits.
    Dropping it entirely is unsafe for generically-titled releases though:
    measured live, an album literally called "Greatest Hits" album-matches every
    hit in the response, and the first was a different artist's compilation at
    composite score 0.233. This is the narrow guard that keeps that out -- it
    asks only "same act?", never "same recording?", so it cannot reintroduce the
    multi-artist weakness the album-first rule exists to route around.

    Unknown on either side returns True, matching _album_titles_match's
    convention in this module: absent evidence is not contrary evidence.
    """
    try:
        from antra.utils.matching import normalize, string_similarity
    except Exception:
        return True

    cand = normalize(candidate_artist or "")
    ours = [normalize(a or "") for a in (our_artists or [])]
    ours = [a for a in ours if a]
    if not cand or not ours:
        return True

    for a in ours:
        # Whole-word containment in either direction. Word boundaries matter:
        # a bare substring test lets a very short act name match almost anything.
        if _re.search(r"\b" + _re.escape(a) + r"\b", cand):
            return True
        if _re.search(r"\b" + _re.escape(cand) + r"\b", a):
            return True
        if string_similarity(a, cand) >= 0.60:
            return True
    return False


def _album_titles_match(current_album: Optional[str], candidate_album: Optional[str]) -> bool:
    current = (current_album or "").strip()
    candidate = (candidate_album or "").strip()
    if not current or not candidate:
        return True
    current_norm = _normalize_release_title(current)
    candidate_norm = _normalize_release_title(candidate)
    if not current_norm or not candidate_norm:
        return True
    if current_norm == candidate_norm:
        return True
    if current_norm in candidate_norm or candidate_norm in current_norm:
        return True
    return _score_similarity(current, [current], candidate, candidate) >= 0.78


def _normalize_release_title(value: str) -> str:
    value = (value or "").lower()
    value = _re.sub(r"\([^)]*\)|\[[^\]]*\]", "", value)
    value = _re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def _score_similarity(
    query_title: str,
    query_artists: list[str],
    result_title: str,
    result_artist: str,
) -> float:
    try:
        from antra.utils.matching import score_similarity
        return score_similarity(query_title, query_artists, result_title, result_artist)
    except Exception:
        # Fallback: simple substring match
        qt = query_title.lower().strip()
        rt = result_title.lower().strip()
        if qt in rt or rt in qt:
            return 0.75
        return 0.0
