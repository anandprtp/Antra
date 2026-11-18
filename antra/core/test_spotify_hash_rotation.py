"""Spotify persisted-query hashes rotate; callers must recover instead of breaking.

Spotify answers an unknown hash with HTTP 412 `"Invalid query hash"` (measured
2026-11-03). Callers keep their own last-known-good hash and use it FIRST -- a newer
hash can ask for different variables -- and only switch to the live web-player
bundle's hash when Spotify reports ours as gone. No network: transports are stubbed.

Run:  python antra/core/test_spotify_hash_rotation.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import requests

import antra.core.spotify_partner as sp
from antra.core.spotify_partner import SpotifyPartnerClient

PASS: list[str] = []
FAIL: list[str] = []


def check(label, got, want):
    (PASS if got == want else FAIL).append(f"{label}: got {got!r}, want {want!r}")


def ok(label, cond):
    (PASS if cond else FAIL).append(label)


OLD, NEW = "a" * 64, "b" * 64


class Resp:
    def __init__(self, status, text='{"data": {"ok": 1}}'):
        self.status_code, self.text = status, text

    def json(self):
        import json
        return json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


class Server:
    """Accepts only `valid` hashes, like Spotify; records every hash it was sent."""

    def __init__(self, valid):
        self.valid, self.seen = set(valid), []

    def post(self, url, json=None, **kw):
        h = json["extensions"]["persistedQuery"]["sha256Hash"]
        self.seen.append(h)
        return Resp(200) if h in self.valid else Resp(412, '"Invalid query hash"')


def reset(live):
    sp._hash_overrides.clear()
    SpotifyPartnerClient._forced_at = 0.0
    SpotifyPartnerClient._hashes, SpotifyPartnerClient._hashes_at = {}, 0.0
    calls = []

    def fake_ops(force=False):
        calls.append(force)
        return dict(live)

    SpotifyPartnerClient.operation_hashes = staticmethod(fake_ops)
    return calls


PAYLOAD = {"operationName": "getAlbum", "variables": {"uri": "x"},
           "extensions": {"persistedQuery": {"version": 1, "sha256Hash": OLD}}}

# -- classifier --------------------------------------------------------------
ok("412 is stale", sp.is_stale_hash(412, ""))
ok("body 'Invalid query hash' is stale", sp.is_stale_hash(200, '"Invalid query hash"'))
ok("PersistedQueryNotFound is stale", sp.is_stale_hash(200, '{"errors":[{"message":"PersistedQueryNotFound"}]}'))
ok("CONTROL: 200 ok is not stale", not sp.is_stale_hash(200, '{"data":{}}'))
ok("CONTROL: 429 is not stale", not sp.is_stale_hash(429, "slow down"))
ok("CONTROL: 401 is not stale", not sp.is_stale_hash(401, "expired"))

# -- known hash works: never touches the bundle ------------------------------
calls = reset({"getAlbum": NEW})
srv = Server({OLD})
r = sp.post_persisted_payload(srv.post, "u", PAYLOAD)
check("working hash: one request", srv.seen, [OLD])
check("working hash: bundle never fetched", calls, [])
check("working hash: response returned", r.status_code, 200)

# -- rotation: retry once with the live hash, and remember it ----------------
calls = reset({"getAlbum": NEW})
srv = Server({NEW})
r = sp.post_persisted_payload(srv.post, "u", PAYLOAD)
check("rotated: old then live hash", srv.seen, [OLD, NEW])
check("rotated: succeeds", r.status_code, 200)
check("rotated: forced refresh asked once", calls, [True])
srv.seen.clear()
sp.post_persisted_payload(srv.post, "u", PAYLOAD)
check("rotated: next call goes straight to the live hash", srv.seen, [NEW])
check("rotated: and does not refetch the bundle", calls, [True])

# -- bundle has nothing newer: give up after one try, return the 412 ---------
calls = reset({"getAlbum": OLD})
srv = Server(set())
r = sp.post_persisted_payload(srv.post, "u", PAYLOAD)
check("same hash in bundle: no pointless retry", srv.seen, [OLD])
check("same hash in bundle: 412 surfaces", r.status_code, 412)

calls = reset({})
srv = Server(set())
r = sp.post_persisted_payload(srv.post, "u", PAYLOAD)
check("operation missing from bundle: single request", srv.seen, [OLD])

# -- a failing retry does not loop -------------------------------------------
calls = reset({"getAlbum": NEW})
srv = Server(set())
r = sp.post_persisted_payload(srv.post, "u", PAYLOAD)
check("both stale: exactly two requests", srv.seen, [OLD, NEW])

# -- refresh rate limit: bundle is not hammered ------------------------------
real_ops = None
SpotifyPartnerClient._forced_at = 0.0
import time
SpotifyPartnerClient._forced_at = time.time()
ops_calls = []
SpotifyPartnerClient._hashes, SpotifyPartnerClient._hashes_at = {"getAlbum": NEW}, time.time()
# restore the REAL classmethod to exercise its gate
import importlib
importlib.reload(sp)
SpotifyPartnerClient = sp.SpotifyPartnerClient
SpotifyPartnerClient._forced_at = time.time()
SpotifyPartnerClient._hashes, SpotifyPartnerClient._hashes_at = {"cached": "c" * 64}, time.time()
got = SpotifyPartnerClient.operation_hashes(force=True)
check("force inside the min gap serves the cache (no download)", got, {"cached": "c" * 64})

# -- library GET path (real SpotifyLibraryClient._graphql) -------------------
from antra.core import spotify_library as lib

sp._hash_overrides.clear()


def stub_ops(force=False):
    return {"libraryV3": NEW}


SpotifyPartnerClient.operation_hashes = staticmethod(stub_ops)
SpotifyPartnerClient._forced_at = 0.0
seen = []


def fake_get(url, headers=None, timeout=None):
    import urllib.parse
    h = urllib.parse.unquote(url).split('"sha256Hash":"')[1].split('"')[0]
    seen.append(h)
    return Resp(200) if h == NEW else Resp(412, '"Invalid query hash"')


lib.requests.get = fake_get
c = lib.SpotifyLibraryClient.__new__(lib.SpotifyLibraryClient)
c._ensure_access_token = lambda: "t"
c._ensure_client_token = lambda: "c"
out = c._graphql("libraryV3", {"a": 1}, OLD)
check("library GET: old then live", seen, [OLD, NEW])
check("library GET: payload returned", out, {"data": {"ok": 1}})
seen.clear()
c._graphql("libraryV3", {"a": 1}, OLD)
check("library GET: remembers the live hash", seen, [NEW])

# -- podcast pathfinder (real PodcastClient._pathfinder) ---------------------
from antra.core import podcast as pod

sp._hash_overrides.clear()
SpotifyPartnerClient.operation_hashes = staticmethod(lambda force=False: {"getEpisodeOrChapter": NEW})
SpotifyPartnerClient._forced_at = 0.0
psent = []


def fake_post(self, url, **kw):
    h = kw["json"]["extensions"]["persistedQuery"]["sha256Hash"]
    psent.append(h)
    r = Resp(200) if h == NEW else Resp(412, '"Invalid query hash"')
    r.raise_for_status()
    return r


cls = next(v for v in vars(pod).values()
           if isinstance(v, type) and hasattr(v, "_pathfinder"))
cls._post = fake_post
inst = cls.__new__(cls)
res = inst._pathfinder("getEpisodeOrChapter", OLD, {"uri": "u"})
check("podcast: old then live", psent, [OLD, NEW])
check("podcast: result returned", res, {"data": {"ok": 1}})
sp._hash_overrides.clear()
psent.clear()
SpotifyPartnerClient.operation_hashes = staticmethod(lambda force=False: {})
try:
    inst._pathfinder("getEpisodeOrChapter", OLD, {"uri": "u"})
    ok("CONTROL: unrecoverable 412 still raises", False)
except requests.HTTPError:
    ok("CONTROL: unrecoverable 412 still raises", True)
check("CONTROL: unrecoverable 412 tried once", psent, [OLD])

# -- every call site actually goes through the helper ------------------------
root = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(root, "spotify.py"), encoding="utf-8").read()
check("spotify.py: no raw pathfinder POST left", src.count('requests.post(\n                    "https://api-partner'), 0)
check("spotify.py: 3 persisted POSTs use the helper", src.count("post_persisted_payload(\n"), 3)
av = open(os.path.join(root, "availability.py"), encoding="utf-8").read()
check("availability.py uses the helper", av.count("post_persisted_payload(\n"), 1)

for line in FAIL:
    print("FAIL", line)
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
