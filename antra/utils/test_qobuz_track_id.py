"""
QZ-1 — a Qobuz link names its track_id, so stop searching for the track.

Reported: https://www.qobuz.com/us-en/album/stamina-estigia-simon-vuarambon/uq8ew5w1qak96
failed with "No matching source found" for both tracks. Measured against the
live mirror on 2026-09-27:

  /api/album/uq8ew5w1qak96      -> 200, both tracks, WITH track_id and isrc
  /api/search/isrc/GBHFW2600069 -> 404  (the ISRC endpoint walks Qobuz's TEXT
                                         index, which does not carry this
                                         brand-new small-label release)
  /api/search?title=Stamina (Extended Mix) -> Simon O'Shine, Ferreck Dawn,
                                         Qubiko ... an unrelated release
  /api/search?title=Stamina     -> the 262 s RADIO EDIT of a 420 s extended mix

So the resolver went hunting for a track the payload had already identified,
and every route it had was wrong. The fix carries `qobuz_track_id` through, the
same way deezer_track_id / tidal_track_id / amazon_asin already are.

Fixtures are the live payloads, so the assertions are about our plumbing and
not about Qobuz's uptime.

Run: python antra/utils/test_qobuz_track_id.py
"""
import pathlib
import sys
from dataclasses import replace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from antra.core.external_music_fetcher import ExternalMusicFetcher
from antra.core.models import TrackMetadata
from antra.core.service import AntraService
from antra.sources.qobuz_mirror import QobuzMirrorAdapter

PASS = FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print("  x %s %s" % (label, detail))


def eq(label, got, want):
    check(label, got == want, "\n      got %r want %r" % (got, want))


# ── live payload, GET https://qobuz.anandserver.cfd/api/album/uq8ew5w1qak96 ──
ALBUM_PAYLOAD = {
    "album_id": "uq8ew5w1qak96",
    "title": "Stamina / Estigia",
    "album_artists": ["Simon Vuarambon"],
    "artwork_url": "https://static.qobuz.com/images/covers/96/ak/uq8ew5w1qak96_600.jpg",
    "release_date": "2026-07-31",
    "release_year": 2026,
    "total_tracks": 2,
    "tracks": [
        {"track_id": "433264302", "title": "Stamina (Extended Mix)",
         "artists": ["Simon Vuarambon"], "track_number": 1, "disc_number": 1,
         "duration_ms": 420000, "isrc": "GBHFW2600069", "explicit": False,
         "album_id": "uq8ew5w1qak96"},
        {"track_id": "433264303", "title": "Estigia (Extended Mix)",
         "artists": ["Simon Vuarambon"], "track_number": 2, "disc_number": 1,
         "duration_ms": 388000, "isrc": "GBHFW2600070", "explicit": False,
         "album_id": "uq8ew5w1qak96"},
    ],
}


class Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)


class RecordingSession:
    """Answers the album call; records every URL it is asked for."""

    def __init__(self, payload=ALBUM_PAYLOAD):
        self.urls = []
        self.headers = {}
        self._payload = payload

    def get(self, url, **kw):
        self.urls.append(url)
        return Resp(self._payload)


class Cfg:
    qobuz_mirror_url = "https://qobuz.example"
    antra_api_key = "ak_test"


# ── 1. the id survives the fetch ──────────────────────────────────────────
f = ExternalMusicFetcher.__new__(ExternalMusicFetcher)
f.cfg = Cfg()
f._session = RecordingSession()
tracks = f._fetch_qobuz_via_mirror("album", "uq8ew5w1qak96")
eq("both tracks fetched", len(tracks), 2)
eq("track_id carried on to TrackMetadata",
   [t.qobuz_track_id for t in tracks], ["433264302", "433264303"])
eq("CONTROL: the ISRCs are still read too",
   [t.isrc for t in tracks], ["GBHFW2600069", "GBHFW2600070"])

# CONTROL: a payload with no track_id must leave the field None, not "None"
no_id = {k: v for k, v in ALBUM_PAYLOAD.items()}
no_id["tracks"] = [{k: v for k, v in t.items() if k != "track_id"}
                   for t in ALBUM_PAYLOAD["tracks"]]
f._session = RecordingSession(no_id)
eq("CONTROL: absent track_id -> None, never the string 'None'",
   [t.qobuz_track_id for t in f._fetch_qobuz_via_mirror("album", "uq8ew5w1qak96")],
   [None, None])

# ── 2. the adapter resolves it without ANY network call ───────────────────
class ExplodingSession:
    """Records every request. The adapter swallows exceptions from its own
    search calls (both the ISRC and the text path catch Exception), so raising
    here would be invisible — the URLs have to be counted instead."""

    headers: dict = {}

    def __init__(self):
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        return Resp({}, status=404)


track = TrackMetadata(
    title="Stamina (Extended Mix)", artists=["Simon Vuarambon"],
    album="Stamina / Estigia", duration_ms=420000, isrc="GBHFW2600069",
    qobuz_track_id="433264302",
)

ad = QobuzMirrorAdapter(mirror_url="https://qobuz.example")
ad._session = ExplodingSession()
res = ad.search(track)
eq("a known id costs ZERO network calls", ad._session.urls, [])
check("a known id resolves", res is not None)
eq("...to that exact Qobuz track", res.stream_id, "433264302")
eq("...at similarity 1.0", res.similarity_score, 1.0)
eq("...as lossless FLAC", (res.is_lossless, res.audio_format.value), (True, "flac"))
eq("...echoing the requested title, so the identity gate is a no-op",
   res.title, track.title)

# CONTROL — the same adapter, same session, WITHOUT the id, must still search.
# Without this, a _direct_result that fired unconditionally would pass every
# assertion above.
ad._session.urls.clear()
ad.search(replace(track, qobuz_track_id=None))
check("CONTROL: with no id the adapter DOES go to the network",
      any("search/isrc" in u for u in ad._session.urls), ad._session.urls)

# ── 3. the id survives enrichment ─────────────────────────────────────────
# A rebuilt object out of an enricher must not silently drop it — that is how
# row_id was lost in issue #74.
enriched = TrackMetadata(title=track.title, artists=track.artists, album=track.album)
AntraService._preserve_track_identity([track], [enriched])
eq("_preserve_track_identity keeps qobuz_track_id",
   enriched.qobuz_track_id, "433264302")

# ── 4. strict matching treats a native id as exact ────────────────────────
from antra.core.resolver import SourceResolver       # noqa: E402


class FakeAdapter:
    def __init__(self, name):
        self.name = name


r = SourceResolver.__new__(SourceResolver)
r.strict_matching = True
# A direct result echoes the track's own title and duration, so it clears
# strict matching with no special case in the resolver. An added
# native_qobuz branch there was unobservable and was removed.
check("strict matching accepts a direct Qobuz result",
      r._passes_strict_identity(track, res, FakeAdapter("qobuz_mirror")))
check("CONTROL: the same native id does NOT excuse a different source",
      not r._passes_strict_identity(
          replace(track, duration_ms=420000),
          type(res)(**{**res.__dict__, "source": "jiosaavn", "duration_ms": 120000,
                       "isrc_match": False, "similarity_score": 0.5}),
          FakeAdapter("jiosaavn")))

print("\n%d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
