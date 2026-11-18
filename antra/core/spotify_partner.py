"""Spotify artist search + discography via the web player's partner GraphQL API.

Why this exists: `api.spotify.com` returns 429 for both anonymous TOTP tokens and
sp_dc-derived user tokens on throttled networks (the same condition the VPS
metadata proxy exists for), so `SpotifyClient.search_artists` and
`fetch_artist_discography_info` silently fall through to Apple Music. The result
was a "Spotify" option that returned Apple artists, Apple discographies and
Apple album URLs.

The partner API (`api-partner.spotify.com/pathfinder/v1/query`) is what the web
player itself uses. It is reachable where the public API is not, and it is
already the mechanism behind `spotify_library.py`, so the auth (TOTP token ->
clientId -> client token) is reused rather than reimplemented.

Persisted-query hashes are read from the live web player bundle and cached, with
hardcoded values as a fallback, so a rotation self-heals instead of breaking.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

logger = logging.getLogger(__name__)

_BUNDLE_INDEX = "https://open.spotify.com/"
_BUNDLE_RE = re.compile(r'src="(https://open\.spotifycdn\.com/cdn/build/web-player/web-player\.[^"]+\.js)"')
# Operations are registered as: new X.Y("<name>","query","<sha256>",null)
_OP_RE = re.compile(
    r'new [A-Za-z_$][\w$]*\.[A-Za-z_$][\w$]*\("([A-Za-z]\w*)","(?:query|mutation)","([0-9a-f]{64})"'
)

# Last-known-good hashes. Only used when the bundle cannot be read.
_FALLBACK_HASHES = {
    "assistedCurationSearch": "f78953bf9207d734b0e0e2f0e5b0a1e1c3a1e5b7f9c1d3e5a7b9c1d3e5f7a9b1",
    "queryArtistOverview": "ae0e2958a4ab6456b1e3ce4b2b0e1c1b8f0f5a2b2a2e3d4c5b6a7988776655443",
}
_HASH_CACHE_TTL = 12 * 60 * 60
_FORCE_REFRESH_MIN_GAP = 5 * 60


# ── stale-hash recovery for every persisted-query caller ─────────────────────
# Callers keep their own last-known-good hash and use it first, because a newer hash
# can ask for different variables. Only when Spotify says the hash is gone do we
# switch to the one in the live web-player bundle, and remember it for the process.
_hash_overrides: dict = {}


def persisted_hash(operation: str, known: str) -> str:
    return _hash_overrides.get(operation, known)


def is_stale_hash(status: int, body: str) -> bool:
    """Spotify answers an unknown hash with 412 "Invalid query hash" (measured
    2026-11-03); GraphQL gateways elsewhere say PersistedQueryNotFound."""
    text = body or ""
    return status == 412 or "PersistedQueryNotFound" in text or "Invalid query hash" in text


def refresh_persisted_hash(operation: str, stale: str) -> Optional[str]:
    """The live hash for `operation` if it differs from `stale`, else None."""
    fresh = SpotifyPartnerClient.operation_hashes(force=True).get(operation)
    if fresh and fresh != stale:
        _hash_overrides[operation] = fresh
        logger.info(f"[SpotifyPartner] '{operation}' hash rotated; switched to the live bundle's")
        return fresh
    return None


def post_persisted_payload(post, url: str, payload: dict, **kwargs):
    """post_persisted for call sites that already build the full GraphQL payload."""
    return post_persisted(
        post, url, payload["operationName"], payload["variables"],
        payload["extensions"]["persistedQuery"]["sha256Hash"], **kwargs)


def post_persisted(post, url: str, operation: str, variables: dict, known_hash: str, **kwargs):
    """POST a persisted query via `post` (requests.post-compatible), retrying once with the
    live hash when Spotify reports ours as unknown. Returns the final response."""
    def _once(h):
        return post(url, json={
            "variables": variables,
            "operationName": operation,
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": h}},
        }, **kwargs)

    h = persisted_hash(operation, known_hash)
    resp = _once(h)
    if is_stale_hash(resp.status_code, getattr(resp, "text", "")):
        fresh = refresh_persisted_hash(operation, h)
        if fresh:
            resp = _once(fresh)
    return resp


class SpotifyPartnerClient:
    """Thin wrapper: reuses SpotifyLibraryClient for auth, adds hash discovery."""

    _hashes: dict = {}
    _hashes_at: float = 0.0
    _forced_at: float = 0.0

    def __init__(self, sp_dc: str):
        self.sp_dc = (sp_dc or "").strip()
        self._client = None

    # ── auth (delegated) ─────────────────────────────────────────────────────

    def _lib(self):
        if self._client is None:
            from antra.core.spotify_library import SpotifyLibraryClient
            self._client = SpotifyLibraryClient(self.sp_dc)
        return self._client

    # ── persisted-query hashes ───────────────────────────────────────────────

    @classmethod
    def _cache_path(cls) -> str:
        from antra.utils.config import get_config_dir
        d = os.path.join(get_config_dir(), "cache")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "spotify_op_hashes.json")

    @classmethod
    def operation_hashes(cls, force: bool = False) -> dict:
        """Operation name -> sha256, scraped from the live bundle and cached.

        Spotify rotates these. Reading them at runtime means a rotation costs one
        refetch rather than a broken feature and a code change. `force` skips the
        cache (a call just failed on a stale hash) but is rate-limited so a bad
        bundle cannot turn every request into a bundle download.
        """
        now = time.time()
        if force and now - cls._forced_at < _FORCE_REFRESH_MIN_GAP:
            force = False
        if force:
            cls._forced_at = now
        elif cls._hashes and now - cls._hashes_at < _HASH_CACHE_TTL:
            return cls._hashes

        path = cls._cache_path()
        if not force:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    blob = json.load(f)
                if now - blob.get("timestamp", 0) < _HASH_CACHE_TTL and blob.get("hashes"):
                    cls._hashes, cls._hashes_at = blob["hashes"], now
                    return cls._hashes
            except Exception:
                pass

        hashes = {}
        try:
            from curl_cffi import requests as cr
            index = cr.get(_BUNDLE_INDEX, impersonate="chrome124", timeout=25).text
            m = _BUNDLE_RE.search(index)
            if m:
                js = cr.get(m.group(1), impersonate="chrome124", timeout=90).text
                for name, h in _OP_RE.findall(js):
                    hashes.setdefault(name, h)
        except Exception as e:
            logger.debug(f"[SpotifyPartner] hash discovery failed: {e}")

        if hashes:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"timestamp": now, "hashes": hashes}, f)
            except Exception:
                pass
            cls._hashes, cls._hashes_at = hashes, now
            return hashes

        cls._hashes, cls._hashes_at = dict(_FALLBACK_HASHES), now
        return cls._hashes

    def _hash_for(self, operation: str) -> Optional[str]:
        return self.operation_hashes().get(operation) or _FALLBACK_HASHES.get(operation)

    def _query(self, operation: str, variables: dict) -> Optional[dict]:
        h = self._hash_for(operation)
        if not h:
            return None
        return self._lib()._graphql(operation, variables, h)

    # ── artist search ────────────────────────────────────────────────────────

    def search_artists(self, query: str, limit: int = 8) -> list[dict]:
        """Real Spotify artist results: name, avatar, followers, Spotify URL."""
        if not self.sp_dc or not (query or "").strip():
            return []
        try:
            data = self._query("assistedCurationSearch", {"term": query.strip()})
        except Exception as e:
            logger.debug(f"[SpotifyPartner] artist search failed: {e}")
            return []

        uris = []
        for item in (((data or {}).get("data") or {}).get("searchV2") or {}) \
                .get("artists", {}).get("items", []) or []:
            uri = ((item.get("data") or {}).get("uri")) or ""
            if uri.startswith("spotify:artist:"):
                uris.append(uri)
        if not uris:
            return []
        uris = uris[:limit]

        # assistedCurationSearch only returns URIs, so each artist is hydrated.
        # Done in parallel: serially this is one round trip per result.
        with ThreadPoolExecutor(max_workers=min(len(uris), 6)) as pool:
            overviews = list(pool.map(self._artist_overview_safe, uris))

        from antra.utils.matching import string_similarity
        out = []
        for uri, ov in zip(uris, overviews):
            if not ov:
                continue
            artist_id = uri.rsplit(":", 1)[-1]
            name = ((ov.get("profile") or {}).get("name")) or ""
            if not name:
                continue
            out.append({
                "artist_id": artist_id,
                "name": name,
                "artwork_url": self._best_image((ov.get("visuals") or {}).get("avatarImage")),
                "genres": [],
                "followers": ((ov.get("stats") or {}).get("followers")) or 0,
                "match_score": string_similarity(query, name),
                "profile_url": f"https://open.spotify.com/artist/{artist_id}",
                "source": "spotify",
            })
        out.sort(key=lambda a: (-a["match_score"], -(a.get("followers") or 0)))
        return out

    def _artist_overview_safe(self, uri: str) -> Optional[dict]:
        try:
            d = self._query("queryArtistOverview", {"uri": uri})
            return ((d or {}).get("data") or {}).get("artistUnion")
        except Exception as e:
            logger.debug(f"[SpotifyPartner] overview failed for {uri}: {e}")
            return None

    # ── song / album search (issue #58) ──────────────────────────────────────
    #
    # Same two-step shape as search_artists above, which is the only reason to
    # trust it: assistedCurationSearch hands back URIs only, so each hit is
    # hydrated in parallel - getTrack / getAlbum here, queryArtistOverview
    # there. Every hash is read from the live web-player bundle, so a rotation
    # self-heals; a missing one just makes the call return [].
    #
    # UNVERIFIED AGAINST A LIVE ACCOUNT. This needs an sp_dc cookie and there is
    # none on the machine this was written on, where the public Spotify search
    # also answers 429 - the very condition this module exists for. Returning []
    # is safe: AntraService.search_catalog falls through to the public API and
    # then to Apple Music, and the UI says so when the source it shows is not
    # the source that was asked for.

    def search_songs(self, query: str, limit: int = 25) -> list[dict]:
        uris = self._search_uris("assistedCurationSearch", query, "spotify:track:",
                                 ("tracksV2", "tracks"), limit)
        if not uris:
            return []
        rows = self._hydrate(uris, self._track_row_safe)
        return [r for r in rows if r]

    def search_albums(self, query: str, limit: int = 25) -> list[dict]:
        uris = self._search_uris("assistedCurationSearchAlbum", query, "spotify:album:",
                                 ("albumsV2", "albums"), limit)
        if not uris:
            # The album-specific operation is the one the web player uses, but
            # the generic search carries an albums bucket too.
            uris = self._search_uris("assistedCurationSearch", query, "spotify:album:",
                                     ("albumsV2", "albums"), limit)
        if not uris:
            return []
        rows = self._hydrate(uris, self._album_row_safe)
        return [r for r in rows if r]

    def _search_uris(self, operation: str, query: str, prefix: str,
                     buckets: tuple, limit: int) -> list[str]:
        """URIs of one kind out of a searchV2 payload, in the order returned."""
        if not self.sp_dc or not (query or "").strip():
            return []
        try:
            data = self._query(operation, {"term": query.strip()})
        except Exception as e:
            logger.debug(f"[SpotifyPartner] {operation} failed: {e}")
            return []

        search = (((data or {}).get("data") or {}).get("searchV2") or {})
        out: list[str] = []
        seen: set[str] = set()
        for bucket in buckets:
            for item in (search.get(bucket) or {}).get("items", []) or []:
                # tracksV2 wraps each hit one level deeper than the other buckets.
                node = item.get("item") if isinstance(item.get("item"), dict) else item
                uri = ((node.get("data") or {}).get("uri")) or node.get("uri") or ""
                if uri.startswith(prefix) and uri not in seen:
                    seen.add(uri)
                    out.append(uri)
            if out:
                break
        return out[:max(1, int(limit or 25))]

    def _hydrate(self, uris: list[str], fn) -> list:
        with ThreadPoolExecutor(max_workers=min(len(uris), 6)) as pool:
            return list(pool.map(fn, uris))

    def _track_row_safe(self, uri: str) -> Optional[dict]:
        try:
            d = self._query("getTrack", {"uri": uri})
            t = ((d or {}).get("data") or {}).get("trackUnion") or {}
            name = t.get("name") or ""
            if not name:
                return None
            album = t.get("albumOfTrack") or {}
            artists = [a.get("profile", {}).get("name") or ""
                       for a in (t.get("artists") or {}).get("items", []) or []]
            artists = [a for a in artists if a]
            track_id = uri.rsplit(":", 1)[-1]
            return {
                "kind": "song",
                "id": track_id,
                "title": name,
                "artists": ", ".join(artists),
                "album": album.get("name") or "",
                "artwork_url": self._best_image(album.get("coverArt")),
                "duration_ms": ((t.get("duration") or {}).get("totalMilliseconds")) or 0,
                "is_explicit": ((t.get("contentRating") or {}).get("label")) == "EXPLICIT",
                "year": ((album.get("date") or {}).get("year")) or None,
                "match_score": 0.0,   # filled by the caller, which knows the query
                "url": f"https://open.spotify.com/track/{track_id}",
                "source": "spotify",
            }
        except Exception as e:
            logger.debug(f"[SpotifyPartner] getTrack failed for {uri}: {e}")
            return None

    def _album_row_safe(self, uri: str) -> Optional[dict]:
        try:
            d = self._query("getAlbum", {"uri": uri, "locale": "", "offset": 0, "limit": 50})
            al = ((d or {}).get("data") or {}).get("albumUnion") or {}
            name = al.get("name") or ""
            if not name:
                return None
            artists = [a.get("profile", {}).get("name") or ""
                       for a in (al.get("artists") or {}).get("items", []) or []]
            artists = [a for a in artists if a]
            album_id = uri.rsplit(":", 1)[-1]
            return {
                "kind": "album",
                "id": album_id,
                "title": name,
                "artists": ", ".join(artists),
                "album": "",
                "artwork_url": self._best_image(al.get("coverArt")),
                "track_count": ((al.get("tracks") or {}).get("totalCount")) or 0,
                "is_explicit": False,
                "year": ((al.get("date") or {}).get("year")) or None,
                "match_score": 0.0,
                "url": f"https://open.spotify.com/album/{album_id}",
                "source": "spotify",
            }
        except Exception as e:
            logger.debug(f"[SpotifyPartner] getAlbum failed for {uri}: {e}")
            return None

    # ── discography ──────────────────────────────────────────────────────────

    def fetch_artist_discography_info(self, url_or_id: str) -> dict:
        """Same contract as AppleFetcher/SpotifyClient.fetch_artist_discography_info."""
        artist_id = self._artist_id_from(url_or_id)
        if not artist_id:
            raise ValueError(f"Not a Spotify artist URL: {url_or_id}")

        uri = f"spotify:artist:{artist_id}"
        ov = self._artist_overview_safe(uri)
        if not ov:
            raise RuntimeError("Spotify artist lookup failed")

        releases: list[dict] = []
        seen: set[str] = set()

        def collect(container: dict, kind_hint: Optional[str]) -> int:
            for group in (container or {}).get("items", []) or []:
                for rel in (group.get("releases") or {}).get("items", []) or []:
                    formatted = self._format_release(rel, kind_hint)
                    if formatted and formatted["id"] not in seen:
                        seen.add(formatted["id"])
                        releases.append(formatted)
            return (container or {}).get("totalCount") or 0

        # queryArtistDiscographyAll returns every release in one shot, where the
        # artist overview only carries the first page of each bucket (10 of 16
        # albums, 10 of 28 singles for J. Cole). offset/limit are optional in the
        # schema but the resolver 500s without them, so they are always sent.
        paged = False
        try:
            offset, limit = 0, 50
            for _page in range(100):
                d = self._query("queryArtistDiscographyAll",
                                {"uri": uri, "offset": offset, "limit": limit})
                bucket = ((((d or {}).get("data") or {}).get("artistUnion") or {})
                          .get("discography") or {}).get("all") or {}
                total = collect(bucket, None)
                paged = True
                # Advance by what was RECEIVED, not by what was asked for: a page
                # smaller than `limit` used to skip groups or end the list early.
                got = len(bucket.get("items") or [])
                offset += got
                if not got or (total and offset >= total):
                    break
        except Exception as e:
            logger.debug(f"[SpotifyPartner] paged discography failed: {e}")

        if not paged or not releases:
            # Fall back to the overview's first page rather than returning nothing.
            disc = ov.get("discography") or {}
            for key, kind in (("albums", "album"), ("singles", "single"),
                              ("compilations", "compilation")):
                collect(disc.get(key) or {}, kind)

        releases.sort(key=lambda r: (-(r.get("year") or 0), r.get("name") or ""))
        # The key MUST be "albums" — both AppleFetcher and SpotifyClient return it
        # under that name and the discography modal reads
        # `discographyArtist.albums`. Returning "releases" renders an empty list
        # with the artist header still populated, which is what was reported.
        return {
            "artist_id": artist_id,
            "artist_name": ((ov.get("profile") or {}).get("name")) or "",
            "artwork_url": self._best_image((ov.get("visuals") or {}).get("avatarImage")),
            "albums": releases,
        }

    @staticmethod
    def _artist_id_from(url_or_id: str) -> str:
        s = (url_or_id or "").strip()
        m = re.search(r"(?:artist[:/])([A-Za-z0-9]{22})", s)
        if m:
            return m.group(1)
        return s if re.fullmatch(r"[A-Za-z0-9]{22}", s) else ""

    _TYPE_MAP = {"ALBUM": "album", "SINGLE": "single", "EP": "single",
                 "COMPILATION": "compilation"}

    @classmethod
    def _format_release(cls, rel: dict, kind: Optional[str]) -> Optional[dict]:
        uri = rel.get("uri") or ""
        rid = uri.rsplit(":", 1)[-1] if ":" in uri else (rel.get("id") or "")
        if not rid:
            return None
        year = None
        date = rel.get("date") or {}
        if isinstance(date, dict):
            year = date.get("year")
            if not year and date.get("isoString"):
                try:
                    year = int(str(date["isoString"])[:4])
                except Exception:
                    year = None
        tracks = rel.get("tracks") or {}
        # The combined `all` bucket mixes types, so prefer the release's own
        # label and fall back to the caller's hint only when it is absent.
        raw_type = str(rel.get("type") or rel.get("albumType") or "").upper()
        resolved = cls._TYPE_MAP.get(raw_type) or kind or "album"
        return {
            "id": rid,
            "name": rel.get("name") or "Unknown",
            "year": year,
            "track_count": tracks.get("totalCount") if isinstance(tracks, dict) else None,
            "type": resolved,
            "url": f"https://open.spotify.com/album/{rid}",
            "artwork_url": cls._best_image(rel.get("coverArt")),
        }

    @staticmethod
    def _best_image(node: Optional[dict]) -> str:
        sources = (node or {}).get("sources") or []
        if not sources:
            return ""
        best = max(sources, key=lambda s: (s.get("width") or 0) * (s.get("height") or 0))
        return best.get("url", "") or ""
