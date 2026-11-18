"""AUDIT-DESKTOP-8 — post_command.py: contract holes the DMX-6 suite did not cover.

  1. A template with an unbalanced quote raised ValueError out of _run (the
     split sat outside the try), failing a track that had already downloaded.
  2. On Windows subprocess.run() waits on the output pipes after killing a
     timed-out child, so a hook that leaves a background process holding stdout
     blocked the run (and the serialising lock) far beyond `timeout`.
  3. A .cmd/.bat hook is re-parsed by cmd.exe, so a title like "x & calc"
     became a second command: the "never a shell" guarantee did not hold.

Run:  python antra/utils/test_post_command_audit.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import antra.utils.post_command as pc  # noqa: E402

FAILS: list[str] = []


def check(label, ok, detail=""):
    if not ok:
        FAILS.append(label)
        print(f"  FAIL  {label} :: {detail}")


TMP = Path(tempfile.mkdtemp(prefix="pcaudit_"))
PY = sys.executable.replace("\\", "/")


def runner(command, timeout=30):
    cfg = pc.PostCommandConfig(enabled=True, confirmed=True, command=command, timeout=timeout)
    return pc.PostCommandRunner(cfg, library_root=str(TMP))


def track(title="T"):
    return SimpleNamespace(title=title, artist_string="A", album="B", album_artists=["A"], release_year=2020,
                           isrc="X", track_number=1, disc_number=1)


# 1. malformed template must be contained
for bad in ['notify "unclosed', "echo 'x"]:
    try:
        rc = runner(bad).run_for_track(file_path=str(TMP / "a.flac"), track=track())
        check(f"unbalanced quote contained ({bad!r})", rc is None, rc)
    except Exception as e:
        check(f"unbalanced quote contained ({bad!r})", False, f"{type(e).__name__}: {e}")
marker = TMP / "ok.txt"
rc = runner(f'"{PY}" -c "open(r\'{marker}\',\'w\').write(\'1\')"').run_for_track(file_path="a.flac", track=track())
check("control: a valid hook still runs", rc == 0 and marker.exists(), rc)

# 2. timeout must hold even when a background child keeps the pipe open
spawner = TMP / "spawn.py"
spawner.write_text(
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])\n"
    "time.sleep(60)\n")
t0 = time.time()
runner(f'"{PY}" "{spawner.as_posix()}"', timeout=2).run_for_track(file_path="a.flac", track=track())
took = time.time() - t0
check("timeout holds with a lingering grandchild", took < 8, f"{took:.1f}s")
# ...and the grandchild must be killed too, not left running after the timeout
gc_marker = TMP / "grandchild_survived.txt"
spawner2 = TMP / "spawn2.py"
spawner2.write_text(
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', "
    "'import time,sys; time.sleep(4); open(sys.argv[1],\"w\").write(\"1\")', sys.argv[1]])\n"
    "time.sleep(60)\n")
runner(f'"{PY}" "{spawner2.as_posix()}" "{gc_marker.as_posix()}"', timeout=1).run_for_track(file_path="a.flac", track=track())
time.sleep(6)
check("timeout kills the whole process tree", not gc_marker.exists(), "grandchild outlived the hook")
t0 = time.time()
runner(f'"{PY}" -c "import time; time.sleep(60)"', timeout=2).run_for_track(file_path="a.flac", track=track())
check("control: plain timeout still enforced", time.time() - t0 < 8, f"{time.time() - t0:.1f}s")

# 3. batch-file hook must not let a value become a second command
if os.name == "nt":
    pwn = TMP / "pwned.txt"
    cmdf = TMP / "hook.cmd"
    cmdf.write_text("@echo off\r\necho %1 > nul\r\n")
    r = runner(f'"{cmdf}" {{title}}')
    r.run_for_track(file_path="a.flac", track=track(f'x" & echo pwned> {pwn} & "'))
    check("cmd.exe cannot be injected through a title", not pwn.exists(), "second command ran")
    # control: a harmless title still reaches a .cmd hook
    out = TMP / "arg.txt"
    cmdf.write_text(f"@echo off\r\necho %~1> \"{out}\"\r\n")
    runner(f'"{cmdf}" {{title}}').run_for_track(file_path="a.flac", track=track("Plain Title"))
    check("control: harmless title still delivered to .cmd", out.exists() and "Plain Title" in out.read_text(), "")

print("FAILED:" if FAILS else "OK", *FAILS, sep="\n  " if FAILS else " ")
sys.exit(1 if FAILS else 0)
