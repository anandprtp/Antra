"""AUDIT-DESKTOP-11 — the ISRC cache's negative entries were write-only.

`put_many` stores "Spotify has no ISRC for this track" for 24 h, but `get_many`
dropped every empty value, so ISRCEnricher.enrich_tracks re-queried spclient for
the same tracks on every run: the rate-limit exposure the cache exists to remove
(v1.1.3 BUG-12). Real SQLite cache in a temp dir; the network is replaced by a
function that fails the test if it is reached.

Run:  python antra/utils/test_isrc_negative_cache.py
"""
from __future__ import annotations

import importlib
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
cache_mod = importlib.import_module("antra.core.isrc_cache")
enr_mod = importlib.import_module("antra.core.isrc_enricher")
models = importlib.import_module("antra.core.models")

FAILS: list[str] = []


def check(label, ok, detail=""):
    if not ok:
        FAILS.append(label)
        print(f"  FAIL  {label} :: {detail}")


tmp = Path(tempfile.mkdtemp(prefix="isrcneg_"))
cache = cache_mod.ISRCCache(tmp / "c.db")
POS, NEG, NEW = "4cOdK2wGLETKBW3PvgPWqT", "7ouMYWpwJ422jRcDASZB7P", "0VjIjW4GlUZAMYd2vXMi3b"
cache.put_many({POS: "usum71703861"}, source="t")
cache.put_many({NEG: ""}, source="t")

# cache API
check("default get_many still returns positives only", cache.get_many([POS, NEG, NEW]) == {POS: "USUM71703861"},
      cache.get_many([POS, NEG, NEW]))
both = cache.get_many([POS, NEG, NEW], include_negative=True)
check("include_negative returns the cached negative as ''", both == {POS: "USUM71703861", NEG: ""}, both)
cache._db.execute("UPDATE track_isrc SET updated_at = ? WHERE track_id = ?", (int(time.time()) - 3 * 86400, NEG))
check("an expired negative is not returned", NEG not in cache.get_many([NEG], include_negative=True))
cache.put_many({NEG: ""}, source="t")  # fresh again

# enrich_tracks end to end
cache_mod.get_isrc_cache = lambda db_path=None: cache
net = {"calls": 0}


def no_network(self, *a, **k):
    net["calls"] += 1
    raise AssertionError("network reached")


enr = enr_mod.ISRCEnricher()
enr._get_anonymous_token = lambda: (net.__setitem__("calls", net["calls"] + 1) or (_ for _ in ()).throw(
    RuntimeError("token requested")))
enr._fetch_track_meta_via_gid = lambda tid: (net.__setitem__("calls", net["calls"] + 1), ("error", None))[1]


def trk(sid):
    return models.TrackMetadata(title="T", artists=["A"], album="B", spotify_id=sid, track_number=1)


t_pos, t_neg = trk(POS), trk(NEG)
filled = enr.enrich_tracks([t_pos, t_neg])
check("cached positive is applied", t_pos.isrc == "USUM71703861", t_pos.isrc)
check("cached negative leaves the track without an ISRC (not '')", not t_neg.isrc, repr(t_neg.isrc))
check("only the positive counts as filled", filled == 1, filled)
check("a fully-cached playlist (positive + negative) makes NO network call", net["calls"] == 0, net["calls"])

# control: a track the cache has never seen still goes to the network
t_new = trk(NEW)
enr.enrich_tracks([t_new])
check("control: an unknown track IS looked up", net["calls"] >= 1, net["calls"])

print("FAILED:" if FAILS else "OK", *FAILS, sep="\n  " if FAILS else " ")
sys.exit(1 if FAILS else 0)
