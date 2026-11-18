"""End-to-end wiring for PORT-2.

The three settings pass through SIX files that share nothing but a string key —
App.svelte -> Go struct -> config.json -> json_cli env -> load_config() ->
EngineConfig -> the engine's write call. A typo in any one of them fails
SILENTLY: the toggle looks saved, and no sidecar ever appears.

So this drives the real chain rather than reading it, and finishes by running
the REAL `DownloadEngine._write_sidecars` against real files on disk.

Run:  python antra/utils/test_sidecars_wiring.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0
KEYS = ("sidecar_cue", "sidecar_nfo", "sidecar_json")


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ok    {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}" + (f"  --> {detail}" if detail else ""))


def eq(label, got, want):
    check(label, got == want, f"got {got!r}, want {want!r}")


def read(rel: str) -> str:
    return (ROOT / rel).read_bytes().decode("utf-8")


def main() -> int:
    # ── 1. Every file in the chain knows all three keys ───────────────────
    print("\n1. The key exists in every file of the chain")
    go = read("antra-wails/app_backend.go")
    ts = read("antra-wails/frontend/wailsjs/go/models.ts")
    sv = read("antra-wails/frontend/src/App.svelte")
    cli = read("antra/json_cli.py")
    cfg_src = read("antra/core/config.py")
    eng_src = read("antra/core/engine.py")
    svc_src = read("antra/core/service.py")
    cat = read("antra-wails/frontend/src/lib/settings-catalog.ts")

    for key in KEYS:
        camel = "Sidecar" + key.split("_")[1].capitalize()
        check(f"Go struct declares {camel} with json:\"{key}\"",
              f'{camel}' in go and f'`json:"{key}"`' in go)
        check(f"models.ts declares + assigns {key}",
              f"{key}: boolean;" in ts and f'this.{key} = source["{key}"]' in ts)
        check(f"App.svelte binds config.{key}",
              f"bind:checked={{config.{key}}}" in sv)
        check(f"App.svelte anchors data-setting=\"{key}\" for the palette",
              f'data-setting="{key}"' in sv)
        check(f"settings-catalog.ts indexes {key}", f"id: '{key}'" in cat)
        check(f"json_cli exports {key.upper()}", f'os.environ["{key.upper()}"]' in cli)
        check(f"config.py reads {key.upper()}", f'os.getenv("{key.upper()}"' in cfg_src)
        check(f"config.py declares the {key} field", f"{key}: bool = False" in cfg_src)
        check(f"EngineConfig declares {key}", f"{key}: bool = False" in eng_src)
        check(f"build_engine passes {key}", f'{key}=getattr(cfg, "{key}", False)' in svc_src)

    # ── 2. json_cli sets them UNCONDITIONALLY (the BUG-23 trap) ───────────
    print("\n2. json_cli sets them unconditionally, so a stale .env cannot win")
    for key in KEYS:
        m = re.search(r'os\.environ\["%s"\] = ([^\n]+)' % key.upper(), cli)
        check(f"{key.upper()} is assigned from a defaulted helper, not `if key in settings`",
              bool(m) and "_art_bool" in m.group(1), m.group(1) if m else "no assignment")
    # ...and prove it by counting: none of the three sits under an `if ... in settings`
    for key in KEYS:
        idx = cli.index(f'os.environ["{key.upper()}"]')
        window = cli[max(0, idx - 260):idx]
        check(f"{key.upper()} is not guarded by `if \"...\" in settings`",
              f'in settings' not in window.split("\n")[-1])

    # ── 3. The env -> Config -> EngineConfig chain, executed ──────────────
    print("\n3. The chain, executed end to end")
    from antra.core.config import load_config

    saved = {k.upper(): os.environ.get(k.upper()) for k in KEYS}
    try:
        for k in KEYS:
            os.environ[k.upper()] = "true"
        cfg = load_config()
        for k in KEYS:
            eq(f"env {k.upper()}=true -> cfg.{k}", getattr(cfg, k), True)
        for k in KEYS:
            os.environ[k.upper()] = "false"
        cfg_off = load_config()
        for k in KEYS:
            eq(f"env {k.upper()}=false -> cfg.{k}", getattr(cfg_off, k), False)
        for k in KEYS:
            os.environ.pop(k.upper(), None)
        cfg_absent = load_config()
        for k in KEYS:
            eq(f"env absent -> cfg.{k} defaults off", getattr(cfg_absent, k), False)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ── 4. THE REAL ENGINE METHOD, on real files ─────────────────────────
    print("\n4. The real DownloadEngine._write_sidecars, on real files")
    from antra.core.engine import DownloadEngine, EngineConfig
    from antra.core.models import DownloadResult, DownloadStatus, TrackMetadata

    tmp = Path(tempfile.mkdtemp(prefix="antra_wiring_"))
    try:
        d = tmp / "After Hours (2020)"
        d.mkdir(parents=True)

        def result(i, status):
            p = d / f"1{i:02d} - Track {i}.flac"
            if status is not DownloadStatus.FAILED:
                p.write_bytes(b"\0" * 64)
            t = TrackMetadata(title=f"Track {i}", artists=["The Weeknd"],
                              album="After Hours", track_number=i, disc_number=1,
                              duration_ms=200000)
            return DownloadResult(track=t, status=status,
                                  file_path=str(p) if status is not DownloadStatus.FAILED else None)

        results = [result(1, DownloadStatus.COMPLETED),
                   result(2, DownloadStatus.SKIPPED),
                   result(3, DownloadStatus.COMPLETED)]

        class _E(DownloadEngine):
            def __init__(self, cfg):          # skip the real __init__'s deps
                self.cfg = cfg

        # 4a. all off -> byte-for-byte no-op
        _E(EngineConfig(output_format="flac"))._write_sidecars(results)
        eq("all toggles off writes nothing",
           sorted(p.suffix for p in d.iterdir()), [".flac"] * 3)

        # 4b. all on -> three files, and SKIPPED is counted
        _E(EngineConfig(output_format="lossless-24", sidecar_cue=True,
                        sidecar_nfo=True, sidecar_json=True))._write_sidecars(results)
        eq("all toggles on writes .cue/.json/.nfo",
           sorted(p.suffix for p in d.iterdir() if p.suffix != ".flac"),
           [".cue", ".json", ".nfo"])
        js = json.loads((d / "After Hours (2020).json").read_text(encoding="utf-8"))
        eq("a SKIPPED track is included (its file is really on disk)",
           js["release"]["track_count"], 3)
        eq("the requested format reaches the sidecar", js["requested_format"], "lossless-24")
        eq("the sidecar is named after the real folder",
           sorted(p.name for p in d.glob("*.nfo")), ["After Hours (2020).nfo"])

        # 4c. a FAILED track is never referenced
        cue = (d / "After Hours (2020).cue").read_text(encoding="utf-8")
        fail_only = [result(9, DownloadStatus.FAILED)]
        check("a FAILED result contributes no file_path", fail_only[0].file_path is None)
        _E(EngineConfig(output_format="flac", sidecar_cue=True))._write_sidecars(fail_only)
        eq("a run of only failures leaves the existing .cue untouched",
           (d / "After Hours (2020).cue").read_text(encoding="utf-8"), cue)

        # 4d. a single-track retry into that folder must not shrink it
        _E(EngineConfig(output_format="flac", sidecar_json=True))._write_sidecars([results[0]])
        eq("a 1-track retry leaves the 3-track .json alone",
           json.loads((d / "After Hours (2020).json").read_text(encoding="utf-8"))
           ["release"]["track_count"], 3)

        # 4e. a broken sidecar module must never fail a finished download
        import antra.utils.sidecars as _sc
        real = _sc.write_release_sidecars
        _sc.write_release_sidecars = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            _E(EngineConfig(output_format="flac", sidecar_nfo=True))._write_sidecars(results)
            check("a raising sidecar module is swallowed, not propagated", True)
        except Exception as e:                                    # pragma: no cover
            check("a raising sidecar module is swallowed, not propagated", False, repr(e))
        finally:
            _sc.write_release_sidecars = real
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ── 5. Regression: the organizer's playlist .m3u is untouched ─────────
    print("\n5. Regression — the existing playlist .m3u is left alone")
    check("write_playlist_manifest still exists in the organizer",
          "def write_playlist_manifest" in read("antra/utils/organizer.py"))
    check("download_playlist still calls it on both the cancel and success paths",
          eng_src.count("self.organizer.write_playlist_manifest(") == 2,
          str(eng_src.count("self.organizer.write_playlist_manifest(")))
    check("sidecars.py ships no m3u builder (the organizer owns that file)",
          "def build_m3u" not in read("antra/utils/sidecars.py"))
    # The sidecar call must be on the SUCCESS path only: a .cue describing 3 of
    # 20 tracks claims to describe the release; a partial .m3u does not.
    cancel_at = eng_src.index("EngineEventType.PLAYLIST_CANCELLED")
    sidecar_at = eng_src.index("self._write_sidecars(final)")
    complete_at = eng_src.index("EngineEventType.PLAYLIST_COMPLETED")
    check("the sidecar call sits after the cancel branch and before COMPLETED",
          cancel_at < sidecar_at < complete_at)
    eq("it is called exactly once", eng_src.count("self._write_sidecars(final)"), 1)

    print(f"\n{'PASS' if FAIL == 0 else 'FAIL'} — {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
