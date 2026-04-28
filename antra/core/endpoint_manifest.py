"""
Runtime endpoint manifest loader.

The app ships with a single remote manifest URL that you control. That manifest
publishes the current endpoint pools for the community-backed lossless sources
so desktop users can follow endpoint changes without reinstalling the app.

Schema:
{
  "hifi": ["https://..."],
  "amazon": ["https://...", "https://..."],
  "dab": {
    "search": ["https://..."],
    "stream": ["https://...", "https://..."]
  }
}
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

# No built-in remote manifest is shipped anymore. Community download endpoints
# are no longer auto-loaded by default; a manifest URL must be provided
# explicitly via ANTRA_ENDPOINT_MANIFEST_URL if this loader is reused for
# non-download experiments.
DEFAULT_ENDPOINT_MANIFEST_URL = ""

try:
    from platformdirs import user_data_dir

    _DATA_DIR = Path(user_data_dir("Antra", "Antra"))
except Exception:
    _DATA_DIR = Path(__file__).resolve().parents[2]

_CACHE_PATH = _DATA_DIR / "endpoint_manifest_cache.json"
_REQUEST_TIMEOUT = 5
_GIST_ID_RE = re.compile(r"([0-9a-f]{32})", re.IGNORECASE)


@dataclass
class EndpointManifest:
    hifi: list[str]
    amazon: list[str]
    apple: list[str]
    dab_search: list[str]
    dab_stream: list[str]

    def health_endpoints(self, source: str) -> list[str]:
        if source == "hifi":
            return list(self.hifi)
        if source == "amazon":
            return list(self.amazon)
        if source == "apple":
            return list(self.apple)
        if source == "dab":
            return list(self.dab_search)
        return []


def load_endpoint_manifest(manifest_url: str | None = None) -> EndpointManifest:
    """Load the endpoint manifest from remote, falling back to the local cache."""
    manifest_url = (manifest_url or os.getenv("ANTRA_ENDPOINT_MANIFEST_URL") or DEFAULT_ENDPOINT_MANIFEST_URL).strip()

    if not manifest_url:
        cached = _read_cache()
        if cached is not None:
            return cached
        logger.info("[Endpoints] No manifest URL configured; endpoint manifest loader is idle")
        return EndpointManifest(hifi=[], amazon=[], apple=[], dab_search=[], dab_stream=[])

    remote_data = _fetch_remote_manifest(manifest_url)
    if remote_data is not None:
        manifest = _parse_manifest(remote_data)
        _write_cache(manifest)
        return manifest

    cached = _read_cache()
    if cached is not None:
        return cached

    logger.warning("[Endpoints] No remote manifest and no cache available")
    return EndpointManifest(hifi=[], amazon=[], apple=[], dab_search=[], dab_stream=[])


def _fetch_remote_manifest(manifest_url: str) -> Any | None:
    session = requests.Session()
    session.trust_env = False

    try:
        response = session.get(manifest_url, timeout=_REQUEST_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        logger.debug(f"[Endpoints] Remote manifest fetch failed: {exc}")

    gist_id = _extract_gist_id(manifest_url)
    if gist_id:
        try:
            response = session.get(
                f"https://api.github.com/gists/{gist_id}",
                timeout=_REQUEST_TIMEOUT,
                headers={"Accept": "application/vnd.github+json"},
            )
            response.raise_for_status()
            payload = response.json()
            return _extract_manifest_from_gist_payload(payload)
        except Exception as exc:
            logger.debug(f"[Endpoints] Gist API manifest fetch failed: {exc}")
    return None


def _extract_gist_id(manifest_url: str) -> str | None:
    match = _GIST_ID_RE.search(manifest_url or "")
    return match.group(1) if match else None


def _extract_manifest_from_gist_payload(payload: Any) -> Any | None:
    if not isinstance(payload, dict):
        return None
    files = payload.get("files")
    if not isinstance(files, dict):
        return None

    for file_info in files.values():
        if not isinstance(file_info, dict):
            continue
        content = file_info.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        try:
            return json.loads(content)
        except Exception:
            continue
    return None


def _parse_manifest(payload: Any) -> EndpointManifest:
    if isinstance(payload, list):
        # Transitional compatibility for the old HiFi-only gist format.
        return EndpointManifest(
            hifi=_normalize_url_list(payload),
            amazon=[],
            apple=[],
            dab_search=[],
            dab_stream=[],
        )
    if not isinstance(payload, dict):
        logger.warning("[Endpoints] Manifest payload is not a supported JSON shape")
        return EndpointManifest(hifi=[], amazon=[], apple=[], dab_search=[], dab_stream=[])

    dab = payload.get("dab")
    dab_search: list[str]
    dab_stream: list[str]
    if isinstance(dab, dict):
        dab_search = _normalize_url_list(dab.get("search"))
        dab_stream = _normalize_url_list(dab.get("stream"))
    else:
        # Legacy fallback: treat `dab` as the search endpoint pool only.
        dab_search = _normalize_url_list(dab)
        dab_stream = []

    return EndpointManifest(
        hifi=_normalize_url_list(payload.get("hifi")),
        amazon=_normalize_url_list(payload.get("amazon")),
        apple=_normalize_url_list(payload.get("apple")),
        dab_search=dab_search,
        dab_stream=dab_stream,
    )


def _normalize_url_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    urls: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        url = item.strip().rstrip("/")
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _write_cache(manifest: EndpointManifest) -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(
            json.dumps(
                {
                    "hifi": manifest.hifi,
                    "amazon": manifest.amazon,
                    "apple": manifest.apple,
                    "dab": {
                        "search": manifest.dab_search,
                        "stream": manifest.dab_stream,
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.debug(f"[Endpoints] Failed to write manifest cache: {exc}")


def _read_cache() -> EndpointManifest | None:
    try:
        if not _CACHE_PATH.exists():
            return None
        payload = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        manifest = _parse_manifest(payload)
        logger.debug("[Endpoints] Loaded endpoint manifest from cache")
        return manifest
    except Exception as exc:
        logger.debug(f"[Endpoints] Failed to read manifest cache: {exc}")
        return None
