"""A Deezer ISRC hit on a DIFFERENT release (a compilation) must not write its
track position, disc, date, genre, label or cover onto our track (WEB-ART-1)."""
import sys, types
sys.dont_write_bytecode = True
import requests
from antra.core import metadata_enricher as me
from antra.core.models import TrackMetadata

FAILS = []
def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond: FAILS.append(name)

class R:
    status_code = 200
    def __init__(self, body): self._b = body
    def json(self): return self._b

def run(hit_album):
    t = TrackMetadata(title="Mr. Brightside", artists=["The Killers"], album="Hot Fuss", isrc="USIR20400274")
    body = {"id": 1, "track_position": 15, "disk_number": 2,
            "album": {"id": 9, "title": hit_album, "release_date": "2012-01-01"}}
    real_get, real_album = requests.get, me._deezer_album_enrich
    called = []
    requests.get = lambda *a, **k: R(body)
    me._deezer_album_enrich = lambda *a, **k: called.append(1)
    out = {}
    try:
        me._deezer_isrc_enrich(t, t.isrc, True, True, True, True, True, True, out)
    finally:
        requests.get, me._deezer_album_enrich = real_get, real_album
    return t, out, called

t, out, called = run("Nu Rock Compilation")
check("compilation hit: track number untouched", t.track_number in (None, 0, 1) and "track_number" not in out)
check("compilation hit: no date / disc written", "release_date" not in out and "disc_number" not in out)
check("compilation hit: album-level enrich not called", not called)

t, out, called = run("Hot Fuss")
check("control: our own album still fills track number", out.get("track_number") == 15)
check("control: our own album still fills date", out.get("release_date") == "2012-01-01")
check("control: album enrich called", bool(called))

t, out, called = run("Hot Fuss (Deluxe Edition)")
check("control: edition suffix still matches", out.get("track_number") == 15)

print("\n%d failed" % len(FAILS)); sys.exit(1 if FAILS else 0)
