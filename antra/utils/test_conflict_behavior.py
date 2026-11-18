"""v2.0.0 DMX-5 — filename_conflict_behavior on desktop.

Drives the REAL LibraryOrganizer, FileTagger and DownloadEngine.download_track
against real FLAC/MP3 files generated with ffmpeg, and asserts on what is on
disk afterwards: which files exist, their audio bytes (FLAC STREAMINFO MD5),
and their tags. Only the network edges are faked — the resolver/adapter (the
adapter writes a real FLAC) and the free metadata enricher.

Run:  python antra/utils/test_conflict_behavior.py [tree-root]
"""
from __future__ import annotations

import hashlib
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []
PASSES = 0


def check(label: str, ok: bool, detail: object = "") -> None:
    global PASSES
    if ok:
        PASSES += 1
    else:
        FAILS.append(f"{label} :: {detail}")
        print(f"  FAIL  {label} :: {detail}")


fp = importlib.import_module("antra_shared.filename_prefs")
org_mod = importlib.import_module("antra.utils.organizer")
eng_mod = importlib.import_module("antra.core.engine")
models = importlib.import_module("antra.core.models")
runtime = importlib.import_module("antra.utils.runtime")
enricher_mod = importlib.import_module("antra.core.metadata_enricher")
assert str(Path(eng_mod.__file__).resolve()).startswith(str(ROOT)), eng_mod.__file__

from mutagen.flac import FLAC  # noqa: E402
from mutagen.id3 import ID3, TIT2, TPE1, TSRC  # noqa: E402

FFMPEG = runtime.get_ffmpeg_exe() or shutil.which("ffmpeg")
assert FFMPEG, "ffmpeg is required"

DURATION_S = 12
TMP = Path(tempfile.mkdtemp(prefix="dmx5_"))
_FIXTURES: dict[str, Path] = {}


def fixture(kind: str, seed: int) -> Path:
    """A real audio file of DURATION_S seconds; different seeds = different audio."""
    key = f"{kind}-{seed}"
    if key in _FIXTURES:
        return _FIXTURES[key]
    out = TMP / f"fixture_{key}.{kind}"
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi",
           "-i", f"anoisesrc=d={DURATION_S}:c=pink:r=44100:a=0.3:s={seed}",
           "-ac", "2"]
    cmd += ["-c:a", "flac"] if kind == "flac" else ["-c:a", "libmp3lame", "-b:a", "192k"]
    subprocess.run(cmd + [str(out)], check=True)
    _FIXTURES[key] = out
    return out


def audio_md5(path: Path) -> str:
    if path.suffix == ".flac":
        return "%032x" % FLAC(str(path)).info.md5_signature
    return hashlib.md5(path.read_bytes()).hexdigest()


def place(kind: str, seed: int, dest: Path, *, title: str, isrc: str, artist: str = "Band",
          provenance: str | None = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(fixture(kind, seed), dest)
    if kind == "flac":
        a = FLAC(str(dest))
        a["title"] = title
        a["artist"] = artist
        a["isrc"] = isrc
        if provenance:
            a["antra_version"] = provenance
        a.save()
    else:
        tags = ID3()
        tags.add(TIT2(encoding=3, text=title))
        tags.add(TPE1(encoding=3, text=artist))
        tags.add(TSRC(encoding=3, text=isrc))
        tags.save(str(dest))
    return dest


def make_track(**over):
    base = dict(title="Song", artists=["Band"], album="Studio Album", album_artists=["Band"],
                release_year=2020, track_number=1, disc_number=1, total_tracks=10,
                duration_ms=DURATION_S * 1000, isrc="USXX12000001", request_kind="album")
    base.update(over)
    return models.TrackMetadata(**base)


class FakeAdapter:
    name = "fake"

    def __init__(self, seed: int = 99, fail: BaseException | None = None, partial: bool = False):
        self.seed, self.fail, self.partial = seed, fail, partial
        self.downloads = 0

    def download(self, result, output_base):
        self.downloads += 1
        out = output_base + ".flac"
        if self.partial:
            with open(out, "wb") as fh:  # adapters stream into their final path
                fh.write(b"fLaC" + b"\x00" * 2048)
        if self.fail is not None:
            raise self.fail
        shutil.copyfile(fixture("flac", self.seed), out)
        return out

    def should_retry_download(self, result, e):
        return False

    def should_exclude_adapter_after_failure(self, result, e):
        return True

    def mark_failed_result(self, result, e):
        pass

    def hydrate_track_metadata(self, track, result):
        pass


class FakeResolver:
    def __init__(self, adapter):
        self.adapter, self.served = adapter, False

    def resolve(self, track, excluded_adapters=None):
        if self.served or (excluded_adapters and self.adapter.name in excluded_adapters):
            return None
        self.served = True
        r = models.SearchResult(source="fake", title=track.title, artists=track.artists, album=track.album,
                                duration_ms=track.duration_ms, audio_format=models.AudioFormat.FLAC,
                                quality_kbps=None, is_lossless=True, download_url=None, stream_id="1",
                                similarity_score=1.0)
        return r, self.adapter

    def last_resolve_report(self):
        return {}

    def __getattr__(self, name):  # record_outcome, record_album_source_*, _mark_rate_limited
        return lambda *a, **k: None


ENRICH_CALLS = []


def fake_enrich(track, result=None, fetch_lyrics=True):
    ENRICH_CALLS.append((track.title, result))
    track.genres = ["Enriched Genre"]


enricher_mod.MetadataEnricher.enrich = staticmethod(fake_enrich)


def run(mode: str, library: Path, track, adapter: FakeAdapter, *, output_format="source"):
    organizer = org_mod.LibraryOrganizer(str(library), filename_conflict_behavior=mode)
    cfg = eng_mod.EngineConfig(max_retries=1, retry_delay=0, fetch_lyrics=False, output_format=output_format)
    engine = eng_mod.DownloadEngine(FakeResolver(adapter), organizer, config=cfg)
    events = []
    engine.event_callback = events.append
    return engine.download_track(track, 1, 1), organizer, events


def lib(name: str) -> Path:
    p = TMP / name
    p.mkdir()
    return p


def leftovers(library: Path, suffix: str) -> list[str]:
    return [str(p) for p in library.rglob(f"*{suffix}")]


# ── 1. normalisation ────────────────────────────────────────────────────────
print("1. normalisation")
for raw, want in [("skip", "skip"), ("overwrite", "overwrite"), ("append_counter", "append_counter"),
                  ("tag_only", "tag_only"), ("rename", "append_counter"), ("keep_both", "append_counter"),
                  ("Tags_Only", "tag_only"), ("  OVERWRITE ", "overwrite"), (None, "skip"), ("", "skip"),
                  ("delete_everything", "skip"), (7, "skip")]:
    check(f"normalize({raw!r})", fp.normalize_conflict_behavior(raw) == want, fp.normalize_conflict_behavior(raw))
check("default preference is skip", fp.migrate_legacy_templates({})["filename_conflict_behavior"] == "skip")
check("unknown in prefs dict degrades to skip",
      fp.migrate_legacy_templates({"filename_conflict_behavior": "nope"})["filename_conflict_behavior"] == "skip")

# ── 2. skip (regression) ────────────────────────────────────────────────────
print("2. skip")
L = lib("skip")
t = make_track()
target = Path(org_mod.LibraryOrganizer(str(L)).get_output_path(t) + ".flac")
place("flac", 1, target, title="OLD TITLE", isrc=t.isrc)
before = audio_md5(target)
ad = FakeAdapter()
res, _, ev = run("skip", L, make_track(), ad)
check("skip: SKIPPED", res.status == models.DownloadStatus.SKIPPED, res.status)
check("skip: nothing downloaded", ad.downloads == 0, ad.downloads)
check("skip: tags untouched", FLAC(str(target))["title"] == ["OLD TITLE"], FLAC(str(target)).get("title"))
check("skip: audio untouched", audio_md5(target) == before)
check("skip: no enrichment pass", not ENRICH_CALLS, ENRICH_CALLS)

# ── 3. tag_only — file at this request's own path ───────────────────────────
print("3. tag_only (own path)")
L = lib("tag_own")
t = make_track()
target = Path(org_mod.LibraryOrganizer(str(L)).get_output_path(t) + ".flac")
place("flac", 1, target, title="OLD TITLE", isrc=t.isrc, provenance="1.1.2")
before = audio_md5(target)
ENRICH_CALLS.clear()
ad = FakeAdapter()
res, _, ev = run("tag_only", L, make_track(), ad)
tags = FLAC(str(target))
check("tag_only: SKIPPED (no download)", res.status == models.DownloadStatus.SKIPPED, res.status)
check("tag_only: nothing downloaded", ad.downloads == 0, ad.downloads)
check("tag_only: title re-tagged", tags.get("title") == ["Song"], tags.get("title"))
check("tag_only: enriched genre written", tags.get("genre") == ["Enriched Genre"], tags.get("genre"))
check("tag_only: enrichment ran without a source result",
      ENRICH_CALLS and ENRICH_CALLS[0][1] is None, ENRICH_CALLS)
check("tag_only: audio bytes unchanged", audio_md5(target) == before)
check("tag_only: provenance from the ORIGINAL build preserved",
      tags.get("antra_version") == ["1.1.2"], tags.get("antra_version"))
check("tag_only: no ANTRA_DOWNLOADED stamped", "antra_downloaded" not in tags, dict(tags))
skipped = [e for e in ev if e.type == eng_mod.EngineEventType.TRACK_SKIPPED]
check("tag_only: event says Re-tagged", skipped and skipped[0].message.startswith("Re-tagged"),
      [e.message for e in skipped])

# tag_only on an MP3 at its own path
L = lib("tag_mp3")
t = make_track()
target = Path(org_mod.LibraryOrganizer(str(L)).get_output_path(t) + ".mp3")
place("mp3", 1, target, title="OLD TITLE", isrc=t.isrc)
ad = FakeAdapter()
res, _, _ = run("tag_only", L, make_track(), ad)
check("tag_only mp3: no download", ad.downloads == 0, ad.downloads)
check("tag_only mp3: title re-tagged", str(ID3(str(target)).get("TIT2")) == "Song", ID3(str(target)).get("TIT2"))

# ── 4. tag_only — the recording is filed under a DIFFERENT release ──────────
print("4. tag_only (found elsewhere)")
L = lib("tag_elsewhere")
other = L / "Band" / "2015 - Best Of" / "07 - Song.flac"
place("flac", 1, other, title="Song (Best Of edit)", isrc="USXX12000001")
before = audio_md5(other)
ad = FakeAdapter()
res, _, ev = run("tag_only", L, make_track(), ad)
check("elsewhere: SKIPPED", res.status == models.DownloadStatus.SKIPPED, res.status)
check("elsewhere: nothing downloaded (still dedups like skip)", ad.downloads == 0, ad.downloads)
check("elsewhere: other release's tags NOT rewritten",
      FLAC(str(other)).get("title") == ["Song (Best Of edit)"], FLAC(str(other)).get("title"))
check("elsewhere: audio untouched", audio_md5(other) == before)
check("elsewhere: event is a plain skip", ev and ev[-1].message == "Track already exists on disk.",
      [e.message for e in ev])

# ── 5. tag_only — nothing on disk downloads normally ────────────────────────
print("5. tag_only (absent)")
L = lib("tag_absent")
ad = FakeAdapter()
res, org, _ = run("tag_only", L, make_track(), ad)
check("absent: COMPLETED", res.status == models.DownloadStatus.COMPLETED, (res.status, res.error_message))
check("absent: downloaded once", ad.downloads == 1, ad.downloads)
check("absent: fresh download DOES get provenance",
      res.file_path and "antra_version" in FLAC(res.file_path), res.file_path)

# ── 6. overwrite — success replaces ─────────────────────────────────────────
print("6. overwrite (success)")
L = lib("ow_ok")
t = make_track()
target = Path(org_mod.LibraryOrganizer(str(L)).get_output_path(t) + ".flac")
place("flac", 1, target, title="OLD TITLE", isrc=t.isrc)
old = audio_md5(target)
ad = FakeAdapter(seed=99)
res, _, _ = run("overwrite", L, make_track(), ad)
check("overwrite: COMPLETED", res.status == models.DownloadStatus.COMPLETED, (res.status, res.error_message))
check("overwrite: downloaded", ad.downloads == 1, ad.downloads)
check("overwrite: same path", res.file_path and Path(res.file_path).resolve() == target.resolve(), res.file_path)
check("overwrite: audio is the NEW audio", audio_md5(target) == audio_md5(fixture("flac", 99)) != old)
check("overwrite: no backup left", not leftovers(L, eng_mod.DownloadEngine._OVERWRITE_STASH_SUFFIX),
      leftovers(L, eng_mod.DownloadEngine._OVERWRITE_STASH_SUFFIX))
check("overwrite: exactly one audio file", len([p for p in L.rglob("*.flac")]) == 1, list(L.rglob("*.flac")))

# ── 7. overwrite — a failed re-download restores the original ───────────────
print("7. overwrite (failure)")
for label, adapter in [("exception", FakeAdapter(fail=RuntimeError("mirror died"), partial=True)),
                       ("partial-then-raise", FakeAdapter(fail=RuntimeError("reset by peer"), partial=True))]:
    L = lib("ow_fail_" + label)
    t = make_track()
    target = Path(org_mod.LibraryOrganizer(str(L)).get_output_path(t) + ".flac")
    place("flac", 1, target, title="OLD TITLE", isrc=t.isrc)
    old_bytes = target.read_bytes()
    res, _, _ = run("overwrite", L, make_track(), adapter)
    check(f"{label}: FAILED", res.status == models.DownloadStatus.FAILED, res.status)
    check(f"{label}: original restored byte-identical", target.exists() and target.read_bytes() == old_bytes)
    check(f"{label}: no backup left", not leftovers(L, eng_mod.DownloadEngine._OVERWRITE_STASH_SUFFIX))

# BaseException escaping download_track entirely (e.g. KeyboardInterrupt)
class Boom(BaseException):
    pass


L = lib("ow_base")
t = make_track()
target = Path(org_mod.LibraryOrganizer(str(L)).get_output_path(t) + ".flac")
place("flac", 1, target, title="OLD TITLE", isrc=t.isrc)
old_bytes = target.read_bytes()
raised = False
try:
    run("overwrite", L, make_track(), FakeAdapter(fail=Boom(), partial=True))
except Boom:
    raised = True
check("base-exception: propagates", raised)
check("base-exception: original restored", target.exists() and target.read_bytes() == old_bytes)
check("base-exception: no backup left", not leftovers(L, eng_mod.DownloadEngine._OVERWRITE_STASH_SUFFIX))

# ── 8. overwrite — a different format at the same stem is replaced ──────────
print("8. overwrite (format change)")
L = lib("ow_fmt")
t = make_track()
base = org_mod.LibraryOrganizer(str(L)).get_output_path(t)
mp3 = place("mp3", 1, Path(base + ".mp3"), title="OLD TITLE", isrc=t.isrc)
res, _, _ = run("overwrite", L, make_track(), FakeAdapter())
check("format: COMPLETED", res.status == models.DownloadStatus.COMPLETED, (res.status, res.error_message))
check("format: old mp3 gone", not mp3.exists())
check("format: new flac present", Path(base + ".flac").exists())

# ── 9. overwrite never replaces a DIFFERENT recording sharing the stem ──────
print("9. overwrite (different recording)")
L = lib("ow_diff")
t = make_track()
target = Path(org_mod.LibraryOrganizer(str(L)).get_output_path(t) + ".flac")
place("flac", 1, target, title="Totally Other Song", artist="Other Band", isrc="GBZZ19999999")
old_bytes = target.read_bytes()
res, _, _ = run("overwrite", L, make_track(), FakeAdapter())
check("diff: COMPLETED", res.status == models.DownloadStatus.COMPLETED, (res.status, res.error_message))
check("diff: other recording untouched", target.read_bytes() == old_bytes)
check("diff: new file written beside it", res.file_path and Path(res.file_path).resolve() != target.resolve(),
      res.file_path)

# ── 10. keep both ───────────────────────────────────────────────────────────
print("10. append_counter (keep both)")
L = lib("keep")
t = make_track()
target = Path(org_mod.LibraryOrganizer(str(L)).get_output_path(t) + ".flac")
place("flac", 1, target, title="OLD TITLE", isrc=t.isrc)
old_bytes = target.read_bytes()
res, _, _ = run("append_counter", L, make_track(), FakeAdapter())
check("keep: COMPLETED", res.status == models.DownloadStatus.COMPLETED, (res.status, res.error_message))
check("keep: original untouched", target.read_bytes() == old_bytes)
check("keep: second copy is '(2)'", res.file_path and Path(res.file_path).stem.endswith("(2)"), res.file_path)

# ── 11. lossless mode still re-downloads a lossy file under tag_only ────────
print("11. tag_only + lossless mode + lossy existing")
L = lib("tag_lossy")
t = make_track()
base = org_mod.LibraryOrganizer(str(L)).get_output_path(t)
place("mp3", 1, Path(base + ".mp3"), title="OLD TITLE", isrc=t.isrc)
ad = FakeAdapter()
res, _, _ = run("tag_only", L, make_track(), ad, output_format="lossless")
check("lossy+lossless: upgrade still downloads", ad.downloads == 1, ad.downloads)

# ── 12. hard-linked playlist copies are not re-tagged ───────────────────────
print("12. retag_target guards")
L = lib("guards")
o = org_mod.LibraryOrganizer(str(L), filename_conflict_behavior="tag_only")
pt = make_track(playlist_name="Mix", playlist_position=3)
pl_path = Path(o.get_output_path(pt) + ".flac")
album_file = place("flac", 1, L / "Band" / "2020 - Studio Album" / "01 - Song.flac", title="Song", isrc=pt.isrc)
os.link(album_file, pl_path)
check("hard link: not a retag target", o.retag_target(pt, str(pl_path)) is None)
os.remove(pl_path)
shutil.copyfile(album_file, pl_path)
check("independent playlist copy: retag target", o.retag_target(pt, str(pl_path)) is not None)
check("skip mode: never a retag target",
      org_mod.LibraryOrganizer(str(L)).retag_target(make_track(), str(album_file)) is None)

# ── wiring ──────────────────────────────────────────────────────────────────
print("13. wiring")
svelte = (ROOT / "antra-wails/frontend/src/App.svelte").read_text(encoding="utf-8")
catalog = (ROOT / "antra-wails/frontend/src/lib/settings-catalog.ts").read_text(encoding="utf-8")
for mode in fp.CONFLICT_BEHAVIORS:
    check(f"UI offers {mode}", f"id: '{mode}'" in svelte)
check("UI select bound", "bind:value={config.filename_conflict_behavior}" in svelte)
check("palette anchor", svelte.count('data-setting="filename_conflict_behavior"') == 1)
check("palette catalog", "id: 'filename_conflict_behavior'" in catalog)
jc = (ROOT / "antra/json_cli.py").read_text(encoding="utf-8")
check("json_cli sets the env var unconditionally",
      'os.environ["FILENAME_CONFLICT_BEHAVIOR"] = settings.get("filename_conflict_behavior") or "skip"' in jc)
gob = (ROOT / "antra-wails/app_backend.go").read_text(encoding="utf-8")
check("Go config carries the field", 'json:"filename_conflict_behavior' in gob)

shutil.rmtree(TMP, ignore_errors=True)
print()
if FAILS:
    print(f"FAIL — {PASSES} passed, {len(FAILS)} failed")
    sys.exit(1)
print(f"PASS — {PASSES} passed, 0 failed")
