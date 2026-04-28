"""
Multi-source resolver. Tries adapters in priority order (waterfall).
The first adapter that returns a result above the acceptance threshold wins.
Only falls through to lower-priority adapters if the current one finds nothing.

Within each priority tier, adapters are rotated on every resolve() call so the
load is distributed evenly across same-priority sources (e.g. Amazon, Apple,
HiFi, and DAB all share priority 2 in the free-lossless tier). Rate-limited
adapters are moved to the back of their tier rather than skipped entirely, so
they remain available as a last resort within the tier if others also fail.
"""
import logging
import re
import threading
import time
from collections import defaultdict
from typing import Optional

from antra.core.models import TrackMetadata, SearchResult
from antra.sources.base import BaseSourceAdapter, RateLimitedError

logger = logging.getLogger(__name__)

# Minimum similarity score to accept a result and stop searching further.
# If a source returns a result below this, we try the next adapter.
ACCEPT_THRESHOLD = 0.70
LOSSLESS_ACCEPT_THRESHOLD = 0.55
LOSSY_ACCEPT_THRESHOLD = 0.65
YOUTUBE_ACCEPT_THRESHOLD = 0.80


# Patterns that identify radio edits, clean versions, or otherwise censored variants.
# Matched against SearchResult titles to detect clean versions when the adapter
# does not expose an explicit flag directly.
_CLEAN_VERSION_RE = re.compile(
    r"\b(radio\s*edit|clean(\s+(version|edit|mix))?|edited(\s+version)?|censored)\b",
    re.IGNORECASE,
)


class SourceResolver:
    """
    Waterfall resolver: iterates adapters in priority order and returns
    the first result that meets the acceptance threshold.

    Priority order (lower number = tried first):
      2  — Amazon    (community proxy — free lossless tier)
      2  — Apple     (community proxy — ALAC hi-res tier)
      2  — HiFi      (Tidal-backed hifi-api pool)
      2  — DAB       (community Qobuz proxy backend)
      3  — Soulseek  (P2P via slskd — optional)
      10 — Qobuz     (if configured, FLAC lossless)
      20 — Tidal     (direct account adapter, if configured)
      25 — JioSaavn  (no credentials, AAC 320kbps — tried after lossless sources)
      30 — YouTube   (always available, last resort)

    Within each priority tier, adapters are rotated on every resolve() call to
    distribute load evenly. Rate-limited adapters are moved to the back of their
    tier (not dropped) for RATE_LIMIT_COOLDOWN_SECONDS so they remain available
    if the other adapters in that tier also fail.

    Enrichment utilities (not source adapters, call these before resolving):
      OdesliEnricher  — resolves Spotify/ISRC → Tidal/Qobuz platform IDs
      LrclibEnricher  — fetches synced (LRC) and plain lyrics post-download
    """

    # How long (seconds) a rate-limited adapter is moved to the back of its tier.
    RATE_LIMIT_COOLDOWN_SECONDS = 30

    def __init__(
        self,
        adapters: list[BaseSourceAdapter],
        preferred_output_format: str = "source",
        preserve_input_order: bool = False,
        prefer_explicit: bool = True,
    ):
        available_adapters = [a for a in adapters if a.is_available()]
        if preserve_input_order:
            self.adapters = available_adapters
        else:
            self.adapters = sorted(
                available_adapters,
                key=lambda a: a.priority,
            )
        self.preferred_output_format = preferred_output_format
        self.preserve_input_order = preserve_input_order
        self.prefer_explicit = prefer_explicit
        # adapter_name → epoch time until which the adapter is rate-limited
        self._rate_limited_until: dict[str, float] = {}
        self._rate_limit_lock = threading.Lock()
        self._tier_rotation: dict[tuple[int, str], int] = {}
        self._tier_rotation_lock = threading.Lock()
        names = [f"{a.name}(p={a.priority})" for a in self.adapters]
        logger.debug(f"Source resolver initialized with adapters: {names}")

    def _mark_rate_limited(self, adapter_name: str, cooldown_seconds: Optional[int] = None) -> None:
        """Record that an adapter is unreliable; deprioritize it globally for cooldown_seconds.

        Called for both API-level rate limits (search) and download-level failures
        (truncated downloads).  Truncated downloads use a longer cooldown so that
        parallel workers stop queuing on the broken adapter immediately.
        """
        seconds = cooldown_seconds if cooldown_seconds is not None else self.RATE_LIMIT_COOLDOWN_SECONDS
        until = time.time() + seconds
        with self._rate_limit_lock:
            self._rate_limited_until[adapter_name] = until
        logger.info(
            f"[Resolver] {adapter_name} deprioritized globally for "
            f"{seconds}s (until {time.strftime('%H:%M:%S', time.localtime(until))})"
        )

    def _is_rate_limited(self, adapter_name: str) -> bool:
        """Return True if the adapter is still in its rate-limit cooldown window."""
        with self._rate_limit_lock:
            until = self._rate_limited_until.get(adapter_name, 0.0)
        return time.time() < until

    def _rotate_tier(
        self,
        priority: int,
        bucket: str,
        adapters: list[BaseSourceAdapter],
    ) -> list[BaseSourceAdapter]:
        if len(adapters) <= 1:
            return adapters
        ordered = sorted(adapters, key=lambda adapter: adapter.name)
        key = (priority, bucket)
        with self._tier_rotation_lock:
            offset = self._tier_rotation.get(key, 0) % len(ordered)
            self._tier_rotation[key] = (offset + 1) % len(ordered)
        return ordered[offset:] + ordered[:offset]

    def _build_resolve_order(self, excluded: set[str]) -> list[BaseSourceAdapter]:
        """Build a per-call adapter list for resolve().

        Rules:
        - Adapters in `excluded` are omitted entirely.
        - When preserve_input_order is True, the original order is kept (specific
          source-preference modes rely on this).
        - Otherwise, adapters are grouped by priority.  Non-rate-limited adapters
          come first (sorted by priority, rotated within each tier for even load
          distribution).  Rate-limited adapters are moved to the END of the entire
          list — not just the back of their tier — so a single cooling adapter
          does not delay every resolve() call
          while higher-numbered-priority working adapters (HiFi, DAB) are available.
          Rate-limited adapters remain available as a global last resort.
        - In lossy-preferred mode (MP3 output): always_lossy adapters (JioSaavn,
          NetEase) come before lossless adapters within normal_ordered.  This avoids
          downloading FLAC from Amazon/HiFi just to transcode it to MP3.  Lossless
          adapters remain available as a fallback if all lossy sources fail.
        """
        if self.preserve_input_order:
            return [a for a in self.adapters if a.name not in excluded]

        by_priority: dict[int, list[BaseSourceAdapter]] = defaultdict(list)
        for adapter in self.adapters:
            if adapter.name not in excluded:
                by_priority[adapter.priority].append(adapter)

        lossy_preferred = self._is_lossy_preferred_mode()
        normal_ordered: list[BaseSourceAdapter] = []
        cooling_ordered: list[BaseSourceAdapter] = []

        if lossy_preferred:
            # MP3 mode: split working adapters into lossy-first then lossless-fallback.
            # Each sub-list preserves priority ordering; within each priority tier,
            # adapters are still rotated for even load distribution.
            lossy_normal: list[BaseSourceAdapter] = []
            lossless_normal: list[BaseSourceAdapter] = []
            for priority in sorted(by_priority.keys()):
                group = by_priority[priority]
                # Adapters with premium endpoints are never cooling — community mirror
                # rate-limiting must not block the premium server from being tried.
                normal = [
                    a for a in group
                    if not self._is_rate_limited(a.name) or getattr(a, "_premium_endpoints", None)
                ]
                cooling = [
                    a for a in group
                    if self._is_rate_limited(a.name) and not getattr(a, "_premium_endpoints", None)
                ]
                normal = self._rotate_tier(priority, "lossy-normal", normal)
                cooling = self._rotate_tier(priority, "lossy-cooling", cooling)
                for a in normal:
                    if getattr(a, "always_lossy", False):
                        lossy_normal.append(a)
                    else:
                        lossless_normal.append(a)
                cooling_ordered.extend(cooling)
            normal_ordered = lossy_normal + lossless_normal
        else:
            for priority in sorted(by_priority.keys()):
                group = by_priority[priority]
                # Adapters with premium endpoints are never cooling — community mirror
                # rate-limiting must not block the premium server from being tried.
                normal = [
                    a for a in group
                    if not self._is_rate_limited(a.name) or getattr(a, "_premium_endpoints", None)
                ]
                cooling = [
                    a for a in group
                    if self._is_rate_limited(a.name) and not getattr(a, "_premium_endpoints", None)
                ]
                normal = self._rotate_tier(priority, "normal", normal)
                cooling = self._rotate_tier(priority, "cooling", cooling)
                normal_ordered.extend(normal)
                cooling_ordered.extend(cooling)

        return normal_ordered + cooling_ordered

    def _is_quality_aware_mode(self) -> bool:
        return self.preferred_output_format in {"source", "flac", "lossless", "alac"}

    def _is_lossless_only_mode(self) -> bool:
        return self.preferred_output_format in {"flac", "lossless", "alac"}

    def _is_lossy_preferred_mode(self) -> bool:
        return self.preferred_output_format in {"mp3", "aac", "m4a"}

    def _quality_tier(self, result: SearchResult) -> int:
        if self.preferred_output_format in {"source", "flac", "lossless"}:
            if result.is_lossless and (result.bit_depth or 0) >= 24 and (result.sample_rate_hz or 0) >= 96000:
                return 5
            if result.is_lossless and (result.bit_depth or 0) >= 24:
                return 4
            if result.is_lossless and result.bit_depth == 16:
                return 3
            if result.is_lossless:
                return 2
            if self._is_lossless_only_mode():
                return 0
            return 1
        return 0

    def _lossless_sort_key(
        self,
        result: SearchResult,
        adapter: BaseSourceAdapter,
        track: Optional[TrackMetadata] = None,
    ) -> tuple:
        """
        Sort key for comparing lossless results across adapters.
        Priority (highest first):
          1. bit_depth (24 > 16 > None)
          2. sample_rate_hz (96000 > 48000 > 44100 > None)
          3. quality_kbps (higher is better)
          4. isrc_match (exact match preferred)
          5. similarity_score (adjusted for explicit preference)
          6. adapter priority (lower number = higher priority, so negate)
        """
        similarity = result.similarity_score
        if track is not None:
            similarity += self._explicit_penalty(track, result)
        return (
            result.bit_depth or 0,
            result.sample_rate_hz or 0,
            result.quality_kbps or 0,
            1 if result.isrc_match else 0,
            similarity,
            -adapter.priority,
        )

    def _explicit_penalty(
        self,
        track: TrackMetadata,
        result: SearchResult,
    ) -> float:
        """Return a similarity score penalty when prefer_explicit is on, the target
        track is known to be explicit, and the result looks like a clean/radio-edit
        version.

        Penalty tiers:
          -0.20  adapter confirmed non-explicit (result.is_explicit = False)
          -0.20  result title contains "radio edit", "clean version", etc. but
                 the original track title does NOT (so the tag is not intentional)
           0.00  everything else (unknown explicit status → neutral)
        """
        if not self.prefer_explicit or not track.is_explicit:
            return 0.0

        if result.is_explicit is False:
            return -0.20

        if _CLEAN_VERSION_RE.search(result.title) and not _CLEAN_VERSION_RE.search(track.title):
            return -0.20

        return 0.0

    def _track_wants_hires(self, track: TrackMetadata) -> bool:
        """Return True if Apple Music reports the track is available in hi-res lossless.
        Used to keep searching past a 16-bit lossless result in quality-aware mode."""
        return "hi-res-lossless" in (track.audio_traits or [])

    def _candidate_key(
        self,
        result: SearchResult,
        adapter: BaseSourceAdapter,
        track: Optional[TrackMetadata] = None,
    ) -> tuple[float, int, int, int, float, int]:
        similarity = result.similarity_score
        if self._is_quality_aware_mode() and adapter.name == "youtube":
            similarity -= 0.10
        if track is not None:
            similarity += self._explicit_penalty(track, result)
            # Small bonus for hi-res results when we know the track is hi-res.
            # Biases final candidate selection toward hi-res when multiple adapters
            # return results — e.g. prefer a 24-bit Amazon result over a 16-bit HiFi
            # result with an identical similarity score.
            if self._track_wants_hires(track) and self._quality_tier(result) >= 4:
                similarity += 0.05
        return (
            float(self._quality_tier(result)),
            result.sample_rate_hz or 0,
            result.quality_kbps or 0,
            1 if result.isrc_match else 0,
            similarity,
            -adapter.priority,
        )

    def _meets_quality_aware_threshold(
        self,
        result: SearchResult,
        adapter: BaseSourceAdapter,
    ) -> bool:
        # In lossless-only mode, an ISRC match on a lossy result is still rejected —
        # we know it's the right track but we refuse to accept a lossy copy.
        if result.isrc_match and not (self._is_lossless_only_mode() and not result.is_lossless):
            return True
        if result.is_lossless:
            return result.similarity_score >= LOSSLESS_ACCEPT_THRESHOLD
        if self._is_lossless_only_mode():
            return False
        if adapter.name == "youtube":
            return result.similarity_score >= YOUTUBE_ACCEPT_THRESHOLD
        return result.similarity_score >= LOSSY_ACCEPT_THRESHOLD

    def _result_looks_clean(self, track: TrackMetadata, result: SearchResult) -> bool:
        """Return True if the result appears to be a clean/edited version while
        the target track is known to be explicit."""
        if not self.prefer_explicit or not track.is_explicit:
            return False
        if result.is_explicit is False:
            return True
        if _CLEAN_VERSION_RE.search(result.title) and not _CLEAN_VERSION_RE.search(track.title):
            return True
        return False

    def _accepts_result_immediately(
        self,
        result: SearchResult,
        adapter: BaseSourceAdapter,
        track: Optional[TrackMetadata] = None,
    ) -> bool:
        # Never immediately accept a confirmed clean/radio-edit result when we
        # know the target is explicit — keep searching for the explicit version.
        if track is not None and self._result_looks_clean(track, result):
            return False

        # In lossy-preferred mode, use a lower threshold for always_lossy adapters
        # (JioSaavn, NetEase). Their metadata is noisier so scores often land at
        # 0.55–0.65. Without this, the resolver falls through to Amazon/HiFi which
        # return FLAC at 0.85+ — then that FLAC gets transcoded to MP3/AAC anyway.
        if self._is_lossy_preferred_mode() and getattr(adapter, "always_lossy", False):
            if result.isrc_match:
                return True
            return result.similarity_score >= LOSSY_ACCEPT_THRESHOLD

        if self._is_quality_aware_mode():
            meets_threshold = self._meets_quality_aware_threshold(result, adapter)
            if not meets_threshold:
                return False
            # Only accept immediately if it is hi-res lossless, otherwise keep
            # searching to see if a higher priority or other adapter has hi-res.
            is_hires = self._quality_tier(result) >= 4
            if not is_hires:
                return False
            return True
            
        if result.isrc_match:
            return True
        return result.similarity_score >= ACCEPT_THRESHOLD

    def resolve(
        self,
        track: TrackMetadata,
        excluded_adapters: Optional[set[str]] = None,
    ) -> Optional[tuple[SearchResult, BaseSourceAdapter]]:
        """
        Waterfall search: try each adapter in priority order.

        In quality-aware (lossless) mode:
          - Queries ALL lossless-capable adapters (never stops early on a lossless hit).
          - After all adapters have been queried, picks the result with the highest
            bit_depth → sample_rate_hz → similarity_score.
          - In lossless-only mode (flac/lossless/alac), fails cleanly if no lossless
            result was found — never falls back to a lossy source.

        In lossy-preferred mode (mp3/aac):
          - Returns immediately on the first always_lossy adapter result that clears
            LOSSY_ACCEPT_THRESHOLD, to avoid downloading FLAC just to transcode it.

        In default (source) mode:
          - Returns the first result that meets ACCEPT_THRESHOLD.
        """
        excluded = excluded_adapters or set()
        best_result: Optional[SearchResult] = None
        best_adapter: Optional[BaseSourceAdapter] = None
        candidates: list[tuple[SearchResult, BaseSourceAdapter]] = []

        # Lossless candidates collected across all adapters — used in quality-aware mode
        # to pick the highest bit_depth/sample_rate result after querying everyone.
        lossless_candidates: list[tuple[SearchResult, BaseSourceAdapter]] = []

        resolve_order = self._build_resolve_order(excluded)

        for adapter in resolve_order:
            # HiFi throttle check — only defer to Amazon if Amazon hasn't already
            # been tried and failed. If Amazon is excluded (failed), we come back
            # to HiFi even when throttled — it's still lossless and better than
            # falling to DAB or JioSaavn.
            if adapter.name == "hifi" and hasattr(adapter, "is_throttled") and adapter.is_throttled():
                amazon_already_failed = "amazon" in excluded
                if not amazon_already_failed:
                    logger.info(
                        "[Resolver] HiFi is throttled — deferring to Amazon first. "
                        "HiFi will be retried if Amazon also fails."
                    )
                    continue
                else:
                    logger.info(
                        "[Resolver] HiFi is throttled but Amazon already failed — "
                        "trying HiFi anyway (preferred lossless fallback)."
                    )

            # In lossless-only mode, skip adapters that only serve lossy audio
            # (Apple, JioSaavn, NetEase, YouTube).
            if self._is_lossless_only_mode() and getattr(adapter, "always_lossy", False):
                logger.debug(f"[Resolver] Skipping {adapter.name} — lossy-only source in lossless mode")
                continue

            if self.preserve_input_order:
                logger.info(f"[Resolver] Trying {adapter.name} for: {track.title}")
            try:
                result = adapter.search(track)
            except RateLimitedError:
                self._mark_rate_limited(adapter.name)
                continue
            except Exception as e:
                logger.warning(f"[Resolver] {adapter.name} search error: {e}")
                continue

            if result is None:
                logger.info(f"[Resolver] {adapter.name} — no match found for: {track.title}")
                continue

            logger.debug(
                f"[Resolver] {adapter.name} → '{result.title}' "
                f"score={result.similarity_score:.2f} ({result.quality_label})"
            )

            candidates.append((result, adapter))

            if best_result is None or self._candidate_key(result, adapter, track) > self._candidate_key(best_result, best_adapter, track):
                best_result = result
                best_adapter = adapter

            # ── Quality-aware (lossless) mode ──────────────────────────────────
            # Collect ALL lossless results from all adapters, then pick the best
            # by bit_depth → sample_rate_hz at the end. Never stop early.
            if self._is_quality_aware_mode():
                is_lossy_adapter = getattr(adapter, "always_lossy", False)

                if result.is_lossless and not is_lossy_adapter:
                    if self._meets_quality_aware_threshold(result, adapter) and not self._result_looks_clean(track, result):
                        lossless_candidates.append((result, adapter))
                        logger.info(
                            f"[Resolver] Lossless candidate via {adapter.name}: "
                            f"'{result.title}' {result.bit_depth or '?'}bit/"
                            f"{result.sample_rate_hz or '?'}Hz ({result.quality_label})"
                        )
                    # Keep going — don't return yet, query remaining adapters
                    continue

                # Lossy result in quality-aware mode — record as fallback but keep going
                continue

            # ── Lossy-preferred mode (mp3/aac) ────────────────────────────────
            if self._is_lossy_preferred_mode():
                if getattr(adapter, "always_lossy", False):
                    # This is a native lossy source — accept it if score is good enough
                    if result.isrc_match:
                        return result, adapter
                    if result.similarity_score >= LOSSY_ACCEPT_THRESHOLD:
                        return result, adapter
                # Lossless adapter in lossy mode — record as fallback but keep
                # trying lossy sources first. Don't accept it here.
                continue

            # ── preserve_input_order mode ─────────────────────────────────────
            if self.preserve_input_order and self._accepts_result_immediately(result, adapter, track):
                if result.isrc_match:
                    logger.info(
                        f"[Resolver] ISRC match via {adapter.name}: '{result.title}' "
                        f"({result.quality_label})"
                    )
                else:
                    logger.debug(
                        f"[Resolver] Accepted via {adapter.name}: '{result.title}' "
                        f"score={result.similarity_score:.2f} ({result.quality_label})"
                    )
                return result, adapter

            if self.preserve_input_order:
                logger.info(
                    f"[Resolver] {adapter.name} result below acceptance threshold for: "
                    f"{track.title} (score={result.similarity_score:.2f})"
                )
                continue

            # ── Default mode ──────────────────────────────────────────────────
            if self._accepts_result_immediately(result, adapter, track):
                logger.debug(
                    f"[Resolver] Accepted via {adapter.name}: '{result.title}' "
                    f"score={result.similarity_score:.2f} ({result.quality_label})"
                )
                return result, adapter

            logger.debug(
                f"[Resolver] {adapter.name} score {result.similarity_score:.2f} < "
                f"threshold {ACCEPT_THRESHOLD}, trying next adapter..."
            )

        # ── Post-loop: quality-aware mode picks best lossless result ──────────
        if self._is_quality_aware_mode():
            if lossless_candidates:
                best_lossless, best_lossless_adapter = max(
                    lossless_candidates,
                    key=lambda item: self._lossless_sort_key(item[0], item[1], track),
                )
                logger.info(
                    f"[Resolver] Best lossless source: {best_lossless_adapter.name} — "
                    f"'{best_lossless.title}' "
                    f"{best_lossless.bit_depth or '?'}bit/{best_lossless.sample_rate_hz or '?'}Hz "
                    f"({best_lossless.quality_label})"
                )
                return best_lossless, best_lossless_adapter

            # No lossless result found
            if self._is_lossless_only_mode():
                if best_result and best_adapter:
                    logger.warning(
                        f"[Resolver] No lossless source found for: {track.title} — "
                        f"{track.artist_string} (lossless-only mode; skipping lossy fallback)"
                    )
                else:
                    logger.warning(f"[Resolver] No source found for: {track.title} — {track.artist_string}")
                return None

            # quality-aware but not lossless-only (e.g. "source" mode) — fall back to best lossy
            if best_result and best_adapter:
                logger.info(
                    f"[Resolver] No lossless found; using best available via "
                    f"{best_adapter.name}: '{best_result.title}' "
                    f"score={best_result.similarity_score:.2f} ({best_result.quality_label})"
                )
                return best_result, best_adapter

        # ── Post-loop: lossy-preferred / default mode ─────────────────────────
        if candidates:
            # In lossy-preferred mode, prefer always_lossy results first,
            # then fall back to lossless (will be transcoded by the engine).
            if self._is_lossy_preferred_mode():
                lossy_candidates = [
                    (r, a) for r, a in candidates
                    if getattr(a, "always_lossy", False)
                    and self._meets_quality_aware_threshold(r, a)
                ]
                if lossy_candidates:
                    best_lossy, best_lossy_adapter = max(
                        lossy_candidates,
                        key=lambda item: self._candidate_key(item[0], item[1], track),
                    )
                    logger.info(
                        f"[Resolver] Lossy fallback via {best_lossy_adapter.name}: "
                        f"'{best_lossy.title}' score={best_lossy.similarity_score:.2f}"
                    )
                    return best_lossy, best_lossy_adapter
                # No lossy source found — fall back to lossless (engine will transcode)
                lossless_fallback = [
                    (r, a) for r, a in candidates
                    if not getattr(a, "always_lossy", False)
                    and self._meets_quality_aware_threshold(r, a)
                ]
                if lossless_fallback:
                    best_lf, best_lf_adapter = max(
                        lossless_fallback,
                        key=lambda item: self._candidate_key(item[0], item[1], track),
                    )
                    logger.info(
                        f"[Resolver] No native {self.preferred_output_format.upper()} source found — "
                        f"using {best_lf_adapter.name} (lossless, will transcode): '{best_lf.title}'"
                    )
                    return best_lf, best_lf_adapter

            acceptable = [
                (result, adapter)
                for result, adapter in candidates
                if self._meets_quality_aware_threshold(result, adapter)
            ]
            if acceptable:
                accepted_result, accepted_adapter = max(
                    acceptable,
                    key=lambda item: self._candidate_key(item[0], item[1], track),
                )
                logger.debug(
                    f"[Resolver] Accepted via {accepted_adapter.name}: '{accepted_result.title}' "
                    f"score={accepted_result.similarity_score:.2f} ({accepted_result.quality_label})"
                )
                return accepted_result, accepted_adapter

        if best_result and best_adapter:
            # In lossy-preferred mode: if the best result is a low-confidence lossy
            # source, prefer a lossless fallback (engine will transcode) over a
            # potentially wrong track from a noisy lossy source.
            if self._is_lossy_preferred_mode() and getattr(best_adapter, "always_lossy", False):
                lossless_fallback = [
                    (r, a) for r, a in candidates
                    if not getattr(a, "always_lossy", False)
                ]
                if lossless_fallback:
                    best_lf, best_lf_adapter = max(
                        lossless_fallback,
                        key=lambda item: self._candidate_key(item[0], item[1], track),
                    )
                    logger.info(
                        f"[Resolver] Low-confidence lossy result — "
                        f"using {best_lf_adapter.name} (lossless, will transcode): '{best_lf.title}'"
                    )
                    return best_lf, best_lf_adapter
            logger.info(
                f"[Resolver] No adapter cleared threshold; using best match via "
                f"{best_adapter.name}: '{best_result.title}' "
                f"score={best_result.similarity_score:.2f} ({best_result.quality_label})"
            )
            return best_result, best_adapter

        logger.warning(f"[Resolver] No source found for: {track.title} — {track.artist_string}")
        return None
