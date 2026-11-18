"""Verify that a downloaded file actually contains the requested recording.

Every existing post-download check reads metadata (ISRC, title, duration, size,
container). None of them listen to the audio, so a source that hands back a
completely different recording under the right tags passes all of them.

This module closes that gap without any account or API key:

  1. Fetch the free 30-second store preview for the requested track
     (Deezer by ISRC first, then a verified iTunes search).
  2. Fingerprint both the preview and the downloaded file with ``fpcalc``
     (Chromaprint), which describes the audio itself.
  3. Slide the preview across the download and keep the best bit-error rate.

Measured on 26 real downloads and 650 mismatched pairs (2026-09-23):

    same recording ................ 0.005 - 0.151
    same recording, other release . 0.067
    remix/instrumental off the
      same master ................. 0.055 - 0.197
    live version .................. 0.258 - 0.278
    cover by another artist ....... 0.291 - 0.359
    completely different song ..... 0.401 - 0.469   (650 pairs, minimum 0.401)

``DEFAULT_THRESHOLD`` sits at 0.35: far above every genuine pair, and below
every different-song pair, with margin on both sides. An official instrumental
or remix built on the same backing track is NOT detectable this way and is not
claimed to be -- it genuinely is the same recording.

Every failure path returns ``SKIPPED``. A track is only ever rejected on
positive evidence that the audio is a different recording; a missing preview,
a missing fpcalc, or any network problem leaves the download alone.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Optional, Sequence

import requests

from antra.utils.runtime import get_clean_subprocess_env, get_fpcalc_exe

logger = logging.getLogger(__name__)

# Verdicts
MATCH = "match"
MISMATCH = "mismatch"
SKIPPED = "skipped"

DEFAULT_THRESHOLD = 0.35

# fpcalc emits ~8.07 fingerprint items per second of audio.
_ITEMS_PER_SEC = 8.07
# Previews fade in/out; drop ~1.5s from each end before comparing.
_EDGE_ITEMS = 12
# Never judge on less than ~15s of overlapping audio.
_MIN_OVERLAP = 120
_PREVIEW_MAX_BYTES = 4 * 1024 * 1024
_HTTP_TIMEOUT = 12
_FPCALC_TIMEOUT = 120


@dataclass
class VerifyResult:
    verdict: str
    score: Optional[float] = None
    reason: str = ""
    preview_source: str = ""

    @property
    def is_mismatch(self) -> bool:
        return self.verdict == MISMATCH


# --------------------------------------------------------------------------
# fingerprinting
# --------------------------------------------------------------------------

def fingerprint(path: str, length: int = 0) -> Optional[list[int]]:
    """Raw Chromaprint fingerprint of ``path``, or None when unavailable.

    ``length`` is the number of seconds to fingerprint (0 = whole file).
    """
    exe = get_fpcalc_exe()
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "-raw", "-json", "-length", str(length), path],
            capture_output=True, text=True, timeout=_FPCALC_TIMEOUT,
            env=get_clean_subprocess_env(),
        )
    except Exception as exc:  # noqa: BLE001 - fingerprinting must never fail a download
        logger.debug("[Verify] fpcalc failed for %s: %s", os.path.basename(path), exc)
        return None
    if proc.returncode != 0:
        logger.debug("[Verify] fpcalc rc=%s: %s", proc.returncode, (proc.stderr or "")[:200])
        return None
    try:
        fp = json.loads(proc.stdout).get("fingerprint") or []
    except Exception:  # noqa: BLE001
        return None
    return [int(x) for x in fp] or None


def best_bit_error_rate(full: Sequence[int], preview: Sequence[int]) -> tuple[float, Optional[int]]:
    """Slide ``preview`` across ``full``; return the best (lowest) bit-error rate.

    0.0 means identical audio, ~0.5 means unrelated audio. The coarse pass finds
    candidate offsets cheaply; the refine pass scores those exactly, so the
    returned rate is never better than an exhaustive search would give.
    """
    prev = list(preview)
    if len(prev) > 3 * _EDGE_ITEMS:
        prev = prev[_EDGE_ITEMS:-_EDGE_ITEMS]
    full = list(full)
    n, m = len(full), len(prev)
    if n < _MIN_OVERLAP or m < _MIN_OVERLAP:
        return 1.0, None

    def rate(off: int, stride: int) -> float:
        a0, b0 = max(off, 0), max(-off, 0)
        overlap = min(n - a0, m - b0)
        if overlap < _MIN_OVERLAP:
            return 1.0
        bits = 0
        count = 0
        for k in range(0, overlap, stride):
            bits += (full[a0 + k] ^ prev[b0 + k]).bit_count()
            count += 1
        return bits / (32.0 * count)

    offsets = range(-(m - _MIN_OVERLAP), n - _MIN_OVERLAP + 1)
    step = 4
    coarse = sorted((rate(off, 4), off) for off in offsets[::step])[:3]
    best = (1.0, None)
    for _, off in coarse:
        for candidate in range(off - step, off + step + 1):
            score = rate(candidate, 1)
            if score < best[0]:
                best = (score, candidate)
    return best


# --------------------------------------------------------------------------
# preview lookup
# --------------------------------------------------------------------------

def _norm(value: Optional[str]) -> str:
    return "".join(c for c in (value or "").lower() if c.isalnum() or c == " ").strip()


def _download_preview(url: str, suffix: str) -> Optional[str]:
    try:
        resp = requests.get(url, timeout=_HTTP_TIMEOUT, stream=True)
        resp.raise_for_status()
        fd, path = tempfile.mkstemp(prefix="antra-preview-", suffix=suffix)
        size = 0
        with os.fdopen(fd, "wb") as fh:
            for chunk in resp.iter_content(64 * 1024):
                size += len(chunk)
                if size > _PREVIEW_MAX_BYTES:
                    break
                fh.write(chunk)
        return path
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Verify] preview download failed: %s", exc)
        return None


def _deezer_preview(isrc: str) -> tuple[Optional[str], str]:
    if not isrc:
        return None, ""
    try:
        data = requests.get(f"https://api.deezer.com/track/isrc:{isrc}",
                            timeout=_HTTP_TIMEOUT).json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Verify] deezer lookup failed: %s", exc)
        return None, ""
    url = data.get("preview") if isinstance(data, dict) else None
    if not url:
        return None, ""
    return _download_preview(url, ".mp3"), "deezer/isrc"


def _itunes_preview(title: str, artist: str, duration_s: Optional[float]) -> tuple[Optional[str], str]:
    """iTunes search, accepted only when title, artist AND duration all agree.

    The duration gate is what keeps a cover, live cut or edit from being used as
    the reference -- an unverified preview would fail a perfectly good download.
    """
    if not title:
        return None, ""
    try:
        results = requests.get(
            "https://itunes.apple.com/search",
            params={"term": f"{title} {artist or ''}".strip(), "entity": "song", "limit": 25},
            timeout=_HTTP_TIMEOUT,
        ).json().get("results", [])
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Verify] itunes lookup failed: %s", exc)
        return None, ""
    want_title, want_artist = _norm(title), _norm(artist)
    for item in results:
        url = item.get("previewUrl")
        if not url:
            continue
        got_title = _norm(item.get("trackName"))
        if want_title not in got_title and got_title not in want_title:
            continue
        if want_artist and want_artist.split(" ")[0] not in _norm(item.get("artistName")):
            continue
        if duration_s:
            got = (item.get("trackTimeMillis") or 0) / 1000.0
            if not got or abs(got - duration_s) > 3:
                continue
        return _download_preview(url, ".m4a"), "itunes/search"
    return None, ""


def fetch_preview(isrc: str = "", title: str = "", artist: str = "",
                  duration_s: Optional[float] = None,
                  allow_itunes: bool = True) -> tuple[Optional[str], str]:
    """Free 30s preview of the requested recording, or (None, '').

    Deezer by ISRC comes first because an ISRC identifies the exact recording.
    `allow_itunes=False` turns the text-search fallback off for callers whose
    host is rate-limited by iTunes -- measured 2026-09-23, the VPS gets HTTP 429
    on 10 of 10 rapid searches, while Deezer-by-ISRC answered 26 of 26 there.
    """
    path, source = _deezer_preview(isrc)
    if path:
        return path, source
    if not allow_itunes:
        return None, ""
    return _itunes_preview(title, artist, duration_s)


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------

def verify_file(file_path: str, *, isrc: str = "", title: str = "", artist: str = "",
                duration_ms: Optional[int] = None,
                threshold: float = DEFAULT_THRESHOLD,
                allow_itunes: bool = True) -> VerifyResult:
    """Check the audio in ``file_path`` against the store preview of the track.

    Returns MISMATCH only on positive evidence of different audio.
    """
    if not get_fpcalc_exe():
        return VerifyResult(SKIPPED, reason="fingerprinting tool unavailable")
    duration_s = (duration_ms / 1000.0) if duration_ms else None
    preview_path, source = fetch_preview(isrc=isrc, title=title, artist=artist,
                                         duration_s=duration_s, allow_itunes=allow_itunes)
    if not preview_path:
        return VerifyResult(SKIPPED, reason="no store preview available for this track")
    try:
        preview_fp = fingerprint(preview_path, length=120)
        if not preview_fp:
            return VerifyResult(SKIPPED, reason="preview could not be fingerprinted",
                                preview_source=source)
        full_fp = fingerprint(file_path)
        if not full_fp:
            return VerifyResult(SKIPPED, reason="download could not be fingerprinted",
                                preview_source=source)
        score, _ = best_bit_error_rate(full_fp, preview_fp)
    finally:
        try:
            os.unlink(preview_path)
        except OSError:
            pass
    if score > threshold:
        return VerifyResult(
            MISMATCH, score=score, preview_source=source,
            reason=(f"audio does not match the requested recording "
                    f"(difference {score:.2f}, limit {threshold:.2f})"),
        )
    return VerifyResult(MATCH, score=score, preview_source=source)
