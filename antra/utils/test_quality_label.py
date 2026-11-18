"""The quality label must never claim more than the source had.

Measured 2026-11-03: a YouTube stream of 128 kbps (or 48 kbps) AAC re-encoded to a 320 kbps MP3
was reported as "MP3 320kbps", because the label was read back from the finished file. The engine
now measures the lossy file it was handed BEFORE any re-encode and the label is capped by it.

Drives the REAL DownloadEngine.download_track (real ffmpeg transcode, real mutagen/ffprobe
measurement); only the adapter/resolver are forced.
Run:  python antra/utils/test_quality_label.py
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
events_mod = importlib.import_module("antra.core.events")
runtime = importlib.import_module("antra.utils.runtime")
enricher_mod = importlib.import_module("antra.core.metadata_enricher")
enricher_mod.MetadataEnricher.enrich = staticmethod(lambda t, r=None, fetch_lyrics=True: None)

FFMPEG = runtime.get_ffmpeg_exe() or shutil.which("ffmpeg")
assert FFMPEG, "ffmpeg is required"
TMP = Path(tempfile.mkdtemp(prefix="qlabel_"))
FAILS: list[str] = []
PASSED = 0


def check(label, ok, detail=""):
    global PASSED
    if ok:
        PASSED += 1
    else:
        FAILS.append(label)
        print(f"  FAIL  {label} :: {detail}")


# name -> (ffmpeg input, codec args, extension)
FIXTURES = {
    "aac128": ("anoisesrc=d=12:c=pink:r=44100:a=0.3", ["-c:a", "aac", "-b:a", "128k"], "m4a"),
    "aac136": ("anoisesrc=d=12:c=pink:r=44100:a=0.3", ["-c:a", "aac", "-b:a", "136k"], "m4a"),
    "aac48": ("anoisesrc=d=12:c=pink:r=44100:a=0.3", ["-c:a", "aac", "-b:a", "48k"], "m4a"),
    "mp3_320": ("anoisesrc=d=12:c=pink:r=44100:a=0.3", ["-c:a", "libmp3lame", "-b:a", "320k"], "mp3"),
    # near-silent FLAC compresses to a few kbps: a lossless source must NOT inherit that as a cap
    "flac_quiet": ("anullsrc=r=44100:cl=stereo", ["-t", "12", "-c:a", "flac"], "flac"),
    "flac_noise": ("anoisesrc=d=12:c=pink:r=44100:a=0.3", ["-c:a", "flac"], "flac"),
}


def fixture(name):
    src, args, ext = FIXTURES[name]
    out = TMP / f"{name}.{ext}"
    if not out.exists():
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", src, "-ac", "2", *args, str(out)], check=True)
    return out


class Adapter:
    def __init__(self, fixture_name, lossy_only):
        self.name, self.fx, self.always_lossy = "fake", fixture_name, lossy_only
        self.is_last_resort = lossy_only

    def download(self, result, output_base):
        src = fixture(self.fx)
        out = output_base + src.suffix
        shutil.copyfile(src, out)
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
    def __init__(self, adapter, lossless, claimed_kbps):
        self.adapter, self.lossless, self.claimed, self.served = adapter, lossless, claimed_kbps, False

    def resolve(self, track, excluded_adapters=None):
        if self.served:
            return None
        self.served = True
        r = models.SearchResult(
            source="fake", title=track.title, artists=track.artists, album=track.album, duration_ms=track.duration_ms,
            audio_format=models.AudioFormat.FLAC if self.lossless else models.AudioFormat.AAC,
            quality_kbps=None if self.lossless else self.claimed, is_lossless=self.lossless, download_url=None,
            stream_id="1", similarity_score=1.0)
        return r, self.adapter

    def last_resolve_report(self):
        return {}

    def __getattr__(self, name):
        return lambda *a, **k: None


def run(fixture_name, *, lossless, lossy_only, claimed=320, fmt="mp3"):
    lib = Path(tempfile.mkdtemp(dir=TMP))
    cfg = eng_mod.EngineConfig(max_retries=1, retry_delay=0, fetch_lyrics=False, output_format=fmt)
    engine = eng_mod.DownloadEngine(Resolver(Adapter(fixture_name, lossy_only), lossless, claimed),
                                    org_mod.LibraryOrganizer(str(lib)), config=cfg)
    evs = []
    engine.event_callback = evs.append
    track = models.TrackMetadata(title="Song", artists=["Band"], album="A", album_artists=["Band"], release_year=2020,
                                 track_number=1, disc_number=1, total_tracks=1, duration_ms=12000, isrc="USXX12000001",
                                 request_kind="album")
    res = engine.download_track(track, 1, 1)
    done = [e for e in evs if e.type is events_mod.EngineEventType.TRACK_COMPLETED]
    return res, (done[0].quality_label if done else None)


# 1. the reported bug: low-bitrate lossy source, re-encoded to a 320 kbps MP3, adapter CLAIMS 320
res, label = run("aac128", lossless=False, lossy_only=True, claimed=320)
check("128 kbps AAC -> MP3 completes", res.status == models.DownloadStatus.COMPLETED and Path(res.file_path).suffix == ".mp3", (res.status, res.file_path))
check("...and is labelled 128 kbps, not 320 (even though the adapter claimed 320)", label == "MP3 128kbps", label)

res, label = run("aac48", lossless=False, lossy_only=True, claimed=None)
check("48 kbps AAC -> MP3 is labelled 48 kbps", label == "MP3 48kbps", label)

def file_kbps(path):
    out = subprocess.run([runtime.get_ffprobe_exe() or "ffprobe", "-v", "error", "-show_entries",
                          "format=bit_rate", "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True).stdout.strip()
    return int(out) // 1000 if out.isdigit() else None


# the encode itself is capped to the source too, so the file and its label agree
check("48 kbps source -> the MP3 itself is ~48 kbps, not 320", (file_kbps(res.file_path) or 999) <= 70, file_kbps(res.file_path))
res128, label128 = run("aac128", lossless=False, lossy_only=True, claimed=320)
check("128 kbps source -> the MP3 itself is ~128 kbps (about 2.5x smaller than a 320 encode)",
      120 <= (file_kbps(res128.file_path) or 0) <= 140, file_kbps(res128.file_path))
check("...and the label agrees with the file", label128 == "MP3 128kbps", label128)

# the encoder rounds UP to the next standard step (136 -> 160), so the file is bigger than the
# source: the label must still not claim more than the source had
res136, label136 = run("aac136", lossless=False, lossy_only=True, claimed=320)
check("136 kbps source is encoded at the 160 step (rounded up, not down)", 150 <= (file_kbps(res136.file_path) or 0) <= 175, file_kbps(res136.file_path))
check("...and is still labelled with the SOURCE's bitrate, not the 160 of the file",
      label136 is not None and label136.startswith("MP3 13") and "160" not in label136, label136)

# 2. controls that must come out the other way
res, label = run("mp3_320", lossless=False, lossy_only=False, claimed=320)
check("CONTROL: a genuine 320 kbps MP3 stays 320", label == "MP3 320kbps", label)

res, label = run("flac_quiet", lossless=True, lossy_only=False, claimed=None)
check("CONTROL: a LOSSLESS source is never capped by its own low FLAC bitrate", label == "MP3 320kbps", label)
check("CONTROL: ...and its MP3 is really encoded at 320 (the cap only applies to lossy sources)",
      (file_kbps(res.file_path) or 0) >= 300, file_kbps(res.file_path))

res, label = run("flac_noise", lossless=True, lossy_only=False, claimed=None, fmt="flac")
check("CONTROL: lossless output keeps its bit-depth label", label is not None and "kbps" not in label and "FLAC" in label, label)

res, label = run("aac128", lossless=False, lossy_only=True, claimed=None, fmt="aac")
check("128 kbps AAC kept as AAC is labelled with the file's real bitrate", label is not None and label.startswith("AAC 1") and "kbps" in label, label)

# 3. the measurement itself
f = str(fixture("aac128"))
got = eng_mod.DownloadEngine._lossy_source_bitrate_kbps(f, type("R", (), {"quality_kbps": 320})())
check("measured bitrate wins over the adapter's claim", got is not None and 115 <= got <= 140, got)
junk = TMP / "junk.m4a"
junk.write_bytes(b"\x00" * 4096)
check("unreadable file falls back to the adapter's claim",
      eng_mod.DownloadEngine._lossy_source_bitrate_kbps(str(junk), type("R", (), {"quality_kbps": 256})()) == 256)
check("unreadable file and no claim -> None, not a guess",
      eng_mod.DownloadEngine._lossy_source_bitrate_kbps(str(junk), type("R", (), {"quality_kbps": None})()) is None)

# 4. the label function on its own
lab = eng_mod.DownloadEngine._quality_label_from_file
mp3 = str(fixture("mp3_320"))
check("label: source lower than file -> source", lab(mp3, models.AudioFormat.MP3, "x", source_kbps=128) == "MP3 128kbps", lab(mp3, models.AudioFormat.MP3, "x", source_kbps=128))
check("label: source higher than file -> file (a downgrade is shown truthfully)", lab(mp3, models.AudioFormat.MP3, "x", source_kbps=900) == "MP3 320kbps")
check("label: no source info -> unchanged behaviour", lab(mp3, models.AudioFormat.MP3, "x") == "MP3 320kbps")

# 5. the encode cap
cap = importlib.import_module("antra.utils.transcoder").AudioTranscoder.cap_kbps
check("cap: 128.4 kbps source -> 128 step", cap(320, 128) == 128, cap(320, 128))
check("cap: 131 kbps (itag 140 measures ~131) stays on the 128 step, within the 4 kbps noise allowance", cap(320, 131) == 128, cap(320, 131))
check("cap: 136 kbps rounds UP to 160 (never encode clearly below the source)", cap(320, 136) == 160, cap(320, 136))
check("cap: 48 kbps source -> 48", cap(320, 48) == 48, cap(320, 48))
check("cap: the configured ceiling still wins (source 320, ceiling 256 -> 256)", cap(256, 320) == 256, cap(256, 320))
check("cap: source above every step -> ceiling", cap(320, 900) == 320, cap(320, 900))
check("cap: unknown source -> ceiling unchanged", cap(320, None) == 320 and cap(320, 0) == 320)
tc = importlib.import_module("antra.utils.transcoder").AudioTranscoder()
args = tc._plan("mp3", max_source_kbps=96).codec_args
check("plan: MP3 gets the capped -b:a", "96k" in args and "320k" not in args, args)
check("plan: no source info keeps 320k", "320k" in tc._plan("mp3").codec_args)
m4a_capped = tc._plan("m4a", file_path="x.opus", max_source_kbps=96).codec_args
check("plan: M4A re-encode is capped too", "96k" in m4a_capped and "256k" not in m4a_capped, m4a_capped)
check("plan: M4A without source info keeps 256k", "256k" in tc._plan("m4a", file_path="x.opus").codec_args)
check("plan: AAC re-encode is capped too", "128k" in tc._plan("aac", file_path="x.opus", max_source_kbps=128).codec_args,
      tc._plan("aac", file_path="x.opus", max_source_kbps=128).codec_args)

print(f"\n{PASSED} passed, {len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
