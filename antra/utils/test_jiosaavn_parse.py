"""JioSaavn: odd-but-valid API shapes must not drop a result; cut streams must not land.

Run:  python antra/utils/test_jiosaavn_parse.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from antra.sources import jiosaavn
from antra.sources.jiosaavn import JioSaavnAdapter, _to_seconds

FAIL = []


def check(label, ok):
    if not ok:
        FAIL.append(label)


# _to_seconds
for v, want in [("245", 245.0), (245, 245.0), (245.5, 245.5), ("", None), (None, None), ("abc", None),
                (0, None), ("0", None), (-3, None)]:
    check(f"_to_seconds({v!r})", _to_seconds(v) == want)

ad = JioSaavnAdapter()

# community shape: duration "" / null / missing, artists null
for label, item in [
    ("empty duration", {"id": "1", "name": "Song", "duration": "", "artists": {"primary": [{"name": "A"}]}}),
    ("null duration", {"id": "1", "name": "Song", "duration": None, "artists": {"primary": [{"name": "A"}]}}),
    ("null artists", {"id": "1", "name": "Song", "duration": "200", "artists": None, "primaryArtists": "A"}),
]:
    t, a, d, sid = ad._parse_item(item, "community")
    check(f"community {label}: item kept", sid == "1" and t == "Song" and a)
check("community: real duration parsed (control)",
      ad._parse_item({"id": "1", "name": "S", "duration": "200", "artists": {"primary": [{"name": "A"}]}},
                     "community")[2] == 200.0)
check("community: empty duration -> None, not a dropped item",
      ad._parse_item({"id": "1", "name": "S", "duration": "", "artists": {"primary": [{"name": "A"}]}},
                     "community")[2] is None)

# official shape: more_info null / duration ""
for label, item in [
    ("more_info null", {"id": "2", "title": "Song", "subtitle": "A, B", "more_info": None}),
    ("empty duration", {"id": "2", "title": "Song", "subtitle": "A", "more_info": {"duration": ""}}),
    ("artistMap null", {"id": "2", "title": "Song", "subtitle": "A", "more_info": {"artistMap": None, "duration": "180"}}),
]:
    t, a, d, sid = ad._parse_item(item, "official")
    check(f"official {label}: item kept", sid == "2" and t == "Song" and a)
check("official: real duration parsed (control)",
      ad._parse_item({"id": "2", "title": "S", "subtitle": "A", "more_info": {"duration": "180"}}, "official")[2] == 180.0)


# download truncation
class Resp:
    def __init__(self, body, headers):
        self.body, self.headers = body, headers

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self.body), chunk_size):
            yield self.body[i:i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class Sess:
    def __init__(self, r):
        self.r = r

    def get(self, *a, **k):
        return self.r


def run(headers):
    out = tempfile.mkdtemp(prefix="jio")
    body = b"y" * 200000
    ad2 = JioSaavnAdapter()
    ad2._session = Sess(Resp(body, headers))
    target = os.path.join(out, "t.m4a")
    try:
        ad2._stream_to_file("http://x/t.m4a", target)
        err = None
    except RuntimeError as e:
        err = e
    return err, sorted(os.listdir(out))


err, files = run({"Content-Length": "200000"})
check("complete stream is written", err is None and files == ["t.m4a"])
err, files = run({"Content-Length": "250000"})
check("truncated stream raises", err is not None)
check("truncated stream leaves nothing behind", files == [])

print("FAIL:\n  " + "\n  ".join(FAIL) if FAIL else "OK")
sys.exit(1 if FAIL else 0)
