"""
Centralized metadata enrichment pipeline.

Fills every available TrackMetadata field by consulting sources in priority order:
  1. source_metadata from the winning adapter (already fetched with the download —
     most authoritative, zero extra API calls)
  2. Deezer free API (ISRC, track#, disc#, release date, 1000px artwork)
  3. iTunes Search API (genre, composer, track#, disc#, year, 3000px artwork)
  4. MusicBrainz (genre tags, ISWC, label — by ISRC or title+artist fallback)
  5. Lyrics: LRCLIB → Paxsenix (synced + plain)
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

            # 3 ── Lyrics ──────────────────────────────────────────────────
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

    try:
        resp = _req.get(
            "https://api.deezer.com/search",
            params={"q": f'artist:"{artist}" track:"{title}"', "limit": 5},
            timeout=8,
        )
        diag["search_status"] = resp.status_code
        if resp.status_code != 200:
            return
        for hit in resp.json().get("data") or []:
            hit_title = hit.get("title") or ""
            hit_artist = (hit.get("artist") or {}).get("name") or ""
            if _score_similarity(title, track.artists, hit_title, hit_artist) < 0.60:
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

    try:
        resp = _req.get(
            "https://itunes.apple.com/search",
            params={"term": f"{artist} {title}", "entity": "song", "limit": 10, "country": "us"},
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
            sim = _score_similarity(title, track.artists, hit_title, hit_artist)
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
            if recordings:
                tags = recordings[0].get("tags") or []
                tags_sorted = sorted(tags, key=lambda t: t.get("count", 0), reverse=True)
                genres = [t["name"].title() for t in tags_sorted[:3] if t.get("name")]
                if genres:
                    track.genres = genres
                    out["genres"] = genres[:]
                    logger.debug("[MetaEnrich] Genres from MB (text): %s -> %s", track.title, genres)
    except Exception as e:
        logger.debug("[MetaEnrich] MB text search failed: %s", e)


# ── 5. Lyrics ────────────────────────────────────────────────────────────────


def _enrich_lyrics(track: "TrackMetadata", out: dict[str, Any]) -> None:
    if track.lyrics or track.synced_lyrics:
        return
    if not track.title or not track.artists:
        return

    try:
        from antra.utils.lyrics import LyricsFetcher
        fetcher = LyricsFetcher()
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

    try:
        import requests as _req
    except ImportError:
        return

    try:
        resp = _req.get(
            "https://itunes.apple.com/search",
            params={"term": f"{artist} {title}", "entity": "song", "limit": 5, "country": "us"},
            timeout=8,
        )
        if resp.status_code != 200:
            return
        for hit in resp.json().get("results") or []:
            if hit.get("wrapperType") != "track":
                continue
            hit_title = hit.get("trackName") or ""
            hit_artist = hit.get("artistName") or ""
            if _score_similarity(title, track.artists, hit_title, hit_artist) < 0.60:
                continue
            art = hit.get("artworkUrl100") or ""
            if art and _album_titles_match(track.album, hit.get("collectionName") or ""):
                url = _re.sub(r"\d+x\d+bb", "3000x3000bb", art)
                track.artwork_url = url
                out["artwork_url"] = url
                logger.debug("[MetaEnrich] Art upgraded to iTunes 3000px: %s", title)
            break
    except Exception as e:
        logger.debug("[MetaEnrich] Art upgrade (iTunes) failed: %s", e)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _is_spotify_art(url: str) -> bool:
    return "i.scdn.co" in url if url else False


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
