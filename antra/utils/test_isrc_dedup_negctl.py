"""Negative control for test_isrc_dedup.py.

Sabotages antra/utils/organizer.py IN PLACE, one edit at a time, and requires
the suite to fail each time. A suite that cannot fail proves nothing. The file
is restored after every sabotage and the md5 is compared at the end, so a
crashed run cannot leave a sabotaged organizer behind.

Sabotage 1 is the reported bug itself put back.

Run: python antra/utils/test_isrc_dedup_negctl.py
"""
import hashlib
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
TARGET = ROOT / "antra" / "utils" / "organizer.py"
SUITE = ROOT / "antra" / "utils" / "test_isrc_dedup.py"

ORIGINAL = TARGET.read_bytes()
ORIGINAL_MD5 = hashlib.md5(ORIGINAL).hexdigest()
TEXT = ORIGINAL.decode("utf-8")

# Anchors are matched against the file's own line endings, which are CRLF here
# and mixed in the web tree — never hard-code "\n" into an anchor.
NL = "\r\n" if "\r\n" in TEXT else "\n"


def lines(*items):
    return NL.join(items)


SABOTAGES = [
    # 1 — the reported bug: no veto at all in is_already_downloaded.
    ("the reported bug put back (no veto on an index hit)",
     lines("                    if self._is_positively_different_recording(track, existing):",
           "                        logger.debug("),
     lines("                    if False and self._is_positively_different_recording(track, existing):",
           "                        logger.debug(")),

    # 2 — veto fires whenever an ISRC is merely present (over-eager).
    ("_isrcs_disagree ignores equality (everything looks different)",
     "        return bool(ours and other and ours != other.strip().upper())",
     "        return bool(ours and other)"),

    # 3 — case sensitivity: tags come back upper, our metadata sometimes lower.
    ("_isrcs_disagree stops normalising case",
     "        ours = (track.isrc or '').strip().upper()",
     "        ours = (track.isrc or '').strip()"),

    # 4 — the in-run half: _mark_done forgets the ISRC, so the veto has no
    #     memory and must fall back to reading a file that may be untagged.
    ("_mark_done no longer remembers the ISRC",
     "        self._remember_path_isrc(resolved, track.isrc)",
     "        pass  # sabotage"),

    # 5 — the file-side half. The organizer learns a file's ISRC from FOUR
    #     independent places (in-run memory, .antra_state, the library scan,
    #     and reading the file), so removing any ONE is invisible — the first
    #     draft removed only the file read and was correctly reported MISSED.
    #     This removes both file-side sources at once, which is what a careless
    #     refactor of the lookup would do, and section 5c catches it.
    ("the file-side ISRC sources are both removed (scan + direct read)",
     [(lines("            self._remember_path_isrc(",
             "                resolved, self._isrc_from_identity_keys(identity_keys)",
             "            )",
             "",
             "        if index is not None:"),
       lines("            pass  # sabotage",
             "",
             "        if index is not None:")),
      (lines("            found = self._isrc_from_identity_keys(",
             "                self._extract_identity_keys_from_file(Path(path))",
             "            )"),
       lines("            found = None",
             "            _unused = (",
             "                None)"))],
     None),

    # 6 — state-backed knowledge dropped.
    ("_build_identity_index stops recording state ISRCs",
     lines("                if key.startswith('isrc:'):",
           "                    self._remember_path_isrc(path, key[len('isrc:'):])"),
     lines("                if False:",
           "                    self._remember_path_isrc(path, key[len('isrc:'):])")),

    # 7 — the key parser returns the raw key, so nothing ever compares equal.
    ("_isrc_from_identity_keys returns the key, not the ISRC",
     "                return key[len('isrc:'):].strip().upper() or None",
     "                return key.strip().upper() or None"),
]

caught = missed = 0
try:
    for i, (label, old, new) in enumerate(SABOTAGES, 1):
        # A sabotage is one (old, new) pair, or a list of them when no single
        # edit is observable on its own (see #5).
        edits = old if isinstance(old, list) else [(old, new)]
        counts = [TEXT.count(o) for o, _ in edits]
        if counts != [1] * len(edits):
            print("  ? %d %s — anchors found %r, SKIPPED (fix the anchor)" % (i, label, counts))
            missed += 1
            continue
        sabotaged = TEXT
        for o, nw in edits:
            sabotaged = sabotaged.replace(o, nw)
        TARGET.write_bytes(sabotaged.encode("utf-8"))
        for p in (ROOT / "antra" / "utils" / "__pycache__").glob("organizer.*"):
            p.unlink(missing_ok=True)   # same-length edits are invisible to mtime+size
        r = subprocess.run([sys.executable, str(SUITE)], capture_output=True, text=True)
        TARGET.write_bytes(ORIGINAL)
        for p in (ROOT / "antra" / "utils" / "__pycache__").glob("organizer.*"):
            p.unlink(missing_ok=True)
        if r.returncode != 0:
            caught += 1
            print("  OK  %d CAUGHT — %s" % (i, label))
        else:
            missed += 1
            print("  x   %d MISSED — %s" % (i, label))
finally:
    TARGET.write_bytes(ORIGINAL)
    for p in (ROOT / "antra" / "utils" / "__pycache__").glob("organizer.*"):
        p.unlink(missing_ok=True)

restored = hashlib.md5(TARGET.read_bytes()).hexdigest()
print("\n%d caught, %d missed" % (caught, missed))
print("organizer.py restored md5-identically:", restored == ORIGINAL_MD5)
sys.exit(0 if (missed == 0 and restored == ORIGINAL_MD5) else 1)
