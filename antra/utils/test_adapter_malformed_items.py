"""Malformed catalogue items must be skipped, not abort the search.

Covers the shared scorer (None / non-string fields) and the TidalMirror text loop.

Run:  python antra/utils/test_adapter_malformed_items.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from antra.core.models import TrackMetadata
from antra.sources.tidal_mirror import TidalMirrorAdapter
from antra.utils.matching import normalize, score_similarity

FAIL = []


def check(label, ok):
    if not ok:
        FAIL.append(label)


# shared scorer
check("control: identical title/artist scores 1.0",
      score_similarity(query_title="Song", query_artists=["A"], result_title="Song", result_artist="A") == 1.0)
for label, kw in [("null result title", dict(result_title=None, result_artist="A")),
                  ("null result artist", dict(result_title="Song", result_artist=None)),
                  ("numeric title", dict(result_title=1999, result_artist="A")),
                  ("null query artists", dict(result_title="Song", result_artist="A", query_artists=None))]:
    args = dict(query_title="Song", query_artists=["A"])
    args.update(kw)
    try:
        s = score_similarity(**args)
        check(f"scorer {label}: returns a number", isinstance(s, float))
    except Exception as e:
        check(f"scorer {label} raised {type(e).__name__}", False)
check("normalize(None) is empty", normalize(None) == "")
check("a numeric title can still match its own text",
      score_similarity(query_title="1999", query_artists=["Prince"], result_title=1999, result_artist="Prince") == 1.0)


# TidalMirror text loop
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


def run(items):
    ad = TidalMirrorAdapter("http://mirror.invalid")
    ad._session = Sess(items)
    t = TrackMetadata(title="Song", artists=["Artist"], album="Al", duration_ms=200000)   # no ISRC: text path
    try:
        return ad.search(t), None
    except Exception as e:
        return None, e


good = {"id": 2, "title": "Song", "artist": {"name": "Artist"}, "duration": 200, "audioQuality": "LOSSLESS"}
r, err = run([good])
check("control: well-formed item accepted", err is None and r is not None and r.stream_id == "2")
for label, bad in [("null title", {"id": 1, "title": None, "artist": {"name": "Artist"}, "duration": 200}),
                   ("missing id", {"title": "Song", "artist": {"name": "Artist"}, "duration": 200}),
                   ("string duration", {"id": 1, "title": "Song", "artist": {"name": "Artist"}, "duration": "abc"})]:
    r, err = run([bad, good])
    check(f"tidal_mirror {label}: search survives", err is None)
    check(f"tidal_mirror {label}: the good item still wins", r is not None and r.stream_id == "2")

print("FAIL:\n  " + "\n  ".join(FAIL) if FAIL else "OK")
sys.exit(1 if FAIL else 0)
