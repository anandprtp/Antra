"""AUDIT-DESKTOP-12 — MusicBrainz free-text genre lookup must verify the recording.

`_mb_text_search_genre` (used when a track has no ISRC) wrote the genre tags of
`recordings[0]` from a Lucene text search without comparing its title/artist to
the track: a cover or an unrelated song that merely ranks first donated its
genres to the file. It now takes the best-matching of the returned recordings
and requires title+artist similarity.

Real function; `requests.get` is replaced.   Run:  python antra/core/test_mb_genre_match.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from antra.core import metadata_enricher as me  # noqa: E402
from antra.core.models import TrackMetadata  # noqa: E402

FAILS: list[str] = []


def check(label, ok, detail=""):
    if not ok:
        FAILS.append(label)
        print(f"  FAIL  {label} :: {detail}")


def rec(title, artist, tags):
    return {"title": title, "artist-credit": [{"name": artist, "artist": {"name": artist}}],
            "tags": [{"name": n, "count": c} for n, c in tags]}


class R:
    ok = True

    def __init__(self, recordings):
        self._r = recordings

    def json(self):
        return {"recordings": self._r}


def run(recordings, title="Bohemian Rhapsody", artist="Queen"):
    t = TrackMetadata(title=title, artists=[artist], album="A", isrc=None)
    out: dict = {}
    orig = requests.get
    requests.get = lambda *a, **k: R(recordings)
    try:
        me._mb_text_search_genre(t, out)
    finally:
        requests.get = orig
    return t, out


WRONG = rec("Bohemian Rhapsody (Metal Cover)", "Some Tribute Band", [("metal", 9), ("cover", 5)])
RIGHT = rec("Bohemian Rhapsody", "Queen", [("rock", 20), ("glam rock", 10)])
UNRELATED = rec("Bohemian Like You", "The Dandy Warhols", [("alternative rock", 12)])

t, out = run([WRONG, RIGHT])
check("the matching recording wins over a higher-ranked wrong one", t.genres == ["Rock", "Glam Rock"], t.genres)
t, out = run([UNRELATED, WRONG])
check("no recording matches -> no genres are written", not t.genres and "genres" not in out, t.genres)
t, out = run([RIGHT, WRONG])
check("control: a correct first result still works", t.genres == ["Rock", "Glam Rock"], t.genres)
t, out = run([rec("Bohemian Rhapsody", "Queen", [])])
check("control: a match with no tags writes nothing", not t.genres)
t, out = run([{"title": None, "artist-credit": None, "tags": [{"name": "x", "count": 1}]}, RIGHT])
check("malformed recordings are skipped, not fatal", t.genres == ["Rock", "Glam Rock"], t.genres)

t, out = run([None, "junk", RIGHT])
check("non-dict entries in the list are skipped, the real match still found", t.genres == ["Rock", "Glam Rock"], t.genres)

print("FAILED:" if FAILS else "OK", *FAILS, sep="\n  " if FAILS else " ")
sys.exit(1 if FAILS else 0)
