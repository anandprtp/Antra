"""Amazon adapter: CENC decryption, manifest parsing and the download path.

Builds real fragmented-MP4 (moof/traf/tfhd/trun/senc + mdat), encrypts the samples with
AES-CTR and checks the pure-Python fallback decrypts them byte-exact. Also covers the
failure modes that used to raise raw struct errors or silently leave samples encrypted.

Run:  python antra/utils/test_amazon_cenc.py
"""
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from Cryptodome.Cipher import AES

from antra.sources import amazon
from antra.sources.amazon import AmazonAdapter, _DirectAmazonClient, _has_http_status

FAIL = []


def check(label, ok):
    if not ok:
        FAIL.append(label)


KEY = bytes(range(16))


def box(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", 8 + len(body)) + kind + body


def encrypt(sample: bytes, iv8: bytes, subs=None) -> bytes:
    c = AES.new(KEY, AES.MODE_CTR, initial_value=iv8.ljust(16, b"\x00"), nonce=b"")
    if not subs:
        return c.encrypt(sample)
    out, cur = bytearray(), 0
    for clear, enc in subs:
        out += sample[cur:cur + clear]
        cur += clear
        out += c.encrypt(sample[cur:cur + enc])
        cur += enc
    return bytes(out)


def build(samples, subs=None, trun_sizes=True, tfhd_default=None, mdat_size_zero=False, trun_cnt=None):
    ivs = [bytes([i + 1]) * 8 for i in range(len(samples))]
    enc = [encrypt(s, iv, subs[i] if subs else None) for i, (s, iv) in enumerate(zip(samples, ivs))]
    # senc
    senc_flags = 2 if subs else 0
    senc = struct.pack(">I", senc_flags) + struct.pack(">I", len(samples))
    for i, iv in enumerate(ivs):
        senc += iv
        if subs:
            senc += struct.pack(">H", len(subs[i]))
            for clear, e in subs[i]:
                senc += struct.pack(">HI", clear, e)
    # tfhd
    if tfhd_default is not None:
        tfhd = struct.pack(">II", 0x10, 1) + struct.pack(">I", tfhd_default)
    else:
        tfhd = struct.pack(">II", 0, 1)
    # trun (data offset patched after moof size is known)
    flags = 0x001 | (0x200 if trun_sizes else 0)
    cnt = len(samples) if trun_cnt is None else trun_cnt
    trun_body = struct.pack(">II", flags, cnt) + b"\x00\x00\x00\x00"
    if trun_sizes:
        trun_body += b"".join(struct.pack(">I", len(s)) for s in samples)

    def moof_bytes(doff):
        t = bytearray(trun_body)
        t[8:12] = struct.pack(">i", doff)
        traf = box(b"traf", box(b"tfhd", tfhd) + box(b"trun", bytes(t)) + box(b"senc", senc))
        return box(b"moof", box(b"mfhd", struct.pack(">II", 0, 1)) + traf)

    moof = moof_bytes(0)
    moof = moof_bytes(len(moof) + 8)           # data starts right after the mdat header
    payload = b"".join(enc)
    mdat = struct.pack(">I", 0 if mdat_size_zero else 8 + len(payload)) + b"mdat" + payload
    return box(b"ftyp", b"isom\x00\x00\x00\x00") + moof + mdat


def decrypt(data: bytes, key=KEY):
    ad = AmazonAdapter(mirrors=[])
    d = tempfile.mkdtemp(prefix="cenc")
    src, dst = os.path.join(d, "in.m4a"), os.path.join(d, "out.m4a")
    open(src, "wb").write(data)
    try:
        err = ad._decrypt_cenc_python(src, dst, key.hex())
        exc = None
    except Exception as e:                      # the contract is "return a string, never raise"
        err, exc = None, e
    out = open(dst, "rb").read() if os.path.exists(dst) else None
    return err, exc, out, src


S = [bytes([i % 251 for i in range(100)]), bytes([(i * 7) % 251 for i in range(200)]), b"\x42" * 50]
plain_payload = b"".join(S)


def payload_of(out):
    return out[out.index(b"mdat") + 4:]


# 1. control: the ordinary case round-trips byte-exact
err, exc, out, _ = decrypt(build(S))
check("control: plain CENC decrypts", err is None and exc is None and payload_of(out) == plain_payload)

# 2. trun without sizes, tfhd default_sample_size (used to be skipped -> still encrypted)
same = [b"\x11" * 64, b"\x22" * 64, b"\x33" * 64]
err, exc, out, _ = decrypt(build(same, trun_sizes=False, tfhd_default=64))
check("trun without sizes uses tfhd default", err is None and exc is None and payload_of(out) == b"".join(same))

# 3. sizes unknowable -> must be an error, never a silent partial decrypt
err, exc, out, _ = decrypt(build(same, trun_sizes=False, tfhd_default=None))
check("unknowable sample sizes -> error string", exc is None and isinstance(err, str) and out is None)

# 4. subsample encryption: clear bytes stay clear, the rest decrypts
sub = [[(10, 90)], [(20, 180)], [(5, 45)]]
err, exc, out, _ = decrypt(build(S, subs=sub))
check("subsample CENC round-trips", err is None and exc is None and payload_of(out) == plain_payload)

# 5. mdat with size 0 ("to end of file")
err, exc, out, _ = decrypt(build(S, mdat_size_zero=True))
check("size-0 mdat is decrypted", err is None and exc is None and out is not None
      and out[out.index(b"mdat") + 4:] == plain_payload)

# 6. corrupt: trun claims more samples than its bytes hold -> string, not struct.error
err, exc, out, _ = decrypt(build(S, trun_cnt=100000))
check("corrupt trun does not raise", exc is None)
check("corrupt trun reports an error string", isinstance(err, str))

# 7. wrong-size key and unreadable input keep returning strings
ad = AmazonAdapter(mirrors=[])
check("bad key length -> string", isinstance(ad._decrypt_cenc_python("x", "y", "00"), str))
check("missing input -> string", isinstance(ad._decrypt_cenc_python("/no/such/file", "y", KEY.hex()), str))

# 8. status matching must not fire on digits inside an ASIN/URL
check("status: plain 401", _has_http_status("HTTP 401 Unauthorized", 401))
check("status: 'API error 403'", _has_http_status("API error 403", 403))
check("status: 401 inside an ASIN is not a status", not _has_http_status("track B0014019XY failed", 401))
check("status: 404 inside a URL id is not a status", not _has_http_status("https://x/api/track/B00404ABCD", 404))

# 9. should_retry_download no longer misreads an ASIN
ad = AmazonAdapter(mirrors=[])
check("retry: a plain timeout is retried", ad.should_retry_download(None, RuntimeError("timeout on B00404ABCD")))
check("retry: a real 404 is not", not ad.should_retry_download(None, RuntimeError("API error 404")))

# 10. _process_download when the probe of the encrypted file fails (codec None)
class Resp:
    def __init__(self, body, headers):
        self.body, self.headers = body, headers

    def raise_for_status(self):
        pass

    def iter_content(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i:i + n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class Sess:
    def __init__(self, r):
        self.r = r

    def get(self, *a, **k):
        return self.r


def process(headers, body=b"z" * 70000):
    ad = AmazonAdapter(mirrors=[])
    ad._session = Sess(Resp(body, headers))
    ad._probe_audio_stream = lambda p: (None, None)           # the failing-probe case
    ad._finalize_audio = lambda p: p
    ad._is_lossless_output = lambda p: True

    def fake_decrypt(src, dst, key):
        open(dst, "wb").write(open(src, "rb").read())
        return None
    ad._decrypt_file = fake_decrypt
    d = tempfile.mkdtemp(prefix="am")
    try:
        path, exc = ad._process_download("http://cdn/x", "00" * 16, os.path.join(d, "t")), None
    except Exception as e:
        path, exc = None, e
    return path, exc, sorted(os.listdir(d))


path, exc, files = process({"Content-Length": "70000"})
check("probe failure with a key does not crash (AttributeError)", exc is None and path is not None)
check("only the final file remains", files == ["t.m4a"])
path, exc, files = process({"Content-Length": "90000"})
check("truncated encrypted download is rejected", isinstance(exc, RuntimeError) and "Truncated" in str(exc))
check("truncated download leaves no .enc temp", files == [])

# 11. DASH manifest: odd attribute values and bad XML
MPD = """<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" xmlns:cenc="urn:mpeg:cenc:2013"><Period>
<AdaptationSet selectionPriority="{prio}"><cenc:pssh>UFNTSA==</cenc:pssh>
<Representation codecs="flac"><SupplementalProperty schemeIdUri="amz-music:bitDepth" value="{bd}"/>
<BaseURL>https://cdn/x.mp4</BaseURL></Representation></AdaptationSet></Period></MPD>"""
dc = _DirectAmazonClient("")
for prio, bd in [("1", "24"), ("", "24"), ("2", ""), ("1", "24.0")]:
    try:
        url, pssh = dc._parse_manifest({"contentResponseList": [{"manifest": MPD.format(prio=prio, bd=bd)}]})
        check(f"manifest prio={prio!r} bd={bd!r} parsed", url == "https://cdn/x.mp4" and pssh == "UFNTSA==")
    except Exception as e:
        check(f"manifest prio={prio!r} bd={bd!r} raised {type(e).__name__}", False)
try:
    dc._parse_manifest({"contentResponseList": [{"manifest": "<MPD><unclosed>"}]})
    check("bad XML must raise", False)
except RuntimeError as e:
    check("bad XML gives a readable RuntimeError", "valid XML" in str(e))
except Exception as e:
    check(f"bad XML raised raw {type(e).__name__}", False)

# 12. Apple adapter: same shared decryptor, plus its cbcs refusal and sinf/enca retyping
from antra.sources.apple import AppleAdapter


def with_moov(data: bytes, scheme: bytes, orig: bytes = b"alac") -> bytes:
    frma = box(b"frma", orig)
    schm = box(b"schm", b"\x00\x00\x00\x00" + scheme + b"\x00\x01\x00\x00")
    entry = box(b"enca", b"\x00" * 28 + box(b"sinf", frma + schm))
    moov = box(b"moov", box(b"stsd", b"\x00\x00\x00\x00" + struct.pack(">I", 1) + entry))
    cut = data.index(b"moof") - 4
    return data[:cut] + moov + data[cut:]


def apple_decrypt(data: bytes):
    d = tempfile.mkdtemp(prefix="apl")
    src, dst = os.path.join(d, "in.m4a"), os.path.join(d, "out.m4a")
    open(src, "wb").write(data)
    try:
        err, exc = AppleAdapter._decrypt_cenc_python(src, dst, KEY.hex()), None
    except Exception as e:
        err, exc = None, e
    return err, exc, (open(dst, "rb").read() if os.path.exists(dst) else None)


err, exc, out = apple_decrypt(with_moov(build(S), b"cenc"))
check("apple: control decrypts", err is None and exc is None and out is not None
      and out[out.index(b"mdat") + 4:] == plain_payload)
check("apple: sample entry retyped enca -> alac", out is not None and b"alac" in out and b"enca" not in out)
check("apple: sinf disarmed", out is not None and b"sinf" not in out and b"free" in out)

err, exc, out = apple_decrypt(with_moov(build(S), b"cbcs"))
check("apple: cbcs is refused with an explanation", exc is None and isinstance(err, str) and "cbcs" in err)
check("apple: cbcs writes no output file", out is None)

err, exc, out = apple_decrypt(with_moov(build(same, trun_sizes=False, tfhd_default=64), b"cenc"))
check("apple: tfhd default size honoured", err is None and exc is None and out is not None
      and out[out.index(b"mdat") + 4:] == b"".join(same))
err, exc, out = apple_decrypt(with_moov(build(S, mdat_size_zero=True), b"cenc"))
check("apple: size-0 mdat decrypted", err is None and exc is None and out is not None)
err, exc, out = apple_decrypt(with_moov(build(S, trun_cnt=100000), b"cenc"))
check("apple: corrupt trun returns a string, does not raise", exc is None and isinstance(err, str))

# 12b. Apple HLS download path (stubbed network)
class HResp:
    def __init__(self, content=b"", headers=None, text=""):
        self.content, self.headers, self.text = content, headers or {}, text

    def raise_for_status(self):
        pass


def hls(playlist, serve):
    """Run _download_and_decrypt_cenc_hls against a fake CDN; serve(url, range) -> HResp."""
    ad = AppleAdapter.__new__(AppleAdapter)
    seen = []
    ad._api_get = lambda url, **k: HResp(text=playlist)
    ad._validate_downloaded_file = lambda *a, **k: None

    class S:
        def get(self, url, headers=None, **k):
            seen.append((url, (headers or {}).get("Range")))
            return serve(url, (headers or {}).get("Range"))
    ad._session = S()
    d = tempfile.mkdtemp(prefix="hls")
    final = os.path.join(d, "t.m4a")
    try:
        out, exc = ad._download_and_decrypt_cenc_hls(
            "https://cdn.test/a/b/pl.m3u8?sig=x/y", KEY.hex(), final, "aac"), None
    except Exception as e:
        out, exc = None, e
    return out, exc, seen, sorted(os.listdir(d)), final


whole = with_moov(build(S), b"cenc")
cut = whole.index(b"moof") - 4
init_bytes, frag_bytes = whole[:cut], whole[cut:]
files_by_name = {"init.mp4": init_bytes, "seg1.m4s": frag_bytes}
PL = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:1,\nseg1.m4s\n#EXT-X-ENDLIST\n'


def serve_plain(url, rng):
    return HResp(files_by_name[url.rsplit("/", 1)[1]])


out, exc, seen, files, final = hls(PL, serve_plain)
check("apple hls: control downloads and decrypts", exc is None and out == final and os.path.exists(final))
check("apple hls: payload decrypted correctly",
      exc is None and open(final, "rb").read().endswith(plain_payload))
check("apple hls: URLs resolve against the playlist, not a '/' inside its query",
      [u for u, _ in seen] == ["https://cdn.test/a/b/init.mp4", "https://cdn.test/a/b/seg1.m4s"])
check("apple hls: no .enc temp left", files == ["t.m4a"])

# two distinct byte-ranged segment URLs -> the per-segment branch (one URL would take the whole-file branch)
files_by_name["seg2.m4s"] = frag_bytes
PL_RANGE = ('#EXTM3U\n#EXT-X-MAP:URI="init.mp4",BYTERANGE="%d@0"\n'
            '#EXTINF:1,\n#EXT-X-BYTERANGE:%d@0\nseg1.m4s\n#EXTINF:1,\n#EXT-X-BYTERANGE:%d@0\nseg2.m4s\n'
            % (len(init_bytes), len(frag_bytes), len(frag_bytes)))
TAIL = b"\x00" * 64            # the rest of a bigger file on the CDN: what an ignored Range would return


def serve_range_ok(url, rng):
    padded = files_by_name[url.rsplit("/", 1)[1]] + TAIL
    a, b = [int(x) for x in rng.split("=")[1].split("-")]
    return HResp(padded[a:b + 1])


def serve_ignores_range(url, rng):
    return HResp(files_by_name[url.rsplit("/", 1)[1]] + TAIL)


out, exc, seen, files, final = hls(PL_RANGE, serve_range_ok)
check("apple hls: honoured Range requests work", exc is None and out == final)
check("apple hls: Range header is sent on every request", len(seen) == 3 and all(r for _, r in seen))

out, exc, seen, files, final = hls(PL_RANGE, serve_ignores_range)   # server ignores Range -> too many bytes
check("apple hls: a server that ignores Range is rejected", isinstance(exc, RuntimeError) and "Range" in str(exc))
check("apple hls: rejected download leaves no .enc temp", ".enc" not in "".join(files))


def serve_short(url, rng):
    data = files_by_name[url.rsplit("/", 1)[1]]
    return HResp(data[:-5], {"Content-Length": str(len(data))})


out, exc, seen, files, final = hls(PL, serve_short)
check("apple hls: truncated segment rejected", isinstance(exc, RuntimeError) and "Truncated" in str(exc))

# 12c. ffmpeg timeout becomes a clear error and removes the partial file
import subprocess as _sp
ad = AppleAdapter.__new__(AppleAdapter)
ad._resolve_playlist = lambda u: (u, "")
d = tempfile.mkdtemp(prefix="fft")
base = os.path.join(d, "t")
real_run = amazon.subprocess.run


def fake_run(*a, **k):
    open(base + ".m4a", "wb").write(b"partial")
    raise _sp.TimeoutExpired("ffmpeg", 300)


import antra.sources.apple as _apple
_apple.subprocess.run = fake_run
try:
    ad._process_download("https://cdn/x.m3u8", KEY.hex(), base)
    exc = None
except Exception as e:
    exc = e
finally:
    _apple.subprocess.run = real_run
check("apple ffmpeg timeout: clear RuntimeError", isinstance(exc, RuntimeError) and "timed out" in str(exc))
check("apple ffmpeg timeout: partial file removed", os.listdir(d) == [])

# 12d. Apple wrapper ALAC stream (Path 1): completeness, cleanup, connection hygiene
from antra.core.models import SearchResult as _SR, AudioFormat as _AF


class StreamResp:
    def __init__(self, status, body=b"", headers=None):
        self.status_code, self.body, self.headers = status, body, headers or {}
        self.closed = False

    def iter_content(self, n):
        for i in range(0, len(self.body), n):
            yield self.body[i:i + n]

    def close(self):
        self.closed = True


def wrapper_download(resp):
    ad = AppleAdapter(mirrors=["http://m"], preferred_output_format="alac")
    ad.always_lossy = False                  # what the wrapper probe sets when the wrapper is up
    calls = []
    ad._api_get = lambda url, **k: (calls.append(url), resp)[1]
    r = _SR(source="apple", title="t", artists=["a"], album=None, duration_ms=1000, audio_format=_AF.ALAC,
            quality_kbps=None, is_lossless=True, download_url=None, stream_id="999", similarity_score=1.0,
            isrc_match=False)
    d = tempfile.mkdtemp(prefix="aw")
    try:
        out, exc = ad.download(r, os.path.join(d, "t")), None
    except Exception as e:
        out, exc = None, e
    return out, exc, sorted(os.listdir(d)), calls


StreamResp.json = lambda self: {}          # the /api/track fallback then fails: no streamUrl

body = b"alac" * 50000
resp = StreamResp(200, body, {"Content-Length": str(len(body)), "X-Quality": "LOSSLESS"})
out, exc, files, calls = wrapper_download(resp)
check("apple wrapper: path 1 (/api/stream/) was exercised", bool(calls) and "/api/stream/" in calls[0])
check("apple wrapper: complete stream written", exc is None and files == ["t.m4a"])
check("apple wrapper: response closed after success", resp.closed)

resp = StreamResp(200, body, {"Content-Length": str(len(body) + 9000), "X-Quality": "LOSSLESS"})
out, exc, files, calls = wrapper_download(resp)
check("apple wrapper: truncated stream was fetched from /api/stream/", bool(calls) and "/api/stream/" in calls[0])
check("apple wrapper: a stream shorter than Content-Length is not kept as a track", out is None and files == [])
check("apple wrapper: truncated response closed", resp.closed)

resp = StreamResp(503)
out, exc, files, calls = wrapper_download(resp)
check("apple wrapper: non-200 response is closed (no leaked connection)", resp.closed)

# 13. Apple retry classification must not read digits out of an ID
apl = AppleAdapter.__new__(AppleAdapter)
check("apple retry: track id containing 404 is still retried",
      AppleAdapter.should_retry_download(apl, None, RuntimeError("ffmpeg failed for track 1404991234")))
check("apple retry: a real 404 is not retried",
      not AppleAdapter.should_retry_download(apl, None, RuntimeError("API error 404")))

print("FAIL:\n  " + "\n  ".join(FAIL) if FAIL else "OK")
sys.exit(1 if FAIL else 0)
