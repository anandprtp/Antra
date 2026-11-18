"""AudioTranscoder.convert must never lose the source if the swap fails.

Same-extension conversion (24-bit FLAC -> 16-bit FLAC) deleted the source before the
rename, so a failed rename left nothing. Real ffmpeg, real files.

Run:  python antra/utils/test_transcoder_safety.py
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from mutagen.flac import FLAC

from antra.utils import transcoder as tmod
from antra.utils.runtime import get_ffmpeg_exe
from antra.utils.transcoder import AudioTranscoder

FF = get_ffmpeg_exe()
assert FF, "ffmpeg is required"
FAIL = []


def check(label, ok):
    if not ok:
        FAIL.append(label)


def make24(d, name="t.flac"):
    p = os.path.join(d, name)
    subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anoisesrc=d=1:r=44100", "-ac", "2",
                    "-c:a", "flac", "-sample_fmt", "s32", p], check=True)
    return p


def depth(p):
    return FLAC(p).info.bits_per_sample


tc = AudioTranscoder()

# 1. control: the normal same-extension conversion works and replaces in place
d = tempfile.mkdtemp(prefix="tc")
src = make24(d)
check("control: source really is 24-bit", depth(src) == 24)
out = tc.convert(src, "lossless-16")
check("control: converted to 16-bit in place", out == src and depth(out) == 16)
check("control: no temp file left", sorted(os.listdir(d)) == ["t.flac"])

# 2. the bug: the final swap fails -> the original must survive untouched
d = tempfile.mkdtemp(prefix="tc")
src = make24(d)
before = open(src, "rb").read()
real_replace = os.replace


def fail_replace(*a, **k):
    raise PermissionError("locked by another process")


tmod.os.replace = fail_replace
try:
    try:
        tc.convert(src, "lossless-16")
        raised = False
    except PermissionError:
        raised = True
finally:
    tmod.os.replace = real_replace
check("a failed swap raises", raised)
check("the original file survives a failed swap", os.path.exists(src) and open(src, "rb").read() == before)
check("the temp file is cleaned up after a failed swap", sorted(os.listdir(d)) == ["t.flac"])

# 3. ffmpeg "succeeds" but writes nothing: never replace the source with an empty file
d = tempfile.mkdtemp(prefix="tc")
src = make24(d)
before = open(src, "rb").read()
real_run = tmod.subprocess.run


class OK:
    returncode, stdout, stderr = 0, "", ""


def empty_run(cmd, *a, **k):
    open(cmd[-1], "wb").close()              # the temp output, zero bytes
    return OK()


tmod.subprocess.run = empty_run
try:
    try:
        tc.convert(src, "lossless-16")
        raised = False
    except RuntimeError:
        raised = True
finally:
    tmod.subprocess.run = real_run
check("an empty ffmpeg output is rejected", raised)
check("the source survives an empty ffmpeg output", os.path.exists(src) and open(src, "rb").read() == before)

# 4. control: a different-extension conversion still removes the source after success
d = tempfile.mkdtemp(prefix="tc")
src = make24(d)
m4a = os.path.join(d, "t.m4a")
subprocess.run([FF, "-y", "-loglevel", "error", "-i", src, "-c:a", "alac", m4a], check=True)
os.remove(src)
out = tc.convert(m4a, "flac")
check("control: m4a -> flac converts and removes the m4a",
      out.endswith(".flac") and os.path.exists(out) and not os.path.exists(m4a))

print("FAIL:\n  " + "\n  ".join(FAIL) if FAIL else "OK")
sys.exit(1 if FAIL else 0)
