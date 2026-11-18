"""Wiring test: config.json -> env -> Config -> EngineConfig -> engine check.

These settings cross five files that share nothing but a string key, so a typo
anywhere fails silently -- the download simply never gets verified. Each hop is
asserted here, ending with the real DownloadEngine method run against a real
downloaded file.

    python antra/utils/test_audio_verify_wiring.py MUSIC_ROOT
"""
from __future__ import annotations

import ast
import importlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import mutagen

PASS = FAIL = 0
_FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  PASS  {label}" + (f" -- {detail}" if detail else ""))
    else:
        FAIL += 1
        _FAILURES.append(label)
        print(f"  FAIL  {label}" + (f" -- {detail}" if detail else ""))


def with_env(**env):
    saved = {k: os.environ.get(k) for k in env}
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    import antra.core.config as cfg_mod
    importlib.reload(cfg_mod)
    config = cfg_mod.load_config()
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return config


def test_env_to_config() -> None:
    print("\n[1] env -> Config")
    on = with_env(VERIFY_AUDIO_ENABLED="true")
    off = with_env(VERIFY_AUDIO_ENABLED="false")
    absent = with_env(VERIFY_AUDIO_ENABLED=None)
    check("VERIFY_AUDIO_ENABLED=true enables it", on.verify_audio_enabled is True)
    check("VERIFY_AUDIO_ENABLED=false disables it", off.verify_audio_enabled is False)
    check("absent means ON (the default), not off",
          absent.verify_audio_enabled is True)

    check("threshold defaults to 0.35",
          abs(with_env(VERIFY_AUDIO_THRESHOLD=None).verify_audio_threshold - 0.35) < 1e-9)
    check("a custom threshold is honoured",
          abs(with_env(VERIFY_AUDIO_THRESHOLD="0.30").verify_audio_threshold - 0.30) < 1e-9)
    check("an absurdly low threshold is clamped (would fail good downloads)",
          with_env(VERIFY_AUDIO_THRESHOLD="0.01").verify_audio_threshold == 0.20)
    check("an absurdly high threshold is clamped (would never fire)",
          with_env(VERIFY_AUDIO_THRESHOLD="0.99").verify_audio_threshold == 0.45)
    check("an unparseable threshold falls back to the default",
          abs(with_env(VERIFY_AUDIO_THRESHOLD="banana").verify_audio_threshold - 0.35) < 1e-9)


def test_json_cli_sets_env() -> None:
    print("\n[2] config.json -> env (json_cli)")
    src = open(os.path.join(os.path.dirname(__file__), "..", "json_cli.py"),
               encoding="utf-8").read()
    check("json_cli maps verify_audio_enabled to the env var",
          'os.environ["VERIFY_AUDIO_ENABLED"] = _art_bool("verify_audio_enabled", True)' in src)
    # BUG-23: a conditional assignment lets a stale .env value win instead.
    tree = ast.parse(src)
    conditional = False
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "verify_audio_enabled" in ast.dump(node.test):
            conditional = True
    check("the assignment is unconditional (a stale .env cannot win)", not conditional)

    # The setting defaults to ON, so the Go field must be *bool: a plain bool
    # decodes an absent key as false and would silently disable the check for
    # every existing install (the documented PreferExplicit trap).
    go = open(os.path.join(os.path.dirname(__file__), "..", "..", "antra-wails",
                           "app_backend.go"), encoding="utf-8").read()
    check("Go config field is *bool so an absent key stays absent",
          'VerifyAudioEnabled *bool `json:"verify_audio_enabled,omitempty"`' in go)

    sv = open(os.path.join(os.path.dirname(__file__), "..", "..", "antra-wails",
                           "frontend", "src", "App.svelte"), encoding="utf-8").read()
    check("the UI treats an absent value as ON",
          "if (config.verify_audio_enabled === undefined || config.verify_audio_enabled === null) {"
          in sv)
    check("the UI default is ON", "verify_audio_enabled: true," in sv)


def test_service_to_engine(tracks) -> None:
    print("\n[3] Config -> EngineConfig -> engine")
    from antra.core.service import AntraService

    config = with_env(VERIFY_AUDIO_ENABLED="true", VERIFY_AUDIO_THRESHOLD="0.33")
    config.output_dir = os.path.dirname(tracks[0]["path"])
    engine = AntraService(config).build_engine(config)
    check("engine receives verify_audio=True", engine.cfg.verify_audio is True)
    check("engine receives the threshold",
          abs(engine.cfg.verify_audio_threshold - 0.33) < 1e-9,
          f"{engine.cfg.verify_audio_threshold}")

    off_cfg = with_env(VERIFY_AUDIO_ENABLED="false")
    off_cfg.output_dir = config.output_dir
    off_engine = AntraService(off_cfg).build_engine(off_cfg)
    check("engine receives verify_audio=False when disabled",
          off_engine.cfg.verify_audio is False)

    from antra.core.models import TrackMetadata
    from antra.utils.audio_verify import get_fpcalc_exe
    if not get_fpcalc_exe():
        print("  SKIP  engine verdicts -- fpcalc not available")
        return

    good, other = tracks[0], tracks[1]

    def meta(t):
        return TrackMetadata(title=t["title"], artists=[t["artist"]], album="",
                             isrc=t["isrc"], duration_ms=t["duration_ms"])

    problem = engine._audio_verification_problem(good["path"], meta(good))
    check("engine accepts a genuine download", problem is None, str(problem))

    problem = engine._audio_verification_problem(good["path"], meta(other))
    if problem is None:
        print("  SKIP  engine rejects wrong audio -- no preview for the impostor track")
    else:
        check("engine rejects a file containing a different recording", True, problem[:90])
        check("the rejection names the track and says it retries another source",
              other["title"][:12] in problem and "next source" in problem, problem[:90])


def test_engine_calls_it() -> None:
    print("\n[4] the check is actually called on the download path")
    src = open(os.path.join(os.path.dirname(__file__), "..", "core", "engine.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "_audio_verification_problem"]
    check("download_track calls the check exactly once", len(calls) == 1, f"{len(calls)} call(s)")
    check("it is guarded by the setting", "if self.cfg.verify_audio:" in src)
    # It must run after the file exists and before the track is accepted.
    idx_call = src.index("_audio_verification_problem(candidate_path")
    idx_accept = src.index("file_path = candidate_path")
    check("it runs before the download is accepted", idx_call < idx_accept)


def main() -> int:
    roots = sys.argv[1:] or [os.path.join(os.path.expanduser("~"), "Music")]
    tracks = []
    for root in roots:
        for dirpath, _, names in os.walk(root):
            for name in names:
                if name.lower().endswith((".flac", ".m4a", ".mp3")):
                    path = os.path.join(dirpath, name)
                    tags = mutagen.File(path, easy=True)
                    if not tags:
                        continue
                    tracks.append(dict(path=path, isrc=(tags.get("isrc") or [""])[0],
                                       title=(tags.get("title") or [""])[0],
                                       artist=(tags.get("artist") or [""])[0],
                                       duration_ms=int(tags.info.length * 1000)))
    if len(tracks) < 2:
        print("need at least 2 audio files -- pass a music root")
        return 2
    test_env_to_config()
    test_json_cli_sets_env()
    test_service_to_engine(tracks)
    test_engine_calls_it()
    print(f"\n{PASS} passed, {FAIL} failed")
    if _FAILURES:
        print("failed: " + ", ".join(_FAILURES))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
