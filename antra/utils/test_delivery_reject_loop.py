"""AUDIT-DESKTOP-7 — a delivery-gate rejection must blacklist the candidate.

When `_final_delivery_problem` rejects a downloaded file the engine discards it
and re-resolves. The exception path calls `adapter.mark_failed_result` first;
the delivery path did not, so an adapter that is never excluded after a failure
(Soulseek: should_exclude_adapter_after_failure -> False) was handed the SAME
candidate again, forever.

Drives the REAL DownloadEngine.download_track; only the resolver/adapter are
fakes. The fake resolver gives up after MAX_SERVES so a regression fails
instead of hanging.

Run:  python antra/utils/test_delivery_reject_loop.py
"""
from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

org_mod = importlib.import_module("antra.utils.organizer")
eng_mod = importlib.import_module("antra.core.engine")
models = importlib.import_module("antra.core.models")
runtime = importlib.import_module("antra.utils.runtime")
enricher_mod = importlib.import_module("antra.core.metadata_enricher")
enricher_mod.MetadataEnricher.enrich = staticmethod(lambda t, r=None, fetch_lyrics=True: None)

FFMPEG = runtime.get_ffmpeg_exe() or shutil.which("ffmpeg")
assert FFMPEG, "ffmpeg is required"
TMP = Path(tempfile.mkdtemp(prefix="dlvloop_"))
SRC = TMP / "src.flac"
subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anoisesrc=d=12:c=pink:r=44100:a=0.3",
                "-ac", "2", "-c:a", "flac", str(SRC)], check=True)

FAILS: list[str] = []


def check(label, ok, detail=""):
    if not ok:
        FAILS.append(label)
        print(f"  FAIL  {label} :: {detail}")


MAX_SERVES = 6


class StickyAdapter:
    """Soulseek-shaped: never excluded after a failure, but remembers bad candidates."""
    name = "sticky"

    def __init__(self):
        self.blocked, self.downloads = set(), 0

    def download(self, result, output_base):
        self.downloads += 1
        out = output_base + ".flac"
        shutil.copyfile(SRC, out)
        return out

    def should_retry_download(self, r, e):
        return False

    def should_exclude_adapter_after_failure(self, r, e):
        return False

    def mark_failed_result(self, r, e):
        self.blocked.add(r.stream_id)

    def hydrate_track_metadata(self, t, r):
        pass


class Resolver:
    def __init__(self, adapter, candidates):
        self.adapter, self.candidates, self.serves = adapter, candidates, 0

    def resolve(self, track, excluded_adapters=None):
        self.serves += 1
        if self.serves > MAX_SERVES:
            raise RuntimeError("engine kept re-resolving: infinite loop")
        for sid in self.candidates:
            if sid not in self.adapter.blocked:
                r = models.SearchResult(source="sticky", title=track.title, artists=track.artists, album=track.album,
                                        duration_ms=track.duration_ms, audio_format=models.AudioFormat.FLAC,
                                        quality_kbps=None, is_lossless=True, download_url=None, stream_id=sid,
                                        similarity_score=1.0)
                return r, self.adapter
        return None

    def last_resolve_report(self):
        return {}

    def __getattr__(self, name):
        return lambda *a, **k: None


def run(candidates, reject_first_n):
    lib = Path(tempfile.mkdtemp(dir=TMP))
    adapter = StickyAdapter()
    resolver = Resolver(adapter, candidates)
    cfg = eng_mod.EngineConfig(max_retries=1, retry_delay=0, fetch_lyrics=False, output_format="source")
    engine = eng_mod.DownloadEngine(resolver, org_mod.LibraryOrganizer(str(lib)), config=cfg)
    calls = {"n": 0}

    def gate(path):
        calls["n"] += 1
        return "path too long" if calls["n"] <= reject_first_n else None

    engine._final_delivery_problem = gate
    track = models.TrackMetadata(title="Song", artists=["Band"], album="A", album_artists=["Band"], release_year=2020,
                                 track_number=1, disc_number=1, total_tracks=1, duration_ms=12000,
                                 isrc="USXX12000001", request_kind="album")
    try:
        res = engine.download_track(track, 1, 1)
    except RuntimeError as e:
        return None, adapter, str(e)
    return res, adapter, ""


# 1. every candidate rejected: must terminate FAILED after trying each once
res, ad, err = run(["a", "b"], reject_first_n=99)
check("rejection of every candidate terminates", res is not None, err)
if res is not None:
    check("-> FAILED", res.status == models.DownloadStatus.FAILED, res.status)
    check("each candidate tried exactly once", ad.downloads == 2, ad.downloads)
    check("rejected candidates are blacklisted", ad.blocked == {"a", "b"}, ad.blocked)

# 2. control: first rejected, second fine -> COMPLETED from the second candidate
res, ad, err = run(["a", "b"], reject_first_n=1)
check("control: next candidate is used", res is not None and res.status == models.DownloadStatus.COMPLETED,
      err or getattr(res, "status", None))
check("control: bad candidate blacklisted, good one not", ad.blocked == {"a"}, ad.blocked)

# 3. control: nothing rejected -> nothing blacklisted
res, ad, err = run(["a"], reject_first_n=0)
check("control: clean delivery blacklists nothing", res is not None and not ad.blocked, ad.blocked)

shutil.rmtree(TMP, ignore_errors=True)
print("FAILED:" if FAILS else "OK", *FAILS, sep="\n  " if FAILS else " ")
sys.exit(1 if FAILS else 0)
