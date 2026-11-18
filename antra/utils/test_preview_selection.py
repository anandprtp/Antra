"""PV-1 (v2.0.0) — preview before download.

What this pins is the one thing that is easy to get wrong and invisible in the
UI: which POSITION a downloaded file claims on the release.

`enrich_tracks_for_download` finishes in `_stamp_disc_totals`, which renumbers a
group sequentially from 1 per disc on the assumption that the group IS the album.
That is right for a full download and actively wrong for a subset — picking
tracks 5 and 9 of Scorpion wrote `1-01 - God's Plan` and `2-01 - In My Feelings`,
mislabelling both files and colliding with the real track 1 of each disc. It was
caught on a live download, not by reading the code, which is why it is a test now.

The suite drives the real `_run_track_selection` with a stub service, so the
renumbering is simulated rather than reached over the network — the assertion is
on json_cli's behaviour around it, which is the part this feature owns.

Run: python antra/utils/test_preview_selection.py    (from the repo root)
"""
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from dataclasses import dataclass

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from antra.core.models import DownloadResult, DownloadStatus, TrackMetadata  # noqa: E402
from antra.json_cli import (  # noqa: E402
    _playlist_header,
    _preview_rows,
    _stamp_row_ids,
    _run_track_selection,
    _track_from_payload,
)

passed = 0
failures = []


def check(label, cond, detail=""):
    global passed
    if cond:
        passed += 1
    else:
        failures.append(label + (("\n      " + str(detail)) if detail else ""))


def eq(label, got, want):
    check(label, got == want, "got:  %r\n      want: %r" % (got, want))


# ── Fixtures ──────────────────────────────────────────────────────────────────

def album(n=25):
    """A two-disc album shaped like the Spotify fixture this was found on."""
    out = []
    for i in range(n):
        disc = 1 if i < 12 else 2
        num = i + 1 if disc == 1 else i - 11
        out.append(TrackMetadata(
            title="Track %d" % (i + 1),
            artists=["Drake"],
            album="Scorpion",
            album_id="1ATL5GLyefJaxhQzSPVrLX",
            spotify_id="sid%d" % i,
            isrc="ISRC%05d" % i,
            track_number=num,
            disc_number=disc,
            duration_ms=200000 + i,
            release_year=2018,
            release_date="2018-06-29",
            artwork_url="https://art/cover.jpg",
            request_kind="album",
        ))
    return out


class Cfg:
    output_format = "lossless"
    output_dir = ""
    library_mode = "smart_dedup"
    library_index_enabled = False
    library_index_db_path = ""


class StubService:
    """Stands in for AntraService.

    `enrich_tracks_for_download` reproduces exactly the renumbering that broke
    this — sequential from 1 per disc — so the test fails if json_cli stops
    undoing it for a subset.
    """

    def __init__(self):
        self.downloaded = None

    def enrich_tracks_for_download(self, tracks, url, options=None):
        per_disc = {}
        for t in tracks:
            d = t.disc_number or 1
            per_disc[d] = per_disc.get(d, 0) + 1
            t.track_number = per_disc[d]
        return tracks

    # complete_playlist added by issue #75 — recorded, because a strict subset
    # must NOT be treated as the whole playlist (it would offer every track the
    # selection left out for deletion as an "orphan").
    def download_tracks(self, tracks, options=None, event_callback=None, organizer=None,
                        complete_playlist=False):
        self.downloaded = list(tracks)
        self.complete_playlist = complete_playlist
        return [DownloadResult(track=t, status=DownloadStatus.COMPLETED,
                               source_used="tidal_mirror") for t in tracks]


def run_selection(tracks, header, url="https://open.spotify.com/album/1ATL5GLyefJaxhQzSPVrLX"):
    """Write a payload, run the real selection path, return (events, service)."""
    cfg = Cfg()
    with tempfile.TemporaryDirectory() as tmp:
        cfg.output_dir = os.path.join(tmp, "lib")
        os.makedirs(cfg.output_dir, exist_ok=True)
        path = os.path.join(tmp, "sel.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"url": url, "header": header,
                       "tracks": [_track_dict(t) for t in tracks]}, fh)
        svc = StubService()
        buf = io.StringIO()
        with redirect_stdout(buf):
            _run_track_selection(path, cfg, svc, options=None)
        events = []
        for line in buf.getvalue().splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
        payload_still_there = os.path.exists(path)
    return events, svc, payload_still_there


def _track_dict(t):
    from dataclasses import asdict
    return asdict(t)


def one(events, typ):
    for e in events:
        if e.get("type") == typ:
            return e
    return None


# ══ 1. the header helper ══════════════════════════════════════════════════════
print("1 — header")
full = album()
hdr = _playlist_header("https://open.spotify.com/album/x", full, Cfg())
eq("title comes from the album", hdr["title"], "Scorpion")
eq("track_count is the release size", hdr["track_count"], 25)
eq("quality badge follows output_format", hdr["quality_badge"], "LOSSLESS")
eq("artwork is carried", hdr["artwork_url"], "https://art/cover.jpg")

# A playlist's own cover and name must win over the first track's album art,
# or every playlist preview shows the first song's album.
pl = album(3)
for i, t in enumerate(pl):
    t.playlist_name = "Road Trip"
    t.playlist_artwork_url = "https://art/playlist.jpg"
    t.request_kind = "playlist"
    t.playlist_position = i + 1
    # Deliberately NOT equal to the playlist position: a playlist pulls tracks
    # from the middle of other albums, and numbering by these is the reported
    # confusion (3, 4, 5, then back to 1).
    t.track_number = (7, 4, 9)[i]
plhdr = _playlist_header("https://open.spotify.com/playlist/x", pl, Cfg())
eq("playlist name beats album name", plhdr["title"], "Road Trip")
eq("playlist cover beats album art", plhdr["artwork_url"], "https://art/playlist.jpg")

eq("empty tracklist does not explode", _playlist_header("u", [], Cfg())["track_count"], 0)

rows = _preview_rows(full[:2])
eq("rows carry what the UI renders",
   sorted(rows[0].keys()),
   sorted(["row_id", "artist", "title", "duration_ms", "isrc", "album",
           "track_number", "disc_number", "playlist_position",
           "artwork_url", "is_explicit"]))
eq("row artist is the joined string", rows[0]["artist"], "Drake")

# The preview numbers a playlist row by its playlist position, because album
# numbers restart at 1 on every release change and read as scrambled. This side
# only has to CARRY the field faithfully - both numbers reach the row, and the
# frontend picks between them on the release kind (measured live: an ordinary
# album fetch fills playlist_position too, so the field alone cannot decide).
pl_rows = _preview_rows(pl)
eq("a playlist row carries its playlist position",
   [r["playlist_position"] for r in pl_rows], [1, 2, 3])
eq("and keeps the album number underneath it",
   [r["track_number"] for r in pl_rows], [7, 4, 9])
eq("a track with no position carries None rather than 0",
   _preview_rows(full[:1])[0]["playlist_position"], None)
eq("an album row still carries its album track number", rows[0]["track_number"], 1)

# issue #74 — the preview screen and the selection job that follows it must
# address the SAME rows, so the id minted by --preview-url has to survive into
# the rows the webview renders. An unstamped track yields "" rather than None,
# so the frontend's makeRowKey() falls back to a positional key instead of
# building the string "row:undefined".
eq("an unstamped track yields an empty id, not a null", rows[0]["row_id"], "")
stamped = album(2)
_stamp_row_ids(stamped)
stamped_rows = _preview_rows(stamped)
eq("a stamped track carries its id into the rendered row",
   [r["row_id"] for r in stamped_rows], [t.row_id for t in stamped])
check("the ids are real", all(r["row_id"] for r in stamped_rows))
check("the ids are distinct", len({r["row_id"] for r in stamped_rows}) == len(stamped_rows))
# The rows are display-only on purpose: the full objects go to the cache file.
check("rows do not carry the whole TrackMetadata", "spotify_id" not in rows[0])


# ══ 2. a PARTIAL selection keeps its real positions ═══════════════════════════
print("2 — partial selection numbering")
full = album()
subset = [full[4], full[20]]     # disc 1 track 5, disc 2 track 9
eq("fixture is the shape the bug had",
   [(t.disc_number, t.track_number) for t in subset], [(1, 5), (2, 9)])

hdr = _playlist_header("https://open.spotify.com/album/x", full, Cfg())
events, svc, leftover = run_selection(subset, hdr)

eq("both tracks were handed to the downloader", len(svc.downloaded), 2)
eq("the real positions survived enrichment's renumbering",
   [(t.disc_number, t.track_number) for t in svc.downloaded], [(1, 5), (2, 9)])
# issue #75 — the same is_partial the numbering guard reads also has to switch
# OFF the playlist folder maintenance. If this were True, every unselected track
# already in the folder would be offered to the user as a leftover to delete.
check("a partial selection is NOT declared a complete playlist",
      svc.complete_playlist is False, repr(svc.complete_playlist))

loaded = one(events, "playlist_loaded")
check("a playlist_loaded is emitted so the UI has a header", loaded is not None)
if loaded:
    eq("header keeps the release title", loaded["title"], "Scorpion")
    eq("track_count reflects the SELECTION, not the release", loaded["track_count"], 2)
    eq("the rows are the selected ones",
       [r["title"] for r in loaded["tracks"]], ["Track 5", "Track 21"])
    check("no stray url field on the loaded event", "url" not in loaded)

summary = one(events, "playlist_summary")
check("a summary is emitted", summary is not None)
if summary:
    eq("summary totals count only the selection", (summary["total"], summary["downloaded"]), (2, 2))
    # The real link has to be on the summary or history cannot offer a re-queue.
    eq("summary carries the original link", summary["url"],
       "https://open.spotify.com/album/1ATL5GLyefJaxhQzSPVrLX")
    eq("summary attributes the source", summary["sources"], {"tidal_mirror": 1 * 2})
    check("summary reports no error", summary["error"] is None)

lib = one(events, "library_update")
check("library_update is emitted for the history/dedup path", lib is not None)
if lib:
    eq("library_update counts what landed", lib["tracks_added"], 2)

check("the payload file is deleted after reading", not leftover)


# ══ 3. a FULL selection behaves exactly like a pasted link ════════════════════
print("3 — full selection")
full = album()
hdr = _playlist_header("https://open.spotify.com/album/x", full, Cfg())
events, svc, _ = run_selection(full, hdr)
# Nothing is restored here, so enrichment's renumbering stands - which is the
# point: downloading everything through the preview must not differ from pasting
# the link, and the sequential numbering is what the link path produces.
eq("all tracks downloaded", len(svc.downloaded), 25)
eq("disc 1 is numbered 1..12",
   [t.track_number for t in svc.downloaded if t.disc_number == 1], list(range(1, 13)))
eq("disc 2 is numbered 1..13",
   [t.track_number for t in svc.downloaded if t.disc_number == 2], list(range(1, 14)))
# The control the partial case above needs: selecting everything IS the whole
# release, so the issue #75 maintenance is allowed to run. Without this pair,
# "partial is False" would also pass if the flag were hard-coded False.
check("a full selection IS declared a complete playlist",
      svc.complete_playlist is True, repr(svc.complete_playlist))


# ══ 4. failure paths report instead of hanging ════════════════════════════════
print("4 — failure paths")
events, svc, _ = run_selection([], _playlist_header("u", album(), Cfg()))
s = one(events, "playlist_summary")
check("an empty selection still emits a summary", s is not None)
if s:
    check("and it carries an error", bool(s["error"]), s)
check("nothing was downloaded", svc.downloaded is None)

# An unreadable payload must not leave the frontend waiting for a job.
with tempfile.TemporaryDirectory() as tmp:
    bad = os.path.join(tmp, "bad.json")
    with open(bad, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    buf = io.StringIO()
    with redirect_stdout(buf):
        _run_track_selection(bad, Cfg(), StubService(), options=None)
    evs = [json.loads(l) for l in buf.getvalue().splitlines()
           if l.strip().startswith("{")]
    check("a corrupt payload emits a summary", one(evs, "playlist_summary") is not None)
    check("a corrupt payload is still deleted", not os.path.exists(bad))


# ══ 5. the cache round-trips a full TrackMetadata ═════════════════════════════
print("5 — cache round-trip")
# This is what makes Download now cheap: the preview's objects are reused rather
# than the link being resolved a second time. A field lost here is a tag lost.
t = album(1)[0]
back = _track_from_payload(_track_dict(t))
for field in ("title", "album", "album_id", "spotify_id", "isrc", "track_number",
              "disc_number", "duration_ms", "release_year", "release_date",
              "artwork_url", "request_kind"):
    eq("round-trip preserves %s" % field, getattr(back, field), getattr(t, field))
eq("round-trip preserves artists", back.artists, t.artists)


# ── Report ────────────────────────────────────────────────────────────────────
print()
if failures:
    print("FAIL — %d passed, %d failed" % (passed, len(failures)))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("ALL PASS — %d assertions" % passed)
