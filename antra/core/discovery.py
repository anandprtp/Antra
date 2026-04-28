import json
import logging
import os
import time
from typing import Optional

import requests

from antra.core.apple_fetcher import AppleFetcher
from antra.utils.config import get_config_dir

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 3 * 24 * 60 * 60  # 3 days

class AppleDiscovery:
    """
    Fetches and caches Top Charts and Genre Playlists from Apple Music.
    """
    def __init__(self):
        self.fetcher = AppleFetcher()
        self.cache_dir = os.path.join(get_config_dir(), "cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cache_file = os.path.join(self.cache_dir, "apple_discovery.json")

    def _load_cache(self) -> dict:
        if os.path.exists(self.cache_file):
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.debug(f"[Discovery] Failed to load cache: {e}")
        return {}

    def _save_cache(self, data: dict):
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.debug(f"[Discovery] Failed to save cache: {e}")

    def get_discovery_data(self, storefront: str = "us", genre_id: Optional[str] = None, genre_name: Optional[str] = None) -> dict:
        cache = self._load_cache()
        key = f"{storefront}_{genre_id or 'all'}"
        
        now = time.time()
        cached_entry = cache.get(key)
        if cached_entry and now - cached_entry.get("timestamp", 0) < CACHE_TTL_SECONDS:
            cached_data = cached_entry["data"]
            # Don't serve a cached empty result — it was likely from a previous failed fetch.
            # Force a fresh network call so the user sees real charts.
            has_content = (
                bool(cached_data.get("top_albums"))
                or bool(cached_data.get("top_playlists"))
                or bool(cached_data.get("genre_albums"))
                or bool(cached_data.get("genre_playlists"))
            )
            if has_content:
                return cached_data

        logger.info(f"[Discovery] Fetching fresh discovery data for {storefront} (genre: {genre_name})")
        data = self._fetch_fresh_data(storefront, genre_id, genre_name)
        
        cache[key] = {
            "timestamp": now,
            "data": data
        }
        self._save_cache(cache)
        return data

    def get_genres(self, storefront: str = "us") -> list:
        cache = self._load_cache()
        key = f"genres_{storefront}"
        now = time.time()
        
        cached_entry = cache.get(key)
        if cached_entry and now - cached_entry.get("timestamp", 0) < CACHE_TTL_SECONDS:
            # Don't serve an empty genres cache — force a fresh fetch
            if cached_entry["data"]:
                return cached_entry["data"]

        token = self.fetcher._get_developer_token()
        if not token:
            return []

        try:
            res = requests.get(
                f"https://api.music.apple.com/v1/catalog/{storefront}/genres",
                headers={"Authorization": f"Bearer {token}", "Origin": "https://music.apple.com"},
                timeout=15
            )
            if res.ok:
                genres = []
                for g in res.json().get("data", []):
                    # Filter out parent containers like 'Music'
                    if g["attributes"]["name"] != "Music":
                        genres.append({
                            "id": g["id"],
                            "name": g["attributes"]["name"]
                        })
                # Sort alphabetically
                genres.sort(key=lambda x: x["name"])
                
                cache[key] = {
                    "timestamp": now,
                    "data": genres
                }
                self._save_cache(cache)
                return genres
        except Exception as e:
            logger.debug(f"[Discovery] Failed to fetch genres: {e}")

        return []

    def _fetch_fresh_data(self, storefront: str, genre_id: Optional[str], genre_name: Optional[str]) -> dict:
        token = self.fetcher._get_developer_token()
        if not token:
            return {"top_albums": [], "top_playlists": [], "genre_playlists": []}

        headers = {"Authorization": f"Bearer {token}", "Origin": "https://music.apple.com"}
        data = {
            "top_albums": [],
            "top_playlists": [],
            "genre_playlists": []
        }

        # 1. Fetch Top Albums (Filtered by genre if provided)
        charts_url = f"https://api.music.apple.com/v1/catalog/{storefront}/charts?types=albums&limit=20"
        if genre_id:
            charts_url += f"&genre={genre_id}"
            
        try:
            res = requests.get(charts_url, headers=headers, timeout=15)
            if res.ok:
                results = res.json().get("results", {})
                if "albums" in results and results["albums"]:
                    for item in results["albums"][0].get("data", []):
                        data["top_albums"].append(self._format_item(item, storefront, "album"))
        except Exception as e:
            logger.debug(f"[Discovery] Failed to fetch top albums: {e}")

        # 2. Fetch Top Playlists (Global, only if no genre is selected)
        if not genre_id:
            charts_url = f"https://api.music.apple.com/v1/catalog/{storefront}/charts?types=playlists&limit=20"
            try:
                res = requests.get(charts_url, headers=headers, timeout=15)
                if res.ok:
                    results = res.json().get("results", {})
                    if "playlists" in results and results["playlists"]:
                        for item in results["playlists"][0].get("data", []):
                            data["top_playlists"].append(self._format_item(item, storefront, "playlist"))
            except Exception as e:
                logger.debug(f"[Discovery] Failed to fetch top playlists: {e}")
        else:
            # 3. Fetch Genre Playlists via Search
            search_url = f"https://api.music.apple.com/v1/catalog/{storefront}/search?types=playlists&limit=20&term={genre_name}"
            try:
                res = requests.get(search_url, headers=headers, timeout=15)
                if res.ok:
                    results = res.json().get("results", {})
                    if "playlists" in results and results["playlists"]:
                        for item in results["playlists"].get("data", []):
                            data["genre_playlists"].append(self._format_item(item, storefront, "playlist"))
            except Exception as e:
                logger.debug(f"[Discovery] Failed to fetch genre playlists: {e}")

        return data

    def _format_item(self, item: dict, storefront: str, item_type: str) -> dict:
        attrs = item.get("attributes", {})
        
        artwork_url = ""
        art = attrs.get("artwork", {})
        if art:
            w = art.get("width", 600)
            h = art.get("height", 600)
            artwork_url = art.get("url", "").replace("{w}", str(w)).replace("{h}", str(h))

        url = attrs.get("url", "")
        # Ensure it has a valid apple music URL if API didn't provide full one
        if not url:
            if item_type == "album":
                url = f"https://music.apple.com/{storefront}/album/{item['id']}"
            elif item_type == "playlist":
                url = f"https://music.apple.com/{storefront}/playlist/{item['id']}"

        return {
            "id": item.get("id"),
            "type": item_type,
            "name": attrs.get("name", "Unknown"),
            "artist_name": attrs.get("artistName", ""),
            "curator_name": attrs.get("curatorName", ""),
            "artwork_url": artwork_url,
            "url": url
        }
