"""Negative control for test_quality_label.py: each sabotage puts one piece of the fix back to the
old behaviour and the suite must FAIL. Files are restored byte-for-byte (md5-checked) even on error.

Run:  python antra/utils/test_quality_label_negctl.py
"""
import hashlib
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEST = "antra/utils/test_quality_label.py"
ENGINE = "antra/core/engine.py"
TRANSCODER = "antra/utils/transcoder.py"

SABOTAGES = [
    ("label never capped by the source (the reported bug)", ENGINE,
     "if source_kbps and (not file_kbps or source_kbps < file_kbps):", "if False:"),
    ("cap picks the HIGHER of source and file", ENGINE,
     "source_kbps < file_kbps", "source_kbps > file_kbps"),
    ("completed label never receives the source bitrate", ENGINE,
     "result.quality_label,\n                    source_kbps=source_lossy_kbps,", "result.quality_label,\n                    source_kbps=None,"),
    ("a lossless source inherits a measured cap", ENGINE,
     "None if result.is_lossless\n                        else", "None if False\n                        else"),
    ("the adapter's claim beats the measured bitrate", ENGINE,
     "        if bitrate:\n            return int(round(int(bitrate) / 1000))", "        if False:\n            return int(round(int(bitrate) / 1000))"),
    ("an unreadable file invents a bitrate instead of using the claim", ENGINE,
     "return int(claimed) if claimed else None", "return int(claimed or 128)"),
    # -- the encode cap --
    ("engine never passes the source bitrate to the encoder", ENGINE,
     "max_source_kbps=source_lossy_kbps,", "max_source_kbps=None,"),
    ("MP3 is always encoded at 320", TRANSCODER,
     '"libmp3lame", "-b:a", f"{cls.cap_kbps(320, max_source_kbps)}k"]', '"libmp3lame", "-b:a", "320k"]'),
    ("AAC re-encode ignores the cap", TRANSCODER,
     '"aac", "-b:a", f"{cls.cap_kbps(320, max_source_kbps)}k"', '"aac", "-b:a", "320k"'),
    ("M4A re-encode ignores the cap", TRANSCODER,
     '"aac", "-b:a", f"{cls.cap_kbps(256, max_source_kbps)}k"', '"aac", "-b:a", "256k"'),
    ("cap lets the encode exceed the configured ceiling", TRANSCODER,
     "return min(configured_kbps, step)", "return step"),
    ("cap rounds to the step BELOW the source", TRANSCODER,
     "s >= source_kbps - 4", "s >= source_kbps + 999"),
    ("cap crashes on an unknown source bitrate", TRANSCODER,
     "if not source_kbps or source_kbps <= 0:", "if False:"),
]


def md5(b):
    return hashlib.md5(b).hexdigest()


def run():
    for root, _, _files in os.walk(os.path.join(ROOT, "antra")):
        if root.endswith("__pycache__"):
            shutil.rmtree(root, ignore_errors=True)  # same-length edits would reuse a stale .pyc
    try:
        return subprocess.run([sys.executable, os.path.join(ROOT, TEST)], cwd=ROOT, capture_output=True,
                              text=True, timeout=240).returncode
    except subprocess.TimeoutExpired:
        return 99


baseline = run()
if baseline != 0:
    print(f"REFUSING: the suite does not pass on the unsabotaged tree (exit {baseline})")
    sys.exit(2)

caught = missed = noop = 0
for label, rel, old, new in SABOTAGES:
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
        rc = run()
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
