"""Negative control for test_row_identity.py (issue #74).

A suite that passes proves nothing until it can be made to fail. Each sabotage
below is applied IN PLACE to the real source file, the real suite is run in a
subprocess, and the sabotage is required to make it fail. Every file is
restored afterwards and the restore is md5-verified.

Sabotage 1 is the reported bug itself, put back.

Run: python antra/core/test_row_identity_negctl.py
"""
import hashlib
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SUITE = os.path.join(ROOT, "antra", "core", "test_row_identity.py")
SERVICE = os.path.join(ROOT, "antra", "core", "service.py")
JSON_CLI = os.path.join(ROOT, "antra", "json_cli.py")


def eol(path):
    """Anchors must match the file's own line endings.

    service.py is CRLF and json_cli.py is LF; a previous suite in this repo
    reported every anchor as "found 0x" for exactly this reason.
    """
    data = open(path, "rb").read()
    return b"\r\n" if data.count(b"\r\n") else b"\n"


def enc(text, path):
    return text.replace("\n", eol(path).decode()).encode("utf-8")


# (label, file, find, replace)
SABOTAGES = [
    (
        "the reported bug: playlist order is not restored",
        SERVICE,
        '        if any((track.request_kind or "").lower() == "playlist" for track in tracks):\n'
        "            reordered_tracks.sort(key=lambda track: input_order[id(track)])\n",
        "",
    ),
    (
        "the fix over-reaches: album sequencing is discarded too",
        SERVICE,
        '        if any((track.request_kind or "").lower() == "playlist" for track in tracks):\n',
        "        if True:\n",
    ),
    (
        "row_id is not carried through _preserve_track_identity",
        SERVICE,
        "            enriched.row_id = original.row_id or enriched.row_id\n",
        "",
    ),
    (
        "every track is stamped with the same id",
        JSON_CLI,
        '                track.row_id = f"{_ROW_ID_PREFIX}-{next(_row_id_counter)}"\n',
        '                track.row_id = f"{_ROW_ID_PREFIX}-1"\n',
    ),
    (
        "stamping overwrites ids instead of being idempotent",
        JSON_CLI,
        '            if not getattr(track, "row_id", None):\n',
        "            if True:\n",
    ),
    (
        "damaged identity is not detected at all",
        JSON_CLI,
        "    rows = list(tracks or [])\n    if not rows:\n        return\n",
        "    rows = list(tracks or [])\n    if rows:\n        return\n",
    ),
    (
        "only the damaged ids are cleared, leaving a half-keyed job",
        JSON_CLI,
        "    for track in rows:\n        try:\n            track.row_id = None\n",
        "    for track in [r for r in rows if not getattr(r, 'row_id', None)]:\n"
        "        try:\n            track.row_id = None\n",
    ),
    (
        "the event envelope drops the row identity",
        JSON_CLI,
        '            "row_id": getattr(event.track, "row_id", None) if event.track else None,\n',
        '            "row_id": None,\n',
    ),
]


def run_suite():
    proc = subprocess.run([sys.executable, SUITE], cwd=ROOT,
                          capture_output=True, text=True)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main():
    code, out = run_suite()
    if code != 0:
        print("REFUSING TO RUN: the suite already fails before any sabotage.")
        print(out)
        return 1
    print("baseline: suite passes\n")

    originals = {p: open(p, "rb").read() for p in {SERVICE, JSON_CLI}}
    digests = {p: hashlib.md5(b).hexdigest() for p, b in originals.items()}

    caught = 0
    try:
        for label, path, find, repl in SABOTAGES:
            needle = enc(find, path)
            body = originals[path]
            if body.count(needle) != 1:
                print(f"  ANCHOR MISSING ({body.count(needle)}x): {label}")
                continue
            open(path, "wb").write(body.replace(needle, enc(repl, path)))
            code, out = run_suite()
            if code != 0:
                caught += 1
                print(f"  caught: {label}")
            else:
                print(f"  MISSED: {label}")
            open(path, "wb").write(body)
    finally:
        for path, body in originals.items():
            open(path, "wb").write(body)

    for path, want in digests.items():
        got = hashlib.md5(open(path, "rb").read()).hexdigest()
        print(f"restored {'OK' if got == want else 'CORRUPT'}: {os.path.basename(path)}")
        if got != want:
            return 1

    print(f"\n{caught}/{len(SABOTAGES)} sabotages caught")
    return 0 if caught == len(SABOTAGES) else 1


if __name__ == "__main__":
    sys.exit(main())
