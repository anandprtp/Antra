"""
Multi-source resolver. Tries adapters in priority order (waterfall).
The first adapter that returns a result above the acceptance threshold wins.
Only falls through to lower-priority adapters if the current one finds nothing.

Within each priority tier, adapters are shuffled on every resolve() call so the
load is distributed evenly across same-priority sources (e.g. Amazon, HiFi, DAB
all share priority 2 in the free-lossless tier). Rate-limited adapters are moved
to the back of their tier rather than skipped entirely, so they remain available
as a last resort within the tier if others also fail.
"""
import logging
import random
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
      1  — Amazon    (no credentials, community proxy — free lossless tier)
      2  — HiFi      (no credentials, hifi-api pool: Monochrome/squid.wtf/QQDL)
      2  — DAB       (no credentials, dab.yeet.su — DABmusic/BeatBoss backend)
      3  — Soulseek  (P2P via slskd — optional)
      10 — Qobuz     (if configured, FLAC lossless)
      20 — Tidal     (if configured, FLAC lossless)
      25 — JioSaavn  (no credentials, AAC 320kbps — tried after lossless sources)
      30 — YouTube   (always available, last resort)

    Within each priority tier, adapters are shuffled on every resolve() call to
    distribute load evenly.  Rate-limited adapters are moved to the back of their
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

    def _build_resolve_order(self, excluded: set[str]) -> list[BaseSourceAdapter]:
        """Build a per-call adapter list for resolve().

        Rules:
        - Adapters in `excluded` are omitted entirely.
        - When preserve_input_order is True, the original order is kept (specific
          source-preference modes rely on this).
        - Otherwise, adapters are grouped by priority.  Non-rate-limited adapters
          come first (sorted by priority, shuffled within each tier for even load
          distribution).  Rate-limited adapters are moved to the END of the entire
          list — not just the back of their tier — so a single cooling adapter
          (e.g. Amazon alone at priority 1) does not delay every resolve() call
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
            # adapters are still shuffled for even load distribution.
            lossy_normal: list[BaseSourceAdapter] = []
            lossless_normal: list[BaseSourceAdapter] = []
            for priority in sorted(by_priority.keys()):
                group = by_priority[priority]
                normal = [a for a in group if not self._is_rate_limited(a.name)]
                cooling = [a for a in group if self._is_rate_limited(a.name)]
                random.shuffle(normal)
                random.shuffle(cooling)
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
                normal = [a for a in group if not self._is_rate_limited(a.name)]
                cooling = [a for a in group if self._is_rate_limited(a.name)]
                random.shuffle(normal)
                random.shuffle(cooling)
                normal_ordered.extend(normal)
                cooling_ordered.extend(cooling)

        return normal_ordered + cooling_ordered

    def _is_quality_aware_mode(self) -> bool:
        return self.preferred_output_format in {"source", "flac", "lossless"}

    def _is_lossless_only_mode(self) -> bool:
        return self.preferred_output_format in {"flac", "lossless"}

    def _is_lossy_preferred_mode(self) -> bool:
        return self.preferred_output_format in {"mp3", "aac", "m4a"}

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
    ) -> tuple[float, int, float, int]:
        similarity = result.similarity_score
        if self._is_quality_aware_mode() and adapter.name == "youtube":
            similarity -= 0.10
        if track is not None:
            similarity += self._explicit_penalty(track, result)
            # Small bonus for 24-bit results when we know the track is hi-res.
            # Biases final candidate selection toward hi-res when multiple adapters
            # return results — e.g. prefer a 24-bit Amazon result over a 16-bit HiFi
            # result with an identical similarity score.
            if self._track_wants_hires(track) and (result.bit_depth or 0) >= 24:
                similarity += 0.05
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

        # Build a per-call ordered list with same-priority adapters shuffled for
        # even load distribution.  Rate-limited adapters land at the back of their
        # tier but are not dropped, so they still serve as a last resort.
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
                    # Fall through — let HiFi run at reduced speed

            if self._is_lossless_only_mode() and getattr(adapter, "always_lossy", False):
                logger.debug(f"[Resolver] Skipping {adapter.name} — lossy source in lossless-only mode")
                continue

            if self.preserve_input_order:
                logger.info(f"[Resolver] Trying {adapter.name} for: {track.title}")
            try:
                result = adapter.search(track)
            except RateLimitedError:
                # Search itself was rate-limited — mark cooldown and move on.
                # The adapter stays at the back of its tier for future resolve() calls.
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

            # Keep track of the best result so far (across all adapters)
            if best_result is None or self._candidate_key(result, adapter, track) > self._candidate_key(best_result, best_adapter, track):
                best_result = result
                best_adapter = adapter

            # Fast path: 24-bit lossless found — no need to query remaining adapters.
            # Skip fast path if this looks like a clean/radio-edit and we prefer explicit.
            if (
                self._is_quality_aware_mode()
                and self._quality_tier(result) == 4
                and self._meets_quality_aware_threshold(result, adapter)
                and not self._result_looks_clean(track, result)
            ):
                logger.info(
                    f"[Resolver] 24-bit lossless match via {adapter.name}: '{result.title}' "
                    f"({result.quality_label}) — skipping remaining adapters"
                )
                return result, adapter

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

            # In quality-aware mode: stop as soon as we have a good lossless
            # result — no point querying slower adapters (e.g. Soulseek) for
            # something we already have at CD quality or better.
            # But keep searching if:
            #   (a) the result looks like a clean/radio-edit version, or
            #   (b) the track is known to be hi-res (audioTraits: hi-res-lossless)
            #       and the current result is only 16-bit — keep looking for 24-bit.
            if self._is_quality_aware_mode():
                if (
                    self._quality_tier(result) >= 2  # lossless (16-bit or better)
                    and self._meets_quality_aware_threshold(result, adapter)
                    and not self._result_looks_clean(track, result)
                ):
                    hires_wanted = self._track_wants_hires(track)
                    is_hires_result = (result.bit_depth or 0) >= 24
                    if hires_wanted and not is_hires_result:
                        logger.info(
                            f"[Resolver] 16-bit lossless via {adapter.name} but track is "
                            f"hi-res — continuing search for 24-bit source"
                        )
                        # Don't return yet; keep searching, but record as best so far
                        continue
                    logger.info(
                        f"[Resolver] Lossless match via {adapter.name}: '{result.title}' "
                        f"({result.quality_label}) — skipping remaining adapters"
                    )
                    return result, adapter
                continue

            # If this adapter's result clears the threshold, use it — don't
            # bother trying lower-priority (usually lower-quality) adapters.
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

        if self._is_quality_aware_mode() and candidates:
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

        # In lossless-only mode: never fall back to a lossy result.
        # If the best we found is lossless but scored below the threshold
        # (low confidence), return it anyway — wrong track is unlikely but
        # at least the format is correct. If it's lossy, hard-fail so the
        # engine marks the track as failed instead of writing an MP3.
        if self._is_lossless_only_mode():
            if best_result and best_adapter:
                if best_result.is_lossless:
                    logger.info(
                        f"[Resolver] Low-confidence lossless match via {best_adapter.name}: "
                        f"'{best_result.title}' ({best_result.quality_label}) — "
                        f"score={best_result.similarity_score:.2f}"
                    )
                    return best_result, best_adapter
                logger.warning(
                    f"[Resolver] No lossless source found for: {track.title} — "
                    f"{track.artist_string} (lossless-only mode; skipping lossy fallback)"
                )
                return None
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
