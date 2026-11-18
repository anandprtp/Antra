"""AUDIT-DESKTOP-13 — null/odd payloads in the Qobuz, Tidal, SoundCloud and Spotify
search fetchers.

  1. Qobuz and Tidal MIRROR album payloads: `len(data.get("tracks", []))` raises
     when `tracks` is present but null.
  2. SoundCloud: a null `collection` page raised TypeError out of the fetch;
     `playlist_position` enumerated the raw list, so a removed track left a gap
     in the numbering (every other fetcher counts the tracks it kept).
  3. Spotify `search_artists`: Web API search can return null entries in
     `items`; one made the whole Spotipy path raise and silently fell through to
     the anonymous / iTunes path. `followers: null` likewise.

Real methods; HTTP / Spotipy replaced.   Run: python antra/core/test_fetcher_nulls_audit.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import antra.core.endpoint_manifest as em  # noqa: E402
import antra.core.external_music_fetcher as emf  # noqa: E402
from antra.core.soundcloud_fetcher import SoundCloudFetcher  # noqa: E402
from antra.core.spotify import SpotifyClient  # noqa: E402

FAILS: list[str] = []


def check(label, ok, detail=""):
    if not ok:
        FAILS.append(label)
        print(f"  FAIL  {label} :: {detail}")


class Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status
        self.ok = status < 400

    def json(self):
        return self._p

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


# ── 1. Qobuz / Tidal mirror, `tracks: null` ─────────────────────────────────
em.load_endpoint_manifest = lambda *a, **k: SimpleNamespace(mirror_tidal="", mirror_qobuz="")
emf._mirror_key = lambda cfg: "k"


def mirror_fetcher(payload):
    f = emf.ExternalMusicFetcher.__new__(emf.ExternalMusicFetcher)
    f.cfg = SimpleNamespace(tidal_mirror_url="https://m.test", qobuz_mirror_url="https://m.test")
    f._session = SimpleNamespace(get=lambda url, **kw: Resp(payload))
    return f


def attempt(fn):
    try:
        return fn()
    except Exception as e:
        return f"{type(e).__name__}: {e}"


body = {"title": "Alb", "album_artists": ["A"], "tracks": None}
got = attempt(lambda: mirror_fetcher(body)._fetch_tidal_public("album", "123"))
check("Tidal mirror album with tracks:null -> empty list, no TypeError", got == [], got)
got = attempt(lambda: mirror_fetcher(body)._fetch_qobuz_via_mirror("album", "abc"))
check("Qobuz mirror album with tracks:null -> empty list, no TypeError", got == [], got)
good = {"title": "Alb", "album_artists": ["A"], "total_tracks": 1,
        "tracks": [{"title": "S", "artists": ["A"], "track_number": 1, "track_id": 5, "duration_ms": 1000}]}
got = attempt(lambda: mirror_fetcher(good)._fetch_tidal_public("album", "123"))
check("control: Tidal mirror album with a track still parses", isinstance(got, list) and len(got) == 1 and got[0].title == "S", got)
got = attempt(lambda: mirror_fetcher(good)._fetch_qobuz_via_mirror("album", "abc"))
check("control: Qobuz mirror album with a track still parses", isinstance(got, list) and len(got) == 1 and got[0].title == "S", got)

# ── 2. SoundCloud ───────────────────────────────────────────────────────────
sc = SoundCloudFetcher(client_id="cid")


class Sess:
    def __init__(self, pages):
        self.pages = list(pages)

    def get(self, url, **kw):
        return Resp(self.pages.pop(0))


sc._session = Sess([{"collection": None, "next_href": None}])
got = attempt(lambda: sc._fetch_paginated_tracks("https://api-v2.soundcloud.com/playlists/1/tracks"))
check("null `collection` page -> [] (no TypeError)", got == [], got)
sc._session = Sess([["not", "a", "dict"]])
got = attempt(lambda: sc._fetch_paginated_tracks("https://api-v2.soundcloud.com/playlists/1/tracks"))
check("non-dict page -> [] (no AttributeError)", got == [], got)
sc._session = Sess([{"collection": [{"id": 1, "title": "a"}], "next_href": "https://api-v2.soundcloud.com/x?o=1"},
                    {"collection": [{"id": 2, "title": "b"}], "next_href": None}])
got = attempt(lambda: sc._fetch_paginated_tracks("https://api-v2.soundcloud.com/playlists/1/tracks"))
check("control: two pages are both collected", isinstance(got, list) and [t["id"] for t in got] == [1, 2], got)


def tr(i, title="x"):
    return {"id": i, "title": f"{title}{i}" if title else "", "user": {"username": "u"}}


# stubs only survive into numbering on the paginated path (tracks_uri); there an
# untitled/removed track used to leave a hole in playlist_position
sc._session = Sess([{"collection": [tr(1), tr(2), tr(3, ""), tr(4)], "next_href": None}])
pl = {"kind": "playlist", "title": "P", "track_count": 4, "tracks": [], "tracks_uri": "https://api-v2.soundcloud.com/playlists/1/tracks"}
got = attempt(lambda: sc._fetch_playlist_tracks(pl))
check("a stub in the middle does not leave a gap in playlist_position",
      isinstance(got, list) and [t.playlist_position for t in got] == [1, 2, 3], got if isinstance(got, str) else [t.playlist_position for t in got])
pl2 = {"kind": "playlist", "title": "P", "track_count": 3, "tracks": [tr(1), tr(2), tr(3)], "tracks_uri": ""}
got = attempt(lambda: sc._fetch_playlist_tracks(pl2))
check("control: a clean playlist is numbered 1..3", isinstance(got, list) and [t.playlist_position for t in got] == [1, 2, 3], got)

# ── 3. Spotify search_artists ───────────────────────────────────────────────
artist = {"id": "a1", "name": "Queen", "images": [{"url": "https://i/x"}], "genres": ["rock"], "followers": {"total": 5}}


def spotify_client(items):
    c = SpotifyClient.__new__(SpotifyClient)
    for a in ("_oauth_sp", "_sp_dc", "_partner_client"):
        setattr(c, a, None)
    c._market = "US"
    c.sp = SimpleNamespace(search=lambda **kw: {"artists": {"items": items}})
    c._search_artists_itunes = lambda q, l: [{"source": "itunes"}]
    c._get_anonymous_access_token = lambda *a: None
    return c


got = attempt(lambda: spotify_client([None, artist]).search_artists("queen"))
check("a null entry in `items` does not abandon the Spotify results",
      isinstance(got, list) and len(got) == 1 and got[0].get("source") == "spotify", got)
a2 = dict(artist, followers=None)
got = attempt(lambda: spotify_client([a2]).search_artists("queen"))
check("followers:null -> followers None, still a Spotify result",
      isinstance(got, list) and len(got) == 1 and got[0]["source"] == "spotify" and got[0]["followers"] is None, got)
got = attempt(lambda: spotify_client([artist]).search_artists("queen"))
check("control: a normal result is unchanged", isinstance(got, list) and got[0]["followers"] == 5 and got[0]["name"] == "Queen", got)

# the anonymous-token path has its own copy of the loop
import antra.core.spotify as spmod  # noqa: E402

c = spotify_client([])
c.sp = None
c._get_anonymous_access_token = lambda *a: "tok"
orig_get = spmod.requests.get
spmod.requests.get = lambda *a, **k: Resp({"artists": {"items": [None, dict(artist, followers=None)]}})
try:
    got = attempt(lambda: c.search_artists("queen"))
finally:
    spmod.requests.get = orig_get
check("anonymous path: null entry and followers:null both tolerated",
      isinstance(got, list) and len(got) == 1 and got[0]["source"] == "spotify" and got[0]["followers"] is None, got)

print("FAILED:" if FAILS else "OK", *FAILS, sep="\n  " if FAILS else " ")
sys.exit(1 if FAILS else 0)
