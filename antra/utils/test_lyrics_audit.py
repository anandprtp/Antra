"""AUDIT-DESKTOP-14 — lyrics: cache poisoning and LRC parsing.

  1. A transient outage was cached as "no lyrics anywhere" (3 days) and a
     plain-only hit found while a synced-capable provider errored was cached for
     7 days: every provider failing with an exception looked identical to every
     provider answering "none".
  2. A cache hit keyed by ISRC was returned without any check against THIS
     track, but one ISRC covers different-length mixes (BUG-MIX-1 cause 3), so a
     298 s version's timings were handed to a 229 s edit.
  3. lrc_to_sylt_frames only understood `[mm:ss.xx(x)]` at the start of a line:
     `[01:23]`, `[00:05.5]` were dropped and `[a][b]text` leaked the second
     timestamp into the lyric text; validate_and_strip_lrc ignored the same forms.

Real LyricsFetcher / LyricsCache (SQLite in a temp dir); providers are replaced.
Run:  python antra/utils/test_lyrics_audit.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from antra.utils import lyrics as ly  # noqa: E402
from antra.utils.lyrics_cache import LyricsCache  # noqa: E402

FAILS: list[str] = []


def check(label, ok, detail=""):
    if not ok:
        FAILS.append(label)
        print(f"  FAIL  {label} :: {detail}")


TMP = Path(tempfile.mkdtemp(prefix="lyaudit_"))
_n = [0]


def fresh_cache():
    _n[0] += 1
    return LyricsCache(TMP / f"c{_n[0]}.db")


def track(isrc="USXX12000001", dur_ms=229000, title="Song"):
    return SimpleNamespace(title=title, isrc=isrc, duration_ms=dur_ms, artists=["A"], primary_artist="A",
                           album="Al", duration_seconds=dur_ms / 1000, spotify_id=None)


def lrc(n_lines, last_s):
    step = last_s / max(1, n_lines - 1)
    return "\n".join("[%02d:%05.2f]line %d" % (int(i * step) // 60, (i * step) % 60, i) for i in range(n_lines))


def fetcher(cache, providers):
    f = ly.LyricsFetcher(use_cache=True)
    f._cache = lambda: cache
    f._providers = lambda t: providers
    return f


def boom(*a, **k):
    raise ConnectionError("offline")


# 1. outages must not be remembered
c = fresh_cache()
t = track()
f = fetcher(c, [("lrclib", boom), ("lrclib-search", boom), ("apple", lambda: (_ for _ in ()).throw(RuntimeError("500")))])
out = f.fetch(t)
check("all providers erroring -> (None, None)", out == (None, None), out)
check("...and NOTHING is cached as a negative", c.get(t) is None, c.get(t))

c = fresh_cache()
f = fetcher(c, [("lrclib", lambda: ("plain words", "")), ("apple", boom)])
out = f.fetch(t)
check("plain found while a later provider errored: plain returned", out[0] == "plain words" and out[1] is None, out)
check("...but it is not cached (the synced provider may just have been down)", c.get(t) is None, c.get(t))

c = fresh_cache()
f = fetcher(c, [("lrclib", lambda: None), ("apple", lambda: None)])
f.fetch(t)
check("control: every provider ANSWERING 'none' is cached as a negative", c.get(t) == ("", "", ""), c.get(t))
c = fresh_cache()
f = fetcher(c, [("lrclib", lambda: ("plain words", "")), ("apple", lambda: None)])
f.fetch(t)
check("control: plain-only with every provider answering IS cached", (c.get(t) or ("", "", ""))[0] == "plain words", c.get(t))
c = fresh_cache()
f = fetcher(c, [("lrclib", lambda: ("p", lrc(30, 200)))])
f.fetch(t)
check("control: a synced hit is cached", bool((c.get(t) or ("", "", ""))[1]), c.get(t))

# 2. an ISRC-keyed hit must still be plausible for THIS track
c = fresh_cache()
long_version = track(dur_ms=298000)
c.put(long_version, "p", lrc(40, 290), "lrclib")           # lyrics running to 290 s
short_edit = track(dur_ms=229000)                           # same ISRC, 229 s
calls = {"n": 0}
f = fetcher(c, [("lrclib", lambda: (calls.__setitem__("n", calls["n"] + 1), ("p2", lrc(40, 220)))[1])])
out = f.fetch(short_edit)
check("implausible cache hit is ignored and providers are consulted", calls["n"] == 1, calls)
check("the plausible provider result is returned instead", out[1] == lrc(40, 220), (out[1] or "")[:40])
f2 = fetcher(c, [("lrclib", lambda: (_ for _ in ()).throw(AssertionError("provider called")))])
same = f2.fetch(long_version) if False else None
c2 = fresh_cache()
c2.put(short_edit, "p", lrc(40, 220), "lrclib")
f3 = fetcher(c2, [("lrclib", lambda: (_ for _ in ()).throw(AssertionError("provider called")))])
try:
    out = f3.fetch(short_edit)
    check("control: a plausible cache hit is served without calling a provider", out[1] == lrc(40, 220), out)
except AssertionError as e:
    check("control: a plausible cache hit is served without calling a provider", False, str(e))

# 3. LRC parsing
frames = ly.lrc_to_sylt_frames("[00:12.00][00:45.00]chorus\n[01:23]no fraction\n[00:05.5]half\n[ar:Artist]\n[00:30.250]ms")
want = [("half", 5500), ("chorus", 12000), ("ms", 30250), ("chorus", 45000), ("no fraction", 83000)]
check("multi-timestamp, no-fraction, 1-digit and 3-digit forms -> sorted frames", frames == want, frames)
check("control: the plain form still works", ly.lrc_to_sylt_frames("[00:12.34]hello") == [("hello", 12340)],
      ly.lrc_to_sylt_frames("[00:12.34]hello"))
check("empty input", ly.lrc_to_sylt_frames("") == [])
v = ly.validate_and_strip_lrc("[00:10.00]keep\n[01:23]beyond\n[00:12.00][02:00.00]one valid\n[02:00.00][03:00.00]both beyond", 60000)
check("validate_and_strip_lrc: [mm:ss] beyond the end is stripped, a line with one valid stamp kept",
      v.splitlines() == ["[00:10.00]keep", "[00:12.00][02:00.00]one valid"], v.splitlines())

print("FAILED:" if FAILS else "OK", *FAILS, sep="\n  " if FAILS else " ")
sys.exit(1 if FAILS else 0)
