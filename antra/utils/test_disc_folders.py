"""v2.0.0 DMX-4 — per-disc folders (multi_disc_handling: folder).

Drives the REAL filename_prefs / LibraryOrganizer / FileTagger and asserts on
paths and on files that land on disk. Every OTHER multi-disc mode is compared
against a pre-DMX-4 copy of the tree (built by reversing only the DMX-4 edits),
so "prefix / dash / track_only / offset are unchanged" is measured.

Run:  python antra/utils/test_disc_folders.py [tree-root]
"""
from __future__ import annotations

import ast
import importlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
FAILS: list[str] = []
PASSES = 0


def check(label: str, ok: bool, detail: object = "") -> None:
    global PASSES
    if ok:
        PASSES += 1
    else:
        FAILS.append(f"{label} :: {detail}")
        print(f"  FAIL  {label} :: {detail}")


# ── pre-DMX-4 tree (for regression) ──────────────────────────────────────────
REVERSE = {
    "antra_shared/filename_prefs.py": [
        ('    # "folder" (v2.0.0 DMX-4): the disc lives in a CD<n>/ subfolder, so the\n'
         '    # number in the filename is the plain track number.\n'
         '    if mode in ("track_only", "folder"):\n', '    if mode == "track_only":\n'),
    ],
    "antra/utils/organizer.py": [
        ("        folder = self._with_disc_folder(self._album_folder(track), track)\r\n"
         "        filename = self._format_filename(track, track.track_number)\r\n",
         "        folder = self._album_folder(track)\r\n"
         "        filename = self._format_filename(track, track.track_number)\r\n"),
        ("            folder = self._with_disc_folder(self._album_folder(track), track)\r\n",
         "            folder = self._album_folder(track)\r\n"),
    ],
}


def build_old_tree() -> Path:
    base = Path(tempfile.mkdtemp(prefix="dmx4_old_"))
    for pkg in ("antra", "antra_shared"):
        shutil.copytree(ROOT / pkg, base / pkg, ignore=shutil.ignore_patterns("*.bak", "__pycache__"))
    for rel, pairs in REVERSE.items():
        p = base / rel
        data = p.read_bytes().decode("utf-8")
        for new, old in pairs:
            assert data.count(new) == 1, (rel, new[:60], data.count(new))
            data = data.replace(new, old)
        p.write_bytes(data.encode("utf-8"))
    return base


def load_tree(tree: Path):
    for name in list(sys.modules):
        if name == "antra" or name.startswith(("antra.", "antra_shared")):
            del sys.modules[name]
    sys.path.insert(0, str(tree))
    try:
        fp = importlib.import_module("antra_shared.filename_prefs")
        org = importlib.import_module("antra.utils.organizer")
        models = importlib.import_module("antra.core.models")
    finally:
        sys.path.pop(0)
    assert str(Path(fp.__file__).resolve()).startswith(str(tree.resolve())), fp.__file__
    return fp, org, models


def tracks(M):
    T = M.TrackMetadata
    return {
        "d1": T(title="Hey Jude", artists=["The Beatles"], album="1967-1970", album_artists=["The Beatles"],
                release_year=1973, track_number=5, disc_number=1, total_discs=2, request_kind="album"),
        "d2": T(title="Let It Be", artists=["The Beatles"], album="1967-1970", album_artists=["The Beatles"],
                release_year=1973, track_number=5, disc_number=2, total_discs=2, request_kind="album"),
        "single_disc": T(title="Come Together", artists=["The Beatles"], album="Abbey Road",
                         album_artists=["The Beatles"], release_year=1969, track_number=1, disc_number=1,
                         total_discs=1, request_kind="album"),
        "no_total_d2": T(title="Across", artists=["X"], album="Y", release_year=2000, track_number=3,
                         disc_number=2, request_kind="album"),
        "no_total_d1": T(title="Near", artists=["X"], album="Y", release_year=2000, track_number=3,
                         disc_number=1, request_kind="album"),
        "d10": T(title="Ten", artists=["X"], album="Box", release_year=2000, track_number=1,
                 disc_number=10, total_discs=12, request_kind="album"),
        "playlist": T(title="Let It Be", artists=["The Beatles"], album="1967-1970", release_year=1973,
                      track_number=5, disc_number=2, total_discs=2, playlist_name="Mix", playlist_position=7,
                      request_kind="playlist"),
        "single_req": T(title="Let It Be", artists=["The Beatles"], album="1967-1970",
                        album_artists=["The Beatles"], release_year=1973, track_number=5, disc_number=2,
                        total_discs=2, request_kind="track"),
    }


old_tree = build_old_tree()
try:
    fp_old, org_old, M_old = load_tree(old_tree)
    old_mods = (fp_old, org_old, M_old)
    fp, orgmod, M = load_tree(ROOT)
    LibraryOrganizer = orgmod.LibraryOrganizer
    TT = tracks(M)
    tmp = Path(tempfile.mkdtemp(prefix="dmx4_"))
    try:
        def rel(org, t):
            return Path(org.get_output_path(t)).relative_to(org.root).as_posix()

        # ── 1. pure helper ──────────────────────────────────────────────────
        print("1. disc_folder_name")
        P = lambda m: fp.migrate_legacy_templates({"multi_disc_handling": m})
        F = P("folder")
        check("disc 1 of 2 -> CD1", fp.disc_folder_name(TT["d1"], F) == "CD1", fp.disc_folder_name(TT["d1"], F))
        check("disc 2 of 2 -> CD2", fp.disc_folder_name(TT["d2"], F) == "CD2")
        check("single-disc album -> None", fp.disc_folder_name(TT["single_disc"], F) is None)
        check("disc 2, total unknown -> CD2", fp.disc_folder_name(TT["no_total_d2"], F) == "CD2")
        check("disc 1, total unknown -> None", fp.disc_folder_name(TT["no_total_d1"], F) is None)
        check("disc 10 -> CD10 (no padding)", fp.disc_folder_name(TT["d10"], F) == "CD10")
        check("disc None, total 3 -> CD1",
              fp.disc_folder_name(M.TrackMetadata(title="a", artists=["b"], album="c", total_discs=3), F) == "CD1")
        for mode in ("prefix", "dash", "track_only", "offset", ""):
            check(f"mode {mode!r} -> never a disc folder", fp.disc_folder_name(TT["d2"], P(mode)) is None)
        check("is_disc_folder_name CD2", fp.is_disc_folder_name("CD2"))
        for bad in ("cd2", "CD", "CD 2", "Disc 2", "CD2x", ""):
            check(f"is_disc_folder_name rejects {bad!r}", not fp.is_disc_folder_name(bad))
        check("migration keeps 'folder'", fp.migrate_legacy_templates({"multi_disc_handling": "folder"})
              ["multi_disc_handling"] == "folder")

        # ── 2. organizer paths in folder mode ───────────────────────────────
        print("2. organizer, folder mode")
        o = LibraryOrganizer(str(tmp / "lib"), full_albums=True, multi_disc_handling="folder")
        r1, r2 = rel(o, TT["d1"]), rel(o, TT["d2"])
        check("disc 1 path", r1 == "The Beatles/1967-1970 (1973)/CD1/05 - Hey Jude", r1)
        check("disc 2 path", r2 == "The Beatles/1967-1970 (1973)/CD2/05 - Let It Be", r2)
        check("same track number on two discs does not collide", r1.rsplit("/", 1)[0] != r2.rsplit("/", 1)[0])
        rs = rel(o, TT["single_disc"])
        check("single-disc album unchanged", rs == "The Beatles/Abbey Road (1969)/01 - Come Together", rs)
        rp = rel(o, TT["playlist"])
        check("playlist track never gets a disc folder", "CD" not in rp.split("/")[:-1], rp)
        rq = rel(o, TT["single_req"])
        check("single track in album folder gets the SAME disc folder as the album",
              rq.rsplit("/", 1)[0] == r2.rsplit("/", 1)[0], rq)
        of = LibraryOrganizer(str(tmp / "lib_file"), full_albums=True, multi_disc_handling="folder",
                              single_track_structure="file")
        check("single_track_structure=file stays at root", "/" not in rel(of, TT["single_req"]), rel(of, TT["single_req"]))
        ofr = LibraryOrganizer(str(tmp / "lib_flatroot"), full_albums=True, multi_disc_handling="folder",
                               flat_library_root=True)
        rfr = rel(ofr, TT["d2"])
        check("flat library root: no CD folder at the root", "/" not in rfr, rfr)
        ocust = LibraryOrganizer(str(tmp / "lib_cust"), full_albums=True, multi_disc_handling="folder",
                                 folder_structure_template="{album} ({year})",
                                 album_track_filename_template="{track}. {title} [{disc}]")
        rc = rel(ocust, TT["d2"])
        check("custom templates compose with the disc folder", rc == "1967-1970 (1973)/CD2/05. Let It Be [2]", rc)
        o3 = LibraryOrganizer(str(tmp / "lib_pad"), full_albums=True, multi_disc_handling="folder",
                              track_number_padding=3)
        check("padding still applied", rel(o3, TT["d2"]).endswith("/CD2/005 - Let It Be"), rel(o3, TT["d2"]))

        # ── 3. release / artist folder helpers ──────────────────────────────
        print("3. release_folder_for / artist_folder_for")
        disc_file = Path(o.get_output_path(TT["d2"]) + ".flac")
        check("release_folder_for strips CD2", Path(o.release_folder_for(str(disc_file))).name == "1967-1970 (1973)",
              o.release_folder_for(str(disc_file)))
        plain_file = Path(o.get_output_path(TT["single_disc"]) + ".flac")
        check("release_folder_for leaves a non-disc parent alone",
              Path(o.release_folder_for(str(plain_file))).name == "Abbey Road (1969)")
        op = LibraryOrganizer(str(tmp / "lib_prefix"), full_albums=True, multi_disc_handling="prefix",
                              folder_structure_template="{album_artist}/{album}")
        cd_named = M.TrackMetadata(title="x", artists=["Artist"], album="CD1", album_artists=["Artist"],
                                   track_number=1, disc_number=1, total_discs=1, request_kind="album")
        cd_file = op.get_output_path(cd_named) + ".flac"
        check("prefix mode: an album literally named CD1 is NOT stripped",
              Path(op.release_folder_for(cd_file)).name == "CD1", op.release_folder_for(cd_file))
        check("artist_folder_for from inside CD2 -> the artist folder",
              Path(o.artist_folder_for(str(disc_file)) or "").name == "The Beatles", o.artist_folder_for(str(disc_file)))
        check("artist_folder_for, single-disc -> the artist folder",
              Path(o.artist_folder_for(str(plain_file)) or "").name == "The Beatles")
        check("artist_folder_for, flat root -> None",
              ofr.artist_folder_for(ofr.get_output_path(TT["d2"]) + ".flac") is None)

        # ── 4. cover sidecar lands in the album folder, not CD<n>/ ──────────
        print("4. cover sidecar")
        tagger_mod = importlib.import_module("antra.utils.tagger")
        tg = tagger_mod.FileTagger.__new__(tagger_mod.FileTagger)
        tg.artwork = type("A", (), {"cover_image_template": tagger_mod.DEFAULT_COVER_TEMPLATE})()
        import threading
        tg._sidecar_lock = threading.Lock()
        seen = {}
        tg._fetch_raw_artwork = lambda url: b"img"
        tg._render_sidecar_stem = lambda tpl, default, track: "cover"
        tg._write_image_sidecars = lambda folder, stem, raw, fp_: seen.setdefault("folder", folder)
        TT["d2"].artwork_url = "https://example/a.jpg"
        tg.save_cover_art_sidecar(str(disc_file), TT["d2"], folder=o.release_folder_for(str(disc_file)))
        check("cover written to the album folder", Path(seen.get("folder", "")).name == "1967-1970 (1973)", seen)
        seen.clear()
        tg.save_cover_art_sidecar(str(plain_file), TT["d2"])
        check("no folder kwarg -> file's own parent (unchanged)", Path(seen.get("folder", "")).name == "Abbey Road (1969)", seen)

        eng = ast.parse((ROOT / "antra" / "core" / "engine.py").read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(eng) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "save_cover_art_sidecar"]
        good = [c for c in calls if any(k.arg == "folder" and "release_folder_for" in ast.unparse(k.value)
                                        for k in c.keywords)]
        check("engine: every cover call passes folder=release_folder_for(...)", len(calls) == 2 and len(good) == 2,
              (len(calls), len(good)))

        # ── 5. exact-path resume against a real tagged FLAC in CD2/ ─────────
        print("5. resume")
        disc_file.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                            "-t", "1", str(disc_file)], capture_output=True)
        if r.returncode == 0:
            from mutagen.flac import FLAC
            f = FLAC(str(disc_file))
            f["title"] = "Let It Be"; f["artist"] = "The Beatles"; f["album"] = "1967-1970"
            f["tracknumber"] = "5"; f["discnumber"] = "2"
            f.save()
            got = o.is_already_downloaded(TT["d2"])
            check("full_albums resume finds the file in CD2/", got and Path(got).resolve() == disc_file.resolve(), got)
            check("disc 1 track 5 is NOT treated as downloaded", o.is_already_downloaded(TT["d1"]) is None)
        else:
            check("ffmpeg available for resume test", False, r.stderr[:200])

        # ── 6. regression: every other mode identical to pre-DMX-4 ──────────
        print("6. regression vs pre-DMX-4 tree")
        diffs = []
        for mode in ("", "prefix", "dash", "track_only", "offset"):
            for pad in (2, 3):
                for key, t in TT.items():
                    for extra in ({}, {"single_track_structure": "file"}, {"flat_library_root": True},
                                  {"folder_structure_template": "{album} ({year})"}):
                        kw = dict(full_albums=True, multi_disc_handling=mode, track_number_padding=pad, **extra)
                        new_o = LibraryOrganizer(str(tmp / "rg_new"), **kw)
                        old_o = org_old.LibraryOrganizer(str(tmp / "rg_new"), **kw)
                        t_old = M_old.TrackMetadata(**t.__dict__)
                        a = Path(new_o.get_output_path(t)).relative_to(new_o.root).as_posix()
                        b = Path(old_o.get_output_path(t_old)).relative_to(old_o.root).as_posix()
                        if a != b:
                            diffs.append((mode, pad, key, extra, a, b))
                        if fp.build_track_stem(t, new_o.filename_preferences, track_number=t.track_number) != \
                                fp_old.build_track_stem(t_old, old_o.filename_preferences, track_number=t.track_number):
                            diffs.append((mode, pad, key, "stem"))
        check("5 modes x 2 paddings x 8 tracks x 4 layouts: paths + stems identical", not diffs, diffs[:3])
        probe = org_old.LibraryOrganizer(str(tmp / "rg_new"), full_albums=True, multi_disc_handling="folder")
        probe_path = Path(probe.get_output_path(M_old.TrackMetadata(**TT["d2"].__dict__))).as_posix()
        check("control: the pre-DMX-4 tree really has no CD folder (comparison is not self vs self)",
              "/CD2/" not in probe_path, probe_path)

        # ── 7. UI wiring ────────────────────────────────────────────────────
        print("7. UI")
        sv = (ROOT / "antra-wails" / "frontend" / "src" / "App.svelte").read_text(encoding="utf-8")
        check("select offers value=\"folder\"", sv.count('<option value="folder">') == 1)
        cat = (ROOT / "antra-wails" / "frontend" / "src" / "lib" / "settings-catalog.ts").read_text(encoding="utf-8")
        check("palette catalog indexes the multi-disc setting",
              "multi_disc_handling" in cat)
        # SF-3 moved the wording itself into the i18n catalogue, so the text the
        # palette searches now lives in en.json rather than settings-catalog.ts.
        loc = (ROOT / "antra-wails" / "frontend" / "src" / "locales" / "en.json").read_text(encoding="utf-8")
        check("the setting's description still mentions CD1/CD2", "CD1/CD2" in loc)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
finally:
    shutil.rmtree(old_tree, ignore_errors=True)

print(f"\n{PASSES} passed, {len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
