"""Negative control for test_qobuz_track_id.py.

Sabotages the four files QZ-1 touches, one edit at a time, and requires the
suite to fail each time. Every file is restored and md5-compared at the end.

Sabotage 1 is the reported bug put back.

Run: python antra/utils/test_qobuz_track_id_negctl.py
"""
import hashlib
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SUITE = ROOT / "antra" / "utils" / "test_qobuz_track_id.py"

FILES = {
    "emf": ROOT / "antra" / "core" / "external_music_fetcher.py",
    "mirror": ROOT / "antra" / "sources" / "qobuz_mirror.py",
    "service": ROOT / "antra" / "core" / "service.py",
}
ORIGINAL = {k: p.read_bytes() for k, p in FILES.items()}
MD5 = {k: hashlib.md5(v).hexdigest() for k, v in ORIGINAL.items()}
TEXT = {k: v.decode("utf-8") for k, v in ORIGINAL.items()}

CRLF = chr(13) + chr(10)
LF = chr(10)


def joined(which, lines):
    """The anchor, spelled with the line ending that region actually uses."""
    for end in (CRLF, LF):
        candidate = end.join(lines)
        if TEXT[which].count(candidate) == 1:
            return candidate
    return None


SABOTAGES = [
    ("emf",
     "the reported bug put back: the album payload's track_id is discarded",
     ["                qobuz_track_id=str(t[\"track_id\"]) if t.get(\"track_id\") else None,"],
     ["                qobuz_track_id=None,"]),

    ("mirror",
     "the adapter ignores the id and searches anyway",
     ["        if qobuz_track_id:",
      "            return self._direct_result(track, str(qobuz_track_id))"],
     ["        if False and qobuz_track_id:",
      "            return self._direct_result(track, str(qobuz_track_id))"]),

    ("mirror",
     "_direct_result streams the wrong thing (title instead of the id)",
     ["            stream_id=str(track_id),",
      "            similarity_score=1.0,",
      "            isrc_match=bool(track.isrc),",
      "            is_explicit=track.is_explicit,"],
     ["            stream_id=str(track.title),",
      "            similarity_score=1.0,",
      "            isrc_match=bool(track.isrc),",
      "            is_explicit=track.is_explicit,"]),

    ("mirror",
     "_direct_result is fired unconditionally (no id needed)",
     ["        qobuz_track_id = getattr(track, \"qobuz_track_id\", None)"],
     ["        qobuz_track_id = getattr(track, \"qobuz_track_id\", None) or 'x'"]),

    ("service",
     "_preserve_track_identity drops the id through enrichment",
     ["            enriched.qobuz_track_id = original.qobuz_track_id or enriched.qobuz_track_id"],
     ["            pass  # sabotage"]),

]


def clear_pyc():
    for d in ("core", "sources", "utils"):
        for p in (ROOT / "antra" / d / "__pycache__").glob("*.pyc"):
            p.unlink(missing_ok=True)


caught = missed = 0
try:
    for i, (which, label, old, new) in enumerate(SABOTAGES, 1):
        anchor = joined(which, old)
        if anchor is None:
            print("  ? %d %s - anchor not unique, SKIPPED" % (i, label))
            missed += 1
            continue
        end = CRLF if CRLF in anchor else LF
        FILES[which].write_bytes(
            TEXT[which].replace(anchor, end.join(new)).encode("utf-8"))
        clear_pyc()
        r = subprocess.run([sys.executable, str(SUITE)], capture_output=True, text=True)
        FILES[which].write_bytes(ORIGINAL[which])
        clear_pyc()
        if r.returncode != 0:
            caught += 1
            print("  OK  %d CAUGHT - %s" % (i, label))
        else:
            missed += 1
            print("  x   %d MISSED - %s" % (i, label))
finally:
    for k, p in FILES.items():
        p.write_bytes(ORIGINAL[k])
    clear_pyc()

restored = all(hashlib.md5(p.read_bytes()).hexdigest() == MD5[k]
               for k, p in FILES.items())
print("\n%d caught, %d missed" % (caught, missed))
print("all files restored md5-identically:", restored)
sys.exit(0 if (missed == 0 and restored) else 1)
