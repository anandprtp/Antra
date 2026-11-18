"""v2.0.0 DMX-3 — filename template tokens.

Drives the REAL build_context / render_template / LibraryOrganizer and asserts
on rendered strings and on files that land on disk. The pre-DMX-3 module is
loaded side by side so "the 13 existing tokens are unchanged" is MEASURED.

Run:  python antra/utils/test_template_tokens.py [tree-root]
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import antra_shared.filename_prefs as fp  # noqa: E402
from antra.core.models import TrackMetadata  # noqa: E402
from antra.utils.organizer import LibraryOrganizer  # noqa: E402

assert str(Path(fp.__file__).resolve()).startswith(str(ROOT)), fp.__file__

FAILS: list[str] = []
PASSES = 0


def check(label: str, ok: bool, detail: object = "") -> None:
    global PASSES
    if ok:
        PASSES += 1
    else:
        FAILS.append(f"{label} :: {detail}")
        print(f"  FAIL  {label} :: {detail}")


def prefs(**kw):
    return fp.migrate_legacy_templates(kw)


OLD_TOKENS = ["title", "artist", "album_artist", "album", "year", "track", "disc",
              "genre", "composer", "isrc", "codec", "bitrate", "quality"]
NEW_TOKENS = ["upc", "label", "explicit", "track_total", "disc_total", "date", "playlist",
              "position", "owner", "main_artists", "feat_artists", "all_artists", "today",
              "type", "album_id", "track_id"]


def tracks() -> list[TrackMetadata]:
    return [
        TrackMetadata(title="Come Together", artists=["The Beatles"], album="Abbey Road",
                      album_artists=["The Beatles"], release_year=1969, release_date="1969-09-26",
                      track_number=1, disc_number=1, total_tracks=17, total_discs=1,
                      isrc="GBAYE0601690", upc="0602547670342", label="Apple Corps",
                      is_explicit=False, album_id="0ETFjACtuP2ADo6LFhL6HN",
                      spotify_id="2EqlS6tkEnglzr7tkKAAYD", source_service="spotify",
                      request_kind="album", genres=["Rock"], composer="Lennon-McCartney"),
        TrackMetadata(title="Type Shit", artists=["Future", "Metro Boomin", "Travis Scott", "Playboi Carti"],
                      album="WE DON'T TRUST YOU", release_date="2024-03-22T00:00:00Z",
                      track_number=4, disc_number=1, total_tracks=17, is_explicit=True,
                      tidal_track_id="352641778", spotify_id="SPOT", source_service="tidal",
                      request_kind="track"),
        TrackMetadata(title="Raindrops (Insane) [with Travis Scott]", artists=["Metro Boomin", "Travis Scott"],
                      album="Heroes & Villains", release_year=2022, track_number=7, disc_number=2,
                      total_discs=2, playlist_name="Road / Trip", playlist_owner="anand",
                      playlist_position=3, request_kind="playlist"),
        TrackMetadata(title="Drake, Drake Song (feat. 21 Savage)", artists=["Drake", "drake", "21 Savage"],
                      album="Her Loss", album_artists=["Drake", "21 Savage"], release_date="2022-11",
                      track_number=12, disc_number=1, apple_music_id="AM1", source_service="apple"),
        TrackMetadata(title="Unknown", artists=[], album="Unknown Album"),
    ]


PREF_SETS = [
    prefs(),
    prefs(featured_artists_mode="move", title_casing="upper", multi_disc_handling="offset"),
    prefs(track_number_padding=3, multi_disc_handling="track_only", remove_duplicate_artists=False),
]

# ── 1. regression: the 13 existing tokens and the default renders ─────────────
print("1. regression vs pre-DMX-3 module")
bak = ROOT / "antra_shared" / "filename_prefs.py.pre-dmx3.bak"
if bak.exists():
    from importlib.machinery import SourceFileLoader
    spec = importlib.util.spec_from_file_location("fp_old", bak, loader=SourceFileLoader("fp_old", str(bak)))
    old = importlib.util.module_from_spec(spec)
    sys.modules["fp_old"] = old  # @dataclass resolves cls.__module__ through sys.modules
    spec.loader.exec_module(old)
    diffs = 0
    for t in tracks():
        for p in PREF_SETS:
            new_ctx = fp.build_context(t, p, track_number=t.track_number, disc_number=t.disc_number)
            old_ctx = old.build_context(t, p, track_number=t.track_number, disc_number=t.disc_number)
            for k in OLD_TOKENS:
                if new_ctx[k] != old_ctx[k]:
                    diffs += 1
            for fn in ("build_single_track_stem", "build_album_zip_name", "build_folder_path"):
                if getattr(fp, fn)(t, p) != getattr(old, fn)(t, p):
                    diffs += 1
            if fp.build_track_stem(t, p, track_number=t.track_number) != old.build_track_stem(t, p, track_number=t.track_number):
                diffs += 1
    check("13 old tokens + 4 default renders byte-identical (5 tracks x 3 prefs)", diffs == 0, diffs)
    check("old module had none of the new tokens", not any(k in old.build_context(tracks()[0], prefs()) for k in NEW_TOKENS))
else:
    check("pre-DMX-3 backup present for regression", False, bak)

# ── 2. registry / key coverage ───────────────────────────────────────────────
print("2. registry")
check("AVAILABLE_TEMPLATE_TOKENS = old + new, in order", fp.AVAILABLE_TEMPLATE_TOKENS == OLD_TOKENS + NEW_TOKENS,
      fp.AVAILABLE_TEMPLATE_TOKENS)
ctx0 = fp.build_context(tracks()[0], prefs())
check("build_context emits exactly the registry", set(ctx0) == set(fp.AVAILABLE_TEMPLATE_TOKENS),
      set(ctx0) ^ set(fp.AVAILABLE_TEMPLATE_TOKENS))
check("web preview context emits exactly the registry",
      set(fp.build_web_preview_context()) == set(fp.AVAILABLE_TEMPLATE_TOKENS))
check("rejected tokens are not offered", not {"bpm", "artist_id", "size"} & set(fp.AVAILABLE_TEMPLATE_TOKENS))

# ── 3. values ────────────────────────────────────────────────────────────────
print("3. values")
T = tracks()
c0 = fp.build_context(T[0], prefs())
for k, v in {"upc": "0602547670342", "label": "Apple Corps", "explicit": "Clean", "track_total": "17",
             "disc_total": "1", "date": "1969-09-26", "type": "album", "album_id": "0ETFjACtuP2ADo6LFhL6HN",
             "track_id": "2EqlS6tkEnglzr7tkKAAYD", "playlist": "", "position": "", "owner": ""}.items():
    check(f"beatles {k}", c0[k] == v, c0[k])
c1 = fp.build_context(T[1], prefs())
check("explicit True -> Explicit", c1["explicit"] == "Explicit", c1["explicit"])
check("timestamp date cut to YYYY-MM-DD", c1["date"] == "2024-03-22", c1["date"])
check("track_id follows source_service (tidal id, not spotify)", c1["track_id"] == "352641778", c1["track_id"])
check("type track", c1["type"] == "track", c1["type"])
check("unknown explicit -> empty, never Clean", fp.build_context(T[2], prefs())["explicit"] == "")
c2 = fp.build_context(T[2], prefs())
check("playlist name", c2["playlist"] == "Road / Trip", c2["playlist"])
check("owner", c2["owner"] == "anand", c2["owner"])
check("position padded to track_number_padding", c2["position"] == "03", c2["position"])
check("position honours padding=3", fp.build_context(T[2], prefs(track_number_padding=3))["position"] == "003")
check("type playlist", c2["type"] == "playlist", c2["type"])
check("year-only fallback for date", c2["date"] == "2022", c2["date"])
c3 = fp.build_context(T[3], prefs())
check("YYYY-MM precision kept, not padded", c3["date"] == "2022-11", c3["date"])
check("track_id apple service", c3["track_id"] == "AM1", c3["track_id"])
c4 = fp.build_context(T[4], prefs())
check("empty track: all new tokens empty except today",
      all(c4[k] == "" for k in NEW_TOKENS if k != "today"), {k: c4[k] for k in NEW_TOKENS})
check("garbage release_date falls back to empty",
      fp.build_context(TrackMetadata(title="x", artists=["a"], album="b", release_date="soon"), prefs())["date"] == "")

# ── 4. artist roles — from title credits only, never list position ───────────
print("4. artist roles")
check("[with X] credit -> feat", c2["feat_artists"] == "Travis Scott", c2["feat_artists"])
check("main excludes the credited name", c2["main_artists"] == "Metro Boomin", c2["main_artists"])
check("all = main + feat, deduped", c2["all_artists"] == "Metro Boomin, Travis Scott", c2["all_artists"])
check("no title credit -> no guessed featured", c1["feat_artists"] == "", c1["feat_artists"])
check("no title credit -> every artist main", c1["main_artists"].startswith("Future, Metro Boomin"), c1["main_artists"])
check("dup artists removed before roles (DMX-2 default)", c3["main_artists"] == "Drake", c3["main_artists"])
check("feat 21 Savage matched case-insensitively", c3["feat_artists"] == "21 Savage", c3["feat_artists"])
mv = fp.build_context(T[1], prefs(featured_artists_mode="move"))
check("move mode: main = primary", mv["main_artists"] == "Future", mv["main_artists"])
check("move mode: feat = the moved artists", mv["feat_artists"] == "Metro Boomin, Travis Scott & Playboi Carti"
      or mv["feat_artists"].startswith("Metro Boomin"), mv["feat_artists"])
check("roles never mutate the track", T[1].artists == ["Future", "Metro Boomin", "Travis Scott", "Playboi Carti"]
      and T[1].title == "Type Shit")

# ── 5. rendering ─────────────────────────────────────────────────────────────
print("5. rendering")
r = fp.render_template("{title} ({explicit})", fp.build_context(T[2], prefs()), prefs())
check("empty token's brackets collapse", r == "Raindrops (Insane) [with Travis Scott]", r)
r = fp.render_template("{position} - {title}", c4, prefs())
check("empty position's dash collapses", r == "Unknown", r)
path = fp.render_template("{playlist}/{position} - {title}", c2, prefs(), is_path=True)
check("slash inside {playlist} does not nest a folder", path.count("/") == 1 and path.startswith("Road _ Trip/"), path)
r = fp.render_template("{date} {label} [{upc}]", c0, prefs())
check("release tokens render", r == "1969-09-26 Apple Corps [0602547670342]", r)
r = fp.render_template("{TRACK_TOTAL}", c0, prefs())
check("tokens case-insensitive", r == "17", r)

# ── 6. {today} pinned per run ────────────────────────────────────────────────
print("6. today")
check("no run date -> current local date", c4["today"] == date.today().isoformat(), c4["today"])
p = prefs()
p[fp.RUN_DATE_KEY] = "2001-01-01"
check("run date key wins", fp.build_context(T[0], p)["today"] == "2001-01-01")
check("migrate_legacy_templates preserves the run date key",
      fp.migrate_legacy_templates(p).get(fp.RUN_DATE_KEY) == "2001-01-01")

# ── 7. real LibraryOrganizer ─────────────────────────────────────────────────
print("7. organizer")
tmp = Path(tempfile.mkdtemp(prefix="dmx3_"))
try:
    org = LibraryOrganizer(str(tmp), full_albums=True,
                           folder_structure_template="{album_artist}/{date} - {album} [{label}]",
                           album_track_filename_template="{track}-{track_total} {title} ({explicit})")
    check("organizer stamps a run date", org.filename_preferences.get(fp.RUN_DATE_KEY) == date.today().isoformat())
    org.filename_preferences[fp.RUN_DATE_KEY] = "1999-12-31"
    out = org.get_output_path(T[0]) + ".flac"
    rel = Path(out).relative_to(tmp).as_posix()
    check("album path uses new tokens", rel == "The Beatles/1969-09-26 - Abbey Road [Apple Corps]/01-17 Come Together (Clean).flac", rel)
    # A REAL tagged FLAC: is_already_downloaded correctly refuses a file whose
    # tags cannot prove it is this track (BUG-3), so 4 untagged bytes would fail.
    import subprocess
    from mutagen.flac import FLAC
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                    "-t", "1", "-y", out], check=True)
    f = FLAC(out)
    f["title"] = T[0].title; f["artist"] = T[0].artists; f["album"] = T[0].album; f["isrc"] = T[0].isrc
    f.save()
    check("resume: same track finds its file by exact path (full_albums)", bool(org.is_already_downloaded(T[0])))
    org2 = LibraryOrganizer(str(tmp), full_albums=True, folder_structure_template="{today}/{album}")
    org2.filename_preferences[fp.RUN_DATE_KEY] = "1999-12-31"
    a = org2.get_output_path(T[0]); b = org2.get_output_path(T[3])
    check("{today} folder identical for every track of a run", Path(a).parent.parent == Path(b).parent.parent
          and Path(a).parent.parent.name == "1999-12-31", (a, b))
    plist = LibraryOrganizer(str(tmp), album_track_filename_template="{position} {title} - {owner}")
    pout = Path(plist.get_output_path(T[2])).name
    check("playlist filename via real organizer", pout == "03 Raindrops (Insane) [with Travis Scott] - anand", pout)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ── 8. UI drift guard ────────────────────────────────────────────────────────
print("8. UI drift")
svelte = (ROOT / "antra-wails" / "frontend" / "src" / "App.svelte").read_text(encoding="utf-8")
m = re.search(r"const TEMPLATE_TOKEN_GROUPS[^=]*=\s*\[(.*?)\n\s*\];", svelte, re.S)
ui_tokens: list[str] = []
if m:
    for grp in re.findall(r"tokens:\s*\[([^\]]*)\]", m.group(1)):
        ui_tokens += re.findall(r"'([a-z_]+)'", grp)
check("UI chip groups found", bool(ui_tokens))
check("UI chips == registry (no missing, no extra, no dup)",
      sorted(ui_tokens) == sorted(fp.AVAILABLE_TEMPLATE_TOKENS) and len(ui_tokens) == len(set(ui_tokens)),
      set(ui_tokens) ^ set(fp.AVAILABLE_TEMPLATE_TOKENS))
demo = re.search(r"const TEMPLATE_DEMO = \{(.*?)\n\s*\};", svelte, re.S)
demo_keys = set(re.findall(r"^\s*([a-z_]+):", demo.group(1), re.M)) if demo else set()
check("TEMPLATE_DEMO covers every token", demo_keys == set(fp.AVAILABLE_TEMPLATE_TOKENS),
      demo_keys ^ set(fp.AVAILABLE_TEMPLATE_TOKENS))
check("old inline 13-chip literal is gone", "'{quality}'] as tok" not in svelte)

print(f"\n{PASSES} passed, {len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
