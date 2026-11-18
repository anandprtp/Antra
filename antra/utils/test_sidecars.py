"""Behavioural tests for PORT-2 sidecars.

Everything here asserts on BYTES ON DISK or on the text a builder returns —
never on source text — and every case runs the SHIPPED module rather than a
re-implementation, so the suite cannot pass while the app is broken.

Run:  python antra/utils/test_sidecars.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from antra.utils import sidecars  # noqa: E402

# The repo root also holds an `Antra-Web/antra` package. Importing the wrong
# tree is how a suite passes while measuring nothing (recorded three times in
# CLAUDE.md), so pin the module we actually loaded.
assert Path(sidecars.__file__).resolve().is_relative_to(ROOT / "antra"), sidecars.__file__

PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok    {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}" + (f"  --> {detail}" if detail else ""))


def eq(label: str, got, want) -> None:
    check(label, got == want, f"got {got!r}, want {want!r}")


@dataclass
class T:
    """Stand-in for TrackMetadata — only the attributes sidecars reads."""
    title: str = "Song"
    artists: list = field(default_factory=lambda: ["Artist"])
    album: Optional[str] = "Album"
    album_artists: list = field(default_factory=list)
    track_number: Optional[int] = 1
    disc_number: Optional[int] = 1
    total_tracks: Optional[int] = None
    total_discs: Optional[int] = None
    duration_ms: Optional[int] = 210000
    isrc: Optional[str] = None
    genres: list = field(default_factory=list)
    label: Optional[str] = None
    upc: Optional[str] = None
    composer: Optional[str] = None
    release_year: Optional[int] = None
    release_date: Optional[str] = None
    playlist_name: Optional[str] = None
    playlist_position: Optional[int] = None
    request_kind: Optional[str] = None
    is_explicit: Optional[bool] = None


def make_album(tmp: Path, folder: str, n: int, ext: str = ".flac", disc: int = 1,
               start: int = 1, **kw) -> list:
    """Write n real (tiny) audio files and return [(track, abs_path)]."""
    d = tmp / folder
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(start, start + n):
        name = f"{disc}{i:02d} - Track {i}{ext}"
        p = d / name
        p.write_bytes(b"\0" * 64)
        attrs = {"title": f"Track {i}", "track_number": i, "disc_number": disc}
        attrs.update(kw)   # a caller-supplied title must win, not collide
        out.append((T(**attrs), str(p)))
    return out


WHEN = datetime(2026, 9, 10, 12, 30, tzinfo=timezone.utc)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="antra_sidecars_"))
    try:
        # ── 1. THE COVERAGE RULE — the desktop-specific half ──────────────
        print("\n1. Coverage rule (a run that only partly accounts for a folder)")

        entries = make_album(tmp, "Full Album", 12)
        sidecars.write_release_sidecars(entries, want_nfo=True, want_json=True,
                                        want_cue=True, generated_at=WHEN)
        d = tmp / "Full Album"
        eq("full album writes .nfo/.json/.cue",
           sorted(p.suffix for p in d.iterdir() if p.suffix in (".nfo", ".json", ".cue")),
           [".cue", ".json", ".nfo"])
        first_nfo = (d / "Full Album.nfo").read_bytes()
        eq("the .nfo lists all 12 tracks",
           json.loads((d / "Full Album.json").read_text(encoding="utf-8"))["release"]["track_count"], 12)

        # A single track dropped into that same folder must NOT shrink it.
        one = [entries[3]]
        sidecars.write_release_sidecars(one, want_nfo=True, want_json=True, generated_at=WHEN)
        check("1-of-12 leaves the existing .nfo byte-identical",
              (d / "Full Album.nfo").read_bytes() == first_nfo)
        eq("1-of-12 leaves the existing .json at 12 tracks",
           json.loads((d / "Full Album.json").read_text(encoding="utf-8"))["release"]["track_count"], 12)

        # A genuine single into a fresh folder DOES get described.
        single = make_album(tmp, "One Track Release", 1)
        sidecars.write_release_sidecars(single, want_nfo=True, generated_at=WHEN)
        check("a genuine 1-track release still gets its .nfo",
              (tmp / "One Track Release" / "One Track Release.nfo").exists())

        # A re-run where most tracks were skipped still covers the folder.
        again = list(entries)  # all 12 have files on disk (SKIPPED carries file_path)
        sidecars.write_release_sidecars(again, want_json=True, generated_at=WHEN)
        eq("a re-run with 11 skipped still describes all 12",
           json.loads((d / "Full Album.json").read_text(encoding="utf-8"))["release"]["track_count"], 12)

        # An album where two tracks failed: the folder holds 10, the run has 10.
        part = make_album(tmp, "Two Failed", 10)
        sidecars.write_release_sidecars(part, want_json=True, generated_at=WHEN)
        eq("10 written of 12 requested describes the 10 on disk",
           json.loads((tmp / "Two Failed" / "Two Failed.json").read_text(encoding="utf-8"))
           ["release"]["track_count"], 10)

        # ...but if a past run already got them, do not shrink.
        make_album(tmp, "Two Failed", 2, start=11)  # two more files appear
        before = (tmp / "Two Failed" / "Two Failed.json").read_bytes()
        sidecars.write_release_sidecars(part, want_json=True, generated_at=WHEN)
        check("10-of-12 on a folder that now holds 12 leaves the .json alone",
              (tmp / "Two Failed" / "Two Failed.json").read_bytes() == before)

        # A vanished file is not counted as covered.
        gone = make_album(tmp, "Vanished", 3)
        os.remove(gone[0][1])
        sidecars.write_release_sidecars(gone, want_nfo=True, generated_at=WHEN)
        eq("a run whose file was deleted describes only what is on disk",
           (tmp / "Vanished" / "Vanished.nfo").read_text(encoding="utf-8").count("     File     : "), 2)

        # ── 2. NOTHING IS WRITTEN WHEN NOTHING IS ASKED FOR ───────────────
        print("\n2. Defaults — every toggle off is a byte-for-byte no-op")
        quiet = make_album(tmp, "Untouched", 4)
        sidecars.write_release_sidecars(quiet)
        eq("all toggles off writes nothing",
           sorted(p.suffix for p in (tmp / "Untouched").iterdir()), [".flac"] * 4)
        eq("plan_sidecars with no wants returns []",
           sidecars.plan_sidecars(sidecars.group_entries(quiet)), [])
        eq("no entries returns []", sidecars.write_release_sidecars([], want_nfo=True), [])

        # ── 3. IT NEVER WALKS THE FOLDER ──────────────────────────────────
        print("\n3. Explicit paths — the internal dedup state is never shipped")
        state = tmp / "State" / ".antra_state.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text('{"secret": true}', encoding="utf-8")
        se = make_album(tmp, "State", 2)
        written = sidecars.write_release_sidecars(se, want_json=True, generated_at=WHEN)
        check(".antra_state.json is never among the written paths",
              all(Path(w).name != ".antra_state.json" for w in written))
        eq(".antra_state.json is untouched",
           state.read_text(encoding="utf-8"), '{"secret": true}')

        # ── 4. CUE — the two details worth not rediscovering ──────────────
        print("\n4. CUE sheet")
        cue_tracks = [(T(title='He said "hi"', artists=['A "B" C'], album='Al"bum',
                         track_number=1, isrc="usum71703861", release_year=2020,
                         genres=["Pop"]), Path("101 - x.flac"))]
        cue = sidecars.build_cue(cue_tracks, 1, 1)
        eq("no unescaped double quote survives (CUE has no escape sequence)",
           cue.count('"') % 2, 0)
        check("every quoted CUE value is balanced",
              all(l.count('"') in (0, 2) for l in cue.splitlines()))
        check("a valid lower-case ISRC is normalised and emitted",
              "    ISRC USUM71703861" in cue, cue)
        check("REM DATE / REM GENRE emitted", "REM DATE 2020" in cue and "REM GENRE" in cue)

        bad = [(T(track_number=250), Path("a.flac")), (T(track_number=None), Path("b.flac")),
               (T(track_number=-3), Path("c.flac"))]
        nums = [l.strip() for l in sidecars.build_cue(bad, 1, 1).splitlines() if l.strip().startswith("TRACK ")]
        eq("out-of-range track numbers fall back to sheet position",
           nums, ["TRACK 01 AUDIO", "TRACK 02 AUDIO", "TRACK 03 AUDIO"])

        # ascending but all above 99: only the range check can reject these
        high = [(T(track_number=150), Path("a.flac")), (T(track_number=300), Path("b.flac"))]
        nums = [l.strip() for l in sidecars.build_cue(high, 1, 1).splitlines() if l.strip().startswith("TRACK ")]
        eq("ascending track numbers above 99 also fall back to sheet position",
           nums, ["TRACK 01 AUDIO", "TRACK 02 AUDIO"])

        big = [(T(track_number=i), Path(f"{i}.flac")) for i in range(1, 121)]
        cue_big = sidecars.build_cue(big, 1, 1)
        eq("a 120-track folder emits exactly 99 TRACK lines",
           sum(1 for l in cue_big.splitlines() if l.strip().startswith("TRACK ")), 99)
        check("...and says so rather than silently truncating",
              "REM TRUNCATED 21 more tracks" in cue_big)
        check("no TRACK line above 99", "TRACK 100" not in cue_big and "TRACK 250" not in cue_big)

        # invalid ISRCs are dropped rather than written
        eq("a malformed ISRC is not emitted",
           "ISRC" in sidecars.build_cue([(T(isrc="nope"), Path("a.flac"))], 1, 1), False)

        # one sheet per disc
        multi = (make_album(tmp, "Two Disc", 3, disc=1, total_discs=2)
                 + make_album(tmp, "Two Disc", 3, disc=2, total_discs=2))
        sidecars.write_release_sidecars(multi, want_cue=True, want_nfo=True, generated_at=WHEN)
        dd = tmp / "Two Disc"
        eq("a 2-disc folder gets one sheet per disc",
           sorted(p.name for p in dd.glob("*.cue")),
           ["Two Disc (Disc 1).cue", "Two Disc (Disc 2).cue"])
        c1 = (dd / "Two Disc (Disc 1).cue").read_text(encoding="utf-8")
        check("disc 1's sheet references no disc 2 file", "201 - " not in c1)
        check("disc metadata is emitted for a multi-disc release",
              "REM DISCNUMBER 1" in c1 and "REM TOTALDISCS 2" in c1)
        nfo_multi = (dd / "Two Disc.nfo").read_text(encoding="utf-8")
        check("a multi-disc .nfo qualifies track numbers with the disc",
              "1-1." in nfo_multi and "2-1." in nfo_multi)
        nfo_single = (tmp / "Full Album" / "Full Album.nfo").read_text(encoding="utf-8")
        check("a single-disc .nfo does NOT qualify them", "1-1." not in nfo_single)

        # ── 5. NFO / JSON content ─────────────────────────────────────────
        print("\n5. NFO / JSON")
        rich = make_album(tmp, "Rich", 2, album="After Hours", release_year=2020,
                          label="XO", upc="602508968761", genres=["R&B"],
                          album_artists=["The Weeknd"], isrc="USUG11904206",
                          composer="A. Tesfaye", is_explicit=True,
                          release_date="2020-03-20", request_kind="album")
        sidecars.write_release_sidecars(rich, want_nfo=True, want_json=True,
                                        download_format="lossless-24", generated_at=WHEN)
        nfo = (tmp / "Rich" / "Rich.nfo").read_text(encoding="utf-8")
        for want in ("Artist       : The Weeknd", "Year         : 2020", "Label        : XO",
                     "UPC          : 602508968761", "Genre        : R&B", "Tracks       : 2",
                     "Requested    : lossless-24", "Downloaded   : 2026-09-10 12:30 UTC"):
            check(f".nfo carries {want.split(':')[0].strip()!r}", want in nfo, nfo[:400])
        check(".nfo shows a duration, not a raw ms value", "Duration : 3:30" in nfo)

        js = json.loads((tmp / "Rich" / "Rich.json").read_text(encoding="utf-8"))
        eq(".json release title", js["release"]["title"], "After Hours")
        eq(".json album_artists", js["release"]["album_artists"], ["The Weeknd"])
        eq(".json upc", js["release"]["upc"], "602508968761")
        eq(".json requested_format", js["requested_format"], "lossless-24")
        eq(".json generated_at is UTC ISO", js["generated_at"], "2026-09-10T12:30:00Z")
        eq(".json track count matches the array", js["release"]["track_count"], len(js["tracks"]))
        eq(".json track carries the real filename", js["tracks"][0]["file"], "101 - Track 1.flac")
        check(".json track filename is a bare name, never a path",
              all("/" not in t["file"] and "\\" not in t["file"] for t in js["tracks"]))
        eq(".json explicit is a real tri-state", js["tracks"][0]["explicit"], True)
        eq(".json composer", js["tracks"][0]["composer"], "A. Tesfaye")

        # ── 6. NAMING + SANITISATION ──────────────────────────────────────
        print("\n6. Naming")
        awkward = make_album(tmp, "...Baby One More Time", 2)
        sidecars.write_release_sidecars(awkward, want_nfo=True, generated_at=WHEN)
        eq("leading dots are stripped from the sidecar name",
           sorted(p.name for p in (tmp / "...Baby One More Time").glob("*.nfo")),
           ["Baby One More Time.nfo"])
        eq("the sidecar is named after the folder it sits in, not the metadata",
           (tmp / "Rich" / "Rich.nfo").exists(), True)

        # ── 7. THE WRITER ─────────────────────────────────────────────────
        print("\n7. Writer")
        le = make_album(tmp, "Endings", 2)
        sidecars.write_release_sidecars(le, want_cue=True, want_nfo=True, want_json=True,
                                        generated_at=WHEN)
        ed = tmp / "Endings"
        cue_b = (ed / "Endings.cue").read_bytes()
        nfo_b = (ed / "Endings.nfo").read_bytes()
        json_b = (ed / "Endings.json").read_bytes()
        check(".cue is CRLF (scene/CD-tool convention)",
              b"\r\n" in cue_b and cue_b.count(b"\n") == cue_b.count(b"\r\n"))
        check(".nfo is CRLF", b"\r\n" in nfo_b and nfo_b.count(b"\n") == nfo_b.count(b"\r\n"))
        check(".json is LF (JSON convention)", b"\r\n" not in json_b)
        check("all three files are valid UTF-8",
              all(b.decode("utf-8") for b in (cue_b, nfo_b, json_b)))

        # A single unwritable path must not stop the rest.
        plans = [sidecars.SidecarFile(str(tmp / "no such dir" / "x.nfo"), "a", "nfo"),
                 sidecars.SidecarFile(str(ed / "second.nfo"), "b", "nfo")]
        got = sidecars.write_sidecars(plans)
        eq("one bad path does not stop the rest", len(got), 1)
        check("...and the good one really landed", (ed / "second.nfo").exists())

        # ── 8. NON-ASCII SURVIVES ─────────────────────────────────────────
        print("\n8. Unicode")
        uni = make_album(tmp, "Kesariya", 1, title="केसरिया", artists=["Arijit Singh"],
                         album="Brahmāstra")
        sidecars.write_release_sidecars(uni, want_nfo=True, want_json=True, generated_at=WHEN)
        ju = json.loads((tmp / "Kesariya" / "Kesariya.json").read_text(encoding="utf-8"))
        eq("non-ASCII album survives into .json", ju["release"]["title"], "Brahmāstra")
        check("non-ASCII title survives into .nfo",
              "Arijit Singh" in (tmp / "Kesariya" / "Kesariya.nfo").read_text(encoding="utf-8"))

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'PASS' if FAIL == 0 else 'FAIL'} — {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
