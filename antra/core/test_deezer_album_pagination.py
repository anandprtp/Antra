"""Deezer album links truncated at 25 tracks (reported on album 53105092).

`/album/{id}` embeds only the first 25 tracks and, unlike the playlist
endpoint, leaves `tracks.next` unset -- so nothing signalled the cut. The fix
walks the dedicated `/album/{id}/tracks` endpoint instead, which also carries
the isrc / track_position / disk_number the embedded objects omit entirely.

Every fixture below mirrors a payload measured live against api.deezer.com on
2026-09-25. No network is used: `_deezer_get` is stubbed so the assertions are
about OUR pagination, not Deezer's uptime.

Run:  python antra/core/test_deezer_album_pagination.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from antra.core.external_music_fetcher import ExternalMusicFetcher

PASS: list[str] = []
FAIL: list[str] = []


def check(label, got, want):
    (PASS if got == want else FAIL).append(f"{label}: got {got!r}, want {want!r}")


def ok(label, cond):
    (PASS if cond else FAIL).append(label)


# -- fixtures ---------------------------------------------------------------

NB_TRACKS = 57
ALBUM_ID = "53105092"
ALBUM_URL = "https://www.deezer.com/en/album/53105092"


def embedded_item(n):
    """An /album/{id} embedded track. Measured: no isrc, no track_position,
    no disk_number -- it carries a nested `album` object instead."""
    return {
        "id": 1000 + n,
        "title": f"Track {n}",
        "title_short": f"Track {n}",
        "duration": 180,
        "explicit_lyrics": True,
        "artist": {"id": 102, "name": "Chris Brown"},
        "album": {"id": int(ALBUM_ID), "title": "Heartbreak On A Full Moon",
                  "cover_xl": "https://cdn/cover-xl.jpg"},
    }


def paged_item(n, disc=1):
    """An /album/{id}/tracks item. Measured: isrc + track_position +
    disk_number present, and NO nested album object."""
    return {
        "id": 1000 + n,
        "title": f"Track {n}",
        "title_short": f"Track {n}",
        "duration": 180,
        "explicit_lyrics": n % 2 == 0,
        "isrc": f"USRC1700{n:03d}",
        "track_position": n,
        "disk_number": disc,
        "artist": {"id": 102, "name": "Chris Brown"},
    }


ALBUM_BODY = {
    "title": "Heartbreak On A Full Moon",
    "nb_tracks": NB_TRACKS,
    "release_date": "2017-12-08",
    "artist": {"name": "Chris Brown"},
    "cover_xl": "https://cdn/cover-xl.jpg",
    "tracks": {"data": [embedded_item(n) for n in range(1, 26)], "next": None},
}

# The fixture must reproduce the bug's precondition, or everything below is
# vacuous: 25 embedded for a 57-track album, and no `next` to follow.
ok("fixture: /album embeds 25 of 57", len(ALBUM_BODY["tracks"]["data"]) == 25)
ok("fixture: /album sets no tracks.next", ALBUM_BODY["tracks"]["next"] is None)


class Fetcher(ExternalMusicFetcher):
    """Real class, real _fetch_deezer / _deezer_track / pagination loop --
    only the HTTP call is replaced."""

    def __init__(self, pages, page_limit=None):
        self.cfg = None
        self._session = None
        self.calls: list[str] = []
        self._pages = pages
        if page_limit is not None:
            self._DEEZER_PAGE_LIMIT = page_limit

    def _deezer_get(self, path):
        self.calls.append(path)
        if path.startswith(f"album/{ALBUM_ID}/tracks"):
            page_body = self._pages.pop(0)
            if isinstance(page_body, Exception):
                raise page_body
            return page_body
        if path == f"album/{ALBUM_ID}":
            return ALBUM_BODY
        if path.startswith(("playlist/", "track/")):
            return self._pages.pop(0)
        raise AssertionError(f"unexpected request: {path}")


def page(items, has_next=True, index=0):
    nxt = (f"https://api.deezer.com/album/{ALBUM_ID}/tracks?index={index + len(items)}"
           if has_next else None)
    return {"data": items, "total": NB_TRACKS, "next": nxt}


def full_pages(limit=100):
    items = [paged_item(n) for n in range(1, NB_TRACKS + 1)]
    out, i = [], 0
    while i < len(items):
        chunk = items[i:i + limit]
        i += limit
        out.append(page(chunk, has_next=i < len(items), index=i - len(chunk)))
    return out


# -- 1. the reported bug ----------------------------------------------------

f = Fetcher(full_pages())
tracks = f.fetch(ALBUM_URL)
check("album returns every track, not 25", len(tracks), NB_TRACKS)
check("last track is the 57th", tracks[-1].title, "Track 57")
check("total_tracks comes from nb_tracks", tracks[0].total_tracks, NB_TRACKS)

# -- 2. pagination mechanics -----------------------------------------------

f = Fetcher(full_pages())
f.fetch(ALBUM_URL)
track_calls = [c for c in f.calls if "/tracks" in c]
check("one /album call", len([c for c in f.calls if c == f"album/{ALBUM_ID}"]), 1)
check("57 tracks at limit=100 is one page", len(track_calls), 1)
check("first page asks index=0 limit=100",
      track_calls[0], f"album/{ALBUM_ID}/tracks?limit=100&index=0")

f = Fetcher(full_pages(limit=25), page_limit=25)
got = f.fetch(ALBUM_URL)
track_calls = [c for c in f.calls if "/tracks" in c]
check("3 pages at limit=25", len(track_calls), 3)
check("index advances by the batch length", track_calls,
      [f"album/{ALBUM_ID}/tracks?limit=25&index={i}" for i in (0, 25, 50)])
check("paged album is complete", len(got), NB_TRACKS)
check("no duplicates across pages", len({t.title for t in got}), NB_TRACKS)

# -- 3. fields the embedded list never had ----------------------------------

f = Fetcher(full_pages())
tracks = f.fetch(ALBUM_URL)
check("every track has an ISRC", sum(1 for t in tracks if t.isrc), NB_TRACKS)
check("ISRC is the real one", tracks[4].isrc, "USRC1700005")
check("track_number is Deezer's own position", [t.track_number for t in tracks[:5]],
      [1, 2, 3, 4, 5])
check("disc_number is populated", tracks[0].disc_number, 1)
# The embedded objects carry NONE of the three -- the control that shows the
# three assertions above are about the endpoint switch and not about luck.
emb = Fetcher([])._deezer_track(embedded_item(1), album_title="A", album_artist="B")
ok("control: an embedded item has no ISRC", emb.isrc is None)
ok("control: an embedded item has no disc number", emb.disc_number is None)

# -- 4. multi-disc ----------------------------------------------------------

# Over 25, or the "shorter than embedded" guard in section 6 would (correctly)
# throw the walk away and this would silently assert against the embedded list.
two_disc = ([paged_item(n, disc=1) for n in range(1, 21)]
            + [paged_item(n, disc=2) for n in range(1, 21)])
f = Fetcher([page(two_disc, has_next=False)])
tracks = f.fetch(ALBUM_URL)
check("multi-disc walk was used, not the embedded list", len(tracks), 40)
check("disc 2 survives", sorted({t.disc_number for t in tracks}), [1, 2])

# -- 5. album_id, which the paged items cannot supply -----------------------

f = Fetcher(full_pages())
tracks = f.fetch(ALBUM_URL)
check("album_id falls back to the id in the URL", tracks[0].album_id, ALBUM_ID)
ok("artwork still comes off the album body",
   tracks[0].artwork_url == "https://cdn/cover-xl.jpg")
check("release year still parsed", tracks[0].release_year, 2017)
check("album title still set", tracks[0].album, "Heartbreak On A Full Moon")

# -- 6. failure never loses the album ---------------------------------------

f = Fetcher([RuntimeError("Deezer API error: quota")])
tracks = f.fetch(ALBUM_URL)
check("page 1 failing falls back to the embedded 25", len(tracks), 25)
ok("the fallback still yields usable tracks", tracks[0].title == "Track 1")

f = Fetcher([page([paged_item(n) for n in range(1, 41)], has_next=True),
             RuntimeError("boom")])
tracks = f.fetch(ALBUM_URL)
check("a partial walk (40) beats the embedded 25", len(tracks), 40)

f = Fetcher([page([paged_item(n) for n in range(1, 11)], has_next=True),
             RuntimeError("boom")])
tracks = f.fetch(ALBUM_URL)
check("a walk shorter than embedded is discarded", len(tracks), 25)

# -- 7. loop termination ----------------------------------------------------

f = Fetcher([page([paged_item(1)], has_next=False)])
f.fetch(ALBUM_URL)
check("no next -> stop after one page", len([c for c in f.calls if "/tracks" in c]), 1)

f = Fetcher([page([paged_item(1)], has_next=True), {"data": [], "next": None}])
tracks = f.fetch(ALBUM_URL)
check("an empty page stops the walk", len([c for c in f.calls if "/tracks" in c]), 2)
check("an empty page does not blank the album", len(tracks), 25)

runaway = Fetcher([page([paged_item(1)], has_next=True) for _ in range(500)])
runaway.fetch(ALBUM_URL)
check("the page cap bounds a server that always says next",
      len([c for c in runaway.calls if "/tracks" in c]),
      ExternalMusicFetcher._DEEZER_MAX_PAGES)

# -- 8. the other two URL kinds are untouched -------------------------------

PLAYLIST = {
    "title": "My Playlist", "creator": {"name": "Me"}, "description": "",
    "picture_xl": "https://cdn/p.jpg",
    "tracks": {"data": [dict(embedded_item(n), isrc=f"X{n}") for n in range(1, 31)]},
}
f = Fetcher([PLAYLIST])
tracks = f.fetch("https://www.deezer.com/en/playlist/999")
check("playlist still reads the embedded list", len(tracks), 30)
ok("playlist makes no /tracks request", not any("/tracks" in c for c in f.calls))
check("playlist positions intact", [t.playlist_position for t in tracks[:3]], [1, 2, 3])

f = Fetcher([dict(paged_item(7), album={"id": 5, "title": "Alb"})])
tracks = f.fetch("https://www.deezer.com/en/track/1007")
check("single track unaffected", len(tracks), 1)
check("single track keeps its own album id", tracks[0].album_id, "5")

# -- report -----------------------------------------------------------------

for line in FAIL:
    print("FAIL", line)
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
