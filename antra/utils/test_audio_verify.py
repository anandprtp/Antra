"""Behavioural tests for audio_verify.

Run with a real fpcalc on PATH or ANTRA_FPCALC set, and real audio files:

    python antra/utils/test_audio_verify.py [MUSIC_ROOT ...]

Network-dependent sections are skipped (reported, never silently passed) when
the preview services are unreachable.
"""
from __future__ import annotations

import glob
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import mutagen

from antra.utils import audio_verify as av

PASS = FAIL = SKIP = 0
_FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {label}" + (f" -- {detail}" if detail else ""))
    else:
        FAIL += 1
        _FAILURES.append(label)
        print(f"  FAIL  {label}" + (f" -- {detail}" if detail else ""))


def skip(label: str, why: str) -> None:
    global SKIP
    SKIP += 1
    print(f"  SKIP  {label} -- {why}")


def load_tracks(roots: list[str]) -> list[dict]:
    out = []
    for root in roots:
        for dirpath, _, names in os.walk(root):
            for name in names:
                if not name.lower().endswith((".flac", ".m4a", ".mp3")):
                    continue
                path = os.path.join(dirpath, name)
                tags = mutagen.File(path, easy=True)
                if not tags:
                    continue
                out.append(dict(
                    path=path,
                    isrc=(tags.get("isrc") or [""])[0],
                    title=(tags.get("title") or [""])[0],
                    artist=(tags.get("artist") or [""])[0],
                    duration_ms=int(tags.info.length * 1000),
                ))
    return out


# --------------------------------------------------------------------------
# 1. matcher maths (no network, no fpcalc)
# --------------------------------------------------------------------------

def test_matcher() -> None:
    print("\n[1] sliding matcher")
    rnd = random.Random(7)
    full = [rnd.getrandbits(32) for _ in range(2000)]

    inner = full[500:800]
    score, off = av.best_bit_error_rate(full, inner)
    check("identical audio scores 0.0", score == 0.0, f"score={score:.3f} off={off}")
    check("identical audio is located at its true offset",
          off is not None and abs(off - (500 + av._EDGE_ITEMS)) <= 1, f"off={off}")

    other = [rnd.getrandbits(32) for _ in range(300)]
    score_u, _ = av.best_bit_error_rate(full, other)
    check("unrelated audio scores far above the threshold",
          score_u > 0.40, f"score={score_u:.3f}")
    check("threshold separates the two", score < av.DEFAULT_THRESHOLD < score_u,
          f"{score:.3f} < {av.DEFAULT_THRESHOLD} < {score_u:.3f}")

    short = full[:20]
    s_short, off_short = av.best_bit_error_rate(full, short)
    check("too little overlap never claims a match",
          s_short == 1.0 and off_short is None, f"score={s_short}")

    # A preview that overhangs the end of the download still matches on the
    # overlapping part, as long as that part clears the minimum overlap.
    tail = full[1700:] + [rnd.getrandbits(32) for _ in range(200)]
    s_tail, _ = av.best_bit_error_rate(full, tail)
    check("preview overhanging the end still matches", s_tail < 0.10, f"score={s_tail:.3f}")

    # Below that floor the answer is "cannot tell", never "mismatch".
    barely = full[1950:] + [rnd.getrandbits(32) for _ in range(50)]
    s_barely, off_barely = av.best_bit_error_rate(full, barely)
    check("too little genuine overlap reports no match rather than guessing",
          s_barely == 1.0 and off_barely is None, f"score={s_barely}")


# --------------------------------------------------------------------------
# 2. fingerprinting real files
# --------------------------------------------------------------------------

def test_fingerprint(tracks: list[dict]) -> None:
    print("\n[2] fingerprinting")
    if not av.get_fpcalc_exe():
        skip("fingerprint real file", "fpcalc not available")
        return
    fp = av.fingerprint(tracks[0]["path"])
    check("real file yields a fingerprint", bool(fp) and len(fp) > 100,
          f"{len(fp or [])} items")
    expected = tracks[0]["duration_ms"] / 1000.0 * av._ITEMS_PER_SEC
    check("fingerprint length tracks audio duration",
          fp is not None and abs(len(fp) - expected) / expected < 0.10,
          f"{len(fp or [])} vs ~{expected:.0f}")
    check("a missing file degrades to None rather than raising",
          av.fingerprint(os.path.join(tempfile.gettempdir(), "antra-no-such.flac")) is None)

    # Direct callers (not just verify_file) must get None, never an exception.
    real = av.get_fpcalc_exe
    try:
        av.get_fpcalc_exe = lambda: None  # type: ignore[assignment]
        check("fingerprint without fpcalc returns None rather than raising",
              av.fingerprint(tracks[0]["path"]) is None)
    except Exception as exc:  # noqa: BLE001
        check("fingerprint without fpcalc returns None rather than raising", False, repr(exc))
    finally:
        av.get_fpcalc_exe = real  # type: ignore[assignment]


# --------------------------------------------------------------------------
# 3. end-to-end verdicts against real previews
# --------------------------------------------------------------------------

def test_verdicts(tracks: list[dict]) -> None:
    print("\n[3] verdicts (network)")
    if not av.get_fpcalc_exe():
        skip("verdicts", "fpcalc not available")
        return
    usable = [t for t in tracks if t["title"]][:6]
    genuine_scores = []
    for track in usable:
        res = av.verify_file(track["path"], isrc=track["isrc"], title=track["title"],
                             artist=track["artist"], duration_ms=track["duration_ms"])
        if res.verdict == av.SKIPPED:
            skip(f"genuine: {track['title'][:28]}", res.reason)
            continue
        genuine_scores.append(res.score)
        check(f"genuine download accepted: {track['title'][:28]}",
              res.verdict == av.MATCH, f"score={res.score:.3f} via {res.preview_source}")

    if len(usable) >= 2:
        # The wrong-audio bug: right tags, different recording in the file.
        a, b = usable[0], usable[1]
        res = av.verify_file(a["path"], isrc=b["isrc"], title=b["title"],
                             artist=b["artist"], duration_ms=b["duration_ms"])
        if res.verdict == av.SKIPPED:
            skip("wrong audio rejected", res.reason)
        else:
            check("wrong audio is rejected", res.verdict == av.MISMATCH,
                  f"score={res.score:.3f}")
            check("rejection message names the problem in plain words",
                  "does not match" in res.reason, res.reason)

    if genuine_scores:
        check("every genuine score has margin under the threshold",
              max(genuine_scores) < av.DEFAULT_THRESHOLD - 0.10,
              f"worst={max(genuine_scores):.3f} threshold={av.DEFAULT_THRESHOLD}")


# --------------------------------------------------------------------------
# 4. degrade-to-skip: nothing may fail a download
# --------------------------------------------------------------------------

def test_degradation(tracks: list[dict]) -> None:
    print("\n[4] degradation (never fails a download)")
    track = tracks[0]

    saved = os.environ.get("ANTRA_FPCALC")
    try:
        os.environ["ANTRA_FPCALC"] = os.path.join(tempfile.gettempdir(), "antra-no-fpcalc")
        real_which = av.get_fpcalc_exe
        av.get_fpcalc_exe = lambda: None  # type: ignore[assignment]
        res = av.verify_file(track["path"], isrc=track["isrc"], title=track["title"])
        check("no fingerprinting tool -> skipped", res.verdict == av.SKIPPED, res.reason)
    finally:
        av.get_fpcalc_exe = real_which  # type: ignore[assignment]
        if saved is None:
            os.environ.pop("ANTRA_FPCALC", None)
        else:
            os.environ["ANTRA_FPCALC"] = saved

    class Boom:
        def get(self, *a, **k):
            raise OSError("network down")

    real_requests = av.requests
    try:
        av.requests = Boom()  # type: ignore[assignment]
        res = av.verify_file(track["path"], isrc=track["isrc"], title=track["title"])
        check("network failure -> skipped, not mismatch", res.verdict == av.SKIPPED, res.reason)
    finally:
        av.requests = real_requests  # type: ignore[assignment]

    res = av.verify_file(track["path"], isrc="ZZ9999999999",
                         title="Zzzz Nonexistent Track 4771", artist="Zzzz Nobody")
    check("unknown track -> skipped, not mismatch", res.verdict == av.SKIPPED, res.reason)

    if av.get_fpcalc_exe():
        leftover_before = set(glob.glob(os.path.join(tempfile.gettempdir(), "antra-preview-*")))
        av.verify_file(track["path"], isrc=track["isrc"], title=track["title"],
                       artist=track["artist"], duration_ms=track["duration_ms"])
        leftover_after = set(glob.glob(os.path.join(tempfile.gettempdir(), "antra-preview-*")))
        check("preview temp files are cleaned up", leftover_after <= leftover_before,
              f"{len(leftover_after - leftover_before)} left behind")


# --------------------------------------------------------------------------
# 5. the preview must be verified before it is trusted
# --------------------------------------------------------------------------

def test_preview_gate() -> None:
    print("\n[5] preview selection gate")

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            pass

    results = {"results": [
        {"trackName": "Some Song", "artistName": "Right Artist",
         "trackTimeMillis": 300000, "previewUrl": "http://example.invalid/wrong-duration.m4a"},
        {"trackName": "Completely Other", "artistName": "Right Artist",
         "trackTimeMillis": 200000, "previewUrl": "http://example.invalid/wrong-title.m4a"},
        {"trackName": "Some Song", "artistName": "Different Person",
         "trackTimeMillis": 200000, "previewUrl": "http://example.invalid/wrong-artist.m4a"},
    ]}
    calls = []

    class FakeRequests:
        def get(self, url, **kwargs):
            calls.append(url)
            return FakeResp(results)

    real_requests = av.requests
    real_dl = av._download_preview
    try:
        av.requests = FakeRequests()  # type: ignore[assignment]
        av._download_preview = lambda url, suffix: "DOWNLOADED:" + url  # type: ignore[assignment]
        path, _ = av._itunes_preview("Some Song", "Right Artist", 200.0)
        check("a hit with the wrong duration is not used as the reference", path is None,
              f"got {path}")

        path, _ = av._itunes_preview("Completely Other", "Wrong Artist Entirely", 200.0)
        check("a hit by a different artist is not used as the reference", path is None,
              f"got {path}")

        results["results"].append({
            "trackName": "Some Song (Remastered)", "artistName": "Right Artist Band",
            "trackTimeMillis": 201000, "previewUrl": "http://example.invalid/good.m4a"})
        path, source = av._itunes_preview("Some Song", "Right Artist", 200.0)
        check("a hit agreeing on title, artist and duration is used",
              path == "DOWNLOADED:http://example.invalid/good.m4a", f"{path} via {source}")
        # The website runs on a host iTunes rate-limits, so it turns the
        # text-search fallback off entirely; that must make no iTunes call.
        calls.clear()
        path, _ = av.fetch_preview(isrc="", title="Some Song", artist="Right Artist",
                                   duration_s=200.0, allow_itunes=False)
        itunes_calls = [c for c in calls if "itunes" in c]
        check("allow_itunes=False makes no iTunes request",
              path is None and not itunes_calls, f"{len(itunes_calls)} call(s)")
    finally:
        av.requests = real_requests  # type: ignore[assignment]
        av._download_preview = real_dl  # type: ignore[assignment]


def main() -> int:
    roots = sys.argv[1:] or [os.path.join(os.path.expanduser("~"), "Music")]
    tracks = load_tracks(roots)
    print(f"fpcalc: {av.get_fpcalc_exe()}")
    print(f"{len(tracks)} audio files in {roots}")
    if not tracks:
        print("no audio files found -- pass a music root as an argument")
        return 2
    test_matcher()
    test_fingerprint(tracks)
    test_verdicts(tracks)
    test_degradation(tracks)
    test_preview_gate()
    print(f"\n{PASS} passed, {FAIL} failed, {SKIP} skipped")
    if _FAILURES:
        print("failed: " + ", ".join(_FAILURES))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
