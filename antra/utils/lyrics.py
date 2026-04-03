import re
import requests
import logging
from typing import Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from antra.core.models import TrackMetadata

logger = logging.getLogger(__name__)

class LyricsFetcher:
    """
    Fetches plain and synced lyrics from multiple providers.
    Primarily uses LRCLIB (free, no key, high-quality synced lyrics).
    """
    def __init__(self, musixmatch_api_key: Optional[str] = None, genius_api_key: Optional[str] = None):
        self.musixmatch_key = musixmatch_api_key
        self.genius_key = genius_api_key
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Antra/1.0 (https://github.com/afkarxyz/antra)"
        })

    def fetch(self, track: "TrackMetadata") -> Tuple[Optional[str], Optional[str]]:
        """
        Fetch lyrics for a track.
        Returns (plain_text, synced_lrc).
        """
        logger.debug(f"[Lyrics] Fetching for: {track.artist_string} - {track.title}")
        
        # 1. Try LRCLIB (Best for synced lyrics)
        try:
            lrc = self._fetch_lrclib(track)
            if lrc:
                plain, synced = lrc
                if plain or synced:
                    return plain, synced
        except Exception as e:
            logger.debug(f"[Lyrics] LRCLIB failed: {e}")

        # 2. Add future Genius/Musixmatch fallbacks here if keys provided
        # For now, we return (None, None) if LRCLIB fails to maintain stability
        return None, None

    def _fetch_lrclib(self, track: "TrackMetadata") -> Optional[Tuple[str, str]]:
        """Fetch from lrclib.net API."""
        params = {
            "artist_name": track.primary_artist,
            "track_name": track.title,
            "album_name": track.album,
            "duration": track.duration_seconds
        }
        
        # Cleanup params — remove None
        params = {k: v for k, v in params.items() if v is not None}
        
        try:
            resp = self.session.get("https://lrclib.net/api/get", params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                plain = data.get("plainLyrics")
                synced = data.get("syncedLyrics")
                return plain, synced
            
            # If not found via exact match, try search
            search_resp = self.session.get("https://lrclib.net/api/search", params={"q": f"{track.primary_artist} {track.title}"}, timeout=10)
            if search_resp.status_code == 200:
                results = search_resp.json()
                if results:
                    # Pick first result
                    res = results[0]
                    return res.get("plainLyrics"), res.get("syncedLyrics")
                    
        except Exception as e:
            logger.debug(f"[Lyrics] LRCLIB request error: {e}")
            
        return None

def validate_and_strip_lrc(lrc_text: str, duration_ms: int) -> str:
    """
    Parse LRC timestamps and strip any lines whose timestamp exceeds
    the track duration. Prevents player desyncs.
    Returns cleaned LRC string.
    """
    if not duration_ms or duration_ms <= 0 or not lrc_text:
        return lrc_text

    pattern = re.compile(r'^\[(\d{1,2}):(\d{2})\.(\d{2,3})\]')
    output_lines = []

    for line in lrc_text.splitlines():
        match = pattern.match(line)
        if match:
            minutes, seconds, centiseconds = match.groups()
            # Handle both 2-digit (cs) and 3-digit (ms) fractional seconds
            frac = int(centiseconds)
            if len(centiseconds) == 2:
                frac *= 10  # convert centiseconds to milliseconds
            elif len(centiseconds) == 3:
                pass # already in milliseconds
            
            line_ms = (int(minutes) * 60 + int(seconds)) * 1000 + frac
            if line_ms > duration_ms:
                logger.debug(
                    f"[Lyrics] Stripping out-of-range line at {line_ms}ms "
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
    frames = []

    for line in lrc_text.splitlines():
        match = pattern.match(line.strip())
        if match:
            minutes, seconds, centiseconds, text = match.groups()
            frac = int(centiseconds)
            if len(centiseconds) == 2:
                frac *= 10
            elif len(centiseconds) == 3:
                pass
                
            timestamp_ms = (int(minutes) * 60 + int(seconds)) * 1000 + frac
            frames.append((text.strip(), timestamp_ms))

    return frames
