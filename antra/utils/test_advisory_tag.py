"""v2.0.0 ADV-1 (issue #43) — the iTunes advisory atom on ALAC / AAC.

Behavioural: every case tags a REAL m4a/mp4/flac/mp3 with the REAL FileTagger
and reads the atom back with mutagen, so a branch that is written but never
reached fails here instead of shipping.

The one structural check is the engine call site, asserted through `ast` on
the real engine.py rather than on its text — the failure mode this guards is
"someone edits that block and drops the keyword", which no behavioural test
of the tagger alone can see.

Run:  python -m antra.utils.test_advisory_tag
"""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from mutagen.mp4 import MP4                             # noqa: E402

from antra.core.models import TrackMetadata             # noqa: E402
from antra.utils.tagger import (                        # noqa: E402
    ADVISORY_CLEAN,
    ADVISORY_EXPLICIT,
    TAG_FIELDS,
    FileTagger,
)

FAILS: list[str] = []
CHECKS = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not ok:
        FAILS.append("%s%s" % (label, (" — " + detail) if detail else ""))
        print("  FAIL  %s%s" % (label, (" — " + detail) if detail else ""))


def ffmpeg_bin() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


FFMPEG = ffmpeg_bin()

_PIXEL_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300ff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffc20011"
    "08000100010101110000ffc40014000100000000000000000000"
    "0000000000009fffda0008010100013f10"
)
FileTagger._fetch_artwork = lambda self, url: (
    (_PIXEL_JPEG, "image/jpeg", 1, 1, 24) if url else None
)
FileTagger._fetch_raw_artwork = lambda self, url: (
    [(_PIXEL_JPEG, "image/jpeg")] if url else None
)

_CODEC = {".m4a": "aac", ".mp4": "aac", ".flac": "flac", ".mp3": "libmp3lame"}


def make_fixture(path: str) -> None:
    ext = os.path.splitext(path)[1].lower()
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "anullsrc=r=44100:cl=stereo", "-t", "1",
         "-c:a", _CODEC[ext], path],
        check=True, capture_output=True,
    )


def track(is_explicit) -> TrackMetadata:
    return TrackMetadata(
        title="Test Title",
        artists=["First Artist"],
        album="Test Album",
        duration_ms=1000,
        isrc="GBAYE0601477",
        track_number=3,
        total_tracks=12,
        # Genres are set so the tagger's MusicBrainz fallback (which fires on
        # `not track.genres and track.isrc`) never runs. It is a LIVE network
        # call, and without this the FLAC/MP3 invariance check in [6] compares
        # three runs that may or may not have had a genre filled in — it failed
        # once for exactly that reason, on `explicit-only keys: ['genre']`,
        # which looked like a defect in the feature and was not.
        genres=["Hip-Hop"],
        is_explicit=is_explicit,
    )


def tag_mp4(tmp: str, tk: TrackMetadata, ext: str = ".m4a", **kw) -> dict:
    """Tag a fresh fixture and return its raw MP4 atoms."""
    tagger = kw.pop("tagger", None) or FileTagger(write_antra_tags=False)
    path = os.path.join(tmp, "probe" + ext)
    if os.path.exists(path):
        os.remove(path)
    make_fixture(path)
    ok = tagger.tag(path, tk, **kw)
    assert ok, "tagger.tag() returned False for %s" % ext
    return {k: list(v) for k, v in MP4(path).items()}


def tag_other(tmp: str, tk: TrackMetadata, ext: str) -> dict:
    path = os.path.join(tmp, "probe" + ext)
    if os.path.exists(path):
        os.remove(path)
    make_fixture(path)
    assert FileTagger(write_antra_tags=False).tag(path, tk), ext
    if ext == ".flac":
        from mutagen.flac import FLAC

        return {k.lower(): list(v) for k, v in FLAC(path).items()}
    from mutagen.id3 import ID3

    out: dict = {}
    for frame in ID3(path).values():
        key = frame.FrameID
        out["TXXX:" + frame.desc if key == "TXXX" else key] = list(
            getattr(frame, "text", []) or ["<binary>"]
        )
    return out


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="antra_adv1_")
    try:
        # ── 1. registry ─────────────────────────────────────────────────────
        print("\n[1] registry")
        check("advisory is a toggleable tag", "advisory" in TAG_FIELDS)
        check("apple's values are 1 explicit / 2 clean",
              (ADVISORY_EXPLICIT, ADVISORY_CLEAN) == (1, 2))

        # ── 2. the mapping the issue asked for ──────────────────────────────
        print("\n[2] explicit -> 1, clean -> 2")
        t = tag_mp4(tmp, track(True))
        check("explicit writes rtng=1", t.get("rtng") == [1], str(t.get("rtng")))
        t = tag_mp4(tmp, track(False))
        check("clean writes rtng=2", t.get("rtng") == [2], str(t.get("rtng")))

        # ── 3. unknown writes NOTHING — the headline decision ───────────────
        print("\n[3] unknown explicitness")
        t = tag_mp4(tmp, track(None))
        check("unknown writes no rtng atom at all", "rtng" not in t, str(t.get("rtng")))
        # CONTROL: the same fixture and the same writer DO produce the atom
        # when the flag is known, so [3] cannot pass merely because the write
        # is broken or the container rejected it.
        check("CONTROL: the known case still wrote one",
              tag_mp4(tmp, track(True)).get("rtng") == [1])
        # and the rest of the tagging is unaffected by the missing flag
        check("CONTROL: unknown still tags normally", t.get("\xa9nam") == ["Test Title"])

        # ── 4. the atom is real, not just a mutagen echo ─────────────────────
        print("\n[4] the file really carries it")
        path = os.path.join(tmp, "raw.m4a")
        make_fixture(path)
        FileTagger(write_antra_tags=False).tag(path, track(True))
        raw = open(path, "rb").read()
        check("b'rtng' is present in the container bytes", b"rtng" in raw)
        check("value reads back as a plain int",
              isinstance(MP4(path)["rtng"][0], int))
        # CONTROL: an unknown-flag file of the same shape has no such atom,
        # so the byte search above is not matching something incidental.
        path2 = os.path.join(tmp, "raw2.m4a")
        make_fixture(path2)
        FileTagger(write_antra_tags=False).tag(path2, track(None))
        check("CONTROL: b'rtng' absent from the unknown-flag file",
              b"rtng" not in open(path2, "rb").read())

        # ── 5. delivered file beats requested recording ─────────────────────
        print("\n[5] delivered vs requested")
        # The case that matters: explicit was asked for, a clean master was
        # delivered. Tagging from the track would assert explicit audio.
        t = tag_mp4(tmp, track(True), delivered_explicit=False)
        check("clean delivery of an explicit request writes 2",
              t.get("rtng") == [2], str(t.get("rtng")))
        t = tag_mp4(tmp, track(False), delivered_explicit=True)
        check("explicit delivery of a clean request writes 1",
              t.get("rtng") == [1], str(t.get("rtng")))
        # Most adapters state nothing, so None must NOT be read as "clean".
        t = tag_mp4(tmp, track(True), delivered_explicit=None)
        check("a silent adapter falls back to the catalogue",
              t.get("rtng") == [1], str(t.get("rtng")))
        t = tag_mp4(tmp, track(None), delivered_explicit=True)
        check("adapter alone is enough when the catalogue is silent",
              t.get("rtng") == [1], str(t.get("rtng")))

        # ── 6. .mp4 too (Atmos writes .mp4, never .m4a) ─────────────────────
        print("\n[6] containers")
        t = tag_mp4(tmp, track(True), ext=".mp4")
        check(".mp4 gets the atom as well", t.get("rtng") == [1], str(t.get("rtng")))

        # FLAC and MP3 must be untouched by the flag — not "no rtng key",
        # which is trivially true of containers that have no such atom, but
        # byte-for-byte the same tag set whatever the explicitness is.
        for ext in (".flac", ".mp3"):
            a = tag_other(tmp, track(True), ext)
            b = tag_other(tmp, track(False), ext)
            c = tag_other(tmp, track(None), ext)
            check("%s tags are identical for explicit/clean/unknown" % ext,
                  a == b == c,
                  "explicit-only keys: %s" % sorted(set(a) ^ set(c)))
            check("%s writes no advisory-shaped tag" % ext,
                  not any("advisor" in k.lower() or k.lower() == "rtng" for k in a),
                  str(sorted(a)))

        # ── 7. the toggle ───────────────────────────────────────────────────
        print("\n[7] toggle")
        off = FileTagger(write_antra_tags=False, disabled_tags=["advisory"])
        t = tag_mp4(tmp, track(True), tagger=off)
        check("off(advisory) writes no atom", "rtng" not in t, str(t.get("rtng")))
        check("off(advisory) keeps every other tag",
              t.get("\xa9nam") == ["Test Title"]
              and "----:com.apple.iTunes:ISRC" in t
              and "trkn" in t,
              str(sorted(t)))
        # An unrelated toggle must not take the advisory with it.
        t = tag_mp4(tmp, track(True),
                    tagger=FileTagger(write_antra_tags=False, disabled_tags=["isrc"]))
        check("CONTROL: off(isrc) leaves the advisory alone", t.get("rtng") == [1])

        # ── 8. the engine hands the delivered flag over ─────────────────────
        print("\n[8] engine wiring")
        engine_src = open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "core", "engine.py"),
            encoding="utf-8",
        ).read()
        passes = []
        for node in ast.walk(ast.parse(engine_src)):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not (isinstance(f, ast.Attribute) and f.attr == "tag"
                    and isinstance(f.value, ast.Attribute) and f.value.attr == "tagger"):
                continue
            kw = {k.arg: k.value for k in node.keywords}
            v = kw.get("delivered_explicit")
            # Accept either `result.is_explicit` or the defensive
            # `getattr(result, "is_explicit", None)`, but in both cases the
            # field being read has to be the explicitness — a keyword wired
            # to something else is not this feature.
            direct = isinstance(v, ast.Attribute) and v.attr == "is_explicit"
            viagetattr = (
                isinstance(v, ast.Call)
                and isinstance(v.func, ast.Name)
                and v.func.id == "getattr"
                and len(v.args) >= 2
                and isinstance(v.args[1], ast.Constant)
                and v.args[1].value == "is_explicit"
            )
            passes.append(direct or viagetattr)
        check("engine.py has tagger.tag call sites", bool(passes), str(passes))
        check("the download path passes delivered_explicit", any(passes), str(passes))
        # CONTROL: the re-tag call site deliberately does NOT pass it (it has
        # no adapter result), so a blanket "every call site passes it" would
        # be wrong and this asserts the split rather than hiding it.
        check("CONTROL: not every call site passes it (re-tag has no result)",
              not all(passes), str(passes))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d checks, %d failed" % (CHECKS, len(FAILS)))
    for f in FAILS:
        print("  - " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
