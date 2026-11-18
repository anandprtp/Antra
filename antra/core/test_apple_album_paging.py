"""AUDIT-DESKTOP-12 — Apple Catalog API album: follow relationships.tracks.next.

`/albums/{id}?include=tracks` returns only the first page of the tracks
relationship and a `next` path for the rest. _fetch_album read the first page,
saw tracks, and returned them: an album longer than a page (box sets,
complete-works compilations) came back short with no signal, and the iTunes
lookup that could have supplied the rest was never tried.

Real AppleFetcher._fetch_album vs a fake session.
Run:  python antra/core/test_apple_album_paging.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from antra.core.apple_fetcher import AppleFetcher  # noqa: E402

FAILS: list[str] = []


def check(label, ok, detail=""):
    if not ok:
        FAILS.append(label)
        print(f"  FAIL  {label} :: {detail}")


def song(i):
    return {"id": str(1000 + i), "attributes": {
        "name": f"Song {i}", "artistName": "Artist", "albumName": "Box", "trackNumber": i, "discNumber": 1,
        "durationInMillis": 200000, "isrc": "USXX%08d" % i, "releaseDate": "2020-01-01"}}


class Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status
        self.ok = status < 400

    def json(self):
        return self._p

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


def make(n_tracks, page=100, fail_page2=False, ignore_next=False):
    all_t = [song(i) for i in range(1, n_tracks + 1)]
    log = []

    class Sess:
        headers = {}

        def get(self, url, headers=None, params=None, timeout=None):
            log.append(url)
            if "itunes.apple.com/lookup" in url:
                return Resp({"results": []})
            if "/tracks" in url and "offset=" in url:
                if fail_page2:
                    return Resp({}, 500)
                off = int(url.split("offset=")[1].split("&")[0])
                chunk = all_t[off:off + page]
                nxt = f"/v1/catalog/us/albums/99/tracks?offset={off + page}" if off + page < n_tracks else None
                d = {"data": chunk}
                if nxt:
                    d["next"] = nxt
                return Resp(d)
            first = all_t[:page]
            rel = {"data": first}
            if n_tracks > page and not ignore_next:
                rel["next"] = f"/v1/catalog/us/albums/99/tracks?offset={page}"
            return Resp({"data": [{"attributes": {"name": "Box", "artistName": "Artist", "upc": "123",
                                                    "releaseDate": "2020-01-01"},
                                   "relationships": {"tracks": rel}}]})

    f = AppleFetcher.__new__(AppleFetcher)
    f._session = Sess()
    f._dev_token = "tok"
    f._get_developer_token = lambda: "tok"
    return f, log


def fetch(f):
    try:
        return f._fetch_album("99", "us")
    except Exception as e:
        return f"{type(e).__name__}: {e}"


f, log = make(250)
got = fetch(f)
check("250-track album: all tracks", isinstance(got, list) and len(got) == 250, got if isinstance(got, str) else len(got))
if isinstance(got, list) and got:
    check("tracks stay in order, album fields applied",
          [t.track_number for t in got] == list(range(1, 251)) and all(t.album == "Box" for t in got))
    check("ISRCs survive from later pages", got[-1].isrc == "USXX00000250", got[-1].isrc)

f, log = make(12)
got = fetch(f)
check("control: a one-page album makes ONE request", isinstance(got, list) and len(got) == 12 and len(log) == 1,
      (got if isinstance(got, str) else len(got), len(log)))

f, log = make(250, ignore_next=True)
got = fetch(f)
check("control: no `next` -> the first page is what we have (no invented paging)",
      isinstance(got, list) and len(got) == 100, got if isinstance(got, str) else len(got))

# a failing later page must not return a silently short album as complete:
# fall through to the iTunes lookup path instead (here it finds nothing -> an error, not 100 tracks)
f, log = make(250, fail_page2=True)
got = fetch(f)
check("failed later page: the short catalog result is NOT returned as the album",
      not (isinstance(got, list) and len(got) == 100), got if isinstance(got, str) else len(got))
check("failed later page: the iTunes lookup fallback was tried", any("itunes.apple.com/lookup" in u for u in log), log)

# the page ceiling: exactly enough pages is complete, one too few is not
import antra.core.apple_fetcher as _af  # noqa: E402

_saved = _af.MAX_ALBUM_PAGES
_af.MAX_ALBUM_PAGES = 3
try:
    f, log = make(8, page=2)  # first page + 3 follow-ups = exactly the ceiling
    got = fetch(f)
    check("ceiling: exactly enough follow-up pages is a complete album",
          isinstance(got, list) and len(got) == 8, got if isinstance(got, str) else len(got))
    f, log = make(10, page=2)  # needs 4 follow-ups; the ceiling is 3
    got = fetch(f)
    check("ceiling: one page too many is NOT returned as the album",
          not (isinstance(got, list) and len(got) == 8), got if isinstance(got, str) else len(got))
finally:
    _af.MAX_ALBUM_PAGES = _saved

print("FAILED:" if FAILS else "OK", *FAILS, sep="\n  " if FAILS else " ")
sys.exit(1 if FAILS else 0)
