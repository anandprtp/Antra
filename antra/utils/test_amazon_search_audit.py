"""AUDIT-DESKTOP-10 — Amazon search/album-page resolution holes.

  1. A transient failure was cached as "this album has no Amazon page": every
     later track of the album in the same session was told there is none.
  2. urllib.parse.quote() keeps "/" by default, so a title/album containing a
     slash ("AC/DC") searched a different URL path.
  3. A non-dict JSON body (an error array) from song.link raised
     AttributeError out of search().

Drives the REAL AmazonAdapter methods; only the HTTP session and the page
fetcher are fakes.   Run:  python antra/utils/test_amazon_search_audit.py
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
amz = importlib.import_module("antra.sources.amazon")
models = importlib.import_module("antra.core.models")

FAILS: list[str] = []


def check(label, ok, detail=""):
    if not ok:
        FAILS.append(label)
        print(f"  FAIL  {label} :: {detail}")


def track(**kw):
    base = dict(title="Song", artists=["Band"], album="Studio Album", album_artists=["Band"], release_year=2020,
                track_number=1, disc_number=1, total_tracks=10, duration_ms=200000, request_kind="album")
    base.update(kw)
    return models.TrackMetadata(**base)


class Resp:
    def __init__(self, ok=True, text="", status=200, payload=None):
        self.ok, self.text, self.status_code, self._p = ok, text, status, payload

    def json(self):
        return self._p


class Session:
    def __init__(self, fn):
        self.fn, self.urls = fn, []
        self.headers = {}

    def get(self, url, **kw):
        self.urls.append(url)
        return self.fn(url, **kw)


ALBUM = [models.TrackMetadata(title="Song", artists=["Band"], album="Studio Album", album_artists=["Band"],
                              track_number=1, duration_ms=200000, amazon_asin="B000000001")]


class Fetcher:
    def fetch(self, url):
        return list(ALBUM)


amz.AmazonMusicFetcher = Fetcher


def adapter(fn):
    a = amz.AmazonAdapter([])
    a._session = Session(fn)
    a._album_urls_via_songlink = lambda t: []
    return a


# 1. transient outage must not be remembered as "no album"
state = {"down": True}


def flaky(url, **kw):
    if state["down"]:
        raise ConnectionError("network down")
    return Resp(text='<a href="/albums/B0ALBUM001">x</a>')


a = adapter(flaky)
t = track()
check("outage: no album found", a._fetch_amazon_album_candidates(t) == [])
state["down"] = False
got = a._fetch_amazon_album_candidates(t)
check("outage is not cached: next track finds the album once the network is back", len(got) == 1, got)

# control: a genuine miss (every request answered, nothing found) IS cached
calls = {"n": 0}


def empty(url, **kw):
    calls["n"] += 1
    return Resp(text="<html>nothing</html>")


b = adapter(empty)
b._fetch_amazon_album_candidates(t)
first = calls["n"]
b._fetch_amazon_album_candidates(t)
check("control: a real miss is still cached (no re-search per track)", calls["n"] == first and first > 0,
      (first, calls["n"]))

# 2. a slash must be encoded
c = adapter(lambda url, **kw: Resp(text=""))
c._fetch_amazon_album_candidates(track(album="AC/DC Live", album_artists=[], artists=[]))
check("album search: slash is percent-encoded",
      any("/search/AC%2FDC" in u for u in c.get_urls()) if hasattr(c, "get_urls") else
      any("/search/AC%2FDC" in u for u in c._session.urls), c._session.urls[:2])
check("album query never contains the placeholder 'Unknown Artist'",
      not any("Unknown" in u for u in c._session.urls), c._session.urls[:1])
d = adapter(lambda url, **kw: Resp(text=""))
d._search_amazon_music_web(track(title="Hot/Cold"))
check("track web search: slash is percent-encoded", any("Hot%2FCold" in u for u in d._session.urls), d._session.urls[:2])
check("control: spaces are still encoded", any("Hot%2FCold%20Band" in u for u in d._session.urls), d._session.urls[:2])

# 3. non-dict JSON from song.link must not raise
e = adapter(lambda url, **kw: Resp(payload=["error"]))
del e.__dict__["_album_urls_via_songlink"]  # use the real method
try:
    out = e._album_urls_via_songlink(track(album_id="ALB123"))
    check("song.link list body -> []", out == [], out)
except Exception as ex:
    check("song.link list body does not raise", False, f"{type(ex).__name__}: {ex}")
f = adapter(lambda url, **kw: Resp(payload={"linksByPlatform": {}, "entitiesByUniqueId": {}}))
del f.__dict__["_album_urls_via_songlink"]
check("control: a dict body with no Amazon link -> []", f._album_urls_via_songlink(track(album_id="ALB123")) == [])

print("FAILED:" if FAILS else "OK", *FAILS, sep="\n  " if FAILS else " ")
sys.exit(1 if FAILS else 0)
