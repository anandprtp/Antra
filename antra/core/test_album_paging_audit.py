"""AUDIT-DESKTOP-12 — Spotify partner-GraphQL ALBUM paging and null fields.

Same defects the playlist loop had (AUDIT-DESKTOP-11), in `getAlbum`:
  1. paging advanced by `limit` and ended on a short page / null totalCount:
     a server that clamps the page truncated the album.
  2. album-level fields (`date`, `artists`, `coverArt`, `tracksV2`) and the
     playability list comprehension sat OUTSIDE any try, so one null raised out
     of the fetch; and a single track with `artists: null` was silently dropped
     by the per-track except.
  3. `{"data": null}` on a later page raised instead of ending paging.

Real SpotifyClient._fetch_album_via_partner_api vs a stateful fake server.
Run:  python antra/core/test_album_paging_audit.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import antra.core.spotify as spmod  # noqa: E402
from antra.core.spotify import SpotifyClient  # noqa: E402

spmod.time.sleep = lambda s: None
FAILS: list[str] = []


def check(label, ok, detail=""):
    if not ok:
        FAILS.append(label)
        print(f"  FAIL  {label} :: {detail}")


def trk(i, disc=1):
    return {"track": {
        "uri": "spotify:track:%022d" % i, "name": f"Song {i}", "trackNumber": i, "discNumber": disc,
        "artists": {"items": [{"profile": {"name": "Artist"}}]},
        "duration": {"totalMilliseconds": 200000}, "playability": {"playable": True}}}


class Resp:
    def __init__(self, payload, ok=True):
        self._p, self.ok, self.status_code, self.text = payload, ok, 200 if ok else 500, ""

    def json(self):
        return self._p


def album_union(page, total, **over):
    au = {"name": "Alb", "artists": {"items": [{"profile": {"name": "Artist"}}]},
          "date": {"isoString": "2020-05-01T00:00:00Z"},
          "coverArt": {"sources": [{"url": "https://i.scdn.co/c", "width": 640, "height": 640}]},
          "tracksV2": {"items": page}}
    if total is not None:
        au["tracksV2"]["totalCount"] = total
    au.update(over)
    return {"data": {"albumUnion": au}}


def serve(entries, *, cap=None, report_total=True, bad_after=None, **over):
    st = {"calls": 0, "offsets": []}

    def post(url, headers=None, json=None, timeout=None):
        st["calls"] += 1
        if st["calls"] > 60:
            raise AssertionError("runaway pagination")
        v = json["variables"]
        st["offsets"].append(v["offset"])
        if bad_after is not None and st["calls"] > bad_after:
            return Resp({"data": None, "errors": [{"message": "x"}]})
        n = min(v["limit"], cap) if cap else v["limit"]
        page = entries[v["offset"]:v["offset"] + n]
        return Resp(album_union(page, len(entries) if report_total else None, **over))

    return post, st


_PATCH = {
    "_fetch_full_album_data": lambda s, a: {},
    "_extract_available_markets": lambda s, f: [],
    "_raise_if_album_unavailable": lambda s, a, f, m: None,
    "_album_available_in_market": lambda s, m: True,
    "_format_availability_note": lambda s, m: "",
    "_get_anonymous_access_token": lambda s, *a: "tok",
    "_current_market": lambda s: "US",
}


def run(post):
    saved = {k: getattr(SpotifyClient, k) for k in _PATCH}
    orig_post = spmod.requests.post
    spmod.requests.post = post
    for k, v in _PATCH.items():
        setattr(SpotifyClient, k, v)
    try:
        c = SpotifyClient.__new__(SpotifyClient)
        for a in ("sp", "_oauth_sp", "_market", "_sp_dc", "_partner_client"):
            setattr(c, a, None)
        return SpotifyClient._fetch_album_via_partner_api(c, "4aawyAB9vmqN3uQ7FjRGTy")
    finally:
        spmod.requests.post = orig_post
        for k, v in saved.items():
            setattr(SpotifyClient, k, v)


def attempt(post):
    """run(), but an exception becomes a failed check instead of a crash."""
    try:
        return run(post)
    except AssertionError:
        raise
    except Exception as e:
        return f"{type(e).__name__}: {e}"


# 1. paging
post, st = serve([trk(i) for i in range(1, 251)], cap=100)
got = attempt(post)
check("clamped pages: all 250 tracks", isinstance(got, list) and len(got) == 250, got if isinstance(got, str) else len(got))
check("clamped pages: offsets follow what was received", st["offsets"][:3] == [0, 100, 200], st["offsets"][:4])
post, st = serve([trk(i) for i in range(1, 701)], cap=300, report_total=False)
got = attempt(post)
check("no totalCount: all 700 tracks", isinstance(got, list) and len(got) == 700, got if isinstance(got, str) else len(got))
loop_page = [trk(i) for i in range(1, 6)]
n = {"c": 0}


def stuck(url, headers=None, json=None, timeout=None):
    n["c"] += 1
    if n["c"] > 60:
        raise AssertionError("runaway pagination")
    return Resp(album_union(loop_page, None))


try:
    got = run(stuck)
    check("offset-ignoring server terminates", n["c"] <= 60 and len(got) >= 5, (n["c"], len(got)))
except AssertionError as e:
    check("offset-ignoring server terminates", False, str(e))

# controls
post, st = serve([trk(i) for i in range(1, 13)])
got = attempt(post)
check("control: 12-track album, one request", isinstance(got, list) and len(got) == 12 and st["calls"] == 1,
      (got if isinstance(got, str) else len(got), st["calls"]))
if isinstance(got, list) and got:
    check("control: numbering, disc and duration kept",
          [t.track_number for t in got] == list(range(1, 13)) and got[0].disc_number == 1 and got[0].duration_ms == 200000)
    check("control: album fields kept", got[0].album == "Alb" and got[0].release_year == 2020 and got[0].artists == ["Artist"])

# 2. nulls
post, _ = serve([trk(i) for i in range(1, 4)], date=None, coverArt=None, artists=None)
got = attempt(post)
check("null date / coverArt / artists on the album: tracks still returned", isinstance(got, list) and len(got) == 3,
      got if isinstance(got, str) else len(got))
post, _ = serve([trk(i) for i in range(1, 4)], tracksV2={"items": [trk(1), trk(2)], "totalCount": None})
got = attempt(post)
check("null totalCount: tracks returned", isinstance(got, list) and len(got) == 2, got if isinstance(got, str) else len(got))
entries = [trk(i) for i in range(1, 6)]
entries[1]["track"]["artists"] = None            # keep the track, use the album artists
entries[2]["track"]["duration"] = None
entries[3]["track"]["playability"] = None
entries[4] = None
post, _ = serve(entries)
got = attempt(post)
check("null track fields keep the tracks (4 of 5; the None entry is dropped)", isinstance(got, list) and len(got) == 4,
      got if isinstance(got, str) else [t.title for t in got])
check("track with null artists falls back to the album artists",
      isinstance(got, list) and len(got) > 1 and got[1].artists == ["Artist"], got if isinstance(got, str) else None)

post, _ = serve([trk(1)], tracksV2=None)
got = attempt(post)
check("tracksV2: null -> empty result, no exception", got == [], got)

# blank artist name must not mask the album-artist fallback; bare-year date must not raise
entries = [trk(1), trk(2)]
entries[0]["track"]["artists"] = {"items": [{"profile": {"name": ""}}]}
post, _ = serve(entries, date={"year": 2019})
got = attempt(post)
check("blank artist name falls back to the album artists",
      isinstance(got, list) and got and got[0].artists == ["Artist"], got if isinstance(got, str) else None)
check("bare-year date is used", isinstance(got, list) and got and got[0].release_year == 2019,
      got if isinstance(got, str) else None)

# 3. error body on a later page keeps earlier pages
post, _ = serve([trk(i) for i in range(1, 301)], cap=100, bad_after=1)
got = attempt(post)
check("error body on a later page keeps the earlier pages", isinstance(got, list) and len(got) == 100,
      got if isinstance(got, str) else len(got))

print("FAILED:" if FAILS else "OK", *FAILS, sep="\n  " if FAILS else " ")
sys.exit(1 if FAILS else 0)
