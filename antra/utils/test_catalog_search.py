"""Issue #58 - the app could only search for an ARTIST.

The reporter: "I don't see a way to search by music title ... if I'm looking for
an artist, I find myself with all his discography and I have to read everything
to find the album where there's the title that interests me".

That is accurate: the only search binding was SearchArtists, and --search-artists
was the only CLI entry point. This suite pins the song/album search added for it.

The load-bearing property is the LAST section: a song hit is a music.apple.com
URL of the `?i=<track id>` shape, and apple_fetcher's own _RE_SONG resolves that
to exactly ONE track. That is why the feature needed no new download path - and
it is paired with an album control that must resolve to many, so the assertion
cannot pass just because the fetcher returns something.

HTTP is stubbed throughout, so every assertion is about our parsing rather than
about Apple's uptime. The payloads are verbatim shapes captured live from
itunes.apple.com and api.spotify.com on 2026-09-26.

Run:  python antra/utils/test_catalog_search.py
Negative control: antra/utils/test_catalog_search_negctl.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from antra.core import apple_fetcher as AF
from antra.core.apple_fetcher import (
    AppleFetcher,
    _clean_itunes_url,
    _itunes_artwork,
    _itunes_year,
)
from antra.core.spotify import SpotifyClient, _year_from_release_date
from antra.utils.matching import search_match_score

FAILURES: list[str] = []
COUNT = 0


def check(label, got, want):
    global COUNT
    COUNT += 1
    if got != want:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")


def check_true(label, got):
    check(label, bool(got), True)


# ── 1. URL cleaning - iTunes appends its own tracking parameter ─────────────
#
# This matters more than it looks: the cleaned URL is what gets QUEUED, so a
# leftover `?uo=4` would be carried into the download as part of the link.

check("song URL: &uo=4 dropped, ?i= kept",
      _clean_itunes_url("https://music.apple.com/us/album/bohemian-rhapsody/6781024026?i=6781024437&uo=4"),
      "https://music.apple.com/us/album/bohemian-rhapsody/6781024026?i=6781024437")
check("album URL: ?uo=4 dropped",
      _clean_itunes_url("https://music.apple.com/us/album/a-night-at-the-opera/6781023399?uo=4"),
      "https://music.apple.com/us/album/a-night-at-the-opera/6781023399")
# CONTROL: a URL with nothing to strip must come back byte-identical. Without
# this, a _clean_itunes_url that truncated everything after the last '/' would
# still pass the two assertions above.
check("CONTROL: a clean URL is returned unchanged",
      _clean_itunes_url("https://music.apple.com/us/album/x/123?i=456"),
      "https://music.apple.com/us/album/x/123?i=456")
check("empty URL is empty, not None", _clean_itunes_url(""), "")


# ── 2. artwork and year helpers ────────────────────────────────────────────

check("artwork upscaled 100 -> 300",
      _itunes_artwork("https://is1-ssl.mzstatic.com/image/thumb/a/b/c.jpg/100x100bb.jpg"),
      "https://is1-ssl.mzstatic.com/image/thumb/a/b/c.jpg/300x300bb.jpg")
check("artwork: None stays None", _itunes_artwork(None), None)
# CONTROL: a URL that does not end in the size suffix must not be mangled.
check("CONTROL: a non-thumbnail URL is unchanged",
      _itunes_artwork("https://example.com/cover.png"), "https://example.com/cover.png")

check("year from an iTunes release date", _itunes_year("1975-10-31T12:00:00Z"), 1975)
check("year: absent -> None", _itunes_year(None), None)
check("year: junk -> None", _itunes_year("n/a!"), None)
check("year from a Spotify release_date", _year_from_release_date("2016-08-05"), 2016)
check("year from a year-precision Spotify date", _year_from_release_date("1975"), 1975)
check("year: empty Spotify date -> None", _year_from_release_date(""), None)


# ── 3. scoring - the max of "title" and "artist title" ─────────────────────
#
# Each spelling of the same intent has to score well, and the pair is the whole
# reason the function takes the artist at all.

bare = search_match_score("bohemian rhapsody", "Queen", "Bohemian Rhapsody")
full = search_match_score("queen bohemian rhapsody", "Queen", "Bohemian Rhapsody")
check_true("a bare title query scores the song highly", bare >= 0.95)
check_true("an 'artist title' query scores the same song highly", full >= 0.95)
# CONTROL: scoring the title ALONE is what the naive implementation does, and
# it is measurably worse for the second spelling. If this control ever comes
# out high, the pair above proves nothing.
from antra.utils.matching import normalize, string_similarity
title_only = string_similarity(normalize("queen bohemian rhapsody"), normalize("Bohemian Rhapsody"))
check_true("CONTROL: title-only scoring is NOT high for 'artist title'", title_only < 0.90)
check_true("an unrelated query scores low",
           search_match_score("tainted love", "Queen", "Bohemian Rhapsody") < 0.5)
check("a missing artist does not crash", type(search_match_score("x", "", "x")), float)


# ── 4. Apple song / album search, HTTP stubbed ─────────────────────────────

SONG_PAYLOAD = {"resultCount": 3, "results": [
    {"trackId": 6781024437, "trackName": "Bohemian Rhapsody", "artistName": "Queen",
     "collectionName": "A Night At The Opera (Deluxe Edition)",
     "trackViewUrl": "https://music.apple.com/us/album/bohemian-rhapsody/6781024026?i=6781024437&uo=4",
     # Measured 2026-09-26 on 4 different queries: for entity=song, iTunes
     # returns collectionViewUrl IDENTICAL to trackViewUrl - it carries the ?i=
     # too. Kept in the fixture so it is faithful, and so nobody later "fixes"
     # the reader to prefer it on the theory that they differ.
     "collectionViewUrl": "https://music.apple.com/us/album/bohemian-rhapsody/6781024026?i=6781024437&uo=4",
     "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/a/b/c.jpg/100x100bb.jpg",
     "trackTimeMillis": 355155, "trackExplicitness": "notExplicit",
     "releaseDate": "1975-10-31T12:00:00Z"},
    # Deliberately a WEAKER title match than the row above, and deliberately
    # AFTER it, so section 4's ordering assertion is not vacuous: sorting by
    # match_score descending would leave this second, but sorting ASCENDING - or
    # any other re-sort - would move it. Its explicit flag is also read here.
    #
    # It is not "Bohemian Rhapsody (Live Aid)", which was the first draft:
    # normalize() strips parentheticals, so that scores 1.000 against a bare
    # "bohemian rhapsody" query and the control could never fail.
    {"trackId": 111, "trackName": "Bohemian Like You", "artistName": "The Dandy Warhols",
     "collectionName": "Thirteen Tales", "trackViewUrl": "https://music.apple.com/us/album/x/1?i=111&uo=4",
     "artworkUrl100": None, "trackTimeMillis": 260000,
     "trackExplicitness": "explicit", "releaseDate": "2000-07-13T12:00:00Z"},
    # Unusable: no trackViewUrl, so there is nothing to queue.
    {"trackId": 222, "trackName": "Orphan", "artistName": "Nobody", "trackViewUrl": ""},
]}

ALBUM_PAYLOAD = {"resultCount": 2, "results": [
    {"collectionId": 6781023399, "collectionName": "A Night At The Opera", "artistName": "Queen",
     "collectionViewUrl": "https://music.apple.com/us/album/a-night-at-the-opera/6781023399?uo=4",
     "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/d/e/f.jpg/100x100bb.jpg",
     "trackCount": 12, "collectionExplicitness": "notExplicit",
     "releaseDate": "1975-11-21T08:00:00Z"},
    # Unusable: no name.
    {"collectionId": 333, "collectionName": "", "collectionViewUrl": "https://music.apple.com/us/album/y/2"},
]}


class _StubResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _StubSession:
    """Records the params it was called with, so the request shape is pinned."""

    def __init__(self, payload, boom=False):
        self.payload = payload
        self.boom = boom
        self.calls = []

    def get(self, url, params=None, timeout=None, **kw):
        self.calls.append({"url": url, "params": dict(params or {})})
        if self.boom:
            raise RuntimeError("network down")
        return _StubResponse(self.payload)


def _fetcher(payload, boom=False):
    f = AppleFetcher()
    f._session = _StubSession(payload, boom)
    return f


f = _fetcher(SONG_PAYLOAD)
songs = f.search_songs("bohemian rhapsody", 25)

check("the unusable row is dropped", len(songs), 2)
check("request hits the iTunes search endpoint", f._session.calls[0]["url"], AF._ITUNES_SEARCH)
check("entity is song", f._session.calls[0]["params"]["entity"], "song")
check("media is music", f._session.calls[0]["params"]["media"], "music")

s0 = songs[0]
check("song kind", s0["kind"], "song")
check("song id", s0["id"], "6781024437")
check("song title", s0["title"], "Bohemian Rhapsody")
check("song artist", s0["artists"], "Queen")
check("song album", s0["album"], "A Night At The Opera (Deluxe Edition)")
check("song duration", s0["duration_ms"], 355155)
check("song year", s0["year"], 1975)
check("song not explicit", s0["is_explicit"], False)
check("song source", s0["source"], "apple")
check("song artwork upscaled",
      s0["artwork_url"], "https://is1-ssl.mzstatic.com/image/thumb/a/b/c.jpg/300x300bb.jpg")
check("song URL cleaned",
      s0["url"], "https://music.apple.com/us/album/bohemian-rhapsody/6781024026?i=6781024437")
check("explicit flag read from trackExplicitness", songs[1]["is_explicit"], True)
# The round trip, and the single most important assertion in the file: the URL
# this search PRODUCES must be one the existing fetcher resolves to a song. Drop
# the `?i=` and it silently becomes the whole album.
check("a produced song URL round-trips through parse_url as a song",
      AppleFetcher().parse_url(s0["url"]), ("song", "6781024437"))
check("a missing artwork stays None", songs[1]["artwork_url"], None)

# iTunes' own relevance ranking is kept rather than re-sorted locally: it knows
# which "Bohemian Rhapsody" people mean, and string similarity does not.
check("iTunes order preserved: row 0 is still row 0", songs[0]["id"], "6781024437")
check("iTunes order preserved: row 1 is still row 1", songs[1]["id"], "111")
# CONTROL: the two rows must actually score differently, or "order preserved"
# would hold under any sort at all.
check_true("CONTROL: the rows do score differently (so the order is not luck)",
           songs[1]["match_score"] < songs[0]["match_score"])

f = _fetcher(ALBUM_PAYLOAD)
albums = f.search_albums("a night at the opera", 25)
check("the nameless album is dropped", len(albums), 1)
check("entity is album", f._session.calls[0]["params"]["entity"], "album")
a0 = albums[0]
check("album kind", a0["kind"], "album")
check("album id", a0["id"], "6781023399")
check("album title", a0["title"], "A Night At The Opera")
check("album artist", a0["artists"], "Queen")
check("album track count", a0["track_count"], 12)
check("album year", a0["year"], 1975)
check("album URL cleaned",
      a0["url"], "https://music.apple.com/us/album/a-night-at-the-opera/6781023399")

# Failure and bounds.
check("a network failure yields [] rather than raising",
      _fetcher(SONG_PAYLOAD, boom=True).search_songs("x", 5), [])
check("an empty query is not sent at all", _fetcher(SONG_PAYLOAD).search_songs("   ", 5), [])
f = _fetcher(SONG_PAYLOAD)
f.search_songs("x", 5000)
check("limit is clamped to the iTunes maximum", f._session.calls[0]["params"]["limit"], 200)
f = _fetcher(SONG_PAYLOAD)
f.search_songs("x", 0)
# 0 reads as "unset" and takes the default rather than asking iTunes for nothing.
check("a zero limit means the default, never 0", f._session.calls[0]["params"]["limit"], 25)
f = _fetcher(SONG_PAYLOAD)
f.search_songs("x", -5)
check("a negative limit is floored at 1", f._session.calls[0]["params"]["limit"], 1)


# ── 5. Spotify Web API row mapping ─────────────────────────────────────────

SPOTIFY_TRACK = {
    "id": "7tFiyTwD0nx5a1eklYtX2J", "name": "Bohemian Rhapsody",
    "artists": [{"name": "Queen"}], "duration_ms": 354320, "explicit": False,
    "album": {"name": "A Night At The Opera", "release_date": "1975-11-21",
              "images": [{"url": "https://i.scdn.co/image/big"}, {"url": "https://i.scdn.co/image/small"}]},
}
SPOTIFY_ALBUM = {
    "id": "1GbtB4zTqAsyfZEsm1RZfx", "name": "A Night At The Opera",
    "artists": [{"name": "Queen"}], "total_tracks": 12, "release_date": "1975-11-21",
    "images": [{"url": "https://i.scdn.co/image/album"}],
}

sc = SpotifyClient("", "", "US")
row = sc._track_search_row(SPOTIFY_TRACK)
check("spotify song kind", row["kind"], "song")
check("spotify song title", row["title"], "Bohemian Rhapsody")
check("spotify song artist", row["artists"], "Queen")
check("spotify song album", row["album"], "A Night At The Opera")
check("spotify song year", row["year"], 1975)
check("spotify song artwork is the first (largest) image",
      row["artwork_url"], "https://i.scdn.co/image/big")
check("spotify song URL", row["url"], "https://open.spotify.com/track/7tFiyTwD0nx5a1eklYtX2J")
check("spotify song source", row["source"], "spotify")
# Left at 0.0 on purpose: the client does not see the query, so the service
# scores it. Section 6 pins that it actually happens.
check("spotify rows come back unscored", row["match_score"], 0.0)
check("a track with no id is dropped", sc._track_search_row({"name": "x"}), None)
check("a track with no name is dropped", sc._track_search_row({"id": "x"}), None)

arow = sc._album_search_row(SPOTIFY_ALBUM)
check("spotify album kind", arow["kind"], "album")
check("spotify album track count", arow["track_count"], 12)
check("spotify album URL", arow["url"], "https://open.spotify.com/album/1GbtB4zTqAsyfZEsm1RZfx")
check("an album with no id is dropped", sc._album_search_row({"name": "x"}), None)


# ── 6. the service dispatcher ──────────────────────────────────────────────

from antra.core.config import Config
from antra.core.service import AntraService

svc = AntraService(config=Config())

check("an empty query short-circuits", svc.search_catalog("   ", kind="song"), [])

# Every kind that is not song/album/artist has to become "song" rather than
# reaching argparse, which would exit 2 on an unknown --search-kind.
seen = {}


def _spy(query, kind, limit):
    seen["kind"] = kind
    return []


svc._search_spotify_catalog = _spy
svc.search_catalog("x", kind="nonsense", source="spotify")
check("an unknown kind normalises to song", seen["kind"], "song")
svc.search_catalog("x", kind="ALBUM", source="spotify")
check("kind is case-insensitive", seen["kind"], "album")

# artist is routed to the long-standing artist search, untouched.
routed = {}
svc.search_artists = lambda q, source="apple": routed.setdefault("hit", (q, source)) or []
svc.search_catalog("queen", kind="artist", source="apple")
check("artist is routed to search_artists", routed["hit"], ("queen", "apple"))

# Spotify rows arrive unscored; the service must score them, because the UI
# renders a match badge for every row whatever produced it.
svc2 = AntraService(config=Config())
svc2._search_spotify_catalog = lambda q, k, l: [
    dict(SPOTIFY_TRACK_ROW := {"kind": "song", "id": "1", "title": "Bohemian Rhapsody",
                               "artists": "Queen", "album": "", "artwork_url": None,
                               "duration_ms": 0, "is_explicit": False, "year": None,
                               "match_score": 0.0, "url": "https://open.spotify.com/track/1",
                               "source": "spotify"})]
out = svc2.search_catalog("bohemian rhapsody", kind="song", source="spotify")
check("the spotify row survives", len(out), 1)
check_true("the service scores an unscored row", out[0]["match_score"] >= 0.95)
check("and does not relabel its source", out[0]["source"], "spotify")

# When Spotify yields nothing the service falls back to Apple - the row's own
# `source` is what tells the UI to say so.
svc3 = AntraService(config=Config())
svc3._search_spotify_catalog = lambda q, k, l: []
AF_real = AF.AppleFetcher


class _FakeApple:
    def search_songs(self, q, limit):
        return [{"kind": "song", "id": "9", "title": "Bohemian Rhapsody", "artists": "Queen",
                 "album": "", "artwork_url": None, "duration_ms": 0, "is_explicit": False,
                 "year": None, "match_score": 0.42, "url": "https://music.apple.com/us/album/x/1?i=9",
                 "source": "apple"}]

    def search_albums(self, q, limit):
        return []


AF.AppleFetcher = _FakeApple
try:
    out = svc3.search_catalog("bohemian rhapsody", kind="song", source="spotify")
finally:
    AF.AppleFetcher = AF_real
check("spotify falling through yields the Apple rows", len(out), 1)
check("a row that came from Apple says so", out[0]["source"], "apple")
check("an already-scored row is left alone", out[0]["match_score"], 0.42)


# ── 7. the property the whole feature rests on ─────────────────────────────
#
# A song hit is a URL, and the existing pipeline already resolves that URL to
# exactly one track. If this stops being true, clicking a song result silently
# downloads the whole album it came from.

check("a song search URL is recognised as a SONG",
      AppleFetcher().parse_url(
          "https://music.apple.com/us/album/bohemian-rhapsody/6781024026?i=6781024437"),
      ("song", "6781024437"))
# CONTROL: the album URL the same album search returns must be an ALBUM. Without
# it, a detect_url_type that answered "song" for everything would pass above.
check("CONTROL: an album search URL is recognised as an ALBUM",
      AppleFetcher().parse_url(
          "https://music.apple.com/us/album/a-night-at-the-opera/6781023399")[0],
      "album")


# ── report ─────────────────────────────────────────────────────────────────

if FAILURES:
    print(f"FAIL - {len(FAILURES)} of {COUNT} checks failed:")
    for x in FAILURES:
        print("  -", x)
    sys.exit(1)
print(f"PASS - {COUNT} checks (issue #58 song/album search)")
