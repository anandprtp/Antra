"""Playlist preview header: owner, cover size, and the year-fill cost.

Three defects, all on the path the desktop takes when the VPS metadata proxy
is unreachable (measured 2026-09-26 on the reported playlist):

  1. the header showed the FIRST TRACK'S ARTISTS instead of the playlist owner
     -- `playlist_owner` was only ever read on the embed-scrape branch, which
     runs solely when TOTP fails, i.e. almost never;
  2. the cover came back at 60px, because the code took `sources[-1]` with the
     comment "largest image last" while a mosaic cover is ordered 640/300/60;
  3. the fetch took 37.6s, of which 37.3s was 48 SERIAL public-metadata fills.

Payload shapes are copied verbatim from the live partner GraphQL response, so
the parsing assertions are about our code and not about Spotify's uptime.

Run:  python -m antra.core.test_playlist_header
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from antra.core.models import TrackMetadata          # noqa: E402
from antra.core.spotify import SpotifyClient         # noqa: E402

FAILS: list[str] = []
CHECKS = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not ok:
        FAILS.append("%s%s" % (label, (" -- " + detail) if detail else ""))
        print("  FAIL  %s%s" % (label, (" -- " + detail) if detail else ""))


# ── fixtures, copied from the live response ──────────────────────────────────

def track_item(name="Song", uri_id="1a2b3c4d5e6f7g8h9i0jkl"):
    return {
        "itemV2": {"data": {
            "uri": "spotify:track:" + uri_id,
            "name": name,
            "artists": {"items": [{"profile": {"name": "An Artist"}}]},
            # measured: albumOfTrack carries NO date on this query
            "albumOfTrack": {
                "name": "An Album",
                "uri": "spotify:album:4aawyAB9vmqN3uQ7FjRGTy",
                "coverArt": {"sources": [{"url": "https://i.scdn.co/image/art"}]},
            },
            "trackDuration": {"totalMilliseconds": 180000},
        }}
    }


def playlist_payload(sources, owner_name="Anand", n_tracks=2, description=""):
    return {"data": {"playlistV2": {
        "name": "Cole, Kenny, Drake, The weeknd & Jiddy",
        "description": description,
        "ownerV2": {"data": {"__typename": "User", "name": owner_name,
                             "username": "315nkhmo2vwtasgl42ritn2dmy6q"}},
        "images": {"items": [{"sources": sources}]},
        "content": {"totalCount": n_tracks,
                    "items": [track_item("Song %d" % i, "%022d" % i)
                              for i in range(n_tracks)]},
    }}}


MOSAIC_DESC = [  # the reported ordering: LARGEST FIRST
    {"url": "https://mosaic.scdn.co/640/abc", "width": 640, "height": 640},
    {"url": "https://mosaic.scdn.co/300/abc", "width": 300, "height": 300},
    {"url": "https://mosaic.scdn.co/60/abc", "width": 60, "height": 60},
]
MOSAIC_ASC = list(reversed(MOSAIC_DESC))
SINGLE = [{"url": "https://i.scdn.co/image/one", "width": 640, "height": 640}]
NO_WIDTH = [{"url": "https://i.scdn.co/image/a"}, {"url": "https://i.scdn.co/image/b"}]


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.ok = True
        self.status_code = 200

    def json(self):
        return self._payload


def fetch_with(payload, monkey):
    """Drive the REAL _fetch_public_playlist_embed against a stubbed transport."""
    import antra.core.spotify as spmod

    calls = {"n": 0}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls["n"] += 1
        # second page must come back empty or the loop never ends
        if calls["n"] > 1:
            empty = {"data": {"playlistV2": {"content": {"items": [], "totalCount": 0}}}}
            return FakeResponse(empty)
        return FakeResponse(payload)

    client = SpotifyClient.__new__(SpotifyClient)
    for attr in ("sp", "_oauth_sp", "_market", "_sp_dc", "_partner_client"):
        if not hasattr(client, attr):
            setattr(client, attr, None)
    monkey(spmod, fake_post)
    return SpotifyClient._fetch_public_playlist_embed(client, "1uH9HT13WFmObpNl1RCng5")


def run_fetch(payload):
    import antra.core.spotify as spmod

    orig_post = spmod.requests.post
    orig_totp = SpotifyClient._get_totp_access_token
    try:
        SpotifyClient._get_totp_access_token = lambda self: "fake-token"
        return fetch_with(payload, lambda m, f: setattr(m.requests, "post", f))
    finally:
        spmod.requests.post = orig_post
        SpotifyClient._get_totp_access_token = orig_totp


def main() -> int:
    # ── 1. the reported bug: the owner ───────────────────────────────────────
    print("\n[1] playlist owner")
    tracks = run_fetch(playlist_payload(MOSAIC_DESC))
    check("tracks came back", len(tracks) == 2, str(len(tracks)))
    check("owner is taken from ownerV2.data.name",
          tracks and tracks[0].playlist_owner == "Anand",
          repr(tracks[0].playlist_owner if tracks else None))
    check("every track carries it", all(t.playlist_owner == "Anand" for t in tracks))
    check("the playlist name is unaffected",
          tracks[0].playlist_name == "Cole, Kenny, Drake, The weeknd & Jiddy")

    # CONTROL: a payload with no owner must NOT invent one, and must not crash.
    no_owner = playlist_payload(MOSAIC_DESC)
    no_owner["data"]["playlistV2"]["ownerV2"] = {}
    t2 = run_fetch(no_owner)
    check("CONTROL: no ownerV2 -> owner stays None",
          t2 and t2[0].playlist_owner is None, repr(t2[0].playlist_owner if t2 else None))

    # username is the fallback when a display name is absent
    uname = playlist_payload(MOSAIC_DESC)
    uname["data"]["playlistV2"]["ownerV2"]["data"].pop("name")
    t3 = run_fetch(uname)
    check("falls back to username when there is no display name",
          t3 and t3[0].playlist_owner == "315nkhmo2vwtasgl42ritn2dmy6q",
          repr(t3[0].playlist_owner if t3 else None))

    # ── 2. the header string the UI actually renders ─────────────────────────
    print("\n[2] header string")
    from antra.json_cli import _playlist_artists_string

    owned = TrackMetadata(title="t", artists=["J. Cole", "21 Savage", "Morray"],
                          album="The Off-Season", duration_ms=1,
                          playlist_name="Cole, Kenny...", playlist_owner="Anand")
    check("a playlist renders its OWNER", _playlist_artists_string([owned]) == "Anand",
          _playlist_artists_string([owned]))
    # CONTROL: this is exactly the reported wrong output, and it must be what
    # you get when the owner is missing -- otherwise [1] proves nothing.
    orphan = TrackMetadata(title="t", artists=["J. Cole", "21 Savage", "Morray"],
                           album="The Off-Season", duration_ms=1,
                           playlist_name="Cole, Kenny...")
    check("CONTROL: without an owner it falls back to the first track's artists",
          _playlist_artists_string([orphan]) == "J. Cole, 21 Savage & Morray",
          _playlist_artists_string([orphan]))
    album = TrackMetadata(title="t", artists=["Drake"], album="Scorpion", duration_ms=1,
                          album_artists=["Drake"])
    check("an ALBUM still renders its artist, not an owner",
          _playlist_artists_string([album]) == "Drake", _playlist_artists_string([album]))

    # ── 3. the cover size ────────────────────────────────────────────────────
    print("\n[3] cover art")
    for label, sources in (("largest first (the reported mosaic)", MOSAIC_DESC),
                           ("largest last", MOSAIC_ASC)):
        got = run_fetch(playlist_payload(sources))
        url = got[0].playlist_artwork_url if got else ""
        check("%s -> picks 640" % label, "/640/" in (url or ""), repr(url))
    got = run_fetch(playlist_payload(SINGLE))
    check("a single source is used as-is",
          got and got[0].playlist_artwork_url == SINGLE[0]["url"],
          repr(got[0].playlist_artwork_url if got else None))
    got = run_fetch(playlist_payload(NO_WIDTH))
    check("sources with no width still yield a url",
          got and (got[0].playlist_artwork_url or "").startswith("https://"),
          repr(got[0].playlist_artwork_url if got else None))

    # ── 4. the year fill is parallel ─────────────────────────────────────────
    print("\n[4] year fill")
    from antra.core.service import AntraService, _YEAR_FILL_WORKERS

    DELAY = 0.25
    N = 16

    class SlowClient:
        def __init__(self):
            self.calls = 0

        def enrich_public_track_metadata(self, track):
            time.sleep(DELAY)
            self.calls += 1
            track.release_year = 2021
            return track

    def measure():
        svc = AntraService.__new__(AntraService)
        client = SlowClient()
        svc._make_spotify_client = lambda cfg: client
        tracks = [TrackMetadata(title="t%d" % i, artists=["a"], album="al", duration_ms=1)
                  for i in range(N)]
        t0 = time.time()
        out = AntraService._fill_missing_years(svc, tracks, cfg=None)
        return time.time() - t0, out, client

    elapsed, out, client = measure()
    serial = DELAY * N
    check("every missing year was filled", all(t.release_year == 2021 for t in out))
    check("one call per track", client.calls == N, str(client.calls))
    check("it ran in parallel (%.2fs vs %.2fs serial)" % (elapsed, serial),
          elapsed < serial / 2, "%.2fs" % elapsed)
    # The pool size is the bound, so the floor is ceil(N/workers) * DELAY.
    floor = (N + _YEAR_FILL_WORKERS - 1) // _YEAR_FILL_WORKERS * DELAY
    check("and no faster than the pool allows (>= %.2fs)" % floor,
          elapsed >= floor * 0.8, "%.2fs" % elapsed)

    # a track that already has a year is never touched
    svc = AntraService.__new__(AntraService)
    client = SlowClient()
    svc._make_spotify_client = lambda cfg: client
    done = [TrackMetadata(title="t", artists=["a"], album="al", duration_ms=1,
                          release_year=1999)]
    AntraService._fill_missing_years(svc, done, cfg=None)
    check("CONTROL: a filled list makes no calls at all", client.calls == 0, str(client.calls))

    # a failing fill must not take the others down
    class HalfBroken(SlowClient):
        def enrich_public_track_metadata(self, track):
            self.calls += 1
            if track.title.endswith("3"):
                raise RuntimeError("boom")
            track.release_year = 2021
            return track

    svc = AntraService.__new__(AntraService)
    broken = HalfBroken()
    svc._make_spotify_client = lambda cfg: broken
    tracks = [TrackMetadata(title="t%d" % i, artists=["a"], album="al", duration_ms=1)
              for i in range(5)]
    out = AntraService._fill_missing_years(svc, tracks, cfg=None)
    check("one failing fill does not abort the rest",
          sum(1 for t in out if t.release_year == 2021) == 4,
          str([t.release_year for t in out]))

    print("\n%d checks, %d failed" % (CHECKS, len(FAILS)))
    for f in FAILS:
        print("  - " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
