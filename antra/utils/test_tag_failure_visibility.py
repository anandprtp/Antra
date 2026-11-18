"""AUDIT-DESKTOP-16 - a track whose tags could not be written must say so.

DECISION (tracker item 9): keep the audio and keep the track COMPLETED, but never
silently. Failing or discarding it would be wrong: the audio is valid, the cause
is deterministic (an unsupported container, or a tag writer that cannot parse the
file), so a re-download would fail identically, and the track's identity is
already recorded for dedupe. What was missing is visibility: the only signal was
a warning in a log nobody opens, so the row said "Added to library" and the
summary counted it as a clean success.

tag() returns False for exactly two reasons: an unsupported container (by design,
file intact) or a core-tag write failure (provenance, lyrics embedding and the
MusicBrainz lookup all contain their own errors, and a supported format's core
tags are one save(): all or nothing). The result, the event and the summary now
carry which one.

Drives the REAL DownloadEngine.download_track and json_cli.emit_event; only the
adapter/resolver and the tagger's outcome are forced.
Run:  python antra/utils/test_tag_failure_visibility.py
"""
from __future__ import annotations

import contextlib
import importlib
import io
import json
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
events_mod = importlib.import_module("antra.core.events")
runtime = importlib.import_module("antra.utils.runtime")
enricher_mod = importlib.import_module("antra.core.metadata_enricher")
enricher_mod.MetadataEnricher.enrich = staticmethod(lambda t, r=None, fetch_lyrics=True: None)

FFMPEG = runtime.get_ffmpeg_exe() or shutil.which("ffmpeg")
assert FFMPEG, "ffmpeg is required"
TMP = Path(tempfile.mkdtemp(prefix="tagvis_"))
FAILS: list[str] = []


def check(label, ok, detail=""):
    if not ok:
        FAILS.append(label)
        print(f"  FAIL  {label} :: {detail}")


def fixture(ext):
    out = TMP / f"fixture.{ext}"
    if not out.exists():
        codec = {"flac": "flac", "wav": "pcm_s16le"}[ext]
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anoisesrc=d=12:c=pink:r=44100:a=0.3",
                        "-ac", "2", "-c:a", codec, str(out)], check=True)
    return out


class Adapter:
    name = "fake"

    def __init__(self, ext):
        self.ext = ext

    def download(self, result, output_base):
        out = output_base + "." + self.ext
        shutil.copyfile(fixture(self.ext), out)
        return out

    def should_retry_download(self, r, e):
        return False

    def should_exclude_adapter_after_failure(self, r, e):
        return True

    def mark_failed_result(self, r, e):
        pass

    def hydrate_track_metadata(self, t, r):
        pass


class Resolver:
    def __init__(self, adapter):
        self.adapter, self.served = adapter, False

    def resolve(self, track, excluded_adapters=None):
        if self.served:
            return None
        self.served = True
        r = models.SearchResult(source="fake", title=track.title, artists=track.artists, album=track.album,
                                duration_ms=track.duration_ms, audio_format=models.AudioFormat.FLAC, quality_kbps=None,
                                is_lossless=True, download_url=None, stream_id="1", similarity_score=1.0)
        return r, self.adapter

    def last_resolve_report(self):
        return {}

    def __getattr__(self, name):
        return lambda *a, **k: None


def run(ext, tag_ok):
    lib = Path(tempfile.mkdtemp(dir=TMP))
    cfg = eng_mod.EngineConfig(max_retries=1, retry_delay=0, fetch_lyrics=False, output_format="source")
    engine = eng_mod.DownloadEngine(Resolver(Adapter(ext)), org_mod.LibraryOrganizer(str(lib)), config=cfg)
    engine.tagger.tag = lambda *a, **k: tag_ok
    evs = []
    engine.event_callback = evs.append
    track = models.TrackMetadata(title="Song", artists=["Band"], album="A", album_artists=["Band"], release_year=2020,
                                 track_number=1, disc_number=1, total_tracks=1, duration_ms=12000, isrc="USXX12000001",
                                 request_kind="album")
    res = engine.download_track(track, 1, 1)
    completed = [e for e in evs if e.type is events_mod.EngineEventType.TRACK_COMPLETED]
    return res, completed


# 1. taggable container, tag writer failed
res, done = run("flac", False)
check("the track is still COMPLETED", res.status == models.DownloadStatus.COMPLETED, res.status)
check("the audio file is kept", bool(res.file_path) and Path(res.file_path).exists(), res.file_path)
check("result says the tag write failed", getattr(res, "tags_problem", None) == "failed", getattr(res, "tags_problem", "<no field>"))
check("the TRACK_COMPLETED event says so too", len(done) == 1 and getattr(done[0], "tags_problem", None) == "failed",
      [getattr(e, "tags_problem", "<no field>") for e in done])

# 2. container the tagger cannot write at all
res, done = run("wav", False)
check("untaggable container: still COMPLETED, file kept", res.status == models.DownloadStatus.COMPLETED and Path(res.file_path).exists())
check("...and reported as 'unsupported', not 'failed'", getattr(res, "tags_problem", None) == "unsupported", getattr(res, "tags_problem", "<no field>"))
check("...in the event as well", len(done) == 1 and getattr(done[0], "tags_problem", None) == "unsupported")

# 3. controls: a successful tag reports nothing
res, done = run("flac", True)
check("control: tags written -> no problem on the result", res.status == models.DownloadStatus.COMPLETED
      and getattr(res, "tags_problem", "<no field>") is None, getattr(res, "tags_problem", "<no field>"))
check("control: ...and none on the event", len(done) == 1 and getattr(done[0], "tags_problem", "<no field>") is None)

# 4. the JSON the UI receives
buf = io.StringIO()
cli = importlib.import_module("antra.json_cli")
with contextlib.redirect_stdout(buf):
    cli.emit_event(events_mod.EngineEvent(type=events_mod.EngineEventType.TRACK_COMPLETED, tags_problem="failed",
                                          track_index=1, track_total=1))
payload = json.loads(buf.getvalue().strip().splitlines()[-1])["payload"]
check("emit_event ships tags_problem to the UI", payload.get("tags_problem") == "failed", payload)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    cli.emit_event(events_mod.EngineEvent(type=events_mod.EngineEventType.TRACK_COMPLETED, track_index=1, track_total=1))
payload = json.loads(buf.getvalue().strip().splitlines()[-1])["payload"]
check("control: absent when there is no problem", payload.get("tags_problem") is None and "tags_problem" in payload, payload)

# 5. the summary counts them
R = models.DownloadResult
ok_r = R(track=models.TrackMetadata(title="a", artists=["x"], album="x"), status=models.DownloadStatus.COMPLETED)
bad_r = R(track=models.TrackMetadata(title="b", artists=["x"], album="x"), status=models.DownloadStatus.COMPLETED, tags_problem="failed")
skip_r = R(track=models.TrackMetadata(title="c", artists=["x"], album="x"), status=models.DownloadStatus.SKIPPED)
summary = cli._selection_summary("u", [ok_r, bad_r, skip_r], 0.0)
check("summary counts tracks saved without tags", summary.get("tags_incomplete") == 1, summary.get("tags_incomplete"))
summary = cli._selection_summary("u", [ok_r, skip_r], 0.0)
check("control: zero when every track was tagged", summary.get("tags_incomplete") == 0, summary.get("tags_incomplete"))

# drift guard: three summary builders count statuses; none may forget the new count
src = Path(cli.__file__).read_text(encoding="utf-8")
check("every summary builder that counts 'skipped' also counts 'tags_incomplete'",
      src.count('"skipped": sum(1 for r in results') == src.count('"tags_incomplete": sum(1 for r in results') == 3,
      (src.count('"skipped": sum(1 for r in results'), src.count('"tags_incomplete": sum(1 for r in results')))

shutil.rmtree(TMP, ignore_errors=True)
print("FAILED:" if FAILS else "OK", *FAILS, sep="\n  " if FAILS else " ")
sys.exit(1 if FAILS else 0)
