"""Negative control for test_lossy_chain.py and test_spotify_hash_rotation.py.

Each sabotage puts one of the fixes back in place, then the owning suite must FAIL. Files are
restored byte-for-byte from an in-memory copy (and md5-checked) even on error.

Run:  python antra/utils/test_lossy_chain_negctl.py
"""
import hashlib
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOSSY = "antra/utils/test_lossy_chain.py"
HASH = "antra/core/test_spotify_hash_rotation.py"

SABOTAGES = [
    ("Deezer reserved again in lossy mode", LOSSY, "antra/core/resolver.py",
     "if self._is_lossless16_mode() or lossy_mode:", "if self._is_lossless16_mode():"),
    ("Amazon id pins lossy jobs to the lossy sources again", LOSSY, "antra/core/resolver.py",
     'if getattr(track, "amazon_asin", None) and not lossy_mode and adapter.name in',
     'if getattr(track, "amazon_asin", None) and adapter.name in'),
    ("Amazon asks for its Opus tier in lossy mode", LOSSY, "antra/sources/amazon.py",
     "self._prefer_lossy_download = False",
     'self._prefer_lossy_download = self._preferred_output_format in {"mp3", "aac", "m4a"}'),
    ("Tidal fetches the 24-bit tier for MP3", LOSSY, "antra/sources/tidal_mirror.py",
     'if self._requires_16bit() or self._preferred_output_format == "mp3":',
     "if self._requires_16bit():"),
    ("FEAT-2 blocks the last-resort re-encode", LOSSY, "antra/utils/transcoder.py",
     "if not allow_lossy_reencode and self.blocks_lossy_reencode(file_path, base_format):",
     "if self.blocks_lossy_reencode(file_path, base_format):"),
    ("lossy-only source accepted straight away in the loop", LOSSY, "antra/core/resolver.py",
     "if (not result.is_lossless and self._serves_native_lossy(adapter)\n                        and self._passes_identity_gate(track, result)):",
     "if not result.is_lossless and self._passes_identity_gate(track, result):"),
    ("lossy-only sources queried even when a master exists", LOSSY, "antra/core/resolver.py",
     "and self._has_usable_lossless(track, candidates)):\n                logger.debug(f\"[Resolver] Skipping",
     "and False):\n                logger.debug(f\"[Resolver] Skipping"),
    ("YouTube has no android rung", LOSSY, "antra/sources/youtube.py",
     '_CLIENT_LADDER = (None, ["android"])', "_CLIENT_LADDER = (None,)"),
    ("YouTube keeps the muxed mp4", LOSSY, "antra/sources/youtube.py",
     'if ext in ("mp4", "3gp"):', 'if ext in ("never",):'),
    ("412 no longer counts as a stale hash", HASH, "antra/core/spotify_partner.py",
     "return status == 412 or", "return"),
    ("rotation never retries with the live hash", HASH, "antra/core/spotify_partner.py",
     "        if fresh:\n            resp = _once(fresh)", "        if False:\n            resp = _once(fresh)"),
    ("rotated hash is not remembered", HASH, "antra/core/spotify_partner.py",
     "        _hash_overrides[operation] = fresh\n", ""),
    ("library GET path ignores stale hashes", HASH, "antra/core/spotify_library.py",
     "        if not _retried and is_stale_hash(r.status_code, r.text):",
     "        if False and is_stale_hash(r.status_code, r.text):"),
    ("podcast path ignores stale hashes", HASH, "antra/core/podcast.py",
     "if resp is not None and is_stale_hash(resp.status_code, resp.text) else None",
     "if False else None"),
    ("InnerTube request sends no visitor id (the 'not a bot' bug)", LOSSY, "antra/sources/youtube.py",
     "dict(_IT_CLIENT, visitorData=visitor)", "dict(_IT_CLIENT)"),
    ("InnerTube never retries a burned visitor id", LOSSY, "antra/sources/youtube.py",
     'if status != "LOGIN_REQUIRED":', "if True:"),
    ("InnerTube prefers Opus over AAC", LOSSY, "antra/sources/youtube.py",
     "_IT_ITAGS = (140, 251)", "_IT_ITAGS = (251, 140)"),
    ("InnerTube accepts a short transfer", LOSSY, "antra/sources/youtube.py",
     "if got != size:", "if False:"),
    ("InnerTube rung not wired into download()", LOSSY, "antra/sources/youtube.py",
     "direct = _innertube_audio(video_url, tmp_stem)", "direct = None"),
    ("InnerTube downloads without ranges", LOSSY, "antra/sources/youtube.py",
     'headers={"Range": f"bytes={got}-{end}"}', "headers={}"),
]


def md5(b):
    return hashlib.md5(b).hexdigest()


def run(test):
    for root, _, files in os.walk(os.path.join(ROOT, "antra")):
        if root.endswith("__pycache__"):
            shutil.rmtree(root, ignore_errors=True)  # same-length edits would reuse a stale .pyc
    try:
        r = subprocess.run([sys.executable, os.path.join(ROOT, test)], cwd=ROOT, capture_output=True,
                           text=True, timeout=240)
        return r.returncode
    except subprocess.TimeoutExpired:
        return 99


caught = missed = noop = 0
for label, test, rel, old, new in SABOTAGES:
    path = os.path.join(ROOT, rel)
    orig = open(path, "rb").read()
    text = orig.decode()
    crlf = "\r\n" in text
    work = text.replace("\r\n", "\n")
    if work.count(old) != 1:
        print(f"NO-OP  {label}: anchor found {work.count(old)}x in {rel}")
        noop += 1
        continue
    mutated = work.replace(old, new)
    try:
        open(path, "wb").write((mutated.replace("\n", "\r\n") if crlf else mutated).encode())
        rc = run(test)
    finally:
        open(path, "wb").write(orig)
    assert md5(open(path, "rb").read()) == md5(orig), f"{rel} not restored"
    if rc != 0:
        caught += 1
        print(f"CAUGHT {label}")
    else:
        missed += 1
        print(f"MISSED {label}")

print(f"\n{caught} caught, {missed} missed, {noop} no-op of {len(SABOTAGES)}")
sys.exit(1 if missed or noop else 0)
