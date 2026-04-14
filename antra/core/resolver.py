"""
Multi-source resolver. Tries adapters in priority order (waterfall).
The first adapter that returns a result above the acceptance threshold wins.
Only falls through to lower-priority adapters if the current one finds nothing.
"""
import logging
from typing import Optional

from antra.core.models import TrackMetadata, SearchResult
from antra.sources.base import BaseSourceAdapter

logger = logging.getLogger(__name__)

# Minimum similarity score to accept a result and stop searching further.
# If a source returns a result below this, we try the next adapter.
ACCEPT_THRESHOLD = 0.70
LOSSLESS_ACCEPT_THRESHOLD = 0.55
LOSSY_ACCEPT_THRESHOLD = 0.65
YOUTUBE_ACCEPT_THRESHOLD = 0.80


class SourceResolver:
    """
    Waterfall resolver: iterates adapters in priority order and returns
    the first result that meets the acceptance threshold.

    Priority order (lower number = tried first):
      0  — HiFi      (no credentials, hifi-api pool: Monochrome/squid.wtf/QQDL)
      2  — Debrid    (TorBox-backed cached torrents — prefers native lossless)
      3  — DAB       (no credentials, dab.yeet.su — DABmusic/BeatBoss backend)
      10 — Qobuz     (if configured, FLAC lossless)
      20 — Tidal     (if configured, FLAC lossless)
      25 — JioSaavn  (no credentials, AAC 320kbps — tried after lossless sources)
      30 — YouTube   (always available, last resort)

    Enrichment utilities (not source adapters, call these before resolving):
      OdesliEnricher  — resolves Spotify/ISRC → Tidal/Qobuz platform IDs
      LrclibEnricher  — fetches synced (LRC) and plain lyrics post-download
    """

    def __init__(
        self,
        adapters: list[BaseSourceAdapter],
        preferred_output_format: str = "source",
        preserve_input_order: bool = False,
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
        names = [f"{a.name}(p={a.priority})" for a in self.adapters]
        logger.debug(f"Source resolver initialized with adapters: {names}")

    def _is_quality_aware_mode(self) -> bool:
        return self.preferred_output_format in {"source", "flac", "lossless"}

    def _is_lossless_only_mode(self) -> bool:
        return self.preferred_output_format in {"flac", "lossless"}

    def _quality_tier(self, result: SearchResult) -> int:
        if self.preferred_output_format in {"source", "flac"}:
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

    def _candidate_key(
        self,
        result: SearchResult,
        adapter: BaseSourceAdapter,
    ) -> tuple[float, int, float, int]:
        similarity = result.similarity_score
        if self._is_quality_aware_mode() and adapter.name == "youtube":
            similarity -= 0.10
        return (
            float(self._quality_tier(result)),
            1 if result.isrc_match else 0,
            similarity,
            -adapter.priority,
        )

    def _meets_quality_aware_threshold(
        self,
        result: SearchResult,
        adapter: BaseSourceAdapter,
    ) -> bool:
        if result.isrc_match:
            return True
        if result.is_lossless:
            return result.similarity_score >= LOSSLESS_ACCEPT_THRESHOLD
        if self._is_lossless_only_mode():
            return False
        if adapter.name == "youtube":
            return result.similarity_score >= YOUTUBE_ACCEPT_THRESHOLD
        return result.similarity_score >= LOSSY_ACCEPT_THRESHOLD

    def _accepts_result_immediately(
        self,
        result: SearchResult,
        adapter: BaseSourceAdapter,
    ) -> bool:
        if result.isrc_match:
            return True
        if self._is_quality_aware_mode():
            return self._meets_quality_aware_threshold(result, adapter)
        return result.similarity_score >= ACCEPT_THRESHOLD

    def resolve(
        self,
        track: TrackMetadata,
        excluded_adapters: Optional[set[str]] = None,
    ) -> Optional[tuple[SearchResult, BaseSourceAdapter]]:
        """
        Waterfall search: try each adapter in priority order.
        - Returns immediately on an ISRC match (guaranteed correct track).
        - Returns the first result that meets ACCEPT_THRESHOLD.
        - If nothing clears the threshold, returns the best result found anyway
          so the download still proceeds.
        """
        excluded = excluded_adapters or set()
        best_result: Optional[SearchResult] = None
        best_adapter: Optional[BaseSourceAdapter] = None
        candidates: list[tuple[SearchResult, BaseSourceAdapter]] = []

        for adapter in self.adapters:
            if adapter.name in excluded:
                if self.preserve_input_order:
                    logger.info(f"[Resolver] Skipping excluded adapter: {adapter.name}")
                else:
                    logger.debug(f"[Resolver] Skipping excluded adapter: {adapter.name}")
                continue

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
                    # Fall through — let HiFi run at reduced speed

            if self._is_lossless_only_mode() and getattr(adapter, "always_lossy", False):
                logger.debug(f"[Resolver] Skipping {adapter.name} — lossy source in lossless-only mode")
                continue

            if self.preserve_input_order:
                logger.info(f"[Resolver] Trying {adapter.name} for: {track.title}")
            try:
                result = adapter.search(track)
            except Exception as e:
                logger.warning(f"[Resolver] {adapter.name} search error: {e}")
                continue

            if result is None:
                if self.preserve_input_order:
                    logger.info(f"[Resolver] {adapter.name} returned no result for: {track.title}")
                else:
                    logger.debug(f"[Resolver] {adapter.name} returned no result for: {track.title}")
                continue


            logger.debug(
                f"[Resolver] {adapter.name} → '{result.title}' "
                f"score={result.similarity_score:.2f} ({result.quality_label})"
            )

            candidates.append((result, adapter))

            # Keep track of the best result so far (across all adapters)
            if best_result is None or self._candidate_key(result, adapter) > self._candidate_key(best_result, best_adapter):
                best_result = result
                best_adapter = adapter

            # Fast path: 24-bit lossless found — no need to query remaining adapters.
            if (
                self._is_quality_aware_mode()
                and self._quality_tier(result) == 4
                and self._meets_quality_aware_threshold(result, adapter)
            ):
                logger.info(
                    f"[Resolver] 24-bit lossless match via {adapter.name}: '{result.title}' "
                    f"({result.quality_label}) — skipping remaining adapters"
                )
                return result, adapter

            if self.preserve_input_order and self._accepts_result_immediately(result, adapter):
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

            # In quality-aware mode: stop as soon as we have a good lossless
            # result — no point querying slower adapters (e.g. Soulseek) for
            # something we already have at CD quality or better.
            if self._is_quality_aware_mode():
                if (
                    self._quality_tier(result) >= 2  # lossless (16-bit or better)
                    and self._meets_quality_aware_threshold(result, adapter)
                ):
                    logger.info(
                        f"[Resolver] Lossless match via {adapter.name}: '{result.title}' "
                        f"({result.quality_label}) — skipping remaining adapters"
                    )
                    return result, adapter
                continue

            # If this adapter's result clears the threshold, use it — don't
            # bother trying lower-priority (usually lower-quality) adapters.
            if self._accepts_result_immediately(result, adapter):
                logger.debug(
                    f"[Resolver] Accepted via {adapter.name}: '{result.title}' "
                    f"score={result.similarity_score:.2f} ({result.quality_label})"
                )
                return result, adapter

            logger.debug(
                f"[Resolver] {adapter.name} score {result.similarity_score:.2f} < "
                f"threshold {ACCEPT_THRESHOLD}, trying next adapter..."
            )

        if self._is_quality_aware_mode() and candidates:
            acceptable = [
                (result, adapter)
                for result, adapter in candidates
                if self._meets_quality_aware_threshold(result, adapter)
            ]
            if acceptable:
                accepted_result, accepted_adapter = max(
                    acceptable,
                    key=lambda item: self._candidate_key(item[0], item[1]),
                )
                logger.debug(
                    f"[Resolver] Accepted via {accepted_adapter.name}: '{accepted_result.title}' "
                    f"score={accepted_result.similarity_score:.2f} ({accepted_result.quality_label})"
                )
                return accepted_result, accepted_adapter

        # In lossless-prefer mode: if no lossless candidate was acceptable but a
        # lossy source did find the track (e.g. region-exclusive Chinese content
        # only available on NetEase), use the best lossy match as a last resort
        # rather than hard-failing. Log a clear warning so the user can see it.
        if self._is_lossless_only_mode():
            if best_result and best_adapter:
                logger.info(
                    f"[Resolver] No lossless source found — using best available "
                    f"({best_adapter.name}, {best_result.quality_label}) for: "
                    f"{track.title}"
                )
                return best_result, best_adapter
            logger.warning(f"[Resolver] No source found for: {track.title} — {track.artist_string}")
            return None

        # Nothing cleared the threshold — fall back to the best we found
        if best_result and best_adapter:
            logger.info(
                f"[Resolver] No adapter cleared threshold; using best match via "
                f"{best_adapter.name}: '{best_result.title}' "
                f"score={best_result.similarity_score:.2f} ({best_result.quality_label})"
            )
            return best_result, best_adapter

        logger.warning(f"[Resolver] No source found for: {track.title} — {track.artist_string}")
        return None
