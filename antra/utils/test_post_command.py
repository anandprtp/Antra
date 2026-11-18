"""
v2.0.0 DMX-6 — behavioural tests for the post-download command hook.

Everything here drives the REAL module and asserts on observed effect: which
argv actually reached subprocess, whether a process spawned at all, and — for
the injection cases — what a real child process received and what did NOT
appear on disk. Nothing asserts on source text.

Run:  python antra/utils/test_post_command.py
"""
from __future__ import annotations

import ast
import io
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import antra.utils.post_command as pc

# The repo root also holds other packages; make sure we loaded OUR module.
assert os.path.abspath(pc.__file__).startswith(os.path.abspath(ROOT)), pc.__file__

PASS = 0
FAIL = 0


def check(label, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  X {label}\n      got:  {got!r}\n      want: {want!r}")


def ok(label, cond):
    check(label, bool(cond), True)


# ── a fake subprocess.run that records every call ────────────────────────
@dataclass
class FakeProc:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class Recorder:
    def __init__(self, returncode=0, raises=None, stdout="", stderr=""):
        self.calls = []
        self.returncode = returncode
        self.raises = raises
        self.stdout = stdout
        self.stderr = stderr

    def __call__(self, argv, **kwargs):  # stands in for pc._execute
        self.calls.append((argv, kwargs))
        if self.raises is not None:
            raise self.raises
        return FakeProc(self.returncode, self.stdout, self.stderr)


def runner(command="", *, enabled=True, confirmed=True, mode="track",
           timeout=30, root=""):
    cfg = pc.PostCommandConfig(
        enabled=enabled, confirmed=confirmed, command=command,
        mode=mode, timeout=timeout,
    )
    return pc.PostCommandRunner(cfg, library_root=root)


class Track:
    """Minimal stand-in with the attributes the runner reads."""
    def __init__(self, title="Song", artists=("A",), album="Alb",
                 album_artists=None, year=2024, isrc="US1234567890",
                 track_number=3, disc_number=1):
        self.title = title
        self.artists = list(artists)
        self.album = album
        self.album_artists = list(album_artists or [])
        self.release_year = year
        self.isrc = isrc
        self.track_number = track_number
        self.disc_number = disc_number

    @property
    def artist_string(self):
        return ", ".join(self.artists)


def fire(r, rec, **kw):
    real = pc._execute
    pc._execute = rec
    try:
        return r.run_for_track(file_path=kw.pop("file_path", "/lib/A/x.flac"),
                               track=kw.pop("track", Track()), **kw)
    finally:
        pc._execute = real


def fire_batch(r, rec, **kw):
    real = pc._execute
    pc._execute = rec
    try:
        return r.run_for_batch(**kw)
    finally:
        pc._execute = real


# ═════════════════════════════════════════════════════════════════════════
print("\n1. INJECTION — a hostile value can never become syntax")
# ═════════════════════════════════════════════════════════════════════════
HOSTILE = '; rm -rf ~ && echo pwned > pwned.txt | cat "$(whoami)"'

rec = Recorder()
r = runner("mytool --title {title} --path {path}")
fire(r, rec, track=Track(title=HOSTILE), file_path="/lib/A/x.flac")
argv = rec.calls[0][0]
check("hostile title is exactly one argv element", argv,
      ["mytool", "--title", HOSTILE, "--path", "/lib/A/x.flac"])
check("argv length unchanged by the payload", len(argv), 5)

rec = Recorder()
r = runner("mytool {album}")
fire(r, rec, track=Track(album='AC/DC "Back" \'in\' `Black`'))
check("quotes and backticks in a value stay one element",
      rec.calls[0][0], ["mytool", 'AC/DC "Back" \'in\' `Black`'])

rec = Recorder()
r = runner("mytool {title}")
fire(r, rec, track=Track(title="a b c   d"))
check("whitespace inside a value does NOT split it",
      rec.calls[0][0], ["mytool", "a b c   d"])

rec = Recorder()
r = runner("mytool {title}")
fire(r, rec, track=Track(title="{path}"))
check("a value that looks like a token is not re-expanded",
      rec.calls[0][0], ["mytool", "{path}"])


# ═════════════════════════════════════════════════════════════════════════
print("\n2. shell=False, always")
# ═════════════════════════════════════════════════════════════════════════
rec = Recorder()
r = runner("tool {path}")
fire(r, rec)
kwargs = rec.calls[0][1]
check("shell is explicitly False", kwargs.get("shell"), False)
ok("argv passed as a list, never a string", isinstance(rec.calls[0][0], list))
check("timeout is passed through", kwargs.get("timeout"), 30)


# ═════════════════════════════════════════════════════════════════════════
print("\n3. GATES — nothing runs unless all three are satisfied")
# ═════════════════════════════════════════════════════════════════════════
for label, kw in [
    ("disabled",            dict(enabled=False, confirmed=True)),
    ("not confirmed",       dict(enabled=True, confirmed=False)),
    ("disabled AND unconfirmed", dict(enabled=False, confirmed=False)),
]:
    rec = Recorder()
    fire(runner("tool {path}", **kw), rec)
    check(f"{label}: zero processes spawned", len(rec.calls), 0)

rec = Recorder()
fire(runner("", enabled=True, confirmed=True), rec)
check("empty command: zero processes spawned", len(rec.calls), 0)

rec = Recorder()
fire(runner("   ", enabled=True, confirmed=True), rec)
check("whitespace-only command: zero processes spawned", len(rec.calls), 0)

rec = Recorder()
fire(runner("tool {path}"), rec)
check("all three gates satisfied: exactly one spawn", len(rec.calls), 1)

check("active property agrees with the gates",
      [runner("t", enabled=False).cfg.active,
       runner("t", confirmed=False).cfg.active,
       runner("").cfg.active,
       runner("t").cfg.active],
      [False, False, False, True])


# ═════════════════════════════════════════════════════════════════════════
print("\n4. MODE gating")
# ═════════════════════════════════════════════════════════════════════════
rec = Recorder()
fire(runner("tool {path}", mode="batch"), rec)
check("batch-mode hook does not fire per track", len(rec.calls), 0)

rec = Recorder()
fire_batch(runner("tool {library}", mode="track", root="/lib"),
           rec, downloaded=1, failed=0, total=1)
check("track-mode hook does not fire per batch", len(rec.calls), 0)

rec = Recorder()
fire_batch(runner("tool {library} {count} {failed} {total} {playlist}",
                  mode="batch", root="/lib"),
           rec, downloaded=8, failed=2, total=10, playlist_name="My Mix")
check("batch tokens render", rec.calls[0][0],
      ["tool", "/lib", "8", "2", "10", "My Mix"])

rec = Recorder()
fire_batch(runner("tool {count}", mode="batch"), rec,
           downloaded=0, failed=0, total=5)
check("zero downloads still renders 0, not dropped",
      rec.calls[0][0], ["tool", "0"])


# ═════════════════════════════════════════════════════════════════════════
print("\n5. TOKEN rendering")
# ═════════════════════════════════════════════════════════════════════════
rec = Recorder()
r = runner("tool {path} {filename} {folder} {title} {artist} {album} "
           "{album_artist} {year} {isrc} {track} {disc} {source} {format} {library}",
           root="/lib")
t = Track(title="T", artists=["A1", "A2"], album="Alb", album_artists=["AA"])
fire(r, rec, track=t, file_path=os.path.join("/lib", "A", "x.flac"),
     source="tidal_mirror", release_folder="/lib/A")
got = rec.calls[0][0]
check("path", got[1], os.path.join("/lib", "A", "x.flac"))
check("filename", got[2], "x.flac")
check("folder", got[3], os.path.dirname(os.path.join("/lib", "A", "x.flac")))
check("title", got[4], "T")
check("artist joins the list", got[5], "A1, A2")
check("album_artist prefers album_artists", got[7], "AA")
check("format is the extension without a dot", got[13], "flac")
check("library root", got[14], "/lib")

rec = Recorder()
fire(runner("tool {album_artist}"), rec,
     track=Track(artists=["A1", "A2"], album_artists=[]))
check("album_artist falls back to the track artists",
      rec.calls[0][0], ["tool", "A1, A2"])

rec = Recorder()
fire(runner("tool {isrc} after"), rec, track=Track(isrc=None))
check("a bare token resolving empty is DROPPED, not passed as ''",
      rec.calls[0][0], ["tool", "after"])

rec = Recorder()
fire(runner("tool --isrc={isrc} after"), rec, track=Track(isrc=None))
check("a token with literal text around it is KEPT",
      rec.calls[0][0], ["tool", "--isrc=", "after"])

rec = Recorder()
fire(runner("tool {nonsense} end"), rec)
check("an unknown token renders empty and is dropped, no crash",
      rec.calls[0][0], ["tool", "end"])

rec = Recorder()
fire(runner("tool {TITLE}"), rec, track=Track(title="T"))
check("tokens are lower-case only; {TITLE} is literal text",
      rec.calls[0][0], ["tool", "{TITLE}"])


# ═════════════════════════════════════════════════════════════════════════
print("\n6. TEMPLATE splitting")
# ═════════════════════════════════════════════════════════════════════════
check("quoted path stays one element",
      pc.split_template('"my tool" a b'), ["my tool", "a", "b"])
check("empty template gives no argv", pc.split_template(""), [])
check("whitespace template gives no argv", pc.split_template("   "), [])

if sys.platform == "win32":
    check("windows: backslashes in an unquoted path survive",
          pc.split_template(r"C:\tools\x.exe arg"), [r"C:\tools\x.exe", "arg"])
    check("windows: quoted path with spaces survives whole",
          pc.split_template(r'"C:\Program Files\x.exe" {path}'),
          [r"C:\Program Files\x.exe", "{path}"])
else:
    check("posix: normal shlex escaping applies",
          pc.split_template(r"tool a\ b"), ["tool", "a b"])


# ═════════════════════════════════════════════════════════════════════════
print("\n7. FAILURE containment — a hook can never fail a track")
# ═════════════════════════════════════════════════════════════════════════
for label, exc in [
    ("timeout",            subprocess.TimeoutExpired("t", 1)),
    ("command not found",  FileNotFoundError(2, "no such file")),
    ("OS error",           OSError(13, "denied")),
    ("unexpected error",   RuntimeError("boom")),
]:
    rec = Recorder(raises=exc)
    try:
        out = fire(runner("tool {path}"), rec)
        check(f"{label}: contained, returns None", out, None)
    except Exception as e:
        FAIL += 1
        print(f"  X {label}: escaped as {type(e).__name__}: {e}")

rec = Recorder(returncode=3, stderr="something went wrong")
out = fire(runner("tool {path}"), rec)
check("non-zero exit is reported, not raised", out, 3)


# ═════════════════════════════════════════════════════════════════════════
print("\n8. NORMALISATION")
# ═════════════════════════════════════════════════════════════════════════
check("unknown mode clamps to the default",
      [pc.normalize_command_mode(x) for x in ("track", "batch", "BATCH", "nope", "", None)],
      ["track", "batch", "batch", "track", "track", "track"])
check("timeout clamps into range",
      [pc.normalize_timeout(x) for x in (30, 0, -5, 99999, "45", None, "junk")],
      [30, pc.MIN_TIMEOUT, pc.MIN_TIMEOUT, pc.MAX_TIMEOUT, 45,
       pc.DEFAULT_TIMEOUT, pc.DEFAULT_TIMEOUT])
check("a config built with junk still has a usable timeout",
      pc.PostCommandConfig(timeout=0).timeout, pc.MIN_TIMEOUT)


# ═════════════════════════════════════════════════════════════════════════
print("\n9. SERIALISATION — parallel workers do not overlap hooks")
# ═════════════════════════════════════════════════════════════════════════
overlap = {"max": 0, "cur": 0}
lk = threading.Lock()


def slow(argv, **kw):
    with lk:
        overlap["cur"] += 1
        overlap["max"] = max(overlap["max"], overlap["cur"])
    time.sleep(0.05)
    with lk:
        overlap["cur"] -= 1
    return FakeProc(0)


r = runner("tool {path}")
real = pc._execute
pc._execute = slow
try:
    ts = [threading.Thread(target=r.run_for_track,
                           kwargs=dict(file_path="/lib/x.flac", track=Track()))
          for _ in range(6)]
    for t_ in ts:
        t_.start()
    for t_ in ts:
        t_.join()
finally:
    pc._execute = real
check("6 concurrent hooks never overlap", overlap["max"], 1)


# ═════════════════════════════════════════════════════════════════════════
print("\n10. REAL subprocess — a genuine child, with a hostile value")
# ═════════════════════════════════════════════════════════════════════════
tmp = tempfile.mkdtemp(prefix="antra_dmx6_")
try:
    sentinel = os.path.join(tmp, "got.txt")
    pwned = os.path.join(tmp, "pwned.txt")
    script = (
        "import sys,io;"
        "io.open(sys.argv[1],'w',encoding='utf-8').write(sys.argv[2])"
    )
    r = runner(
        '"%s" -c "%s" "%s" {title}' % (sys.executable, script, sentinel),
        root=tmp,
    )
    rc = r.run_for_track(file_path=os.path.join(tmp, "x.flac"),
                         track=Track(title=HOSTILE))
    check("real child exited 0", rc, 0)
    ok("real child actually ran (sentinel written)", os.path.exists(sentinel))
    if os.path.exists(sentinel):
        check("child received the hostile title VERBATIM as one argument",
              io.open(sentinel, encoding="utf-8").read(), HOSTILE)
    ok("no shell redirect happened — pwned.txt was NOT created",
       not os.path.exists(pwned))
    ok("no stray pwned.txt in the library root either",
       not os.path.exists(os.path.join(tmp, "pwned.txt")))

    # A disabled runner must not touch disk at all.
    s2 = os.path.join(tmp, "should_not_exist.txt")
    r2 = runner('"%s" -c "%s" "%s" x' % (sys.executable, script, s2),
                enabled=False, root=tmp)
    r2.run_for_track(file_path=os.path.join(tmp, "x.flac"), track=Track())
    ok("disabled runner spawned nothing (no file on disk)",
       not os.path.exists(s2))

    # Real timeout: the child must be terminated, not waited on forever.
    r3 = runner('"%s" -c "import time;time.sleep(30)"' % sys.executable,
                timeout=1, root=tmp)
    t0 = time.time()
    out = r3.run_for_track(file_path="/x.flac", track=Track())
    el = time.time() - t0
    check("timeout returns None rather than raising", out, None)
    ok("timeout actually terminated the child quickly (<10s)", el < 10)
finally:
    shutil.rmtree(tmp, ignore_errors=True)


# ═════════════════════════════════════════════════════════════════════════
print("\n11. ENGINE WIRING — fires on a NEW file only, and once per batch")
# ═════════════════════════════════════════════════════════════════════════
eng_src = io.open(os.path.join(ROOT, "antra", "core", "engine.py"),
                  encoding="utf-8").read()
tree = ast.parse(eng_src)


def calls_named(node, name):
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr == name:
            out.append(n)
    return out


check("run_for_track called exactly once in engine.py",
      len(calls_named(tree, "run_for_track")), 1)
check("run_for_batch called exactly once in engine.py",
      len(calls_named(tree, "run_for_batch")), 1)

fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
ok("run_for_track lives in download_track, not the impl",
   len(calls_named(fns["download_track"], "run_for_track")) == 1)
ok("run_for_batch lives in download_playlist",
   len(calls_named(fns["download_playlist"], "run_for_batch")) == 1)

# Two ordering properties, both load-bearing:
#
#  1. The hook is gated on status == COMPLETED, so a SKIPPED track (already in
#     the library, nothing written) never re-triggers a rescan.
#  2. It fires AFTER _finish_overwrite_stash. In overwrite mode the displaced
#     original sits beside the new file as .antra-overwrite-old until that call
#     runs — a hook that scans the folder would otherwise see the stray backup.
wrapper_src = ast.get_source_segment(eng_src, fns["download_track"]) or ""
ok("track hook is gated on DownloadStatus.COMPLETED",
   "result.status == DownloadStatus.COMPLETED and result.file_path" in wrapper_src)
ok("track hook runs AFTER _finish_overwrite_stash",
   wrapper_src.index("_finish_overwrite_stash(\n            stash, success=")
   < wrapper_src.index("run_for_track("))
ok("track hook runs BEFORE the wrapper returns",
   wrapper_src.index("run_for_track(") < wrapper_src.rindex("return result"))

# The hook must be built from config, not hardcoded.
ok("engine builds a PostCommandRunner", "PostCommandRunner(" in eng_src)
ok("service passes PostCommandConfig into EngineConfig",
   "post_command=PostCommandConfig(" in io.open(
       os.path.join(ROOT, "antra", "core", "service.py"), encoding="utf-8").read())


# ═════════════════════════════════════════════════════════════════════════
print("\n12. CONFIG CHAIN — env -> load_config -> build_engine")
# ═════════════════════════════════════════════════════════════════════════
saved = {k: os.environ.get(k) for k in (
    "POST_COMMAND_ENABLED", "POST_COMMAND_CONFIRMED", "POST_COMMAND",
    "POST_COMMAND_MODE", "POST_COMMAND_TIMEOUT")}
try:
    os.environ.update({
        "POST_COMMAND_ENABLED": "true",
        "POST_COMMAND_CONFIRMED": "true",
        "POST_COMMAND": "mytool {path}",
        "POST_COMMAND_MODE": "batch",
        "POST_COMMAND_TIMEOUT": "77",
    })
    from antra.core.config import load_config
    cfg = load_config()
    check("env -> cfg.post_command_enabled", cfg.post_command_enabled, True)
    check("env -> cfg.post_command_confirmed", cfg.post_command_confirmed, True)
    check("env -> cfg.post_command", cfg.post_command, "mytool {path}")
    check("env -> cfg.post_command_mode", cfg.post_command_mode, "batch")
    check("env -> cfg.post_command_timeout", cfg.post_command_timeout, 77)

    built = pc.PostCommandConfig(
        enabled=cfg.post_command_enabled,
        confirmed=cfg.post_command_confirmed,
        command=cfg.post_command,
        mode=cfg.post_command_mode,
        timeout=cfg.post_command_timeout,
    )
    check("the built config is active", built.active, True)
    check("the built config keeps the chosen mode", built.mode, "batch")

    # Defaults with NOTHING set — what every existing config.json produces.
    for k in saved:
        os.environ.pop(k, None)
    cfg2 = load_config()
    check("default: hook disabled", cfg2.post_command_enabled, False)
    check("default: not confirmed", cfg2.post_command_confirmed, False)
    check("default: no command", cfg2.post_command, "")
    check("default: inactive", pc.PostCommandConfig(
        enabled=cfg2.post_command_enabled,
        confirmed=cfg2.post_command_confirmed,
        command=cfg2.post_command).active, False)
finally:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ═════════════════════════════════════════════════════════════════════════
print("\n13. json_cli sets every var UNCONDITIONALLY (the BUG-23 trap)")
# ═════════════════════════════════════════════════════════════════════════
cli = io.open(os.path.join(ROOT, "antra", "json_cli.py"), encoding="utf-8").read()
for var in ("POST_COMMAND_ENABLED", "POST_COMMAND_CONFIRMED", "POST_COMMAND",
            "POST_COMMAND_MODE", "POST_COMMAND_TIMEOUT"):
    idx = cli.find('os.environ["%s"]' % var)
    ok("json_cli sets %s" % var, idx != -1)
    if idx != -1:
        # the assignment must not be guarded by `if "..." in settings`
        line_start = cli.rfind("\n", 0, idx)
        prev = cli[max(0, line_start - 200):line_start]
        ok("%s is not behind an `if key in settings` guard" % var,
           'in settings' not in prev.split("\n")[-1])


print("\n" + "=" * 62)
print(f"DMX-6 post-download command: {PASS} passed, {FAIL} failed")
print("=" * 62)
sys.exit(1 if FAIL else 0)
