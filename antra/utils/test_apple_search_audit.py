"""AUDIT-DESKTOP-10 — Apple search: isrc_match must be earned, and the ISRC
lookup must not be repeated.

  1. The mirror answering 200 to /api/search/isrc WITHOUT a track_id set
     isrc_match=True anyway. A later TEXT-search hit then came back flagged as
     an ISRC match, and SourceResolver._passes_identity_gate returns True
     immediately on isrc_match, skipping the recording-class check (the same
     shortcut BUG-MIX-1 closed for guessed ISRCs).
  2. After the first ISRC lookup failed (non-200) the identical request was
     sent again, doubling the wait and the load on a struggling mirror.

Drives the REAL AppleAdapter.search; only HTTP, Odesli and the catalog are fakes.
Run:  python antra/utils/test_apple_search_audit.py
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
apple = importlib.import_module("antra.sources.apple")
models = importlib.import_module("antra.core.models")

FAILS: list[str] = []


def check(label, ok, detail=""):
    if not ok:
        FAILS.append(label)
        print(f"  FAIL  {label} :: {detail}")


class Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p = status, payload

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._p


def make(isrc_resp, search_resp):
    a = apple.AppleAdapter(["https://m.test"])
    a.urls = []

    def api_get(url, **kw):
        a.urls.append(url)
        return isrc_resp if "/api/search/isrc/" in url else search_resp

    a._api_get = api_get
    a._get_working_mirror = lambda force_rotate=False: "https://m.test"
    a._odesli.resolve = lambda t: {}
    a._public_itunes_search = lambda *x, **k: None
    a._apple_fetcher._fetch_song = lambda i: None
    return a


def track(**kw):
    base = dict(title="Song", artists=["Band"], album="Alb", album_artists=["Band"], release_year=2020,
                track_number=1, disc_number=1, total_tracks=10, duration_ms=200000, isrc="USXX12000001")
    base.update(kw)
    return models.TrackMetadata(**base)


TEXT_HIT = Resp(200, {"track_id": "999", "title": "Song", "artist": "Band"})

# 1. empty 200 from the ISRC endpoint + a text-search hit
a = make(Resp(200, {}), TEXT_HIT)
r = a.search(track())
check("text hit is returned", r is not None and r.stream_id == "999", r)
check("an ISRC answer with no track_id does not set isrc_match", r is not None and r.isrc_match is False,
      getattr(r, "isrc_match", None))

# control: a genuine ISRC answer still sets it
b = make(Resp(200, {"track_id": "555", "title": "Song", "artist": "Band"}), Resp(404))
r = b.search(track())
check("control: a real ISRC match is flagged", r is not None and r.stream_id == "555" and r.isrc_match is True,
      (r and r.stream_id, r and r.isrc_match))
# control: no ISRC on the track -> never flagged
c = make(Resp(404), TEXT_HIT)
r = c.search(track(isrc=None))
check("control: text hit on an ISRC-less track is not flagged", r is not None and r.isrc_match is False, r)

# 1b. same rule on the lossy-mode path (the second ISRC block)
g = apple.AppleAdapter(["https://m.test"], preferred_output_format="mp3")
g._api_get = lambda url, **kw: Resp(200, {}) if "/api/search/isrc/" in url else TEXT_HIT
g._get_working_mirror = lambda force_rotate=False: "https://m.test"
g._odesli.resolve = lambda t: {}
g._public_itunes_search = lambda *x, **k: None
g._apple_fetcher._fetch_song = lambda i: None
r = g.search(track())
check("lossy mode: empty ISRC 200 does not flag the later text hit",
      r is not None and r.stream_id == "999" and r.isrc_match is False, (r and r.stream_id, r and r.isrc_match))

# 2. a failing ISRC lookup is requested once
d = make(Resp(500), TEXT_HIT)
d.search(track())
n = sum("/api/search/isrc/" in u for u in d.urls)
check("failed ISRC lookup is not repeated", n == 1, f"{n} requests")
# control: lossy mode skips the first block, so the second must still run once
e = apple.AppleAdapter(["https://m.test"], preferred_output_format="mp3")
e.urls = []
e._api_get = lambda url, **kw: (e.urls.append(url), Resp(200, {"track_id": "777"}))[1]
e._get_working_mirror = lambda force_rotate=False: "https://m.test"
e._apple_fetcher._fetch_song = lambda i: None
r = e.search(track())
check("control: lossy mode still resolves via the ISRC lookup", r is not None and r.stream_id == "777"
      and sum("/api/search/isrc/" in u for u in e.urls) == 1, (r and r.stream_id, e.urls))

print("FAILED:" if FAILS else "OK", *FAILS, sep="\n  " if FAILS else " ")
sys.exit(1 if FAILS else 0)
