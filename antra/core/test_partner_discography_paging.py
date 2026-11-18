"""AUDIT-DESKTOP-13 — SpotifyPartnerClient.fetch_artist_discography_info paging.

The loop advanced `offset` by the requested `limit` (50) and stopped when that
passed `totalCount`. A server that returns fewer groups per page than asked
(the endpoint is already known to cap its defaults) therefore skipped groups
(offsets 0, 50, ... while the pages held 20 each) or ended the discography after
the first page. It now advances by the groups RECEIVED and stops on an empty page.

Real method; `_query` / `_artist_overview_safe` replaced.
Run:  python antra/core/test_partner_discography_paging.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from antra.core.spotify_partner import SpotifyPartnerClient  # noqa: E402

FAILS: list[str] = []


def check(label, ok, detail=""):
    if not ok:
        FAILS.append(label)
        print(f"  FAIL  {label} :: {detail}")


def group(i):
    return {"releases": {"items": [{"uri": "spotify:album:%022d" % i, "name": f"Rel {i}", "date": {"year": 2000 + i % 20},
                                    "type": "ALBUM", "tracks": {"totalCount": 10}}]}}


def make(n_groups, cap, total_semantics="groups"):
    groups = [group(i) for i in range(1, n_groups + 1)]
    log = []

    def query(op, variables):
        log.append(variables["offset"])
        if len(log) > 120:
            raise AssertionError("runaway pagination")
        off, lim = variables["offset"], variables["limit"]
        page = groups[off:off + min(lim, cap)]
        total = n_groups if total_semantics == "groups" else n_groups * 3  # e.g. counts releases, not groups
        allb = {"items": page}
        if total_semantics != "none":
            allb["totalCount"] = total
        return {"data": {"artistUnion": {"discography": {"all": allb}}}}

    c = SpotifyPartnerClient.__new__(SpotifyPartnerClient)
    c._query = query
    c._artist_overview_safe = lambda uri: {"profile": {"name": "Artist"}, "visuals": {}, "discography": {}}
    return c, log


def fetch(c):
    try:
        return c.fetch_artist_discography_info("4aawyAB9vmqN3uQ7FjRGTy")
    except Exception as e:
        return f"{type(e).__name__}: {e}"


c, log = make(45, cap=20)
got = fetch(c)
n = len(got["albums"]) if isinstance(got, dict) else got
check("page clamped to 20 of a requested 50: all 45 releases", n == 45, n)
check("offsets follow what was received", log[:3] == [0, 20, 40], log[:5])

c, log = make(45, cap=20, total_semantics="releases")
got = fetch(c)
n = len(got["albums"]) if isinstance(got, dict) else got
check("totalCount counting releases rather than groups: still all 45, and it terminates", n == 45 and len(log) <= 6, (n, log))

c, log = make(45, cap=20, total_semantics="none")
got = fetch(c)
n = len(got["albums"]) if isinstance(got, dict) else got
check("no totalCount at all: pages until an empty page, all 45 releases", n == 45, (n, log))

c, log = make(30, cap=50)
got = fetch(c)
n = len(got["albums"]) if isinstance(got, dict) else got
check("control: 30 groups under the cap -> 30 releases", n == 30, n)

c, log = make(120, cap=50)
got = fetch(c)
n = len(got["albums"]) if isinstance(got, dict) else got
check("control: an unclamped server pages 50 at a time (3 pages) and returns all 120", n == 120 and log[:3] == [0, 50, 100], (n, log))

print("FAILED:" if FAILS else "OK", *FAILS, sep="\n  " if FAILS else " ")
sys.exit(1 if FAILS else 0)
