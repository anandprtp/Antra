"""Negative control - prove test_deezer_album_pagination.py can fail.

Each sabotage is a way the Deezer album fix could be silently dead. Every one
must be CAUGHT, or the main suite is passing for free. Sabotages are applied
in-place to the real module and the file is restored byte-identically.

Run:  python antra/core/test_deezer_album_pagination_negctl.py
"""
import hashlib
import io
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

TARGET = os.path.join(HERE, "external_music_fetcher.py")
SUITE = os.path.join(HERE, "test_deezer_album_pagination.py")

ORIGINAL = io.open(TARGET, "rb").read()
ORIGINAL_MD5 = hashlib.md5(ORIGINAL).hexdigest()
CRLF = b"\r\n" in ORIGINAL

CAUGHT: list[str] = []
MISSED: list[str] = []


def suite_fails() -> bool:
    """Run the real suite in a fresh interpreter - an in-process rerun would
    reuse the already-imported, unsabotaged module."""
    proc = subprocess.run([sys.executable, SUITE], cwd=ROOT,
                          capture_output=True, text=True)
    return proc.returncode != 0


def sabotage(label: str, old: str, new: str) -> None:
    src = ORIGINAL.decode("utf-8").replace("\r\n", "\n")
    if src.count(old) != 1:
        MISSED.append(f"{label} (anchor matched {src.count(old)}x - NOT APPLIED)")
        return
    broken = src.replace(old, new)
    if CRLF:
        broken = broken.replace("\n", "\r\n")
    try:
        io.open(TARGET, "wb").write(broken.encode("utf-8"))
        (CAUGHT if suite_fails() else MISSED).append(label)
    finally:
        io.open(TARGET, "wb").write(ORIGINAL)


# 1. THE REPORTED BUG ITSELF: read the embedded list and never paginate.
sabotage(
    "pre-fix behaviour - the embedded 25-track list is used as-is",
    "            tracks = self._deezer_album_tracks(item_id, embedded)",
    "            tracks = embedded",
)

# 2. The helper exists but only ever fetches the first page.
sabotage(
    "the walk stops after one page",
    '                if not page.get("next"):\n                    break',
    "                break",
)

# 3. The index never advances - page 1 forever.
sabotage(
    "index does not advance between pages",
    "                index += len(batch)",
    "                index += 0",
)

# 4. The request shape changes silently. Correctness survives here, but an
#    unnoticed limit change is exactly how a 25-track cap could come back.
sabotage(
    "page limit silently reverts to Deezer's default of 25",
    "    _DEEZER_PAGE_LIMIT = 100",
    "    _DEEZER_PAGE_LIMIT = 25",
)

# 5. The fallback swallows a perfectly good walk.
sabotage(
    "the fallback discards a successful walk",
    "        if len(collected) >= len(embedded):\n            return collected",
    "        if False:\n            return collected",
)

# 6. The opposite: a failed walk is returned instead of the embedded list, so
#    a Deezer hiccup empties the album entirely.
sabotage(
    "a failed walk empties the album instead of falling back",
    "        if len(collected) >= len(embedded):\n            return collected\n"
    "        return embedded",
    "        return collected",
)

# 7. album_id is dropped, so paged tracks lose the album they belong to
#    (the paged items carry no nested album object to fall back on).
sabotage(
    "album_id is no longer passed to _deezer_track",
    "                    album_id=str(item_id),\n",
    "",
)

# 8. The ISRC the dedicated endpoint uniquely provides is thrown away.
sabotage(
    "ISRC is no longer read off the paged item",
    # anchored on the following line: the qobuz/tidal builders carry a
    # byte-identical isrc line of their own.
    '            isrc=item.get("isrc") or None,\n            deezer_track_id=',
    "            isrc=None,\n            deezer_track_id=",
)

# 9. The page cap is removed, so a server that always answers `next` loops
#    forever instead of being bounded.
sabotage(
    "the page cap is removed",
    "            for _ in range(self._DEEZER_MAX_PAGES):",
    "            for _ in range(500_000):",
)

restored = io.open(TARGET, "rb").read()
if hashlib.md5(restored).hexdigest() != ORIGINAL_MD5:
    MISSED.append("FILE NOT RESTORED - external_music_fetcher.py differs from its "
                  f"pre-run bytes (md5 {ORIGINAL_MD5})")

for line in MISSED:
    print("MISSED", line)
for line in CAUGHT:
    print("caught", line)
print(f"\n{len(CAUGHT)} caught, {len(MISSED)} missed")
sys.exit(1 if MISSED else 0)
