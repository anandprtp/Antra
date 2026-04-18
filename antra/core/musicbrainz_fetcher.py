import logging
import time
import requests
from typing import Optional

from antra.core.models import TrackMetadata

logger = logging.getLogger(__name__)

# Respect MusicBrainz 1 req/sec rate limit
_last_request_time = 0.0

def _mb_get(url: str, params: dict) -> dict:
    global _last_request_time
    now = time.time()
    elapsed = now - _last_request_time
    if elapsed < 1.0:
        time.sleep(1.0 - elapsed)
        
    headers = {
        "User-Agent": "AntraMusic/1.0 ( https://github.com/antra-music/antra )"
    }
    
    _last_request_time = time.time()
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    if resp.ok:
        return resp.json()
    return {}

def enrich_metadata(meta: TrackMetadata) -> TrackMetadata:
    """
    Query MusicBrainz by ISRC to find missing genres and the ISWC composer code.
    Runs synchronously and adheres to the 1 req/sec limit.
    """
    if not meta.isrc:
        return meta
        
    try:
        # Search recording by ISRC
        data = _mb_get(
            "https://musicbrainz.org/ws/2/recording/",
            {"query": f"isrc:{meta.isrc}", "fmt": "json"}
        )
        
        recordings = data.get("recordings", [])
        if not recordings:
            return meta
            
        mb_rec = recordings[0]
        
        # 1. Extract ISWC
        iswcs = mb_rec.get("iswcs", [])
        if iswcs and not meta.iswc:
            meta.iswc = iswcs[0]
            
        # 2. Extract Genres (from tags) if missing
        if not meta.genres:
            tags = mb_rec.get("tags", [])
            if tags:
                # Tags are voted, sort by count
                tags.sort(key=lambda t: t.get("count", 0), reverse=True)
                # Take top 3
                meta.genres = [t["name"].title() for t in tags[:3]]
                
    except Exception as e:
        logger.debug(f"[MusicBrainz] Failed to enrich metadata for {meta.isrc}: {e}")
        
    return meta
