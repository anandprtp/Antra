"""NetEase adapter: glob-metacharacter folders and the duration gate.

Run:  python antra/utils/test_netease_adapter.py
"""
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from antra.core.models import SearchResult, AudioFormat, TrackMetadata
from antra.sources.netease import NetEaseAdapter

FAIL = []


def check(label, ok):
    if not ok:
        FAIL.append(label)


def result(stream_id="42"):
    return SearchResult(source="netease", title="t", artists=["a"], album=None, duration_ms=200000,
                        audio_format=AudioFormat.MP3, quality_kbps=None, is_lossless=False,
                        download_url=None, stream_id=stream_id, similarity_score=1.0, isrc_match=False)


class FakeYDL:
    mode = "ok"

    def __init__(self, opts):
        self.stem = opts["outtmpl"].replace(".%(ext)s", "")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def extract_info(self, url, download=True):
        if FakeYDL.mode == "ok":
            open(self.stem + ".m4a", "wb").write(b"audio")     # info says mp3 -> forces the glob fallback
            return {"ext": "mp3"}
        open(self.stem + ".mp3.part", "wb").write(b"half")     # partial left by a failed download
        raise RuntimeError("network down")


sys.modules["yt_dlp"] = types.SimpleNamespace(YoutubeDL=FakeYDL)
ad = NetEaseAdapter()

# Folder names with glob metacharacters, plus a plain one as the control.
for folder in ["Plain Album", "Album [Deluxe]", "What? * Edition"]:
    if os.name == "nt" and any(c in folder for c in "?*"):
        continue                                              # not legal on Windows
    root = tempfile.mkdtemp(prefix="ne")
    d = os.path.join(root, folder)
    os.makedirs(d)

    FakeYDL.mode = "ok"
    try:
        path = ad.download(result(), os.path.join(d, "track"))
    except Exception as e:
        path = None
        check(f"[{folder}] fallback download raised: {e}", False)
    check(f"[{folder}] file found via glob fallback and moved", path and os.path.exists(path)
          and os.listdir(d) == ["track.m4a"])

    FakeYDL.mode = "fail"
    try:
        ad.download(result("43"), os.path.join(d, "track2"))
        check(f"[{folder}] failure must raise", False)
    except ValueError:
        pass
    check(f"[{folder}] partial file cleaned up", [f for f in os.listdir(d) if "_ne_tmp" in f] == [])


# Duration gate (search side).
def song(sid, name, dt):
    return {"id": sid, "name": name, "ar": [{"name": "Artist"}], "al": {"name": "Al"}, "dt": dt}


track = TrackMetadata(title="Song", artists=["Artist"], album="Al", duration_ms=200000)
good = ad._best_match([song(1, "Song", 201000)], track)
check("control: right-length match accepted", good is not None and good.stream_id == "1")
check("a 30 s clip of the same title is rejected", ad._best_match([song(2, "Song", 30000)], track) is None)
both = ad._best_match([song(2, "Song", 30000), song(1, "Song", 201000)], track)
check("right length wins over a clip regardless of order", both is not None and both.stream_id == "1")
check("no duration on the result is still accepted", ad._best_match([song(3, "Song", None)], track) is not None)

print("FAIL:\n  " + "\n  ".join(FAIL) if FAIL else "OK")
sys.exit(1 if FAIL else 0)
