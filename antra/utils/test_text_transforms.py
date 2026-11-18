"""v2.0.0 DMX-2 — output-only text transforms.

Behavioural, not source-reading. Every tag case tags a REAL ffmpeg-generated
FLAC / MP3 / M4A with the REAL `FileTagger` and reads the tags back with
mutagen, and every filename case renders through the REAL `LibraryOrganizer`.
A transform that is written but never reached therefore fails here instead of
shipping — which is the whole point, since DMX-1 found two such gates.

Four things are asserted that matter more than the transforms themselves:

  1. MATCHING IS UNTOUCHED. `transform_text` must not mutate the track, and the
     resolver's own inputs (`strip_collab`, `score_similarity`) must see the
     catalogue's original strings. If that ever stops holding, BUG-1's
     wrong-audio-correct-metadata class comes straight back.
  2. TAGS AND FILENAMES AGREE. Both go through the same transform, so a
     `.m3u` cannot point at a name the file does not have.
  3. DEDUP SURVIVES A TITLE TRANSFORM. `_track_identity_keys` reads the raw
     track while `_identity_keys_from_values` reads the written tags; the key
     sets must still intersect, or every such track re-downloads forever.
  4. THE DEFAULTS ARE A NO-OP, except remove_duplicate_artists.

Run:  python -m antra.utils.test_text_transforms
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from antra.core.models import TrackMetadata                      # noqa: E402
from antra.utils.matching import strip_collab, string_similarity  # noqa: E402
from antra.utils.organizer import LibraryOrganizer                # noqa: E402
from antra.utils.tagger import FileTagger                         # noqa: E402
from antra_shared.filename_prefs import (                         # noqa: E402
    CASING_MODES,
    FEATURED_MODES,
    MULTI_ARTIST_SEPARATORS,
    apply_casing,
    dedupe_artists,
    migrate_legacy_templates,
    resolve_various_artists,
    split_featured,
    strip_album_version,
    transform_text,
)

FAILS: list[str] = []
CHECKS = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not ok:
        FAILS.append("%s%s" % (label, (" - " + detail) if detail else ""))
        print("  FAIL  %s%s" % (label, (" - " + detail) if detail else ""))


def eq(label: str, got, want) -> None:
    check(label, got == want, "got %r want %r" % (got, want))


def ffmpeg_bin() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


FFMPEG = ffmpeg_bin()

# Artwork is stubbed for the same reason DMX-1 stubs it: these cases are about
# text, and fetching a real cover would make the suite need a network.
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


def make_fixture(path: str) -> None:
    cmd = [FFMPEG, "-y", "-v", "error", "-f", "lavfi",
           "-i", "anullsrc=r=44100:cl=stereo", "-t", "1"]
    ext = os.path.splitext(path)[1].lower()
    if ext == ".flac":
        cmd += ["-c:a", "flac"]
    elif ext == ".mp3":
        cmd += ["-c:a", "libmp3lame", "-b:a", "128k"]
    elif ext == ".m4a":
        cmd += ["-c:a", "aac", "-b:a", "128k", "-f", "ipod"]
    cmd.append(path)
    subprocess.run(cmd, check=True, capture_output=True)


def sample_track(**over) -> TrackMetadata:
    data = dict(
        title="Jimmy Cooks (feat. 21 Savage)",
        artists=["Drake", "Drake", "21 Savage"],
        album="Honestly, Nevermind",
        album_artists=["Drake"],
        duration_ms=1000,
        release_date="2022-06-17",
        release_year=2022,
        track_number=14,
        total_tracks=14,
        disc_number=1,
        total_discs=1,
        genres=["Hip-Hop"],
    )
    data.update(over)
    return TrackMetadata(**data)


def read_text_tags(path: str) -> dict:
    """(title, artist_values, album_artist) as actually stored, per container."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".flac":
        from mutagen.flac import FLAC

        a = FLAC(path)
        return {
            "title": (a.get("title") or [None])[0],
            "artist": list(a.get("artist") or []),
            "album_artist": (a.get("albumartist") or [None])[0],
        }
    if ext == ".mp3":
        from mutagen.id3 import ID3

        a = ID3(path)

        def txt(frame):
            f = a.getall(frame)
            return str(f[0].text[0]) if f and f[0].text else None

        return {
            "title": txt("TIT2"),
            "artist": [txt("TPE1")] if txt("TPE1") is not None else [],
            "album_artist": txt("TPE2"),
        }
    from mutagen.mp4 import MP4

    a = MP4(path)
    return {
        "title": (a.get("\xa9nam") or [None])[0],
        "artist": list(a.get("\xa9ART") or []),
        "album_artist": (a.get("aART") or [None])[0],
    }


def tag_with(tmp: str, name: str, prefs: dict, track: TrackMetadata, ext: str) -> dict:
    path = os.path.join(tmp, name + ext)
    make_fixture(path)
    tagger = FileTagger(write_antra_tags=False, filename_preferences=prefs)
    ok = tagger.tag(path, track)
    check("tag() succeeded (%s%s)" % (name, ext), ok)
    return read_text_tags(path)


# ═════════════════════════════════════════════════════════════════════════════
def section(title: str) -> None:
    print("\n" + title)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="antra_dmx2_")
    n = [0]

    def uniq(base: str) -> str:
        n[0] += 1
        return "%s_%d" % (base, n[0])

    DEFAULTS = migrate_legacy_templates({})

    # ── 1. pure helpers ──────────────────────────────────────────────────────
    section("1. helpers")
    eq("casing nothing", apply_casing("dOn'T sToP", "nothing"), "dOn'T sToP")
    eq("casing lower", apply_casing("DON'T STOP", "lower"), "don't stop")
    eq("casing upper", apply_casing("don't stop", "upper"), "DON'T STOP")
    # str.title() yields "Don'T" / "Rock'N'Roll"; the apostrophe must not be a
    # word boundary, which is why this is a regex and not .title().
    eq("casing start keeps apostrophes", apply_casing("don't rock'n'roll", "start"),
       "Don't Rock'n'roll")
    eq("casing start after bracket", apply_casing("song (live at wembley)", "start"),
       "Song (Live At Wembley)")
    eq("casing sentence", apply_casing("DON'T STOP ME NOW", "sentence"), "Don't stop me now")
    # A title that does not START with a letter has no first word to
    # capitalise. Walking forward to the first letter anywhere would capitalise
    # the SECOND word ("2 Of amerikaz"), which reads as a proper noun.
    eq("casing sentence leaves a bracketed start lower",
       apply_casing("(INTRO) HELLO", "sentence"), "(intro) hello")
    eq("casing sentence leaves a digit start lower",
       apply_casing("2 OF AMERIKAZ", "sentence"), "2 of amerikaz")
    eq("casing unknown mode is a no-op", apply_casing("Mixed CASE", "bogus"), "Mixed CASE")
    eq("casing empty", apply_casing("", "upper"), "")

    eq("album version parens", strip_album_version("Song (Album Version)"), "Song")
    eq("album version brackets", strip_album_version("Song [album ver.]"), "Song")
    eq("album version dash", strip_album_version("Song - Album Version"), "Song")
    # These MUST survive: version_mismatch() rejects a wrong recording on
    # exactly these words, so stripping them would make a library where two
    # different recordings share one name.
    for keep in ("Song (Live)", "Song (Remix)", "Song - Instrumental", "Song (Acoustic Version)"):
        eq("album version keeps %r" % keep, strip_album_version(keep), keep)
    eq("album version no-op", strip_album_version("Song"), "Song")

    eq("dedupe exact", dedupe_artists(["Drake", "Drake", "21 Savage"]), ["Drake", "21 Savage"])
    eq("dedupe case/space", dedupe_artists([" drake ", "Drake"]), ["drake"])
    # Deliberately NOT a substring pass: it would drop a real credit.
    eq("dedupe keeps a superset name", dedupe_artists(["Drake", "Drake & Future"]),
       ["Drake", "Drake & Future"])
    eq("dedupe drops blanks", dedupe_artists(["A", "", None, "A"]), ["A"])

    eq("split feat parens", split_featured("Song (feat. A & B)"), ("Song", ["A", "B"]))
    eq("split feat brackets", split_featured("Song [ft. A]"), ("Song", ["A"]))
    eq("split feat trailing", split_featured("Song feat. A and B"), ("Song", ["A", "B"]))
    eq("split with", split_featured("Song (with C)"), ("Song", ["C"]))
    eq("split none", split_featured("Song"), ("Song", []))
    eq("split never empties the title", split_featured("(feat. A)")[0], "(feat. A)")

    eq("VA kept when on", resolve_various_artists("Various Artists", ["Real"], {}), "Various Artists")
    eq("VA swapped when off",
       resolve_various_artists("Various Artists", ["Real"], {"album_various_artists": False}),
       "Real")
    eq("VA off leaves a normal name",
       resolve_various_artists("Drake", ["Drake"], {"album_various_artists": False}), "Drake")
    eq("VA off with no artists keeps what it had",
       resolve_various_artists("Various Artists", [], {"album_various_artists": False}),
       "Various Artists")

    # ── 2. the track is never mutated ────────────────────────────────────────
    section("2. matching inputs are untouched")
    track = sample_track()
    before = (track.title, list(track.artists), list(track.album_artists))
    loud = migrate_legacy_templates({
        "title_casing": "upper", "artist_casing": "upper",
        "featured_artists_mode": "move", "remove_album_version": True,
        "multi_artist_separator": "slash", "album_various_artists": False,
    })
    tx = transform_text(track, loud)
    eq("track.title unchanged", track.title, before[0])
    eq("track.artists unchanged", list(track.artists), before[1])
    eq("track.album_artists unchanged", list(track.album_artists), before[2])
    check("transform actually did something", tx.title != track.title, tx.title)
    # The two functions the resolver actually calls must still see the original.
    eq("strip_collab sees the raw title", strip_collab(track.title), "Jimmy Cooks")
    check("similarity against the raw title is still 1.0",
          string_similarity(track.title, before[0]) == 1.0)

    # ── 3. tags, all three containers ────────────────────────────────────────
    section("3. tag writes")
    for ext in (".flac", ".mp3", ".m4a"):
        t = sample_track()
        got = tag_with(tmp, uniq("default"), DEFAULTS, t, ext)
        eq("default title%s" % ext, got["title"], "Jimmy Cooks (feat. 21 Savage)")
        # Default = the artist list with the duplicate removed. FLAC/M4A keep a
        # real multi-value field; MP3 has to be one joined string.
        if ext == ".mp3":
            eq("default artist%s" % ext, got["artist"], ["Drake, 21 Savage"])
        else:
            eq("default artist%s" % ext, got["artist"], ["Drake", "21 Savage"])
        eq("default album artist%s" % ext, got["album_artist"], "Drake")

        p = migrate_legacy_templates({"title_casing": "upper"})
        eq("title upper%s" % ext, tag_with(tmp, uniq("tc"), p, sample_track(), ext)["title"],
           "JIMMY COOKS (FEAT. 21 SAVAGE)")

        p = migrate_legacy_templates({"artist_casing": "lower"})
        got = tag_with(tmp, uniq("ac"), p, sample_track(), ext)
        eq("artist lower%s" % ext, got["artist"],
           ["drake, 21 savage"] if ext == ".mp3" else ["drake", "21 savage"])
        eq("artist casing hits album artist too%s" % ext, got["album_artist"], "drake")

        p = migrate_legacy_templates({"remove_duplicate_artists": False})
        got = tag_with(tmp, uniq("nodedupe"), p, sample_track(), ext)
        eq("dedupe off keeps the repeat%s" % ext, got["artist"],
           ["Drake, Drake, 21 Savage"] if ext == ".mp3" else ["Drake", "Drake", "21 Savage"])

        p = migrate_legacy_templates({"featured_artists_mode": "remove"})
        got = tag_with(tmp, uniq("featrm"), p, sample_track(), ext)
        eq("feat remove strips the title%s" % ext, got["title"], "Jimmy Cooks")
        eq("feat remove keeps the artists%s" % ext, got["artist"],
           ["Drake, 21 Savage"] if ext == ".mp3" else ["Drake", "21 Savage"])

        p = migrate_legacy_templates({"featured_artists_mode": "move"})
        got = tag_with(tmp, uniq("featmv"), p, sample_track(), ext)
        eq("feat move keeps the credit in the title%s" % ext, got["title"],
           "Jimmy Cooks (feat. 21 Savage)")
        eq("feat move leaves one artist%s" % ext, got["artist"], ["Drake"])

        p = migrate_legacy_templates({"remove_album_version": True})
        eq("album version stripped%s" % ext,
           tag_with(tmp, uniq("av"), p, sample_track(title="Song (Album Version)"), ext)["title"],
           "Song")

        for sep_id, sep in MULTI_ARTIST_SEPARATORS.items():
            p = migrate_legacy_templates({"multi_artist_separator": sep_id})
            got = tag_with(tmp, uniq("sep" + sep_id), p, sample_track(), ext)
            eq("separator %s collapses artists%s" % (sep_id, ext), got["artist"],
               [sep.join(["Drake", "21 Savage"])])

        va = sample_track(artists=["Real Artist"], album_artists=["Various Artists"])
        eq("VA kept by default%s" % ext,
           tag_with(tmp, uniq("vaon"), DEFAULTS, va, ext)["album_artist"], "Various Artists")
        p = migrate_legacy_templates({"album_various_artists": False})
        eq("VA replaced when off%s" % ext,
           tag_with(tmp, uniq("vaoff"), p, va, ext)["album_artist"], "Real Artist")

    # A track whose artist list carries no repeat must be byte-identical to the
    # pre-DMX-2 output, which is the claim the defaults rest on.
    clean = sample_track(artists=["Drake", "21 Savage"])
    got = tag_with(tmp, uniq("clean"), DEFAULTS, clean, ".flac")
    eq("clean track default artist", got["artist"], ["Drake", "21 Savage"])
    eq("clean track default title", got["title"], "Jimmy Cooks (feat. 21 Savage)")

    # ── 4. filenames and folders go through the same transform ───────────────
    section("4. filenames agree with tags")
    for label, prefs_in, want_stem, want_dir in (
        ("default", {}, "14 - Jimmy Cooks (feat. 21 Savage)", "Drake"),
        ("title upper", {"title_casing": "upper"},
         "14 - JIMMY COOKS (FEAT. 21 SAVAGE)", "Drake"),
        ("artist upper", {"artist_casing": "upper"},
         "14 - Jimmy Cooks (feat. 21 Savage)", "DRAKE"),
        ("feat move", {"featured_artists_mode": "move"},
         "14 - Jimmy Cooks (feat. 21 Savage)", "Drake"),
        ("feat remove", {"featured_artists_mode": "remove"}, "14 - Jimmy Cooks", "Drake"),
    ):
        root = os.path.join(tmp, "lib_" + label.replace(" ", "_"))
        org = LibraryOrganizer(root, **prefs_in)
        t = sample_track()
        out = org.get_output_path(t) + ".flac"
        eq("filename stem (%s)" % label, os.path.splitext(os.path.basename(out))[0], want_stem)
        eq("artist folder (%s)" % label, os.path.basename(os.path.dirname(os.path.dirname(out))),
           want_dir)

        # The decisive one: the name on disk must equal the title in the tag.
        make_fixture(out)
        tagged = FileTagger(write_antra_tags=False,
                            filename_preferences=org.filename_preferences)
        check("tag() on the real path (%s)" % label, tagged.tag(out, t))
        tag_title = read_text_tags(out)["title"]
        check("tag title matches the filename (%s)" % label,
              tag_title in os.path.basename(out),
              "tag=%r file=%r" % (tag_title, os.path.basename(out)))

    # Various Artists reaches the FOLDER, not only the tag.
    va = sample_track(artists=["Real Artist"], album_artists=["Various Artists"])
    on = LibraryOrganizer(os.path.join(tmp, "va_on"))
    off = LibraryOrganizer(os.path.join(tmp, "va_off"), album_various_artists=False)
    eq("VA folder kept by default",
       (on.get_output_path(va) + ".flac").replace("\\", "/").split("/")[-3],
       "Various Artists")
    eq("VA folder replaced when off",
       (off.get_output_path(va) + ".flac").replace("\\", "/").split("/")[-3],
       "Real Artist")

    # ── 5. dedup survives a title transform ──────────────────────────────────
    section("5. library dedup")
    t = sample_track(title="Song (Album Version)", isrc=None, spotify_id=None, upc=None)
    plain = LibraryOrganizer(os.path.join(tmp, "id_off"))
    strip = LibraryOrganizer(os.path.join(tmp, "id_on"), remove_album_version=True)
    ka, kb = set(plain._track_identity_keys(t)), set(strip._track_identity_keys(t))
    check("transform keys are ADDITIVE", ka.issubset(kb), "lost %r" % sorted(ka - kb))
    check("transform adds the written-title key", len(kb) > len(ka))
    # Casing and dedup cannot move a key: _normalize_identity_part lowercases,
    # and _artists_canonical_key builds a set.
    for label, kw in (("casing", {"title_casing": "upper", "artist_casing": "upper"}),
                      ("dedupe", {"remove_duplicate_artists": True})):
        o = LibraryOrganizer(os.path.join(tmp, "id_" + label), **kw)
        eq("%s leaves identity keys alone" % label, set(o._track_identity_keys(t)), ka)

    # End to end: write the file with the transform on, then ask a FRESH
    # organizer (same settings) whether it is already downloaded. This is the
    # case that would re-download forever if the two key sides disagreed.
    root = os.path.join(tmp, "dedup_e2e")
    org = LibraryOrganizer(root, remove_album_version=True)
    out = org.get_output_path(t) + ".flac"
    make_fixture(out)
    tagger = FileTagger(write_antra_tags=False, filename_preferences=org.filename_preferences)
    check("tagged the dedup fixture", tagger.tag(out, t))
    org.mark_downloaded(t, out)
    fresh = LibraryOrganizer(root, remove_album_version=True)
    check("already-downloaded after a title transform", bool(fresh.is_already_downloaded(t)),
          "keys=%r" % sorted(fresh._track_identity_keys(t)))
    # And a file written BEFORE the setting was switched on is still found.
    plain_root = os.path.join(tmp, "dedup_legacy")
    legacy = LibraryOrganizer(plain_root)
    legacy_out = legacy.get_output_path(t) + ".flac"
    make_fixture(legacy_out)
    FileTagger(write_antra_tags=False,
               filename_preferences=legacy.filename_preferences).tag(legacy_out, t)
    legacy.mark_downloaded(t, legacy_out)
    switched = LibraryOrganizer(plain_root, remove_album_version=True)
    check("a pre-transform file is still recognised",
          bool(switched.is_already_downloaded(t)))

    # ── 6. regressions ───────────────────────────────────────────────────────
    section("6. regressions")
    # Every mode the UI can send must be a value Python knows.
    for mode in CASING_MODES:
        p = migrate_legacy_templates({"title_casing": mode})
        eq("casing %r survives migration" % mode, p["title_casing"], mode)
    for mode in FEATURED_MODES:
        p = migrate_legacy_templates({"featured_artists_mode": mode})
        eq("feat %r survives migration" % mode, p["featured_artists_mode"], mode)
    for sep_id in MULTI_ARTIST_SEPARATORS:
        p = migrate_legacy_templates({"multi_artist_separator": sep_id})
        eq("separator %r survives migration" % sep_id, p["multi_artist_separator"], sep_id)
    p = migrate_legacy_templates({"title_casing": "BOGUS", "featured_artists_mode": "x",
                                  "multi_artist_separator": "nul"})
    eq("bogus casing clamps", p["title_casing"], "nothing")
    eq("bogus feat clamps", p["featured_artists_mode"], "nothing")
    eq("bogus separator clamps", p["multi_artist_separator"], "default")
    # An absent key must read as the documented default, because that is what
    # every config.json written before DMX-2 looks like.
    empty = migrate_legacy_templates({})
    eq("absent title_casing", empty["title_casing"], "nothing")
    eq("absent remove_duplicate_artists", empty["remove_duplicate_artists"], True)
    eq("absent album_various_artists", empty["album_various_artists"], True)
    eq("absent remove_album_version", empty["remove_album_version"], False)
    # A tagger built with no prefs at all must behave as the defaults, not crash.
    bare = FileTagger(write_antra_tags=False)
    eq("bare tagger clamps prefs", bare.filename_preferences["title_casing"], "nothing")
    # Empty artist list must not raise, and must not invent a name.
    tx = transform_text(sample_track(artists=[], album_artists=[]), loud)
    eq("no artists -> empty list", tx.artists, [])
    check("no artists -> album artist falls back", tx.album_artist == "Unknown Artist",
          tx.album_artist)
    # The one pre-existing defect fixed alongside: the folder modal has always
    # SENT "dash" for "Replace with -", which only "hyphen" used to accept.
    from antra_shared.filename_prefs import render_template
    for policy in ("dash", "hyphen"):
        eq("whitespace %r replaces spaces" % policy,
           render_template("{artist} - {title}", {"artist": "A B", "title": "C D"},
                           migrate_legacy_templates({"whitespace_handling": policy})),
           "A-B---C-D")
    eq("whitespace keep preserves",
       render_template("{artist}", {"artist": "A B"},
                       migrate_legacy_templates({"whitespace_handling": "keep"})), "A B")

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n%d checks, %d failed" % (CHECKS, len(FAILS)))
    for f in FAILS:
        print("  - %s" % f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
