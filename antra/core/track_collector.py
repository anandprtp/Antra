from typing import Optional
import logging
from antra.core.models import TrackMetadata

logger = logging.getLogger(__name__)

class TrackCollector:
    @staticmethod
    def parse_track_from_gql(track_data: dict, playlist_name: str, index: int) -> Optional[TrackMetadata]:
        try:
            typename = track_data.get("__typename", "")
            if typename not in ("Track", "TrackResponseWrapper"):
                return None

            inner = track_data.get("data") or track_data
            if inner.get("__typename") != "Track":
                return None

            spotify_id = inner.get("id") or inner.get("uri", "").split(":")[-1]
            if not spotify_id or len(spotify_id) < 10:
                return None

            name = inner.get("name", "Unknown")

            artists = []
            first = inner.get("firstArtist", {})
            if first:
                for a in first.get("items", []):
                    n = (a.get("profile", {})).get("name") or a.get("name")
                    if n: artists.append(n)
            other = inner.get("otherArtists", {})
            if other:
                for a in other.get("items", []):
                    n = (a.get("profile", {})).get("name") or a.get("name")
                    if n: artists.append(n)
            if not artists:
                for a in inner.get("artists", {}).get("items", []):
                    n = (a.get("profile") or {}).get("name") or a.get("name")
                    if n: artists.append(n)

            album_data = inner.get("albumOfTrack", {})
            album_name = album_data.get("name", "")
            album_id = (album_data.get("uri") or "").split(":")[-1] or None

            artwork_url = None
            sources = album_data.get("coverArt", {}).get("sources", [])
            if sources:
                best = max(sources, key=lambda s: s.get("width", 0) or 0)
                artwork_url = best.get("url")

            dur_data = inner.get("duration", {})
            duration_ms = dur_data.get("totalMilliseconds") or dur_data.get("totalMs")

            # Validate we have enough data to be useful before returning
            real_name = name.strip() if name else ""
            real_artists = [a for a in artists if a and a.strip()]
            if not real_name or not real_artists:
                logger.warning(
                    f"[TrackCollector] Skipping incomplete track — "
                    f"spotify_id={spotify_id}, name={repr(real_name)}, artists={real_artists}. "
                    f"Open https://open.spotify.com/track/{spotify_id} to identify it."
                )
                return None

            return TrackMetadata(
                title=real_name,
                artists=real_artists,
                album=album_name,
                album_id=album_id,
                duration_ms=int(duration_ms) if duration_ms else None,
                isrc=None,  # Populated later by ISRCEnricher
                spotify_id=spotify_id,
                artwork_url=artwork_url,
                playlist_name=playlist_name,
                playlist_position=index,
            )
        except Exception as e:
            logger.debug(f"[TrackCollector] Failed to parse track: {e}")
            return None

    @staticmethod
    def extract_tracks_from_playlist_gql(data: dict) -> list[dict]:
        tracks = []
        try:
            # Bug fix: use proper itemV2 payload traversal matching successful sync scripts
            items = data.get("data", {}).get("playlistV2", {}).get("content", {}).get("items", [])
            for item in items:
                track_data = item.get("itemV2", {}).get("data", {})
                if not track_data:
                    track_data = item.get("itemV2", {})
                if track_data:
                    tracks.append(track_data)
        except Exception as e:
            logger.debug(f"[TrackCollector] Playlist GQL parse error: {e}")
        return tracks

    @staticmethod
    def extract_tracks_from_fetch_library_tracks(data: dict) -> list[dict]:
        tracks = []
        try:
            items = data.get("data", {}).get("me", {}).get("library", {}).get("tracks", {}).get("items", [])
            for item in items:
                track_wrapper = item.get("track", {})
                track_data = track_wrapper.get("data") or track_wrapper
                if not track_data: continue

                if not track_data.get("id") and not track_data.get("uri"):
                    _uri = track_wrapper.get("_uri", "")
                    if _uri:
                        track_data = dict(track_data)
                        track_data["uri"] = _uri
                tracks.append(track_data)
        except Exception as e:
            logger.debug(f"[TrackCollector] fetchLibraryTracks parse error: {e}")
        return tracks

    @staticmethod
    def deduplicate_and_build(raw_tracks: list[dict], playlist_name: str) -> list[TrackMetadata]:
        seen = set()
        unique = []
        idx = 1
        for raw in raw_tracks:
            parsed = TrackCollector.parse_track_from_gql(raw, playlist_name, idx)
            if parsed and parsed.spotify_id not in seen:
                seen.add(parsed.spotify_id)
                unique.append(parsed)
                idx += 1
        logger.info(f"[TrackCollector] Built {len(unique)} unique tracks for '{playlist_name}'")
        return unique
