"""v2.0.0 DMX-1 — per-tag write toggles, date format, legacy ID3v1.

Behavioural, not source-reading: every case tags a REAL file with the REAL
`FileTagger` and reads the tags back with mutagen, so a gate that is written
but never reached fails here rather than shipping.

Fixtures are generated with ffmpeg, one silent second per container, so the
three writers are exercised on genuine FLAC / MP3 / M4A rather than on stubs.

Run:  python -m antra.utils.test_tag_toggles
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from antra.core.models import TrackMetadata            # noqa: E402
from antra.utils.tagger import (                        # noqa: E402
    DATE_FORMATS,
    TAG_FIELDS,
    FileTagger,
    render_date,
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

# A 1x1 JPEG. Artwork is stubbed rather than fetched: DMX-1's `cover` toggle
# gates the CALL to _fetch_artwork, and asserting that over a real network
# would test FR-2's fetcher instead of this gate (and fail offline).
_PIXEL_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300ff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffc20011"
    "08000100010101110000ffc40014000100000000000000000000"
    "0000000000009fffda0008010100013f10"
)


def _stub_artwork(self, url):
    return (_PIXEL_JPEG, "image/jpeg", 1, 1, 24) if url else None


def _stub_raw_artwork(self, url):
    return [(_PIXEL_JPEG, "image/jpeg")] if url else None


FileTagger._fetch_artwork = _stub_artwork
FileTagger._fetch_raw_artwork = _stub_raw_artwork


def make_fixture(path: str) -> None:
    """One second of silence in whatever container `path`'s extension names."""
    cmd = [
        FFMPEG, "-y", "-v", "error",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-t", "1",
    ]
    ext = os.path.splitext(path)[1].lower()
    if ext == ".flac":
        cmd += ["-c:a", "flac"]
    elif ext == ".mp3":
        cmd += ["-c:a", "libmp3lame", "-b:a", "128k"]
    elif ext == ".m4a":
        cmd += ["-c:a", "aac", "-b:a", "128k", "-f", "ipod"]
    cmd.append(path)
    subprocess.run(cmd, check=True, capture_output=True)


def sample_track() -> TrackMetadata:
    return TrackMetadata(
        title="Test Title",
        artists=["First Artist", "Second Artist"],
        album="Test Album",
        album_artists=["Album Artist"],
        duration_ms=1000,
        isrc="GBAYE0601477",
        upc="602557227499",
        release_date="2024-04-12",
        release_year=2024,
        track_number=3,
        total_tracks=12,
        disc_number=2,
        genres=["Hip-Hop"],
        composer="A Composer",
        iswc="T-034.524.680-1",
        label="Test Records",
        artwork_url="https://example.invalid/cover.jpg",
        spotify_id="4uLU6hMCjMI75M1A2tKUQC",
        audio_traits=["hi-res-lossless"],
        lyrics="plain lyric line",
        synced_lyrics="[00:00.10]synced lyric line",
        # ADV-1: an unknown explicitness writes NO rtng atom, so the shared
        # fixture has to state one or [2]/[3] would pass vacuously.
        is_explicit=True,
    )


def read_tags(path: str) -> dict:
    """Normalise the three containers into one dict of present tag keys."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".flac":
        from mutagen.flac import FLAC

        audio = FLAC(path)
        out = {k.lower(): list(v) for k, v in audio.items()}
        if audio.pictures:
            out["__pictures__"] = list(audio.pictures)
        return out
    if ext == ".mp3":
        from mutagen.id3 import ID3

        audio = ID3(path)
        out: dict = {}
        for frame in audio.values():
            key = frame.FrameID
            if key == "TXXX":
                out["TXXX:" + frame.desc] = list(frame.text)
            elif key == "APIC":
                out.setdefault("APIC", []).append(frame)
            elif key in ("USLT", "SYLT"):
                out.setdefault(key, []).append(frame)
            else:
                out[key] = list(getattr(frame, "text", []))
        return out
    from mutagen.mp4 import MP4

    audio = MP4(path)
    return {k: list(v) for k, v in audio.items()}


def tag_file(tmp: str, ext: str, tagger: FileTagger, track: TrackMetadata) -> dict:
    path = os.path.join(tmp, "probe" + ext)
    if os.path.exists(path):
        os.remove(path)
    make_fixture(path)
    ok = tagger.tag(path, track)
    assert ok, "tagger.tag() returned False for %s" % ext
    return read_tags(path)


# Which key carries each TAG_FIELDS id, per container. `None` means the
# container genuinely has no home for that field, so it is not asserted.
KEYMAP = {
    ".flac": {
        "title": "title", "artist": "artist", "album": "album",
        "album_artist": "albumartist", "date": "date", "year": "year",
        "track_number": "tracknumber", "disc_number": "discnumber",
        "genre": "genre", "composer": "composer", "label": "organization",
        "isrc": "isrc", "barcode": "barcode", "iswc": "iswc",
        "cover": "__pictures__", "lyrics": "lyrics",
        "synced_lyrics": "syncedlyrics", "spotify_id": "spotify_id",
        "audio_traits": "audio_traits",
        # ADV-1 is MP4-only by design, so FLAC/MP3 genuinely have no home.
        "advisory": None,
    },
    ".mp3": {
        "title": "TIT2", "artist": "TPE1", "album": "TALB",
        "album_artist": "TPE2", "date": "TDRC", "year": None,
        "track_number": "TRCK", "disc_number": "TPOS",
        "genre": "TCON", "composer": "TCOM", "label": "TPUB",
        "isrc": "TSRC", "barcode": "TXXX:BARCODE", "iswc": "TXXX:ISWC",
        "cover": "APIC", "lyrics": "USLT", "synced_lyrics": "SYLT",
        "spotify_id": "TXXX:SPOTIFYID", "audio_traits": "TXXX:AUDIO_TRAITS",
        "advisory": None,
    },
    ".m4a": {
        "title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb",
        "album_artist": "aART", "date": "\xa9day", "year": None,
        "track_number": "trkn", "disc_number": "disk",
        "genre": "\xa9gen", "composer": "\xa9wrt",
        "label": "----:com.apple.iTunes:LABEL",
        "isrc": "----:com.apple.iTunes:ISRC", "barcode": None, "iswc": None,
        "cover": "covr", "lyrics": "\xa9lyr", "synced_lyrics": None,
        "spotify_id": "----:com.apple.iTunes:SPOTIFYID",
        "audio_traits": None,
        "advisory": "rtng",
    },
}


# Container slots written from more than one TAG_FIELDS id, so the
# "off(x) removes x" rule cannot hold for them.
SHARED_SLOTS: set = set()


def ui_tag_ids() -> set:
    """Every switch id in App.svelte's TAG_GROUPS table.

    Read out of the real file rather than duplicated here, so the two lists
    cannot agree merely because someone updated a copy.
    """
    app = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "antra-wails", "frontend", "src", "App.svelte",
    )
    if not os.path.exists(app):
        return set()
    import re

    src = open(app, encoding="utf-8").read()
    start = src.find("const TAG_GROUPS")
    # End at the closing "];" of THIS declaration. Widening the slice
    # to the next unrelated marker sweeps in CONFLICT_BEHAVIORS, the
    # date formats and the text-transform tables, which all use the
    # same `{ id: '...' }` shape - measured, 20 phantom ids.
    match = re.search(r"^  \];", src[start:], re.M) if start >= 0 else None
    if start < 0 or match is None:
        return set()
    end = start + match.start()
    return set(re.findall(r"\{\s*id:\s*'([a-z_]+)'", src[start:end]))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="antra_dmx1_")
    track = sample_track()
    try:
        # ── 1. Registry sanity ──────────────────────────────────────────────
        print("\n[1] registry")
        check("TAG_FIELDS has no duplicates", len(set(TAG_FIELDS)) == len(TAG_FIELDS))
        for ext, keymap in KEYMAP.items():
            # track_total has no key of its own — it rides inside the
            # track-number string — so it is asserted in [4] instead.
            missing = set(TAG_FIELDS) - set(keymap) - {"track_total"}
            check("%s keymap covers every TAG_FIELDS id" % ext, not missing, str(sorted(missing)))
            extra = set(keymap) - set(TAG_FIELDS)
            check("%s keymap has no ids TAG_FIELDS lacks" % ext, not extra, str(sorted(extra)))

        # ADV-1 — the App.svelte comment has always claimed this suite
        # compares the two lists. It did not; the only sentinel was a
        # hardcoded "20 switches" count in the .cjs, which cannot see a
        # RENAMED id — and a renamed id is a switch that silently does
        # nothing, which is the exact failure the comment warns about.
        ui_ids = ui_tag_ids()
        check("App.svelte TAG_GROUPS was parseable", bool(ui_ids), str(ui_ids))
        check("UI switches match TAG_FIELDS exactly",
              ui_ids == set(TAG_FIELDS),
              "ui-only=%s py-only=%s" % (sorted(ui_ids - set(TAG_FIELDS)),
                                         sorted(set(TAG_FIELDS) - ui_ids)))

        # track_total has no key of its own — it rides inside track_number.
        check("track_total is in TAG_FIELDS", "track_total" in TAG_FIELDS)

        # ── 2. Default behaviour writes everything ──────────────────────────
        print("\n[2] defaults — every mapped tag present")
        default_tags = {}
        for ext, keymap in KEYMAP.items():
            tags = tag_file(tmp, ext, FileTagger(), track)
            default_tags[ext] = tags
            for field, key in keymap.items():
                if key is None:
                    continue
                check("%s default writes %s" % (ext, field), key in tags,
                      "key %r absent; got %s" % (key, sorted(tags)[:8]))

        # ── 3. Each toggle removes exactly its own tag ──────────────────────
        print("\n[3] one toggle off at a time")
        for ext, keymap in KEYMAP.items():
            for field, key in keymap.items():
                if key is None or field == "track_total":
                    continue
                if (ext, field) in SHARED_SLOTS:
                    # One slot fed by two toggles — switching one off does
                    # not empty it. Covered explicitly in [8].
                    continue
                tagger = FileTagger(disabled_tags=[field])
                tags = tag_file(tmp, ext, tagger, track)
                check("%s: off(%s) removes %s" % (ext, field, key), key not in tags,
                      "still present: %r" % (tags.get(key),))
                # and nothing else went missing with it
                for other, okey in keymap.items():
                    if okey is None or other == field:
                        continue
                    if other == "track_total" or field == "track_number" and other == "track_total":
                        continue
                    if not (okey in tags):
                        check("%s: off(%s) kept %s" % (ext, field, other), False,
                              "collateral loss of %r" % okey)

        # ── 4. track_total is separable from track_number ───────────────────
        print("\n[4] track_total")
        t = tag_file(tmp, ".flac", FileTagger(), track)
        check("flac default tracknumber is 3/12", t["tracknumber"] == ["3/12"], str(t.get("tracknumber")))
        t = tag_file(tmp, ".flac", FileTagger(disabled_tags=["track_total"]), track)
        check("flac off(track_total) gives bare 3", t["tracknumber"] == ["3"], str(t.get("tracknumber")))
        t = tag_file(tmp, ".mp3", FileTagger(disabled_tags=["track_total"]), track)
        check("mp3 off(track_total) gives bare 3", t["TRCK"] == ["3"], str(t.get("TRCK")))
        t = tag_file(tmp, ".m4a", FileTagger(), track)
        check("m4a default trkn is (3,12)", t["trkn"] == [(3, 12)], str(t.get("trkn")))
        t = tag_file(tmp, ".m4a", FileTagger(disabled_tags=["track_total"]), track)
        check("m4a off(track_total) is (3,0)", t["trkn"] == [(3, 0)], str(t.get("trkn")))

        # ── 5. date / year are independent ──────────────────────────────────
        print("\n[5] date vs year")
        t = tag_file(tmp, ".flac", FileTagger(disabled_tags=["date"]), track)
        check("flac off(date) keeps year", t.get("year") == ["2024"] and "date" not in t,
              "date=%r year=%r" % (t.get("date"), t.get("year")))
        t = tag_file(tmp, ".flac", FileTagger(disabled_tags=["year"]), track)
        check("flac off(year) keeps date", t.get("date") == ["2024-04-12"] and "year" not in t,
              "date=%r year=%r" % (t.get("date"), t.get("year")))

        # ── 6. date_format ──────────────────────────────────────────────────
        print("\n[6] date_format")
        expected = {"iso": "2024-04-12", "year": "2024", "dmy": "12/04/2024", "mdy": "04/12/2024"}
        for fmt, want in expected.items():
            t = tag_file(tmp, ".flac", FileTagger(date_format=fmt), track)
            check("flac date_format=%s -> %s" % (fmt, want), t.get("date") == [want], str(t.get("date")))
            check("flac date_format=%s keeps YEAR 4-digit" % fmt, t.get("year") == ["2024"],
                  str(t.get("year")))
            # MP3: TDRC is a structured ISO timestamp, so dmy/mdy must fall
            # back rather than be written as a corrupted "0012-04-2024".
            mp3_want = want if fmt in ("iso", "year") else "2024-04-12"
            t = tag_file(tmp, ".mp3", FileTagger(date_format=fmt), track)
            check("mp3 date_format=%s -> %s" % (fmt, mp3_want),
                  [str(x) for x in t.get("TDRC", [])] == [mp3_want], str(t.get("TDRC")))
            # MP4 (c)day IS free text, so it honours all four.
            t = tag_file(tmp, ".m4a", FileTagger(date_format=fmt), track)
            check("m4a date_format=%s -> %s" % (fmt, want),
                  t.get("©day") == [want], str(t.get("©day")))
        t = tag_file(tmp, ".flac", FileTagger(date_format="not-a-format"), track)
        check("unknown date_format falls back to ISO", t.get("date") == ["2024-04-12"], str(t.get("date")))

        # render_date unit cases — a year-only release cannot become D/M/Y.
        check("render_date year-only + dmy degrades", render_date(None, 1999, "dmy") == "1999")
        check("render_date empty -> None", render_date(None, None, "iso") is None)
        for fmt in DATE_FORMATS:
            check("render_date(%s) is a str" % fmt, isinstance(render_date("2024-04-12", 2024, fmt), str))

        # ── 7. ID3v1 ────────────────────────────────────────────────────────
        print("\n[7] legacy ID3v1")

        def has_id3v1(path: str) -> bool:
            with open(path, "rb") as fh:
                fh.seek(-128, os.SEEK_END)
                return fh.read(3) == b"TAG"

        p = os.path.join(tmp, "v1on.mp3")
        make_fixture(p)
        FileTagger().tag(p, track)
        check("default writes ID3v1", has_id3v1(p))
        p = os.path.join(tmp, "v1off.mp3")
        make_fixture(p)
        FileTagger(write_id3v1=False).tag(p, track)
        check("write_id3v1=False writes none", not has_id3v1(p))
        # the ID3v2 side must be untouched by that switch
        t = read_tags(p)
        check("v1 off still writes ID3v2 title", t.get("TIT2") == ["Test Title"], str(t.get("TIT2")))

        # ── 8. Lyrics toggles reach embed_lyrics and the sidecars ───────────
        print("\n[8] lyrics")
        t = tag_file(tmp, ".mp3", FileTagger(disabled_tags=["synced_lyrics"]), track)
        check("mp3 off(synced) drops SYLT", "SYLT" not in t)
        check("mp3 off(synced) keeps USLT", "USLT" in t)
        t = tag_file(tmp, ".mp3", FileTagger(disabled_tags=["lyrics"]), track)
        check("mp3 off(lyrics) drops nothing else", "SYLT" in t)
        t = tag_file(tmp, ".mp3", FileTagger(disabled_tags=["lyrics", "synced_lyrics"]), track)
        check("mp3 both off -> no USLT/SYLT", "USLT" not in t and "SYLT" not in t)
        t = tag_file(tmp, ".m4a", FileTagger(disabled_tags=["lyrics"]), track)
        check("m4a off(lyrics) drops the lyric atom", "\xa9lyr" not in t,
              "(c)lyr IS the lyrics tag for MP4, so the toggle must empty it")
        t = tag_file(tmp, ".m4a", FileTagger(disabled_tags=["synced_lyrics"]), track)
        check("m4a off(synced) keeps plain lyrics in the atom",
              t.get("\xa9lyr") == ["plain lyric line"], str(t.get("\xa9lyr")))
        t = tag_file(tmp, ".flac", FileTagger(disabled_tags=["lyrics"]), track)
        check("flac off(lyrics) keeps SYNCEDLYRICS", "syncedlyrics" in t)
        t = tag_file(tmp, ".flac", FileTagger(disabled_tags=["synced_lyrics"]), track)
        check("flac off(synced) keeps plain LYRICS",
              t.get("lyrics") == ["plain lyric line"], str(t.get("lyrics")))
        t = tag_file(tmp, ".m4a", FileTagger(disabled_tags=["lyrics", "synced_lyrics"]), track)
        check("m4a both off -> no lyric atom", "\xa9lyr" not in t)

        # sidecars: an unsupported container still writes .lrc/.txt, gated
        side_dir = os.path.join(tmp, "side")
        os.makedirs(side_dir, exist_ok=True)
        stub = os.path.join(side_dir, "t.ogg")
        open(stub, "wb").write(b"OggS")
        FileTagger()._write_lyrics_sidecars(stub, track)
        check("sidecar .lrc written by default", os.path.exists(stub[:-4] + ".lrc"))
        check("sidecar .txt written by default", os.path.exists(stub[:-4] + ".txt"))
        for suffix in (".lrc", ".txt"):
            os.remove(stub[:-4] + suffix)
        FileTagger(disabled_tags=["lyrics", "synced_lyrics"])._write_lyrics_sidecars(stub, track)
        check("sidecars suppressed when both lyric tags off",
              not os.path.exists(stub[:-4] + ".lrc") and not os.path.exists(stub[:-4] + ".txt"))

        # ── 9. Regressions ──────────────────────────────────────────────────
        print("\n[9] regressions")
        check("unknown disabled id changes nothing",
              tag_file(tmp, ".flac", FileTagger(disabled_tags=["not_a_tag"]), track).keys()
              == default_tags[".flac"].keys())
        check("case/whitespace in ids is normalised",
              "title" not in tag_file(tmp, ".flac", FileTagger(disabled_tags=[" TITLE "]), track))
        # provenance is its own switch and must not be affected
        t = tag_file(tmp, ".flac", FileTagger(disabled_tags=list(TAG_FIELDS)), track)
        check("all tags off still writes ANTRA_VERSION", "antra_version" in t, str(sorted(t)))
        t = tag_file(tmp, ".flac", FileTagger(write_antra_tags=False), track)
        check("write_antra_tags=False still writes title", t.get("title") == ["Test Title"])
        check("write_antra_tags=False drops ANTRA_VERSION", "antra_version" not in t)
        # everything off: the file must still be valid and tagged with nothing
        t = tag_file(tmp, ".mp3", FileTagger(disabled_tags=list(TAG_FIELDS), write_antra_tags=False), track)
        # TSSE is ffmpeg's encoder stamp on the fixture, not something we write.
        leftover = [k for k in t if not k.startswith("TXXX:ANTRA") and k != "TSSE"]
        check("mp3 all-off leaves no content frames", leftover == [], str(leftover))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d checks, %d failed" % (CHECKS, len(FAILS)))
    for f in FAILS:
        print("  -", f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
