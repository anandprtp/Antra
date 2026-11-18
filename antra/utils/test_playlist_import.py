"""v2.0.0 PORT-3 - Python half of the playlist-file importer.

Drives the real module and the real ``json_cli._run_playlist_import`` against a
real LibraryOrganizer in a temp dir; only the download service is faked.

Run: python antra/utils/test_playlist_import.py [repo-root]
"""

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from types import SimpleNamespace

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

import antra.utils.playlist_import as pi  # noqa: E402
from antra.utils.organizer import LibraryOrganizer  # noqa: E402

assert os.path.abspath(pi.__file__).startswith(ROOT), f"wrong tree: {pi.__file__}"

passed = 0
failures = []


def check(label, cond, detail=""):
    global passed
    if cond:
        passed += 1
    else:
        failures.append(f"{label} {detail}".rstrip())


def row(**kw):
    base = {"title": "Song", "artists": ["Artist"]}
    base.update(kw)
    return base


# 1 -- row validation ----------------------------------------------------------
check("valid row kept", pi.sanitize_row(row()) is not None)
check("missing title dropped", pi.sanitize_row(row(title="")) is None)
check("whitespace title dropped", pi.sanitize_row(row(title="   ")) is None)
check("non-string title dropped", pi.sanitize_row(row(title=123)) is None)
check("artists not a list dropped", pi.sanitize_row(row(artists="Drake")) is None)
check("empty artists dropped", pi.sanitize_row(row(artists=["", "  "])) is None)
check("non-dict row dropped", pi.sanitize_row(["Song", "Artist"]) is None)
s = pi.sanitize_row(row(title="A" * 900, artists=[f"a{i}" for i in range(20)] + [None, 5]))
check("title capped at 500", len(s["title"]) == 500)
check("artists capped at 12, non-strings ignored", s["artists"] == [f"a{i}" for i in range(12)])
s = pi.sanitize_row(row(title="Line1\nLine2\x00", artists=["A\tB"]))
check("control characters removed from title", s["title"] == "Line1 Line2", repr(s["title"]))
check("control characters removed from artist", s["artists"] == ["A B"], repr(s["artists"]))
s = pi.sanitize_row(row(track_number=True, disc_number=0, duration_ms=-5, release_year=1850))
check("bool is not a track number", s["track_number"] is None)
check("out-of-range ints become None, row kept",
      (s["disc_number"], s["duration_ms"], s["release_year"]) == (None, None, None))
s = pi.sanitize_row(row(track_number=7, disc_number=2, duration_ms=198973, release_year=2018))
check("in-range ints kept", (s["track_number"], s["disc_number"], s["duration_ms"], s["release_year"]) == (7, 2, 198973, 2018))
check("float duration dropped (not an int)", pi.sanitize_row(row(duration_ms=1.5))["duration_ms"] is None)
check("isrc normalised", pi.sanitize_row(row(isrc="us-cm5-18-00004"))["isrc"] == "USCM51800004")
check("malformed isrc dropped, row kept", pi.sanitize_row(row(isrc="NOPE"))["isrc"] is None)

rows, dropped = pi.sanitize_rows([row(), {"title": ""}, row(title="Two")])
check("sanitize_rows keeps good rows in order", [r["title"] for r in rows] == ["Song", "Two"])
check("sanitize_rows counts dropped", dropped == 1)
rows, dropped = pi.sanitize_rows([row(title=f"T{i}") for i in range(pi.MAX_TRACKS + 7)])
check("rows over MAX_TRACKS counted as dropped", len(rows) == pi.MAX_TRACKS and dropped == 7)
check("non-list payload -> nothing", pi.sanitize_rows({"title": "x"}) == ([], 0))
check("blank name -> default", pi.playlist_name("  ") == pi.DEFAULT_NAME)
check("name capped at 200", len(pi.playlist_name("n" * 999)) == 200)

# 2 -- TrackMetadata + the real organizer --------------------------------------
tracks = pi.tracks_from_rows([row(title="First"), row(title="Second"), row(title="Third", track_number=9)], "Road Trip")
check("playlist_position is 1..N in file order", [t.playlist_position for t in tracks] == [1, 2, 3])
check("request_kind playlist", all(t.request_kind == "playlist" for t in tracks))
check("playlist_name stamped", all(t.playlist_name == "Road Trip" for t in tracks))

with tempfile.TemporaryDirectory() as lib:
    org = LibraryOrganizer(lib)
    paths = [org.get_output_path(t) for t in tracks]
    names = [os.path.basename(p) for p in paths]
    check("files numbered by POSITION, not all 01 (v1.1.7 BUG-1 shape)",
          names[0].startswith("01") and names[1].startswith("02") and names[2].startswith("03"), str(names))
    check("all land in one playlist folder", len({os.path.dirname(p) for p in paths}) == 1)
    hostile = pi.tracks_from_rows(pi.sanitize_rows([row(title="../../../etc/passwd", artists=["../evil"])])[0],
                                  "../../outside")
    hp = os.path.abspath(org.get_output_path(hostile[0]))
    check("traversal-looking title and name stay inside the library", hp.startswith(os.path.abspath(lib) + os.sep), hp)

# 3 -- payload file ------------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    bad = os.path.join(d, "bad.json")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    try:
        pi.load_import_payload(bad)
        check("invalid JSON raises ValueError", False)
    except ValueError:
        check("invalid JSON raises ValueError", True)
    arr = os.path.join(d, "arr.json")
    with open(arr, "w", encoding="utf-8") as fh:
        json.dump([row()], fh)
    try:
        pi.load_import_payload(arr)
        check("non-object payload raises ValueError", False)
    except ValueError:
        check("non-object payload raises ValueError", True)

# 4 -- json_cli._run_playlist_import, real function, fake service --------------
import antra.json_cli as cli  # noqa: E402  (a real import - catches start-time NameErrors)

check("json_cli imported from this tree", os.path.abspath(cli.__file__).startswith(ROOT), cli.__file__)


class FakeService:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    # complete_playlist added by issue #75. Keeping it out of the double is what
    # made this suite fail when the real signature grew it, which is the point.
    def download_tracks(self, tracks, options=None, event_callback=None, organizer=None,
                        complete_playlist=False):
        self.calls.append((list(tracks), organizer, complete_playlist))
        if self.fail:
            raise RuntimeError("engine exploded")
        out = []
        for i, t in enumerate(tracks):
            status = "COMPLETED" if i == 0 else "FAILED"
            out.append(SimpleNamespace(status=SimpleNamespace(name=status), file_path=None,
                                       source_used="qobuz_mirror" if i == 0 else None))
        return out


def run_import(payload, service, raw=None):
    d = tempfile.mkdtemp()
    lib = tempfile.mkdtemp()
    path = os.path.join(d, "antra-import-x.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(raw if raw is not None else json.dumps(payload))
    cfg = SimpleNamespace(output_dir=lib, output_format="flac")
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._run_playlist_import(path, cfg, service, options=None)
    events = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip().startswith("{")]
    return events, path


svc = FakeService()
events, path = run_import({"name": "Road Trip", "source_file": "road.csv", "format": "csv",
                           "tracks": [row(title="One", isrc="USCM51800004"), {"title": ""}, row(title="Two")]}, svc)
types = [e["type"] for e in events]
check("temp payload file deleted after reading", not os.path.exists(path))
check("playlist_loaded emitted before the summary",
      "playlist_loaded" in types and types.index("playlist_loaded") < types.index("playlist_summary"), str(types))
loaded = next(e for e in events if e["type"] == "playlist_loaded")
check("playlist_loaded carries the validated rows in order", [t["title"] for t in loaded["tracks"]] == ["One", "Two"])
check("playlist_loaded title + PLAYLIST kind", loaded["title"] == "Road Trip" and loaded["content_type"] == "PLAYLIST")
check("service got TrackMetadata in file order with positions",
      [(t.title, t.playlist_position) for t in svc.calls[0][0]] == [("One", 1), ("Two", 2)])
check("service got a real LibraryOrganizer rooted at output_dir",
      isinstance(svc.calls[0][1], LibraryOrganizer))
# issue #75 — an imported file lists the WHOLE playlist, so the folder
# maintenance (renumber + orphan report) is allowed to run on it. Getting this
# False would silently disable both; getting it True on a partial run would
# offer the user their own library to delete.
check("import declares itself a complete playlist", svc.calls[0][2] is True)
summary = next(e for e in events if e["type"] == "playlist_summary")
check("summary url is file:<name>, not a link", summary["url"] == "file:road.csv", summary["url"])
check("summary counts", (summary["total"], summary["downloaded"], summary["failed"]) == (2, 1, 1))
check("summary sources", summary["sources"] == {"qobuz_mirror": 1})
check("dropped row reported in the log",
      any(e["type"] == "log" and "1 row(s) dropped" in e["message"] for e in events))

svc = FakeService()
events, path = run_import({"name": "", "tracks": [{"title": ""}]}, svc)
summary = next(e for e in events if e["type"] == "playlist_summary")
check("no usable tracks -> error summary, service never called", summary["error"] and not svc.calls, str(summary))
check("file deleted on the error path too", not os.path.exists(path))

events, path = run_import(None, FakeService(), raw="{broken")
summary = next(e for e in events if e["type"] == "playlist_summary")
check("corrupt payload -> error summary, no traceback", bool(summary["error"]))
check("corrupt payload file still deleted", not os.path.exists(path))

events, _ = run_import({"name": "X", "tracks": [row()]}, FakeService(fail=True))
summary = next(e for e in events if e["type"] == "playlist_summary")
check("engine exception -> error summary carrying the message", "engine exploded" in (summary["error"] or ""))

print(f"\n{passed} passed, {len(failures)} failed")
for f in failures:
    print("  FAIL " + f)
sys.exit(1 if failures else 0)
