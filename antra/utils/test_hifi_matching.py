"""HiFi adapter: ISRC/text matching must be length-aware and survive null fields.

Run:  python antra/utils/test_hifi_matching.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from antra.core.models import TrackMetadata
from antra.sources.hifi import HifiAdapter

FAIL = []


def check(label, ok):
    if not ok:
        FAIL.append(label)


class Resp:
    status_code = 200

    def __init__(self, items):
        self._items = items

    def json(self):
        return {"data": {"items": self._items}}


class Sess:
    def __init__(self, items):
        self.items = items

    def get(self, *a, **k):
        return Resp(self.items)


def adapter(items):
    ad = HifiAdapter(endpoints=["http://ep1"])
    ad._session = Sess(items)
    ad._live_endpoints = ["http://ep1"]
    ad._live_endpoints_ts = time.time()
    return ad


def item(i, title="Song", artist="Artist", dur=200, isrc="ISRC1", **extra):
    d = {"id": i, "title": title, "artist": {"name": artist}, "duration": dur, "isrc": isrc,
         "audioQuality": "LOSSLESS", "album": {"title": "Al"}}
    d.update(extra)
    return d


track = TrackMetadata(title="Song", artists=["Artist"], album="Al", duration_ms=200000, isrc="ISRC1")

# ISRC path
r = adapter([item(1, dur=201)]).search(track)
check("control: ISRC hit of the right length accepted", r is not None and r.stream_id.endswith("|1"))
r = adapter([item(1, dur=420)]).search(track)
check("ISRC hit of a clearly wrong length is not returned as an exact match", r is None or not r.isrc_match)
r = adapter([item(1, dur=420), item(2, dur=200)]).search(track)
check("right-length ISRC item wins over a wrong-length one",
      r is not None and r.stream_id.endswith("|2") and r.isrc_match)
r = adapter([item(1, dur=None)]).search(track)
check("ISRC hit with no duration still accepted", r is not None and r.isrc_match)

# text path (no ISRC on the query track)
t2 = TrackMetadata(title="Song", artists=["Artist"], album="Al", duration_ms=200000)
r = adapter([item(1, dur=201, isrc=None)]).search(t2)
check("control: text match of the right length accepted", r is not None and r.stream_id.endswith("|1"))
r = adapter([item(1, dur=420, isrc=None)]).search(t2)
check("text match of a clearly wrong length rejected", r is None)
r = adapter([item(1, dur=420, isrc=None), item(2, dur=200, isrc=None)]).search(t2)
check("right length wins regardless of order", r is not None and r.stream_id.endswith("|2"))

# null fields must not abort the search
try:
    r = adapter([item(1, isrc=None, artist=None) | {"artist": None},
                 item(2, isrc=None)]).search(t2)
    check("null artist on one item does not kill the search", r is not None and r.stream_id.endswith("|2"))
except Exception as e:
    check(f"null artist raised {type(e).__name__}", False)
try:
    r = adapter([item(1, isrc=None) | {"album": None}]).search(t2)
    check("null album still yields a result", r is not None and r.album is None)
except Exception as e:
    check(f"null album raised {type(e).__name__}", False)
try:
    r = adapter([item(1, isrc=None) | {"title": None}, item(2, isrc=None)]).search(t2)
    check("null title does not kill the search", r is not None)
except Exception as e:
    check(f"null title raised {type(e).__name__}", False)

print("FAIL:\n  " + "\n  ".join(FAIL) if FAIL else "OK")
sys.exit(1 if FAIL else 0)
