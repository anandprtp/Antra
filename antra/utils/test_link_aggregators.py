"""Squigly / MusicBrainz / Unitune link resolvers (offline, stubbed HTTP)."""
import json
from types import SimpleNamespace

from antra.sources import link_aggregators as LA
from antra.sources.odesli import OdesliEnricher

fails = []


def check(name, cond):
    if not cond:
        fails.append(name)
        print("FAIL", name)


class R:
    def __init__(self, status=200, body=None, text=""):
        self.ok = status < 400
        self.status_code = status
        self._b = body
        self.text = text or (json.dumps(body) if body is not None else "")

    def json(self):
        return self._b


trk = SimpleNamespace(title="Blinding Lights", artists=["The Weeknd"], isrc="USUG11904206",
                      spotify_id="0VjIjW4GlUZAMYd2vXMi3b", duration_ms=200000)

# --- strict URL parsing: the BUG-ASIN-1 guard
ids = LA.ids_from_links([
    "https://music.amazon.com/albums/B086Q2QNLH?trackAsin=B086Q41M9C",
    "https://tidal.com/browse/track/171492468",
    "https://www.deezer.com/track/908604612",
    "https://music.apple.com/us/album/blinding-lights/1729918970?i=1729919128",
    "https://www.qobuz.com/us-en/album/x/y/track/55",
])
check("amazon trackAsin", ids.get("amazonMusic") == "B086Q41M9C")
check("tidal browse url", ids.get("tidal") == "171492468")
check("deezer", ids.get("deezer") == "908604612")
check("apple ?i=", ids.get("appleMusic") == "1729919128")
check("qobuz", ids.get("qobuz") == "55")
# Unitune's real payload for services it could not match:
junk = LA.ids_from_links([
    "https://music.amazon.com/search/The+Weeknd+Blinding+Lights",
    "https://music.amazon.com/albums/B086Q2QNLH",          # album, not a track
    "https://listen.tidal.com/search?q=The%20Weeknd%20Blinding%20Lights",
    "https://music.apple.com/search?term=The+Weeknd+Blinding+Lights",
    None, "",
])
check("search/album urls give no ids", junk == {})

# --- Squigly: create -> page -> embedded payload
page = ("<html>x window.__SQUIGLY_LINK__ = " + json.dumps(
    {"data": {"services": {"amazon": {"url": "https://music.amazon.com/albums/B086Q2QNLH?trackAsin=B086Q41M9C"},
                           "bandcamp": None}}}) + ";</script></html>")
posted = []


class S:
    def post(self, url, json=None, timeout=0):
        posted.append(json)
        return R(200, {"full_url": "https://squigly.link/song/a/b"})

    def get(self, url, **kw):
        return R(200, text=page)


check("squigly ids", LA.squigly_links(S(), trk) == {"amazonMusic": "B086Q41M9C"})
check("squigly sends the spotify url", posted and posted[0]["url"].endswith("0VjIjW4GlUZAMYd2vXMi3b"))


class Evil(S):
    def post(self, url, json=None, timeout=0):
        return R(200, {"full_url": "https://evil.example/x"})


check("squigly refuses a foreign page host", LA.squigly_links(Evil(), trk) == {})
check("no spotify id -> squigly/unitune skip",
      LA.squigly_links(S(), SimpleNamespace(spotify_id=None)) == {}
      and LA.unitune_links(S(), SimpleNamespace(spotify_id=None)) == {})

# --- MusicBrainz: verifies the recording before trusting its links
LA_MB = LA.musicbrainz_links
import antra.utils.musicbrainz as MB
MB._throttle = lambda: None


class M:
    def __init__(self, title="Blinding Lights", length=200046, score=100):
        self.t, self.l, self.s = title, length, score

    def get(self, url, params=None, headers=None, timeout=0):
        if url.endswith("/recording"):
            return R(200, {"recordings": [{"id": "rid", "score": self.s, "title": self.t, "length": self.l,
                                           "artist-credit": [{"name": "The Weeknd"}]}]})
        return R(200, {"relations": [{"url": {"resource": "https://tidal.com/track/155054813"}},
                                     {"url": {"resource": "https://www.deezer.com/track/1078622712"}}]})


check("mb good recording", LA_MB(M(), trk) == {"tidal": "155054813", "deezer": "1078622712"})
check("mb wrong title rejected", LA_MB(M(title="Completely Different Song"), trk) == {})
check("mb wrong length rejected (other mix)", LA_MB(M(length=420000), trk) == {})
check("mb low score rejected", LA_MB(M(score=60), trk) == {})
check("mb needs an isrc", LA_MB(M(), SimpleNamespace(isrc=None)) == {})

# --- Unitune with its real shape: only the deezer link is a track link
class U(S):
    def get(self, url, params=None, timeout=0):
        return R(200, {"linksByPlatform": {
            "amazonMusic": {"url": "https://music.amazon.com/search/The+Weeknd+Blinding+Lights"},
            "deezer": {"url": "https://www.deezer.com/track/908604612"},
            "tidal": {"url": "https://listen.tidal.com/search?q=x"}}})


check("unitune keeps only real links", LA.unitune_links(U(), trk) == {"deezer": "908604612"})

# --- orchestration: stops at amazon, survives a failing source, merges
calls = []
orig = (LA.squigly_links, LA.musicbrainz_links, LA.unitune_links)
LA.squigly_links = lambda s, t: (_ for _ in ()).throw(RuntimeError("boom"))
LA.musicbrainz_links = lambda s, t: (calls.append("mb"), {"tidal": "1"})[1]
LA.unitune_links = lambda s, t: (calls.append("un"), {"amazonMusic": "B000000001", "tidal": "2"})[1]
res = LA.resolve_extra(trk, S())
check("failing source does not stop the rest", res == {"tidal": "1", "amazonMusic": "B000000001"})
LA.musicbrainz_links = lambda s, t: (calls.append("mb2"), {"amazonMusic": "B000000002"})[1]
calls.clear()
LA.resolve_extra(trk, S())
check("stops once amazon is found", "un" not in calls)
LA.squigly_links, LA.musicbrainz_links, LA.unitune_links = orig

# --- wired into the real enricher, after the earlier resolvers came up empty
enr = OdesliEnricher.__new__(OdesliEnricher)
enr._cache, enr._api_key, enr._session = {}, None, S()
for n in ("_try_odesli", "_try_songwhip", "_try_songstats", "_try_deezer_songlink",
          "_try_itunes_odesli"):
    setattr(enr, n, lambda t: {})
enr._search_amazon = lambda t: None
enr._store = lambda k, r: None
import antra.sources.link_aggregators as mod
mod.resolve_extra = lambda t, s=None: {"amazonMusic": "B086Q41M9C"}
check("enricher uses it", enr.resolve(trk).get("amazonMusic") == "B086Q41M9C")

print("FAILED:" if fails else "ok", fails or "")
raise SystemExit(1 if fails else 0)
