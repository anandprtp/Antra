"""AUDIT-DESKTOP-17 - the direct-account adapters (qobuz.py, deezer.py, tidal.py)
treated duration as a discount, which for an exact match is no check at all.

  MIN_SIMILARITY is 0.80 and the duration discount is x0.8, so an exact title and
  artist match (1.0) whose length is wildly wrong (a 420 s extended mix offered
  for a 262 s radio edit) scored exactly 0.80 and was ACCEPTED. The mirror
  adapters hard-reject a severe mismatch; these three did not.

  Also found while reading the same code:
   * Qobuz: a high-scoring item that could not be turned into a result still
     raised best_score, so a lower-scoring VALID item could no longer win.
   * Tidal: an ISRC match with a wrong length kept isrc_match=True; the
     Odesli-resolved id path returned a cross-platform GUESS at 1.0 with no check
     at all; `getattr(track, "isrc", "").upper()` raised when isrc was None and
     the surrounding except reported the valid track as "could not be loaded";
     a candidate whose duration was None raised out of the whole search.
   * The text queries searched for the literal artist "Unknown Artist".

Real adapter methods; only the network edge is faked.
Run:  python antra/utils/test_direct_adapter_duration.py
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
models = importlib.import_module("antra.core.models")
qobuz = importlib.import_module("antra.sources.qobuz")
deezer = importlib.import_module("antra.sources.deezer")
tidal = importlib.import_module("antra.sources.tidal")
matching = importlib.import_module("antra.utils.matching")

FAILS: list[str] = []


def check(label, ok, detail=""):
    if not ok:
        FAILS.append(label)
        print(f"  FAIL  {label} :: {detail}")


def track(**kw):
    base = dict(title="Song", artists=["Band"], album="Al", album_artists=["Band"], release_year=2020,
                track_number=1, disc_number=1, total_tracks=10, duration_ms=262000, isrc=None)
    base.update(kw)
    return models.TrackMetadata(**base)


class Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status
        self.ok = status < 400

    def json(self):
        return self._p

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


def attempt(fn):
    try:
        return fn()
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def accepted(res, stream_id=None):
    return isinstance(res, models.SearchResult) and (stream_id is None or res.stream_id == stream_id)


# ── the shared rule ──────────────────────────────────────────────────────────
rule = getattr(matching, "has_severe_duration_mismatch", None)
check("matching.has_severe_duration_mismatch exists", callable(rule))
if callable(rule):
    check("rule: 420 s offered for 262 s is severe", rule(262000, 420000) is True)
    check("rule: 120 s offered for 262 s is severe", rule(262000, 120000) is True)
    check("rule: 265 s vs 262 s is not", rule(262000, 265000) is False)
    check("rule: a 12 s difference is not (the x0.8 discount still covers it)", rule(262000, 274000) is False)
    check("rule: a short track is not condemned by percentages alone (15 s off a 60 s track)", rule(60000, 45000) is False)
    check("rule: ...nor a long one by percentages alone (11 s over a 30 s track)", rule(30000, 41000) is False)
    check("rule: unknown on either side is never severe", rule(None, 100000) is False and rule(262000, None) is False
          and rule(0, 100000) is False and rule(262000, 0) is False)


# ── Qobuz ────────────────────────────────────────────────────────────────────
def qobuz_item(i, title="Song", artist="Band", dur=262, **kw):
    d = {"id": i, "title": title, "performer": {"name": artist}, "duration": dur, "album": {"title": "Al"},
         "maximum_bit_depth": 24, "maximum_sampling_rate": 96.0}
    d.update(kw)
    return d


def qobuz_search(items, tr):
    a = qobuz.QobuzAdapter()
    a._login = lambda: None
    a._api_get = lambda ep, **k: Resp({"tracks": {"items": items}})
    return attempt(lambda: a._search_by_text(tr))


check("qobuz: an exact match 420 s long for a 262 s track is rejected",
      qobuz_search([qobuz_item(1, dur=420)], track()) is None, qobuz_search([qobuz_item(1, dur=420)], track()))
check("qobuz: a much shorter exact match is rejected", qobuz_search([qobuz_item(1, dur=120)], track()) is None)
check("qobuz: control: the right length is accepted", accepted(qobuz_search([qobuz_item(1, dur=263)], track()), "1"))
check("qobuz: control: a 12 s difference is still accepted (discount band)", accepted(qobuz_search([qobuz_item(1, dur=274)], track()), "1"))
check("qobuz: the right-length candidate wins over a wrong-length one listed first",
      accepted(qobuz_search([qobuz_item(1, dur=420), qobuz_item(2, dur=262)], track()), "2"),
      qobuz_search([qobuz_item(1, dur=420), qobuz_item(2, dur=262)], track()))
bad = {"title": "Song", "performer": {"name": "Band"}, "duration": 262}      # no id: _item_to_result -> None
check("qobuz: an unbuildable top item does not block a valid lower-scoring one",
      accepted(qobuz_search([bad, qobuz_item(7, artist="Band & Guest")], track()), "7"),
      qobuz_search([bad, qobuz_item(7, artist="Band & Guest")], track()))

queries = []
a = qobuz.QobuzAdapter()
a._login = lambda: None
a._api_get = lambda ep, **k: (queries.append(k["params"]["query"]), Resp({"tracks": {"items": []}}))[1]
a._search_by_text(track(artists=[]))
check("qobuz: the query never contains the placeholder 'Unknown Artist'", queries and "Unknown" not in queries[0], queries)


# ── Deezer ───────────────────────────────────────────────────────────────────
def deezer_item(i, title="Song", artist="Band", dur=262):
    return {"id": i, "title": title, "artist": {"name": artist}, "duration": dur, "album": {"title": "Al"}}


def deezer_search(items, tr):
    a = deezer.DeezerAdapter("arl")
    a._session = SimpleNamespace(get=lambda url, **k: Resp({"data": items}))
    return attempt(lambda: a._search_by_text(tr))


check("deezer: an exact match 420 s long for a 262 s track is rejected",
      deezer_search([deezer_item(1, dur=420)], track()) is None, deezer_search([deezer_item(1, dur=420)], track()))
check("deezer: control: the right length is accepted", accepted(deezer_search([deezer_item(1, dur=262)], track()), "1"))
check("deezer: control: a 12 s difference is still accepted", accepted(deezer_search([deezer_item(1, dur=274)], track()), "1"))
check("deezer: the right-length candidate wins over a wrong-length one listed first",
      accepted(deezer_search([deezer_item(1, dur=420), deezer_item(2, dur=262)], track()), "2"),
      deezer_search([deezer_item(1, dur=420), deezer_item(2, dur=262)], track()))
dq = []
a = deezer.DeezerAdapter("arl")
a._session = SimpleNamespace(get=lambda url, params=None, **k: (dq.append(params["q"]), Resp({"data": []}))[1])
a._search_by_text(track(artists=[]))
check("deezer: the query never contains the placeholder 'Unknown Artist'", dq and "Unknown" not in dq[0], dq)


# ── Tidal ────────────────────────────────────────────────────────────────────
def ttrack(i, title="Song", artist="Band", dur=262, isrc=None, **kw):
    t = SimpleNamespace(id=i, name=title, artist=SimpleNamespace(name=artist), album=SimpleNamespace(name="Al"),
                        duration=dur, isrc=isrc, audio_quality="LOSSLESS", explicit=False)
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def tidal_search(tracks, tr, odesli_id=None, session_track=None):
    a = tidal.TidalAdapter()
    sess = SimpleNamespace(search=lambda q, **k: {"tracks": tracks}, track=lambda i: session_track)
    a._get_session = lambda: sess
    a._odesli = SimpleNamespace(resolve=lambda t: ({"tidal": odesli_id} if odesli_id else {}))
    return attempt(lambda: a.search(tr))


check("tidal: an exact match 420 s long for a 262 s track is rejected",
      tidal_search([ttrack(1, dur=420)], track()) is None, tidal_search([ttrack(1, dur=420)], track()))
check("tidal: control: the right length is accepted", accepted(tidal_search([ttrack(1, dur=262)], track()), "1"))
check("tidal: control: a 12 s difference is still accepted", accepted(tidal_search([ttrack(1, dur=274)], track()), "1"))
check("tidal: the right-length candidate wins over a wrong-length one listed first",
      accepted(tidal_search([ttrack(1, dur=420), ttrack(2, dur=262)], track()), "2"),
      tidal_search([ttrack(1, dur=420), ttrack(2, dur=262)], track()))

r = tidal_search([ttrack(1, dur=420, isrc="USXX12000001")], track(isrc="USXX12000001"))
check("tidal: an ISRC match of a wildly different length is not trusted (shared ISRC across mixes)",
      r is None or (accepted(r) and r.isrc_match is False), r if isinstance(r, str) else getattr(r, "isrc_match", None))
check("tidal: control: an ISRC match of the right length keeps isrc_match",
      accepted(tidal_search([ttrack(1, dur=262, isrc="USXX12000001")], track(isrc="USXX12000001")), "1")
      and tidal_search([ttrack(1, dur=262, isrc="USXX12000001")], track(isrc="USXX12000001")).isrc_match is True)

r = tidal_search([ttrack(1, dur=None), ttrack(2, dur=262)], track())
check("tidal: a candidate with duration None does not raise out of the search", accepted(r), r)

# the Odesli-resolved id path
good = ttrack(55, dur=262, isrc=None)
r = tidal_search([], track(isrc="USXX12000001"), odesli_id="55", session_track=good)
check("tidal: an Odesli-resolved track whose isrc is None is still used (was reported 'could not be loaded')",
      accepted(r, "55"), r)
wrong = ttrack(56, dur=420, isrc="USXX12000001")
r = tidal_search([], track(isrc="USXX12000001"), odesli_id="56", session_track=wrong)
check("tidal: an Odesli-resolved track of a wildly different length is not returned at 1.0", not accepted(r, "56"), r)
fine = ttrack(57, dur=263, isrc="USXX12000001")
r = tidal_search([], track(isrc="USXX12000001"), odesli_id="57", session_track=fine)
check("tidal: control: an Odesli-resolved track of the right length is returned", accepted(r, "57") and r.isrc_match is True, r)

tq = []
a = tidal.TidalAdapter()
a._get_session = lambda: SimpleNamespace(search=lambda q, **k: (tq.append(q), {"tracks": []})[1])
a._odesli = SimpleNamespace(resolve=lambda t: {})
a.search(track(artists=[]))
check("tidal: the query never contains the placeholder 'Unknown Artist'", tq and "Unknown" not in tq[0], tq)

print("FAILED:" if FAILS else "OK", *FAILS, sep="\n  " if FAILS else " ")
sys.exit(1 if FAILS else 0)
