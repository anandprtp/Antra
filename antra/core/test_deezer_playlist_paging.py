"""AUDIT-DESKTOP-12 — Deezer playlists longer than 400 tracks were truncated.

Measured live 2026-10-26: playlists of 3085 and 2731 tracks embed 400 tracks in
`/playlist/{id}` with `tracks.next = null`; `/playlist/{id}/tracks` walked all
3085 in 31 pages, an ISRC on every track. The BUG-DZ-1 note that playlists are
unaffected was true only up to 400 (it was measured on 198).

Real ExternalMusicFetcher._fetch_deezer; only `_deezer_get` is stubbed.
Run:  python antra/core/test_deezer_playlist_paging.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from antra.core.external_music_fetcher import ExternalMusicFetcher  # noqa: E402

FAILS: list[str] = []
PID = "15412498503"
URL = f"https://www.deezer.com/en/playlist/{PID}"


def check(label, ok, detail=""):
    if not ok:
        FAILS.append(label)
        print(f"  FAIL  {label} :: {detail}")


def item(n):
    return {"id": 9000 + n, "title": f"Track {n}", "duration": 180, "isrc": "USRC%08d" % n,
            "artist": {"name": "A"}, "album": {"id": 1, "title": "Al", "cover_xl": "x"}}


class F(ExternalMusicFetcher):
    def __init__(self, total, embedded_n, walk_fail_at=None, declared=True):
        self.cfg = None
        self._session = None
        self.calls: list[str] = []
        self.total, self.embedded_n, self.walk_fail_at, self.declared = total, embedded_n, walk_fail_at, declared

    def _deezer_get(self, path):
        self.calls.append(path)
        if path == f"playlist/{PID}":
            body = {"title": "P", "creator": {"name": "me"}, "description": "",
                    "tracks": {"data": [item(n) for n in range(1, self.embedded_n + 1)], "next": None}}
            if self.declared:
                body["nb_tracks"] = self.total
            return body
        if path.startswith(f"playlist/{PID}/tracks"):
            idx = int(path.split("index=")[1])
            if self.walk_fail_at is not None and idx >= self.walk_fail_at:
                raise RuntimeError("504 Gateway Time-out")
            chunk = [item(n) for n in range(idx + 1, min(idx + 100, self.total) + 1)]
            return {"data": chunk, "next": f"x?index={idx + 100}" if idx + 100 < self.total else None}
        raise AssertionError(f"unexpected request: {path}")


# the bug
f = F(total=1050, embedded_n=400)
got = f.fetch(URL)
check("1050-track playlist: all tracks", len(got) == 1050, len(got))
check("positions are contiguous", [t.playlist_position for t in got] == list(range(1, 1051)) if got else False)
check("last track present with its ISRC", got and got[-1].title == "Track 1050" and got[-1].isrc == "USRC00001050",
      got[-1].title if got else None)

# controls
f = F(total=198, embedded_n=198)
got = f.fetch(URL)
check("control: a playlist that fits makes NO /tracks request", len(got) == 198 and not any("/tracks" in c for c in f.calls),
      (len(got), f.calls))
f = F(total=400, embedded_n=400)
check("control: exactly 400 of 400 makes no /tracks request",
      len(f.fetch(URL)) == 400 and not any("/tracks" in c for c in f.calls), f.calls)
f = F(total=1050, embedded_n=400, declared=False)
check("control: no nb_tracks -> embedded list used (nothing invented)", len(f.fetch(URL)) == 400)

# a walk that fails on the first page must fall back to what was embedded; a partial walk beyond it is kept
f = F(total=1050, embedded_n=400, walk_fail_at=0)
check("walk failing immediately keeps the 400 embedded tracks", len(f.fetch(URL)) == 400)
f = F(total=1050, embedded_n=400, walk_fail_at=500)
check("walk failing after 500 keeps the 500 collected (better than 400)", len(f.fetch(URL)) == 500)

print("FAILED:" if FAILS else "OK", *FAILS, sep="\n  " if FAILS else " ")
sys.exit(1 if FAILS else 0)
