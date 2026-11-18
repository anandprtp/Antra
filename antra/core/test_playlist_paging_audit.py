"""AUDIT-DESKTOP-11 — Spotify partner-GraphQL playlist paging and null entries.

  1. The loop advanced by `limit` and stopped on a short page: a server that
     returns fewer entries than asked truncated the playlist (100 of 250), and
     a missing totalCount stopped it after the first page.
  2. One null field in one entry (`itemV2: null`, `artists: null`, ...) raised
     AttributeError, and the outer `except` returned [] - the WHOLE playlist
     lost for one removed track.
  3. A GraphQL error body (`{"data": null}`) on a later page raised the same
     way and discarded the pages already fetched; an HTTP error there already
     kept them.

Real SpotifyClient._fetch_public_playlist_embed against a stateful fake server.
Run:  python antra/core/test_playlist_paging_audit.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import antra.core.spotify as spmod  # noqa: E402
from antra.core.spotify import SpotifyClient  # noqa: E402

spmod.time.sleep = lambda s: None  # the loop sleeps 0.15 s between pages

FAILS: list[str] = []


def check(label, ok, detail=""):
    if not ok:
        FAILS.append(label)
        print(f"  FAIL  {label} :: {detail}")


def item(i):
    return {"itemV2": {"data": {
        "uri": "spotify:track:%022d" % i, "name": f"Song {i}",
        "artists": {"items": [{"profile": {"name": "Artist"}}]},
        "albumOfTrack": {"name": "Album", "uri": "spotify:album:4aawyAB9vmqN3uQ7FjRGTy",
                         "coverArt": {"sources": [{"url": "https://i.scdn.co/a"}]}},
        "trackDuration": {"totalMilliseconds": 180000}}}}


class Resp:
    def __init__(self, payload, ok=True):
        self._p, self.ok, self.status_code = payload, ok, 200 if ok else 500

    def json(self):
        return self._p


def serve(entries, *, cap=None, report_total=True, fail_after=None, fail_body=None):
    """A fake fetchPlaylist honouring offset; `cap` clamps the page size."""
    state = {"calls": 0, "offsets": []}

    def post(url, headers=None, json=None, timeout=None):
        state["calls"] += 1
        v = json["variables"]
        off, lim = v["offset"], v["limit"]
        state["offsets"].append(off)
        if state["calls"] > 60:
            raise AssertionError("runaway pagination")
        if fail_after is not None and state["calls"] > fail_after:
            return Resp(fail_body)
        n = min(lim, cap) if cap else lim
        page = entries[off:off + n]
        content = {"items": page}
        if report_total:
            content["totalCount"] = len(entries)
        return Resp({"data": {"playlistV2": {"name": "P", "content": content}}})

    return post, state


def run(post):
    orig_post, orig_totp = spmod.requests.post, SpotifyClient._get_totp_access_token
    spmod.requests.post = post
    SpotifyClient._get_totp_access_token = lambda self: "tok"
    try:
        c = SpotifyClient.__new__(SpotifyClient)
        for a in ("sp", "_oauth_sp", "_market", "_sp_dc", "_partner_client"):
            setattr(c, a, None)
        return SpotifyClient._fetch_public_playlist_embed(c, "1uH9HT13WFmObpNl1RCng5")
    finally:
        spmod.requests.post, SpotifyClient._get_totp_access_token = orig_post, orig_totp


# 1a. server clamps pages below the requested limit
post, st = serve([item(i) for i in range(250)], cap=100)
got = run(post)
check("clamped pages: all 250 tracks", len(got) == 250, len(got))
check("clamped pages: contiguous positions", [t.playlist_position for t in got] == list(range(1, 251)) if got else False)
check("clamped pages: offsets follow what was RECEIVED", st["offsets"][:3] == [0, 100, 200], st["offsets"][:4])

# 1b. no totalCount: keep going until an empty page
post, st = serve([item(i) for i in range(2500)], cap=1000, report_total=False)
got = run(post)
check("no totalCount: all 2500 tracks", len(got) == 2500, len(got))

# 1c. a server that ignores offset must not loop forever
loop_page = [item(i) for i in range(5)]
state = {"n": 0}


def stuck(url, headers=None, json=None, timeout=None):
    state["n"] += 1
    if state["n"] > 60:
        raise AssertionError("runaway pagination")
    return Resp({"data": {"playlistV2": {"content": {"items": loop_page}}}})


got = run(stuck)
check("offset-ignoring server terminates", state["n"] <= 60 and len(got) >= 5, (state["n"], len(got)))

# controls: ordinary playlists are unchanged
post, st = serve([item(i) for i in range(2)])
got = run(post)
check("control: 2-track playlist", len(got) == 2 and st["calls"] == 1, (len(got), st["calls"]))
post, st = serve([item(i) for i in range(1000)])
got = run(post)
check("control: exactly one full page ends without a spare request",
      len(got) == 1000 and st["calls"] == 1, (len(got), st["calls"]))

# 2. null fields inside entries
entries = [item(0), {"itemV2": None}, {}, None, item(1)]
bad = item(2)
bad["itemV2"]["data"]["artists"] = None
entries.append(bad)
bad = item(3)
bad["itemV2"]["data"]["albumOfTrack"] = None
entries.append(bad)
bad = item(4)
bad["itemV2"]["data"]["trackDuration"] = None
entries.append(bad)
bad = item(5)
bad["itemV2"]["data"]["albumOfTrack"]["coverArt"] = None
entries.append(bad)
post, _ = serve(entries)
got = run(post)
check("null entries: every real track survives", [t.spotify_id for t in got] == ["%022d" % i for i in range(6)],
      [t.title for t in got])

# 3. GraphQL error body on page 2 keeps page 1
post, _ = serve([item(i) for i in range(300)], cap=100, fail_after=1, fail_body={"data": None, "errors": [{"message": "x"}]})
got = run(post)
check("error body on a later page keeps the earlier pages", len(got) == 100, len(got))

print("FAILED:" if FAILS else "OK", *FAILS, sep="\n  " if FAILS else " ")
sys.exit(1 if FAILS else 0)
