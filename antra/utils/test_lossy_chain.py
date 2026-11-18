"""MP3/AAC jobs must be servable by every service, not just Apple (measured 2026-11-03).

Real runs of one track, each source forced alone, found: Deezer skipped outright in lossy
mode ("Deezer is reserved for Deezer URLs"), a track with an Amazon id pinned to
{amazon, youtube}, Amazon asked for its Opus tier and handed back an .opus file for an MP3
job (FEAT-2 blocks opus -> mp3), Tidal fetching the 24-bit tier only to discard it, and
YouTube failing 403 on every full download. No network: adapters are stubs.

Run:  python antra/utils/test_lossy_chain.py
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from antra.core.models import TrackMetadata
from antra.core.resolver import SourceResolver
from antra.sources.base import BaseSourceAdapter
from antra.utils.transcoder import AudioTranscoder

PASS: list[str] = []
FAIL: list[str] = []


def check(label, got, want):
    (PASS if got == want else FAIL).append(f"{label}: got {got!r}, want {want!r}")


def ok(label, cond):
    (PASS if cond else FAIL).append(label)


class Stub(BaseSourceAdapter):
    def __init__(self, name, always_lossy=False):
        self.name, self.priority, self.always_lossy = name, 1, always_lossy

    def is_available(self):
        return True

    def search(self, track):
        return None

    def download(self, result, output_path):
        raise NotImplementedError


NAMES = ["tidal_mirror", "qobuz_mirror", "amazon", "deezer_mirror", "youtube", "jiosaavn"]


def kept(fmt, **track_kw):
    adapters = [Stub(n, always_lossy=n in ("youtube", "jiosaavn")) for n in NAMES]
    r = SourceResolver(adapters, preferred_output_format=fmt)
    t = TrackMetadata(title="Blinding Lights", artists=["The Weeknd"], album="After Hours", **track_kw)
    return {a.name for a in r.adapters if not r._should_skip_adapter_for_track(t, a)}


# -- resolver: plain Spotify track, lossy modes ------------------------------
for fmt in ("mp3", "aac"):
    got = kept(fmt)
    ok(f"{fmt}: deezer_mirror is no longer reserved for Deezer links", "deezer_mirror" in got)
    ok(f"{fmt}: every service and YouTube is eligible", {"tidal_mirror", "qobuz_mirror", "amazon", "youtube"} <= got)

# -- resolver: a track that carries an Amazon id ------------------------------
for fmt in ("mp3", "aac"):
    got = kept(fmt, amazon_asin="B0ABCDEF12")
    ok(f"{fmt}+ASIN: an Amazon id is a hint, not a pin", {"tidal_mirror", "qobuz_mirror", "deezer_mirror", "youtube"} <= got)

# -- CONTROLS: the rules that must NOT have moved -----------------------------
ok("CONTROL lossless-24: Deezer still reserved for Deezer links", "deezer_mirror" not in kept("lossless-24"))
ok("CONTROL lossless-24: a Deezer link still unlocks it", "deezer_mirror" in kept("lossless-24", source_service="deezer"))
ok("CONTROL lossless-16: Deezer still first-class", "deezer_mirror" in kept("lossless-16"))
asin_ll = kept("lossless-24", amazon_asin="B0ABCDEF12")
ok("CONTROL lossless-24+ASIN: lossy sources still skipped", not ({"youtube", "jiosaavn"} & asin_ll))
check("CONTROL flac+ASIN: unchanged (Amazon plus the other lossless services, no lossy)", kept("flac", amazon_asin="B0ABCDEF12"), {"amazon", "qobuz_mirror", "tidal_mirror"})

# -- resolver: which source WINS for an MP3 / AAC job ---------------------------
from antra.core.models import AudioFormat, SearchResult


class Hit(Stub):
    """Stub that finds the track; `lossless` and `native` mimic the real adapters' results."""

    def __init__(self, name, lossless, native=(), always_lossy=False):
        super().__init__(name, always_lossy=always_lossy)
        self.lossless, self.native_lossy_formats, self.searched = lossless, frozenset(native), 0

    def search(self, track):
        self.searched += 1
        return SearchResult(
            source=self.name, title=track.title, artists=track.artists, album=track.album,
            duration_ms=track.duration_ms,
            audio_format=AudioFormat.FLAC if self.lossless else AudioFormat.AAC,
            quality_kbps=None if self.lossless else 320, is_lossless=self.lossless,
            bit_depth=16 if self.lossless else None, sample_rate_hz=44100, download_url=None,
            stream_id=self.name, similarity_score=1.0, isrc_match=True)


def winner(fmt, adapters):
    t = TrackMetadata(title="Blinding Lights", artists=["The Weeknd"], album="After Hours",
                      duration_ms=200000, isrc="USUG11904206")
    r = SourceResolver(adapters, preferred_output_format=fmt, preserve_input_order=True)
    res = r.resolve(t)
    return (res[1].name if res else None), adapters


w, ads = winner("mp3", [Hit("jiosaavn", False, always_lossy=True), Hit("qobuz_mirror", True)])
check("mp3: a lossless master beats JioSaavn even when JioSaavn is listed first", w, "qobuz_mirror")
w, ads = winner("mp3", [Hit("qobuz_mirror", True), Hit("jiosaavn", False, always_lossy=True), Hit("youtube", False, always_lossy=True)])
check("mp3: lossy-only sources are not even searched once a master exists", [a.searched for a in ads], [1, 0, 0])
w, _ = winner("mp3", [Hit("jiosaavn", False, always_lossy=True), Hit("youtube", False, always_lossy=True)])
check("CONTROL mp3: with no lossless source, a lossy one is still used", w, "jiosaavn")
w, _ = winner("aac", [Hit("qobuz_mirror", True), Hit("apple", False, native={"aac", "m4a"})])
check("aac: the native AAC source wins over transcoding a master", w, "apple")
w, _ = winner("aac", [Hit("jiosaavn", False, always_lossy=True), Hit("qobuz_mirror", True)])
check("aac: a non-native lossy source still loses to a lossless master", w, "qobuz_mirror")
w, _ = winner("mp3", [Hit("apple", False, native={"aac", "m4a"}), Hit("qobuz_mirror", True)])
check("mp3: Apple's AAC is not 'native' for an MP3 job", w, "qobuz_mirror")

# -- FEAT-2 exemption for last-resort sources --------------------------------
tc = AudioTranscoder(prevent_lossy_transcode=True)
with tempfile.TemporaryDirectory() as d:
    m4a = os.path.join(d, "x.m4a")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=d=1", "-c:a", "aac", m4a], check=True)
    check("FEAT-2 default: AAC is kept as .m4a for an MP3 job", tc.needs_conversion(m4a, "mp3"), False)
    check("last resort: allowed to re-encode to the requested MP3", tc.needs_conversion(m4a, "mp3", allow_lossy_reencode=True), True)
    out = tc.convert(m4a, "mp3", allow_lossy_reencode=True)
    ok("convert() honours the flag and writes an .mp3", out.endswith(".mp3") and os.path.exists(out))
    check("CONTROL same-family AAC -> AAC never needed a re-encode flag", tc.needs_conversion(m4a, "m4a"), True)
    flac = os.path.join(d, "x.flac")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=d=1", flac], check=True)
    check("CONTROL lossless source -> MP3 converts without the flag", tc.needs_conversion(flac, "mp3"), True)
    check("CONTROL FEAT-2 off: AAC -> MP3 converts", AudioTranscoder(False).needs_conversion(m4a, "mp3"), True)

# -- Amazon: ask for the FLAC master, never the Opus tier ---------------------
from antra.sources.amazon import AmazonAdapter

for fmt in ("mp3", "aac", "m4a"):
    check(f"amazon {fmt}: does not request the Opus tier", AmazonAdapter(["http://x"], preferred_output_format=fmt)._prefer_lossy_download, False)

# -- Tidal: MP3 needs only the 16-bit master ----------------------------------
from antra.sources.tidal_mirror import TidalMirrorAdapter

def params(fmt):
    return TidalMirrorAdapter("http://x", preferred_output_format=fmt)._stream_params()

check("tidal mp3 -> prefer_16", params("mp3"), {"prefer_16": "1"})
check("CONTROL tidal aac -> native AAC", params("aac"), {"prefer_aac": "1"})
check("CONTROL tidal lossless-24 -> strict_24", params("lossless-24"), {"strict_24": "1"})
check("CONTROL tidal lossless-16 -> prefer_16", params("lossless-16"), {"prefer_16": "1"})
check("CONTROL tidal source -> no param", params("source"), None)

# -- YouTube: client ladder + audio extraction --------------------------------
import yt_dlp
from antra.core.models import AudioFormat, SearchResult
from antra.sources import youtube as yt

tried: list = []


def fake_ydl(outcomes, make_ext="mp4", tmp=None):
    class Y:
        def __init__(self, opts):
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=True):
            client = (self.opts.get("extractor_args") or {}).get("youtube", {}).get("player_client")
            tried.append(client)
            outcome = outcomes.pop(0)
            if outcome == "403":
                raise yt_dlp.utils.DownloadError("unable to download video data: HTTP Error 403: Forbidden")
            stem = self.opts["outtmpl"].replace(".%(ext)s", "")
            src = {"mp4": MP4, "m4a": M4A}[outcome]
            open(f"{stem}.{outcome}", "wb").write(open(src, "rb").read())
            return {"ext": outcome}
    return Y


def run_download(outcomes):
    tried.clear()
    d = tempfile.mkdtemp()  # kept so the caller can inspect the file; the OS temp cleaner reaps it
    orig = yt_dlp.YoutubeDL
    yt_dlp.YoutubeDL = fake_ydl(list(outcomes))
    try:
        res = SearchResult(source="youtube", title="t", artists=["a"], album="", duration_ms=1000,
                           audio_format=AudioFormat.AAC, quality_kbps=48, is_lossless=False,
                           download_url=None, stream_id="https://www.youtube.com/watch?v=abc",
                           similarity_score=1.0)
        return yt.YouTubeAdapter().download(res, os.path.join(d, "out")), d
    finally:
        yt_dlp.YoutubeDL = orig


with tempfile.TemporaryDirectory() as fx:
    MP4 = os.path.join(fx, "a.mp4")
    M4A = os.path.join(fx, "a.m4a")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=d=1", "-f", "lavfi", "-i",
                    "color=c=black:s=64x64:d=1", "-c:a", "aac", "-c:v", "libx264", "-shortest", MP4], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=d=1", "-c:a", "aac", M4A], check=True)

    path, _ = run_download(["m4a"])
    check("default client works: android is never tried", tried, [None])
    ok("CONTROL default client keeps its m4a", path.endswith(".m4a"))

    path, _ = run_download(["403", "mp4"])
    check("403 on defaults falls to the android client", tried, [None, ["android"]])
    ok("muxed mp4 is reduced to an audio-only m4a", path.endswith(".m4a") and os.path.exists(path))
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
                           capture_output=True, text=True).stdout.split()
    check("extracted file has no video stream", probe, ["audio"])

    try:
        run_download(["403", "403"])
        ok("every client failing raises", False)
    except ValueError as e:
        ok("every client failing raises with the YouTube prefix", "[YouTube]" in str(e))
    check("both rungs were tried before giving up", tried, [None, ["android"]])

# -- YouTube: direct InnerTube rung (stubbed requests) --------------------------
import requests as _rq

DATA = os.urandom(5_000_000)


class _Resp:
    def __init__(self, code=200, js=None, content=b"", text=""):
        self.status_code, self._js, self.content, self.text = code, js, content, text

    def json(self):
        return self._js


def run_it(player_replies, visitor_ok=True, truncate=False, overrun=False):
    posts, gets = [], []
    real_get, real_post = _rq.get, _rq.post

    def fake_get(url, headers=None, timeout=None, **kw):
        gets.append((url, headers))
        if "sw.js_data" in url:
            return _Resp(200, text=("x Cg" + "A" * 50 + " y") if visitor_ok else "nothing here")
        a, b = headers["Range"].split("=")[1].split("-")
        chunk = DATA[int(a):int(b) + 1]
        if overrun and int(b) + 1 >= len(DATA):
            chunk = chunk + b"extra"  # a server ignoring the Range end: byte count no longer matches
        return _Resp(206, content=chunk[:-10] if truncate and int(b) + 1 >= len(DATA) else chunk)

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        posts.append((json, headers))
        return _Resp(200, js=player_replies.pop(0))

    _rq.get, _rq.post = fake_get, fake_post
    yt._visitor_cache.update(id=None, at=0.0)
    d = tempfile.mkdtemp()
    try:
        out = yt._innertube_audio("https://www.youtube.com/watch?v=abcdefghijk", os.path.join(d, "stem"))
    finally:
        _rq.get, _rq.post = real_get, real_post
    return out, posts, gets, d


def ok_reply(*itags):
    return {"playabilityStatus": {"status": "OK"}, "streamingData": {"adaptiveFormats": [
        {"itag": i, "url": f"https://gv/{i}", "contentLength": str(len(DATA)), "bitrate": 1} for i in itags]}}


BOT = {"playabilityStatus": {"status": "LOGIN_REQUIRED", "reason": "Sign in to confirm you're not a bot"}}

out, posts, gets, d = run_it([ok_reply(251, 140)])
ok("InnerTube: downloads the whole stream and prefers itag 140 (.m4a)", bool(out) and out.endswith(".m4a") and open(out, "rb").read() == DATA)
check("InnerTube: the player request carries a visitor id (without one YouTube answers 'not a bot')",
      (posts[0][0]["context"]["client"].get("visitorData", "")[:2], posts[0][1].get("X-Goog-Visitor-Id", "")[:2]), ("Cg", "Cg"))
ok("InnerTube: ranged reads, not one open-ended GET",
   all("Range" in (h or {}) for u, h in gets if "gv/" in u) and len([1 for u, h in gets if "gv/" in u]) == 2)
check("InnerTube: no partial file left behind", [f for f in os.listdir(d) if "part" in f], [])

out, posts, _, d = run_it([ok_reply(251)])
ok("InnerTube: falls to itag 251 (.webm) when 140 is absent", bool(out) and out.endswith(".webm"))

out, posts, _, d = run_it([BOT, ok_reply(140)])
ok("InnerTube: one retry with a fresh visitor id after LOGIN_REQUIRED", out is not None and len(posts) == 2)
out, posts, _, d = run_it([BOT, BOT])
ok("InnerTube: gives up (None) after the retry, so yt-dlp can take over", out is None and len(posts) == 2)
out, posts, _, d = run_it([{"playabilityStatus": {"status": "UNPLAYABLE"}}])
ok("InnerTube: UNPLAYABLE is not retried", out is None and len(posts) == 1)
out, _, _, d = run_it([ok_reply(140)], truncate=True)
ok("InnerTube: a short transfer returns None and leaves no file", out is None and os.listdir(d) == [])
out, _, _, d = run_it([ok_reply(140)], overrun=True)
ok("InnerTube: a byte count that does not match the announced size is rejected", out is None and os.listdir(d) == [])
out, posts, _, d = run_it([ok_reply(140)], visitor_ok=False)
ok("InnerTube: no visitor id obtainable -> None without calling player", out is None and posts == [])
ok("InnerTube: a non-video URL is ignored", yt._innertube_audio("https://example.com/x", os.path.join(d, "s")) is None)

# download() uses the direct file and never starts yt-dlp when the rung succeeds
tmpd = tempfile.mkdtemp()
direct = os.path.join(tmpd, "_yt_tmp_x.m4a")
open(direct, "wb").write(b"audio")
orig_it, orig_ydl = yt._innertube_audio, yt_dlp.YoutubeDL
yt._innertube_audio = lambda url, stem: direct


def _no_ydl(*a, **k):
    raise AssertionError("yt-dlp must not run")


yt_dlp.YoutubeDL = _no_ydl
try:
    r = yt.YouTubeAdapter().download(SearchResult(
        source="youtube", title="t", artists=["a"], album="", duration_ms=1000, audio_format=AudioFormat.AAC,
        quality_kbps=48, is_lossless=False, download_url=None, stream_id="https://www.youtube.com/watch?v=abcdefghijk",
        similarity_score=1.0), os.path.join(tmpd, "out"))
    ok("download(): InnerTube success skips yt-dlp and renames to <output>.m4a", r.endswith("out.m4a") and open(r, "rb").read() == b"audio")
finally:
    yt._innertube_audio, yt_dlp.YoutubeDL = orig_it, orig_ydl

for line in FAIL:
    print("FAIL", line)
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
