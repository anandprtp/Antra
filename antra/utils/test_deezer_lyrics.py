"""Offline checks for the Deezer lyrics source (antra/utils/deezer_lyrics.py + its hook in
LyricsFetcher). Drives the REAL LyricsFetcher against a fake HTTP session; payload shapes are the
mirror's /api/lyrics, /api/search/isrc and api.deezer.com/track/isrc answers.

    python -m antra.utils.test_deezer_lyrics      # exit 0 when every check passes
"""
import os
import re
import sys
import types

HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, HERE)
sys.dont_write_bytecode = True

from antra.utils import deezer_lyrics as dl          # noqa: E402
from antra.utils.lyrics import LyricsFetcher         # noqa: E402

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


class Resp:
    def __init__(self, status=200, body=None, bad_json=False):
        self.status_code, self._body, self._bad = status, body, bad_json

    def json(self):
        if self._bad:
            raise ValueError("not json")
        return self._body


class Session:
    """session.get by URL substring; every call is recorded. A value that is an Exception is raised;
    a list is consumed one entry per call."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []
        self.headers = {}

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        for frag, val in self.routes.items():
            if frag in url:
                if isinstance(val, list):
                    val = val.pop(0)
                if isinstance(val, Exception):
                    raise val
                return val
        raise AssertionError("unexpected GET " + url)

    def n(self, frag):
        return sum(1 for c in self.calls if frag in c)


MIRROR = "https://dz.example"
MIRROR2 = "https://dz2.example"


def track(**kw):
    base = dict(title="Blinding Lights", duration_ms=200000, isrc="USUG11904206", deezer_track_id=None,
                spotify_id=None, album="After Hours", artists=["The Weeknd"])
    base.update(kw)
    return types.SimpleNamespace(**base)


LRC = "\n".join(f"[00:{i:02d}.00]line{i}" for i in range(1, 8))
GOOD = Resp(200, {"trackId": "9", "synced": LRC, "plain": "words", "lines": [], "writers": None, "copyrights": None})
ISRC_OK = Resp(200, {"track_id": "9", "isrc": "USUG11904206", "title": "Blinding Lights", "artist": "The Weeknd",
                     "duration_ms": 200000})

# ------------------------------------------------------------- id resolution
s = Session({})
check("a carried Deezer id is used with no network at all",
      dl.resolve_deezer_id(s, [MIRROR], "k", track(deezer_track_id="123")) == "123" and s.calls == [])
check("a non-numeric carried id is ignored (falls to ISRC)",
      dl.resolve_deezer_id(Session({"/api/search/isrc/": ISRC_OK}), [MIRROR], "k", track(deezer_track_id="abc")) == "9")
check("no id and no ISRC: nothing to resolve", dl.resolve_deezer_id(Session({}), [MIRROR], "k", track(isrc="")) is None)

s = Session({"/api/search/isrc/": ISRC_OK})
check("ISRC resolved through our mirror when title and length agree",
      dl.resolve_deezer_id(s, [MIRROR], "k", track()) == "9" and s.n("api.deezer.com") == 0)

PUBLIC_MATCH = Resp(200, {"id": 9, "title": "Blinding Lights", "duration": 200})
s = Session({"/api/search/isrc/": Resp(200, {"track_id": "9", "title": "A Totally Different Song", "duration_ms": 200000}),
             "api.deezer.com/track/isrc:": PUBLIC_MATCH})
check("title mismatch is rejected (a shared ISRC can be another song)", dl.resolve_deezer_id(s, [MIRROR], "k", track()) is None)
check("a mirror answer that does not match is FINAL: the public API (which would match) is not asked",
      s.n("api.deezer.com") == 0)

s = Session({"/api/search/isrc/": Resp(200, {"track_id": "9", "title": "Blinding Lights", "duration_ms": 290000})})
check("length mismatch beyond 5 s is rejected (another mix of the same recording)",
      dl.resolve_deezer_id(s, [MIRROR], "k", track()) is None)
s = Session({"/api/search/isrc/": Resp(200, {"track_id": "9", "title": "Blinding Lights", "duration_ms": 204000})})
check("length within 5 s is accepted", dl.resolve_deezer_id(s, [MIRROR], "k", track()) == "9")
s = Session({"/api/search/isrc/": Resp(200, {"track_id": "9", "title": "Blinding Lights", "duration_ms": 0})})
check("unknown length falls back to the title match", dl.resolve_deezer_id(s, [MIRROR], "k", track()) == "9")
s = Session({"/api/search/isrc/": Resp(200, {"track_id": "9", "title": "", "duration_ms": 200000})})
check("an answer with no title cannot be verified, so it is refused", dl.resolve_deezer_id(s, [MIRROR], "k", track()) is None)

s = Session({"/api/search/isrc/": Resp(404, {})})
check("mirror 404 = Deezer has no track for the ISRC, and the public API is NOT asked",
      dl.resolve_deezer_id(s, [MIRROR], "k", track()) is None and s.n("api.deezer.com") == 0)

s = Session({"/api/search/isrc/": ConnectionError("down"),
             "api.deezer.com/track/isrc:": Resp(200, {"id": 9, "title": "Blinding Lights", "duration": 200})})
check("mirror unreachable: the public API answers instead (and is verified the same way)",
      dl.resolve_deezer_id(s, [MIRROR], "k", track()) == "9" and s.n("api.deezer.com") == 1)
s = Session({"/api/search/isrc/": ConnectionError("down"),
             "api.deezer.com/track/isrc:": Resp(200, {"id": 9, "title": "Blinding Lights", "duration": 400})})
check("public API answer with the wrong length is rejected too", dl.resolve_deezer_id(s, [MIRROR], "k", track()) is None)
s = Session({"/api/search/isrc/": ConnectionError("down"),
             "api.deezer.com/track/isrc:": Resp(200, {"error": {"code": 800}})})
check("public API error object -> None", dl.resolve_deezer_id(s, [MIRROR], "k", track()) is None)

s = Session({"/api/search/isrc/": [Resp(500, {}), ISRC_OK]})
check("a 500 from the first mirror moves to the next mirror",
      dl.resolve_deezer_id(s, [MIRROR, MIRROR2], "k", track()) == "9")

# ------------------------------------------------------------- fetching
s = Session({"/api/lyrics/9": GOOD})
res = dl.fetch_deezer_lyrics(s, [MIRROR], "k", track(deezer_track_id="9"))
check("fetch returns (plain, synced) like the Apple source", res == ("words", LRC))

s = Session({"/api/lyrics/9": [Resp(404, {})]})
check("404 = no lyrics: None, and the next mirror is not asked",
      dl.fetch_deezer_lyrics(s, [MIRROR, MIRROR2], "k", track(deezer_track_id="9")) is None and s.n("/api/lyrics/") == 1)
s = Session({"/api/lyrics/9": [Resp(500, {}), GOOD]})
check("a 500 moves to the next mirror", dl.fetch_deezer_lyrics(s, [MIRROR, MIRROR2], "k", track(deezer_track_id="9")) == ("words", LRC))
s = Session({"/api/lyrics/9": [ConnectionError("x"), GOOD]})
check("an unreachable mirror moves to the next mirror", dl.fetch_deezer_lyrics(s, [MIRROR, MIRROR2], "k", track(deezer_track_id="9")) == ("words", LRC))
s = Session({"/api/lyrics/9": Resp(200, {"synced": "", "plain": ""})})
check("an empty body is None", dl.fetch_deezer_lyrics(s, [MIRROR], "k", track(deezer_track_id="9")) is None)
s = Session({"/api/lyrics/9": Resp(200, bad_json=True)})
check("a non-JSON body is skipped", dl.fetch_deezer_lyrics(s, [MIRROR], "k", track(deezer_track_id="9")) is None)
check("no mirrors configured: None without any call",
      dl.fetch_deezer_lyrics(Session({}), [], "k", track(deezer_track_id="9")) is None)
s = Session({"/api/lyrics/9": GOOD, "/api/search/isrc/": ISRC_OK})
dl.fetch_deezer_lyrics(s, [MIRROR], "k", track(deezer_track_id="9"))
check("with a carried id there is no ISRC lookup at all", s.n("/api/search/isrc/") == 0)

# ------------------------------------------------------------- the chain
def fetcher(**kw):
    f = LyricsFetcher(use_cache=False, mirror_api_key="k", **kw)
    f._fetch_lrclib_exact = lambda t, with_album: None
    f._fetch_lrclib_search = lambda t, title: None
    return f


names = [n for n, _ in fetcher(apple_mirrors=["https://ap.example"], deezer_mirrors=[MIRROR])._providers(track())]
check("chain: deezer sits directly after apple", names.index("deezer") == names.index("apple") + 1)
check("chain: deezer is behind every LRCLIB shape", names.index("deezer") > max(i for i, n in enumerate(names) if n.startswith("lrclib")))
names = [n for n, _ in fetcher(deezer_mirrors=[MIRROR])._providers(track())]
check("chain: deezer works without an apple mirror", "deezer" in names and "apple" not in names)
names = [n for n, _ in fetcher(apple_mirrors=["https://ap.example"])._providers(track())]
check("chain: no deezer mirror configured -> no deezer provider", "deezer" not in names)
names = [n for n, _ in fetcher(deezer_mirrors=[MIRROR + "/"])._providers(track(spotify_id="sp"))]
check("chain: deezer comes before the last-resort paxsenix", names.index("deezer") < names.index("paxsenix"))

f = fetcher(deezer_mirrors=[MIRROR])
f.session = Session({"/api/lyrics/9": GOOD})
plain, synced = f.fetch(track(deezer_track_id="9"))
check("end to end: LRCLIB empty, Deezer supplies the synced lyrics", synced == LRC and plain == "words")

f = fetcher(deezer_mirrors=[MIRROR])
f._fetch_lrclib_exact = lambda t, with_album: ("lrclib words", "[00:01.00]a\n[00:02.00]b\n[00:03.00]c")
f.session = Session({})
plain, synced = f.fetch(track(deezer_track_id="9"))
check("end to end: a synced LRCLIB hit wins and Deezer is never asked", synced.startswith("[00:01.00]a") and f.session.calls == [])

f = fetcher(deezer_mirrors=[MIRROR])
f._fetch_lrclib_exact = lambda t, with_album: ("plain only", "")
f.session = Session({"/api/lyrics/9": GOOD})
plain, synced = f.fetch(track(deezer_track_id="9"))
check("end to end: a plain-only LRCLIB hit does not stop the search; Deezer's synced wins", synced == LRC)

overrun = LRC + "\n[06:40.00]way past the end"
f = fetcher(deezer_mirrors=[MIRROR])
f.session = Session({"/api/lyrics/9": Resp(200, {"synced": overrun, "plain": ""})})
plain, synced = f.fetch(track(deezer_track_id="9"))
check("end to end: Deezer lyrics that run far past the track are rejected by the existing gate", synced is None)

f = fetcher(deezer_mirrors=[MIRROR])
f.session = Session({"/api/lyrics/9": Resp(200, {"synced": "[00:01.00]a\n[00:02.00]b", "plain": ""})})
plain, synced = f.fetch(track(deezer_track_id="9"))
check("end to end: fewer than 3 timed lines is rejected by the existing gate", synced is None)

f = fetcher(deezer_mirrors=[MIRROR])
f.session = Session({"/api/lyrics/9": Resp(200, {"synced": "", "plain": "only words"})})
plain, synced = f.fetch(track(deezer_track_id="9"))
check("end to end: Deezer plain-only is kept as the best plain answer", plain == "only words" and synced is None)

# ------------------------------------------------------------- wiring in the engine's service
svc = open(os.path.join(HERE, "antra", "core", "service.py"), encoding="utf-8").read()
_at = svc.index("lyrics_fetcher = LyricsFetcher(")
check("service.py builds the LyricsFetcher with the Deezer mirror",
      "deezer_mirrors=[cfg.deezer_mirror_url]" in svc[_at:_at + 1200])

print()
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for x in FAILS:
        print("  -", x)
    sys.exit(1)
print("ALL CHECKS PASSED")
