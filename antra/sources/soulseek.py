"""
Soulseek source adapter via slskd.

This adapter talks to a running slskd instance (Soulseek daemon) using its
HTTP API, via the official `slskd-api` Python client. It:

  1. Performs a text search for "title - artist"
  2. Collects search responses across users
  3. Scores candidates by format / bit depth / sample rate / filename match
  4. Enqueues the best candidate for download
  5. Polls the transfers API until the download completes, then moves the
     finished file into Antra's output directory

Priority: 1 — runs after HiFi (0) and before Debrid (2), DAB (3), etc.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from typing import Any, Iterator, Optional

import requests

from antra.core.models import AudioFormat, SearchResult, TrackMetadata
from antra.sources.base import BaseSourceAdapter
from antra.utils.matching import duration_close, score_similarity, normalize

logger = logging.getLogger(__name__)

try:  # Optional dependency; adapter is disabled if missing.
    import slskd_api  # type: ignore
except Exception:  # pragma: no cover - import guard
    slskd_api = None  # type: ignore


# ── Scoring / timeouts ─────────────────────────────────────────────────────────

SEARCH_TIMEOUT_MS = 60000  # slskd search timeout in ms
SEARCH_RESPONSE_LIMIT = 80
SEARCH_POLL_INTERVAL = 0.5  # seconds
# slskd searches can finish a few seconds after the nominal search timeout,
# especially on busy networks with many responses. Keep polling through a
# grace window so we don't declare "no result" before slskd is actually done.
SEARCH_POLL_DEADLINE = max(80.0, (SEARCH_TIMEOUT_MS / 1000.0) + 20.0)
SEARCH_INCOMPLETE_GRACE_PERIOD = 30.0

DOWNLOAD_POLL_INTERVAL = 1.0  # seconds
DOWNLOAD_POLL_DEADLINE = 600.0  # seconds (10 minutes max per track)
QUEUED_STALL_DEADLINE = 180.0  # seconds allowed in queued-like states
NO_MATCH_REMOVED_CHECK_AFTER = 20.0  # seconds with no match before checking includeRemoved=True
STATUS_LOG_INTERVAL = 10.0  # seconds

MIN_SIMILARITY = 0.55
MIN_LOSSY_BITRATE_KBPS = 224

# Strip collaboration credits from track titles before building Soulseek search
# queries.  Soulseek users typically name files without these suffixes, so
# "On Time (with John Legend)" → "On Time" matches far more shared files.
_COLLAB_RE = re.compile(
    r'\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+[^\)\]]+[\)\]]',
    re.IGNORECASE,
)


@dataclass
class _SoulseekFile:
    username: str
    filename: str
    size: int
    bitrate_kbps: Optional[int]
    sample_rate_hz: Optional[int]
    bit_depth: Optional[int]
    duration_s: Optional[float]
    has_free_upload_slot: bool
    queue_length: Optional[int]
    upload_speed_bps: Optional[float]


class SoulseekAdapter(BaseSourceAdapter):
    """Lossless-first Soulseek adapter backed by a slskd instance."""

    name = "soulseek"
    priority = 3  # After Amazon (1) and HiFi (2)

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        seed_after_download: bool = False,
    ):
        """
        :param base_url: Full base URL to slskd, e.g. "http://localhost:5030".
        :param api_key:  Optional API key configured in slskd.
        """
        if slskd_api is None:
            raise RuntimeError(
                "slskd-api is not installed. Install with 'pip install slskd-api'."
            )

        # The client expects host and optional url_base; it internally adds /api/v0.
        # Accept both "http://host:port" and "http://host:port/base".
        from urllib.parse import urlparse

        parsed = urlparse(base_url)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(f"Invalid SLSKD_BASE_URL: {base_url!r}")

        host = f"{parsed.scheme}://{parsed.netloc}"
        url_base = parsed.path or "/"

        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or ""
        self._seed_after_download = seed_after_download
        self._downloads_dir: Optional[str] = None
        self._blocked_stream_ids: set[str] = set()
        self._search_cache: dict[tuple[str, str], list[SearchResult]] = {}

        self._client = slskd_api.SlskdClient(host=host, api_key=api_key or None, url_base=url_base)  # type: ignore[arg-type]
        self._searches = self._client.searches
        self._transfers = self._client.transfers
        self._files = self._client.files

    # ── BaseSourceAdapter interface ────────────────────────────────────────────

    def is_available(self) -> bool:
        if slskd_api is None:
            return False
        try:
            # Confirm daemon reachability and (if configured) API-key auth.
            # Using a direct call is more reliable than relying on slskd-api's
            # auth_valid() which may depend on JWT vs API-key flows.
            endpoint = self._base_url + "/api/v0/application"
            headers: dict[str, str] = {}
            if self._api_key:
                headers["X-API-Key"] = self._api_key
            resp = requests.get(endpoint, headers=headers, timeout=4)
            return resp.status_code == 200
        except Exception as exc:  # pragma: no cover - depends on local daemon
            logger.debug(f"[Soulseek] Availability probe failed: {exc}")
            return False

    def search(self, track: TrackMetadata) -> Optional[SearchResult]:
        clean_title = _COLLAB_RE.sub("", track.title).strip() or track.title
        stripped_collab = clean_title != track.title
        if stripped_collab:
            logger.info(
                "[Soulseek] Stripped collab credits from search title: %r → %r",
                track.title, clean_title,
            )
        query = f"{clean_title} - {track.primary_artist}"
        cache_key = self._search_cache_key(track)
        cached_results = getattr(self, "_search_cache", {}).get(cache_key)
        if cached_results is not None:
            cached_match = self._next_unblocked_cached_result(cached_results)
            if cached_match:
                logger.debug(f"[Soulseek] Cache hit for: {query}")
                return cached_match
            logger.debug(f"[Soulseek] Cache hit with no remaining candidates for: {query}")
            return None
        logger.debug(f"[Soulseek] Searching: {query}")

        result = self._run_search(track, query, cache_key)
        if result is not None:
            return result

        # Fallback: title-only query when collab credits were stripped but the
        # primary query still found nothing (e.g. file shared as plain "On Time.flac"
        # with no artist in the name either).  Only runs for tracks that had collab
        # credits to strip — avoids doubling search time for ordinary failures.
        if stripped_collab:
            logger.info("[Soulseek] Primary query found nothing; retrying title-only: %r", clean_title)
            return self._run_search(track, clean_title, cache_key)
        return None

    def _run_search(self, track: TrackMetadata, query: str, cache_key: tuple) -> Optional[SearchResult]:
        search_id = str(uuid.uuid4())
        payload = {
            "id": search_id,
            "searchText": query,
            "searchTimeout": SEARCH_TIMEOUT_MS,
            "responseLimit": SEARCH_RESPONSE_LIMIT,
            "filterResponses": True,
            "fileLimit": 10000,
            "minimumPeerUploadSpeed": 0,
            "minimumResponseFileCount": 1,
            "maximumPeerQueueLength": 1000000,
        }
        
        try:
            headers = {"X-API-Key": self._api_key} if self._api_key else {}
            resp = requests.post(
                f"{self._base_url}/api/v0/searches",
                json=payload,
                headers=headers,
                timeout=10,
            )
            # If a 409 still occurs (daemon text conflict), fallback to query deletion
            if resp.status_code == 409:
                logger.debug(f"[Soulseek] 409 Conflict. Forcing query deletion: {query}")
                requests.delete(f"{self._base_url}/api/v0/searches/{requests.utils.quote(query)}", headers=headers, timeout=5)
                resp = requests.post(
                    f"{self._base_url}/api/v0/searches",
                    json=payload,
                    headers=headers,
                    timeout=10,
                )
            
            resp.raise_for_status()
            search_info = resp.json()
        except Exception as e:
            logger.warning(f"[Soulseek] search POST unexpected error: {e}")
            return None

        deadline = time.monotonic() + SEARCH_POLL_DEADLINE
        responses: list[dict[str, Any]] = []
        search_completed = False
        extended_deadline = False

        # Poll asynchronously-completing search until slskd reports completion
        # or we hit the deadline. Empty responses are not enough to conclude
        # "no result" because slskd can still be collecting responses.
        while True:
            now = time.monotonic()
            if now >= deadline:
                if not search_completed and not extended_deadline:
                    deadline = now + SEARCH_INCOMPLETE_GRACE_PERIOD
                    extended_deadline = True
                    logger.info(
                        "[Soulseek] Search still running after %.0fs; waiting an extra %.0fs for %r",
                        SEARCH_POLL_DEADLINE,
                        SEARCH_INCOMPLETE_GRACE_PERIOD,
                        query,
                    )
                else:
                    break
            state_info: dict[str, Any] = {}
            if hasattr(self._searches, "state"):
                try:
                    state_info = self._searches.state(search_id, includeResponses=False)  # type: ignore[call-arg]
                except Exception as exc:
                    logger.debug(f"[Soulseek] search state error: {exc}")
                    state_info = {}
            search_completed = bool(state_info.get("isComplete"))
            response_count = state_info.get("responseCount") or 0

            try:
                if response_count or search_completed or not state_info:
                    responses = self._searches.search_responses(search_id)  # type: ignore[call-arg]
            except Exception as exc:
                logger.debug(f"[Soulseek] search_responses error: {exc}")
                break
            if responses:
                break
            if search_completed:
                break
            time.sleep(SEARCH_POLL_INTERVAL)

        if not responses:
            if search_completed:
                self._search_cache[cache_key] = []
            else:
                logger.debug("[Soulseek] Search did not complete before deadline for query %r", query)
            logger.debug("[Soulseek] No responses for query %r", query)
            return None

        candidates: list[tuple[float, SearchResult]] = []
        for resp in responses:
            username = resp.get("username") or resp.get("user") or ""
            files = resp.get("files") or []
            if not username or not isinstance(files, list):
                continue
            for raw in files:
                file_obj = self._parse_file(username, raw, response=resp)
                if not file_obj:
                    continue
                scored = self._score_file(track, file_obj)
                if not scored:
                    continue
                score, result = scored
                if result.stream_id and result.stream_id in getattr(self, "_blocked_stream_ids", set()):
                    continue
                candidates.append((score, result))

        if not candidates:
            self._search_cache[cache_key] = []
            logger.debug("[Soulseek] No suitable candidates for %s", track.title)
            return None

        candidates.sort(key=lambda item: item[0], reverse=True)
        ranked_results = [
            result
            for score, result in candidates
            if score >= MIN_SIMILARITY
        ]
        self._search_cache[cache_key] = ranked_results
        if not ranked_results:
            logger.debug(
                "[Soulseek] Best similarity %.2f below threshold %.2f for %s",
                candidates[0][0],
                MIN_SIMILARITY,
                track.title,
            )
            return None

        best_score, best_result = candidates[0]

        logger.debug(
            "[Soulseek] Best match score=%.2f: %s (user=%s, %s)",
            best_score,
            best_result.title,
            best_result.stream_id.split("|", 1)[0] if best_result.stream_id else "?",
            best_result.quality_label,
        )
        return self._next_unblocked_cached_result(ranked_results)

    def download(self, result: SearchResult, output_path: str) -> str:
        """
        Enqueue the selected file in slskd's download queue and wait until it
        finishes, then move the completed file into Antra's target path.
        """
        if not result.stream_id:
            raise ValueError("[Soulseek] Missing stream_id in SearchResult")

        try:
            username, filename, size_str = result.stream_id.split("|", 2)
        except ValueError:
            raise ValueError(f"[Soulseek] Invalid stream_id: {result.stream_id!r}")

        try:
            size = int(size_str)
        except ValueError:
            size = 0

        logger.debug(
            "[Soulseek] Enqueue download: user=%s file=%s size=%s",
            username,
            filename,
            size_str,
        )

        ok = False
        for attempt in range(4):
            try:
                ok = self._transfers.enqueue(  # type: ignore[call-arg]
                    username=username,
                    files=[{"filename": filename, "size": size}],
                )
                break
            except requests.exceptions.HTTPError as e:
                if getattr(e.response, "status_code", 0) == 429 and attempt < 3:
                    logger.debug(f"[Soulseek] 429 Too Many Requests on enqueue, retrying in {2.0 * (attempt + 1)}s")
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise
        if not ok:
            raise RuntimeError(f"[Soulseek] Failed to enqueue download for {filename}")

        deadline = time.monotonic() + DOWNLOAD_POLL_DEADLINE
        download_path: Optional[str] = None
        completed_missing_path: Optional[str] = None
        first_queued_at: Optional[float] = None
        enqueue_at = time.monotonic()
        last_status_log_at = 0.0
        last_wait_log_at = 0.0
        checked_removed = False  # only do the includeRemoved=True pass once

        while time.monotonic() < deadline:
            try:
                downloads = self._transfers.get_all_downloads(includeRemoved=False)  # type: ignore[call-arg]
            except Exception as exc:
                logger.debug(f"[Soulseek] get_all_downloads error: {exc}")
                time.sleep(DOWNLOAD_POLL_INTERVAL)
                continue

            matched_transfer = False
            for dl in self._iter_download_entries(downloads):
                if str(dl.get("username") or dl.get("user")) != username:
                    continue
                dl_filename = dl.get("filename") or dl.get("file") or ""
                if not dl_filename or os.path.basename(dl_filename) != os.path.basename(filename):
                    continue

                matched_transfer = True
                status, state_tokens = self._extract_transfer_state(dl)
                now = time.monotonic()

                # Log progress periodically so long waits are visible in CLI.
                if now - last_status_log_at >= STATUS_LOG_INTERVAL:
                    transferred = dl.get("bytesTransferred") or dl.get("bytes_transferred") or 0
                    remaining = dl.get("bytesRemaining") or dl.get("bytes_remaining") or 0
                    speed = dl.get("averageSpeed") or dl.get("average_speed") or 0
                    queue_pos = dl.get("queuePosition") or dl.get("queue_position")
                    logger.debug(
                        "[Soulseek] Transfer state=%s queue=%s transferred=%s remaining=%s speed=%s",
                        status or "?",
                        queue_pos if queue_pos is not None else "-",
                        self._format_megabytes(transferred),
                        self._format_megabytes(remaining),
                        self._format_speed_megabytes(speed),
                    )
                    last_status_log_at = now

                if state_tokens.intersection({"failed", "error", "cancelled", "canceled", "rejected", "denied", "aborted"}):
                    raise RuntimeError(
                        f"[Soulseek] Download failed for {filename!r}: state={status}"
                    )
                if state_tokens.intersection({"completed", "succeeded", "success", "finished"}):
                    path = self._resolve_download_path(dl, filename, username=username)
                    if path:
                        download_path = path
                        break
                    completed_missing_path = self._candidate_download_path(dl, filename, username=username)
                    if completed_missing_path and now - last_wait_log_at >= STATUS_LOG_INTERVAL:
                        logger.info(
                            "[Soulseek] Transfer completed in slskd but file is not on disk yet: %s",
                            completed_missing_path,
                        )
                        last_wait_log_at = now
                    first_queued_at = None
                elif state_tokens.intersection({"queued", "requested", "initializing", "incomplete", "inprogress"}):
                    if first_queued_at is None:
                        first_queued_at = now
                    elif (now - first_queued_at) >= QUEUED_STALL_DEADLINE:
                        raise TimeoutError(
                            f"[Soulseek] Download stalled in state={status} for too long: {filename!r}"
                        )

            if not matched_transfer:
                now = time.monotonic()
                if now - last_wait_log_at >= STATUS_LOG_INTERVAL:
                    logger.info(
                        "[Soulseek] Waiting for transfer to appear in slskd queue: user=%s file=%s",
                        username,
                        os.path.basename(filename),
                    )
                    last_wait_log_at = now

                # slskd auto-removes completed transfers from the active list.
                # If the transfer hasn't appeared after a grace period, do one pass
                # with includeRemoved=True to catch transfers that completed and
                # were auto-removed before our first poll (fast peer / tiny file).
                if not checked_removed and (now - enqueue_at) >= NO_MATCH_REMOVED_CHECK_AFTER:
                    checked_removed = True
                    try:
                        removed_downloads = self._transfers.get_all_downloads(includeRemoved=True)  # type: ignore[call-arg]
                    except Exception as exc:
                        logger.debug(f"[Soulseek] get_all_downloads(includeRemoved=True) error: {exc}")
                        removed_downloads = []
                    for dl in self._iter_download_entries(removed_downloads):
                        if str(dl.get("username") or dl.get("user")) != username:
                            continue
                        dl_filename = dl.get("filename") or dl.get("file") or ""
                        if not dl_filename or os.path.basename(dl_filename) != os.path.basename(filename):
                            continue
                        _, state_tokens = self._extract_transfer_state(dl)
                        if state_tokens.intersection({"completed", "succeeded", "success", "finished"}):
                            logger.info(
                                "[Soulseek] Transfer for %s was auto-removed from slskd queue after completion; locating file on disk",
                                os.path.basename(filename),
                            )
                            path = self._resolve_download_path(dl, filename, username=username)
                            if path:
                                download_path = path
                                break
                            completed_missing_path = self._candidate_download_path(dl, filename, username=username)
                        break
                    if download_path:
                        break

            if download_path:
                break
            time.sleep(DOWNLOAD_POLL_INTERVAL)

        if not download_path:
            if completed_missing_path:
                raise FileNotFoundError(
                    f"[Soulseek] Transfer completed in slskd but file was not found at {completed_missing_path!r}"
                )
            raise TimeoutError(
                f"[Soulseek] Timed out waiting for download of {filename!r}"
            )

        ext = os.path.splitext(download_path)[1] or ".flac"
        final_path = output_path + ext
        os.makedirs(os.path.dirname(os.path.abspath(final_path)), exist_ok=True)

        if os.path.abspath(download_path) != os.path.abspath(final_path):
            for attempt in range(5):
                try:
                    shutil.move(download_path, final_path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    logger.debug(f"[Soulseek] File locked, retrying move in 1s (attempt {attempt + 1})")
                    time.sleep(1.0)

            if self._seed_after_download:
                self._seed_hardlink(download_path, final_path)
            else:
                self._cleanup_empty_download_dirs(download_path)

        logger.info(f"[Soulseek] Downloaded: {final_path}")
        return final_path

    def mark_failed_result(self, result: SearchResult, error: Exception) -> None:
        if not result.stream_id:
            return
        blocked = getattr(self, "_blocked_stream_ids", None)
        if blocked is None:
            blocked = set()
            self._blocked_stream_ids = blocked
        blocked.add(result.stream_id)

    def should_retry_download(self, result: SearchResult, error: Exception) -> bool:
        return False

    def should_exclude_adapter_after_failure(
        self,
        result: SearchResult,
        error: Exception,
    ) -> bool:
        return False

    @staticmethod
    def _search_cache_key(track: TrackMetadata) -> tuple[str, str]:
        return normalize(track.title), normalize(track.primary_artist)

    def _next_unblocked_cached_result(
        self,
        results: list[SearchResult],
    ) -> Optional[SearchResult]:
        blocked = getattr(self, "_blocked_stream_ids", set())
        for result in results:
            if result.stream_id and result.stream_id in blocked:
                continue
            return result
        return None

    # ── Internals ──────────────────────────────────────────────────────────────

    @staticmethod
    def _iter_download_entries(downloads: Any) -> Iterator[dict[str, Any]]:
        if not isinstance(downloads, list):
            return

        for item in downloads:
            if not isinstance(item, dict):
                continue

            directories = item.get("directories")
            if isinstance(directories, list):
                for directory in directories:
                    if not isinstance(directory, dict):
                        continue
                    for file_info in directory.get("files") or []:
                        if isinstance(file_info, dict):
                            yield file_info
                continue

            yield item

    @staticmethod
    def _extract_transfer_state(dl: dict[str, Any]) -> tuple[str, set[str]]:
        status = str(
            dl.get("stateDescription")
            or dl.get("state")
            or dl.get("status")
            or ""
        ).lower()
        tokens = {
            token
            for token in re.split(r"[^a-z]+", status)
            if token
        }
        return status, tokens

    def _resolve_download_path(self, dl: dict[str, Any], fallback_filename: str, username: str = "") -> Optional[str]:
        direct_path = dl.get("localPath") or dl.get("path") or dl.get("targetPath")
        if isinstance(direct_path, str) and os.path.isfile(direct_path):
            return direct_path

        for candidate in self._candidate_download_paths(dl, fallback_filename, username=username):
            if os.path.isfile(candidate):
                return candidate
        files_api_match = self._resolve_download_path_via_files_api(dl, username=username)
        if files_api_match:
            return files_api_match
        return None

    def _candidate_download_path(self, dl: dict[str, Any], fallback_filename: str, username: str = "") -> Optional[str]:
        candidates = self._candidate_download_paths(dl, fallback_filename, username=username)
        return candidates[0] if candidates else None

    def _candidate_download_paths(self, dl: dict[str, Any], fallback_filename: str, username: str = "") -> list[str]:
        downloads_dir = self._get_downloads_dir()
        relative_name = str(dl.get("filename") or dl.get("file") or fallback_filename or "")
        dl_username = username or str(dl.get("username") or dl.get("user") or "")
        if not downloads_dir or not relative_name:
            return []

        normalized_relative = relative_name.lstrip("\\/").replace("\\", os.sep).replace("/", os.sep)
        segments = [segment for segment in normalized_relative.split(os.sep) if segment]
        # Build relative-path variants from most-specific to least-specific.
        # slskd stores downloads as: {downloads_dir}/{username}/{remote_path_without_drive}
        # so the username-prefixed variants must come first.
        rel_variants = [normalized_relative]
        if len(segments) > 1:
            rel_variants.append(os.path.join(*segments[1:]))
        rel_variants.append(os.path.basename(normalized_relative))

        candidates: list[str] = []
        seen: set[str] = set()

        def _add(path: str) -> None:
            p = os.path.abspath(path)
            if p not in seen:
                seen.add(p)
                candidates.append(p)

        # Username-prefixed variants first (slskd default layout).
        if dl_username:
            for variant in rel_variants:
                _add(os.path.join(downloads_dir, dl_username, variant))

        # Without username prefix (external slskd or flat layout).
        for variant in rel_variants:
            _add(os.path.join(downloads_dir, variant))

        return candidates

    def _get_downloads_dir(self) -> Optional[str]:
        if self._downloads_dir is not None:
            return self._downloads_dir

        headers: dict[str, str] = {}
        if self._api_key:
            headers["X-API-Key"] = self._api_key

        try:
            resp = requests.get(self._base_url + "/api/v0/options", headers=headers, timeout=4)
            if resp.status_code != 200:
                return None
            payload = resp.json()
        except Exception as exc:
            logger.debug(f"[Soulseek] Failed to read slskd options: {exc}")
            return None

        downloads_dir = ((payload or {}).get("directories") or {}).get("downloads")
        if isinstance(downloads_dir, str) and downloads_dir:
            self._downloads_dir = downloads_dir
        return self._downloads_dir

    @staticmethod
    def _iter_downloaded_files(
        directory: Any,
        relative_dir: str = "",
    ) -> Iterator[tuple[dict[str, Any], str]]:
        if not isinstance(directory, dict):
            return

        for file_info in directory.get("files") or []:
            if isinstance(file_info, dict):
                yield file_info, relative_dir

        for nested in directory.get("directories") or []:
            if not isinstance(nested, dict):
                continue
            nested_name = str(
                nested.get("name")
                or os.path.basename(str(nested.get("fullName") or ""))
                or ""
            ).strip()
            next_relative_dir = relative_dir
            if nested_name:
                next_relative_dir = (
                    os.path.join(relative_dir, nested_name)
                    if relative_dir else nested_name
                )
            yield from SoulseekAdapter._iter_downloaded_files(
                nested,
                next_relative_dir,
            )

    def _resolve_download_path_via_files_api(
        self,
        dl: dict[str, Any],
        username: str = "",
    ) -> Optional[str]:
        downloads_dir = self._get_downloads_dir()
        basename = os.path.basename(str(dl.get("filename") or dl.get("file") or ""))
        expected_size = dl.get("size")
        if not downloads_dir or not basename:
            return None

        try:
            downloads_tree = self._files.get_downloads_dir(recursive=True)  # type: ignore[call-arg]
        except Exception as exc:
            logger.debug(f"[Soulseek] Failed to inspect slskd downloads dir: {exc}")
            return None

        for file_info, relative_dir in self._iter_downloaded_files(downloads_tree):
            file_name = str(file_info.get("name") or os.path.basename(str(file_info.get("fullName") or "")))
            if file_name != basename:
                continue

            try:
                actual_size = int(file_info.get("length") or 0)
            except (TypeError, ValueError):
                actual_size = 0
            try:
                wanted_size = int(expected_size) if expected_size is not None else 0
            except (TypeError, ValueError):
                wanted_size = 0
            if wanted_size and actual_size and actual_size != wanted_size:
                continue

            for candidate in self._files_api_candidates(
                downloads_dir=downloads_dir,
                file_info=file_info,
                relative_dir=relative_dir,
                username=username or str(dl.get("username") or dl.get("user") or ""),
            ):
                if os.path.isfile(candidate):
                    return candidate
        return None

    @staticmethod
    def _files_api_candidates(
        downloads_dir: str,
        file_info: dict[str, Any],
        relative_dir: str,
        username: str = "",
    ) -> list[str]:
        candidates: list[str] = []
        seen: set[str] = set()

        def _add(path: str) -> None:
            absolute = os.path.abspath(path)
            if absolute not in seen:
                seen.add(absolute)
                candidates.append(absolute)

        raw_full_name = str(file_info.get("fullName") or "").strip()
        file_name = str(
            file_info.get("name")
            or os.path.basename(raw_full_name)
            or ""
        ).strip()

        if raw_full_name:
            normalized_full_name = raw_full_name.replace("\\", os.sep).replace("/", os.sep)
            if os.path.isabs(normalized_full_name):
                _add(normalized_full_name)
            else:
                _add(os.path.join(downloads_dir, normalized_full_name))
                if username:
                    normalized_username = username.strip("\\/")
                    full_parts = [
                        part for part in normalized_full_name.split(os.sep) if part
                    ]
                    if not full_parts or full_parts[0] != normalized_username:
                        _add(os.path.join(downloads_dir, normalized_username, normalized_full_name))

        if file_name:
            if relative_dir:
                rel_path = os.path.join(relative_dir, file_name)
                _add(os.path.join(downloads_dir, rel_path))
                if username:
                    normalized_username = username.strip("\\/")
                    rel_parts = [part for part in rel_path.split(os.sep) if part]
                    if not rel_parts or rel_parts[0] != normalized_username:
                        _add(os.path.join(downloads_dir, normalized_username, rel_path))
            _add(os.path.join(downloads_dir, file_name))
            if username:
                _add(os.path.join(downloads_dir, username.strip("\\/"), file_name))

        return candidates

    def _seed_hardlink(self, original_download_path: str, library_path: str) -> None:
        """
        Create a hardlink at the original slskd download path pointing to the
        library file. Both paths share the same inode — zero extra disk space.
        slskd will detect the file in its downloads directory and seed it.
        Falls back to a regular copy if the filesystem doesn't support hardlinks
        (e.g. cross-device, FAT32, or NAS with different filesystem).
        """
        try:
            os.makedirs(os.path.dirname(os.path.abspath(original_download_path)), exist_ok=True)
            os.link(library_path, original_download_path)
            logger.debug(f"[Soulseek] Seed hardlink created: {original_download_path}")
        except OSError as e:
            # Cross-device link or filesystem doesn't support hardlinks — skip silently.
            # We don't fall back to copy: seeding is best-effort, not worth doubling disk use.
            logger.debug(f"[Soulseek] Could not create seed hardlink ({e}), skipping seed")

    def _cleanup_empty_download_dirs(self, original_download_path: str) -> None:
        downloads_dir = self._get_downloads_dir()
        if not downloads_dir:
            return

        root = os.path.abspath(downloads_dir)
        current = os.path.dirname(os.path.abspath(original_download_path))

        while current.startswith(root) and current != root:
            try:
                os.rmdir(current)
            except OSError:
                break
            current = os.path.dirname(current)

    @staticmethod
    def _format_megabytes(value: Any) -> str:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return "-"
        return f"{numeric / (1024 * 1024):.1f} MB"

    @staticmethod
    def _format_speed_megabytes(value: Any) -> str:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return "-"
        return f"{numeric / (1024 * 1024):.2f} MB/s"

    @staticmethod
    def _parse_file(
        username: str,
        raw: dict[str, Any],
        response: Optional[dict[str, Any]] = None,
    ) -> Optional[_SoulseekFile]:
        filename = raw.get("filename") or raw.get("file") or ""
        if not filename:
            return None
        size = int(raw.get("size") or 0)
        bitrate = raw.get("bitrate") or raw.get("bitRate")
        sample_rate = raw.get("sampleRate") or raw.get("sample_rate")
        bit_depth = raw.get("bitDepth") or raw.get("bit_depth")
        duration_ms = raw.get("durationMs") or raw.get("duration_ms")
        duration_s = raw.get("duration") if duration_ms is None else duration_ms / 1000.0
        has_free_upload_slot = bool((response or {}).get("hasFreeUploadSlot"))
        queue_length_raw = (response or {}).get("queueLength")
        upload_speed_raw = (response or {}).get("uploadSpeed")

        try:
            bitrate_kbps = int(bitrate) if bitrate is not None else None
        except (TypeError, ValueError):
            bitrate_kbps = None

        try:
            sample_rate_hz = int(sample_rate) if sample_rate is not None else None
        except (TypeError, ValueError):
            sample_rate_hz = None

        try:
            bit_depth_int = int(bit_depth) if bit_depth is not None else None
        except (TypeError, ValueError):
            bit_depth_int = None

        if isinstance(duration_s, str):
            try:
                duration_s = float(duration_s)
            except ValueError:
                duration_s = None

        try:
            queue_length = int(queue_length_raw) if queue_length_raw is not None else None
        except (TypeError, ValueError):
            queue_length = None

        try:
            upload_speed_bps = float(upload_speed_raw) if upload_speed_raw is not None else None
        except (TypeError, ValueError):
            upload_speed_bps = None

        return _SoulseekFile(
            username=username,
            filename=str(filename),
            size=size,
            bitrate_kbps=bitrate_kbps,
            sample_rate_hz=sample_rate_hz,
            bit_depth=bit_depth_int,
            duration_s=float(duration_s) if duration_s is not None else None,
            has_free_upload_slot=has_free_upload_slot,
            queue_length=queue_length,
            upload_speed_bps=upload_speed_bps,
        )

    @staticmethod
    def _format_from_extension(filename: str) -> tuple[AudioFormat, bool]:
        ext = os.path.splitext(filename.lower())[1]
        if ext in {".flac", ".ape", ".wv"}:
            return AudioFormat.FLAC, True
        if ext in {".wav"}:
            return AudioFormat.FLAC, True
        if ext in {".m4a", ".aac"}:
            return AudioFormat.AAC, False
        return AudioFormat.MP3, False

    def _score_file(
        self,
        track: TrackMetadata,
        file_obj: _SoulseekFile,
    ) -> Optional[tuple[float, SearchResult]]:
        lower_name = file_obj.filename.lower()

        # Hard reject obviously wrong content.
        if any(
            bad in lower_name
            for bad in (
                "karaoke",
                "instrumental",
                "tribute",
                "8d audio",
                "nightcore",
            )
        ):
            return None

        # Reject remixes unless title explicitly contains that marker.
        if "remix" in lower_name and "remix" not in normalize(track.title):
            return None

        audio_format, is_lossless = self._format_from_extension(file_obj.filename)

        if not is_lossless and file_obj.bitrate_kbps is not None:
            if file_obj.bitrate_kbps < MIN_LOSSY_BITRATE_KBPS:
                return None

        # Base textual similarity using filename (without directory).
        basename = os.path.splitext(os.path.basename(file_obj.filename))[0]
        text_score = score_similarity(
            query_title=track.title,
            query_artists=track.artists,
            result_title=basename,
            result_artist=file_obj.username,
        )

        # Slight boost if both normalized title and artist appear in the filename.
        norm_title = normalize(track.title)
        norm_artist = normalize(track.primary_artist)
        norm_name = normalize(basename)
        if norm_title and norm_title in norm_name and norm_artist and norm_artist in norm_name:
            text_score = max(text_score, 0.85)

        if file_obj.duration_s and track.duration_seconds:
            if not duration_close(track.duration_seconds, file_obj.duration_s, tolerance=7):
                text_score *= 0.75

        if text_score < 0.25:
            return None

        # Quality tier based on format, bit depth and sample rate.
        format_score = {
            AudioFormat.FLAC: 3.0,
            AudioFormat.AAC: 1.5,
            AudioFormat.MP3: 1.0,
        }.get(audio_format, 0.5)

        bit_depth_score = 0.0
        if is_lossless:
            if (file_obj.bit_depth or 0) >= 24:
                bit_depth_score = 2.0
            elif file_obj.bit_depth == 16:
                bit_depth_score = 1.0

        sample_rate_score = 0.0
        if is_lossless and file_obj.sample_rate_hz:
            if file_obj.sample_rate_hz >= 96000:
                sample_rate_score = 2.0
            elif file_obj.sample_rate_hz >= 44100:
                sample_rate_score = 1.0

        quality_score = format_score + bit_depth_score + 0.5 * sample_rate_score

        queue_length = file_obj.queue_length if file_obj.queue_length is not None else 0
        queue_score = 0.0
        if file_obj.has_free_upload_slot:
            queue_score += 0.55
        if queue_length == 0:
            queue_score += 0.45
        elif queue_length <= 2:
            queue_score += 0.35
        elif queue_length <= 10:
            queue_score += 0.25
        elif queue_length <= 25:
            queue_score += 0.15
        elif queue_length <= 100:
            queue_score += 0.05
        queue_score = min(queue_score, 1.0)

        upload_speed_bps = max(file_obj.upload_speed_bps or 0.0, 0.0)
        speed_score = min(upload_speed_bps / (4 * 1024 * 1024), 1.0)

        # Final composite: keep similarity dominant, but reward candidates
        # that can actually start and sustain faster transfers.
        final_score = (
            text_score * 0.57
            + (quality_score / 7.0) * 0.18
            + queue_score * 0.20
            + speed_score * 0.05
        )

        duration_ms = (
            int(file_obj.duration_s * 1000) if file_obj.duration_s is not None else None
        )

        result = SearchResult(
            source=self.name,
            title=basename,
            artists=track.artists,
            album=track.album,
            duration_ms=duration_ms or track.duration_ms,
            audio_format=audio_format,
            quality_kbps=file_obj.bitrate_kbps,
            is_lossless=is_lossless,
            download_url=None,
            stream_id=f"{file_obj.username}|{file_obj.filename}|{file_obj.size}",
            similarity_score=final_score,
            isrc_match=False,
            bit_depth=file_obj.bit_depth if is_lossless else None,
            sample_rate_hz=file_obj.sample_rate_hz if is_lossless else None,
        )
        return final_score, result
