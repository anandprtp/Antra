"""Qobuz/Deezer mirror downloads must reject a stream shorter than its Content-Length.

A cut transfer used to be accepted as a finished file (Deezer even wrote it under
the real filename), the v1.1.8 BUG-4 "1.2 MB won't play" symptom.

Run:  python antra/utils/test_mirror_truncation.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from antra.core.models import SearchResult, AudioFormat
from antra.sources import deezer_mirror, qobuz_mirror
from antra.utils.mirror_http import ensure_complete_transfer

FAIL = []


def check(label, ok):
    if not ok:
        FAIL.append(label)


class Resp:
    def __init__(self, body=b"", headers=None, status=200, json_body=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self._json = json_body
        self.closed = False

    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]

    def json(self):
        return self._json

    def raise_for_status(self):
        pass

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def result():
    return SearchResult(source="test", title="t", artists=["a"], album="al", duration_ms=1000, audio_format=AudioFormat.FLAC,
                        quality_kbps=None, is_lossless=True, bit_depth=16, sample_rate_hz=44100,
                        download_url=None, stream_id="123", similarity_score=1.0, isrc_match=True)


# ── the helper ───────────────────────────────────────────────────────────────
for label, headers, written, raises in [
    ("complete", {"Content-Length": "10"}, 10, False),                    # control: must pass
    ("longer than announced is not truncation", {"Content-Length": "10"}, 12, False),
    ("short", {"Content-Length": "10"}, 9, True),
    ("no length header (relay stream)", {}, 1, False),
    ("content-encoded: length counts compressed bytes", {"Content-Length": "10", "Content-Encoding": "gzip"}, 3, False),
    ("garbage length", {"Content-Length": "abc"}, 1, False),
]:
    try:
        ensure_complete_transfer(Resp(headers=headers), written, "T")
        got = False
    except RuntimeError:
        got = True
    check(f"helper: {label}", got == raises)


# ── Qobuz ────────────────────────────────────────────────────────────────────
class QSession:
    def __init__(self, resp):
        self.resp = resp

    def get(self, *a, **k):
        return self.resp


def qobuz(body, headers):
    out = tempfile.mkdtemp(prefix="qz")
    ad = qobuz_mirror.QobuzMirrorAdapter("http://mirror.invalid")
    resp = Resp(body, {"Content-Type": "audio/flac", **headers})
    ad._session = QSession(resp)
    base = os.path.join(out, "track")
    try:
        path = ad.download(result(), base)
        err = None
    except RuntimeError as e:
        path, err = None, e
    return path, err, os.listdir(out), resp


body = b"x" * 300000
path, err, files, resp = qobuz(body, {"Content-Length": str(len(body))})
check("qobuz complete: file written", path and os.path.exists(path) and files == ["track.flac"])
check("qobuz complete: response closed", resp.closed)

path, err, files, resp = qobuz(body, {"Content-Length": str(len(body) + 5000)})
check("qobuz truncated: raises", err is not None and "Truncated" in str(err))
check("qobuz truncated: no file and no .part left", files == [])
check("qobuz truncated: response closed", resp.closed)

path, err, files, resp = qobuz(body, {})
check("qobuz without Content-Length is still accepted", path is not None)


# ── Deezer ───────────────────────────────────────────────────────────────────
class DSession:
    def __init__(self, stream):
        self.stream = stream

    def get(self, url, **k):
        if "/api/track/" in url:
            return Resp(json_body={"decryptionKey": "k"})
        return self.stream


def deezer(body, headers):
    out = tempfile.mkdtemp(prefix="dz")
    ad = deezer_mirror.DeezerMirrorAdapter("http://mirror.invalid")
    ad._session = DSession(Resp(body, headers))
    orig = deezer_mirror._decrypt_stream
    deezer_mirror._decrypt_stream = lambda enc, tid, key: b"fLaC" + enc
    try:
        try:
            path, err = ad.download(result(), os.path.join(out, "track")), None
        except RuntimeError as e:
            path, err = None, e
    finally:
        deezer_mirror._decrypt_stream = orig
    return path, err, os.listdir(out)


path, err, files = deezer(body, {"Content-Length": str(len(body))})
check("deezer complete: file written, no .part", path and files == ["track.flac"])
path, err, files = deezer(body, {"Content-Length": str(len(body) + 5000)})
check("deezer truncated: raises", err is not None and "Truncated" in str(err))
check("deezer truncated: nothing written", files == [])

print("FAIL:\n  " + "\n  ".join(FAIL) if FAIL else "OK")
sys.exit(1 if FAIL else 0)
