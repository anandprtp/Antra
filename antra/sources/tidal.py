"""
Tidal source adapter — lossless FLAC via tidalapi.

Install: pip install tidalapi

Tidal offers MQA (Master Quality Authenticated) and lossless FLAC.
This adapter uses the unofficial tidalapi library.
"""
import logging
import os
from typing import Optional

from antra.core.models import TrackMetadata, SearchResult, AudioFormat
from antra.sources.base import BaseSourceAdapter
from antra.utils.matching import score_similarity, duration_close

logger = logging.getLogger(__name__)

MIN_SIMILARITY = 0.80


class TidalAdapter(BaseSourceAdapter):
    name = "tidal"
    priority = 20  # Second priority after Qobuz

    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self._session = None

    def is_available(self) -> bool:
        try:
            import tidalapi  # noqa
            return bool(self.email and self.password)
        except ImportError:
            return False

    def _get_session(self):
        if self._session:
            return self._session
        try:
            import tidalapi
            session = tidalapi.Session()
            session.login(self.email, self.password)
            self._session = session
            logger.info("[Tidal] Logged in successfully.")
            return session
        except Exception as e:
            raise RuntimeError(f"Tidal login failed: {e}")

    def search(self, track: TrackMetadata) -> Optional[SearchResult]:
        try:
            session = self._get_session()
        except Exception as e:
            logger.warning(f"[Tidal] {e}")
            return None

        query = f"{track.title} {track.primary_artist}"
        try:
            results = session.search(query, models=["track"], limit=10)
            tidal_tracks = results.get("tracks", [])
        except Exception as e:
            logger.warning(f"[Tidal] Search failed: {e}")
            return None

        best = None
        best_score = 0.0

        for t in tidal_tracks:
            artist_name = t.artist.name if hasattr(t, "artist") else ""
            score = score_similarity(
                query_title=track.title,
                query_artists=track.artists,
                result_title=t.name,
                result_artist=artist_name,
            )

            # ISRC check
            isrc_match = False
            if track.isrc and hasattr(t, "isrc") and t.isrc:
                if t.isrc.upper() == track.isrc.upper():
                    score = 1.0
                    isrc_match = True

            if hasattr(t, "duration") and track.duration_seconds:
                if not duration_close(track.duration_seconds, t.duration, tolerance=5):
                    score *= 0.8

            if score > best_score:
                best_score = score
                best = SearchResult(
                    source=self.name,
                    title=t.name,
                    artists=[artist_name],
                    album=t.album.name if hasattr(t, "album") else None,
                    duration_ms=int(t.duration * 1000) if hasattr(t, "duration") else None,
                    audio_format=AudioFormat.FLAC,
                    quality_kbps=None,
                    is_lossless=True,
                    download_url=None,
                    stream_id=str(t.id),
                    similarity_score=score,
                    isrc_match=isrc_match,
                )

        if best and best_score >= MIN_SIMILARITY:
            logger.debug(f"[Tidal] Match score={best_score:.2f}: {best.title}")
            return best

        return None

    def download(self, result: SearchResult, output_path: str) -> str:
        """
        Download Tidal track as FLAC.

        Note: Tidal requires FLAC decryption for MQA tracks.
        This implementation uses tidalapi's stream URL endpoint.
        For full MQA support, integrate with a tool like tidal-dl.
        """
        session = self._get_session()
        track_id = int(result.stream_id)

        try:
            # Get stream URL
            track = session.track(track_id)
            stream = track.get_url()

            final_path = output_path + ".flac"
            os.makedirs(os.path.dirname(final_path), exist_ok=True)

            import requests
            with requests.get(stream, stream=True) as r:
                r.raise_for_status()
                with open(final_path, "wb") as f:
                    for chunk in r.iter_content(65536):
                        f.write(chunk)

            return final_path

        except Exception as e:
            raise RuntimeError(f"Tidal download failed for track {track_id}: {e}")
