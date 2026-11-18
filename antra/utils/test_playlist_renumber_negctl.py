"""
issue #75 negative control.

Each sabotage breaks ONE property of the shipped code in place, re-runs
test_playlist_renumber.py, and requires it to FAIL. Every file is restored
byte-identically (md5-checked) whatever happens.

The first sabotage is the reported bug itself: with the folder check removed,
ensure_playlist_copy goes back to asking only "is there a file at the stem the
current position renders?" and the suite must see the duplicate come back. A
suite that still passes there is not testing the bug.

Run: python antra/utils/test_playlist_renumber_negctl.py
"""
import hashlib
import io
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SUITE = ROOT / "antra" / "utils" / "test_playlist_renumber.py"

ORG = "antra/utils/organizer.py"
CLI = "antra/json_cli.py"
CFG = "antra/core/config.py"
ENG = "antra/core/engine.py"

# (label, file, find, replace)
SABOTAGES = [
    # ── THE REPORTED BUG ────────────────────────────────────────────────────
    #
    # The two guards are deliberately redundant for the reported case — a file
    # sitting in the playlist folder is caught by the cheap parent-directory
    # check AND by the identity scan — so removing either ALONE is not a
    # sabotage, and the control correctly reported the first attempt as MISSED.
    # This one removes the cheap check and neuters the scan, which is the shipped
    # bug exactly: the only question left is "is there a file at the stem the
    # CURRENT position renders?".
    ("the reported bug: only ask about the current position's stem", ORG,
     ("        if self._is_in_folder(canonical_path, folder):\n"
      "            return canonical_path\n",
      "        existing = self._playlist_copy_in_folder(track, folder)\n"),
     ("        if False:\n"
      "            return canonical_path\n",
      "        existing = None\n")),
    # The identity scan on its own is still load-bearing: it is the ONLY thing
    # that finds an old-numbered copy when the canonical file lives under its
    # album, which the cheap directory check cannot see.
    ("the identity scan alone (the album-filed case)", ORG,
     "        existing = self._playlist_copy_in_folder(track, folder)\n",
     "        existing = None\n"),

    # ── RENUMBERING ─────────────────────────────────────────────────────────
    ("renumber renames in ONE phase, so a uniform shift collides", ORG,
     "        renamed = self._two_phase_rename(folder_res, runnable)",
     "        renamed = self._one_phase_rename_SABOTAGE(folder_res, runnable)"),
    ("the toggle is ignored — renumbering always runs", ORG,
     "        if not playlist_name or not self.playlist_renumber or self.playlist_in_library_mode:\n"
     "            return {}\n"
     "        folder = self.playlist_folder_for(playlist_name)",
     "        if not playlist_name or self.playlist_in_library_mode:\n"
     "            return {}\n"
     "        folder = self.playlist_folder_for(playlist_name)"),
    ("renumber clobbers a destination it does not own", ORG,
     "            if not (\n"
     "                os.path.exists(extended_path(str(desired)))\n"
     "                and os.path.normcase(str(desired)) not in sources\n"
     "            )",
     "            if True"),
    ("the state is not repointed, so dedup loses every renamed file", ORG,
     "        if renamed:\n            self._repath(renamed)",
     "        if renamed:\n            pass"),

    # ── ORPHANS ─────────────────────────────────────────────────────────────
    ("orphan finder ignores the keep list (everything is an orphan)", ORG,
     "                if os.path.normcase(str(path.resolve())) in keep:\n                    continue\n",
     "                if False:\n                    continue\n"),
    ("orphan finder offers non-audio files too", ORG,
     "                if not path.is_file() or path.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:",
     "                if not path.is_file():"),
    ("pruning does not forget the deleted files", ORG,
     "        if removed:\n            self._forget_paths(removed)",
     "        if removed:\n            pass"),
    ("library storage mode still maintains a playlist folder", ORG,
     "        if not playlist_name or self.playlist_in_library_mode:\n"
     "            return []\n"
     "        folder = self.playlist_folder_for(playlist_name)\n"
     "        if not folder.is_dir():\n"
     "            return []\n"
     "        keep: set[str] = set()",
     "        if not playlist_name:\n"
     "            return []\n"
     "        folder = self.playlist_folder_for(playlist_name)\n"
     "        if not folder.is_dir():\n"
     "            return []\n"
     "        keep: set[str] = set()"),

    # ── MODE NORMALISATION ──────────────────────────────────────────────────
    ("an unknown prune mode is kept instead of failing safe to ask", ORG,
     '    return text if text in PRUNE_MODES else "ask"',
     "    return text"),

    # ── WIRING (the BUG-23 trap: the setting reaches Config but not the object
    #    that uses it, or a stale .env decides it) ────────────────────────────
    ("_make_organizer drops the renumber setting", CLI,
     '        playlist_renumber=getattr(cfg, "playlist_renumber", True),\n', ""),
    ("_make_organizer drops the prune setting", CLI,
     '        playlist_prune_orphans=getattr(cfg, "playlist_prune_orphans", "ask"),\n', ""),
    ("load_config ignores PLAYLIST_PRUNE_ORPHANS", CFG,
     '        playlist_prune_orphans=os.getenv("PLAYLIST_PRUNE_ORPHANS", "ask"),',
     '        playlist_prune_orphans="ask",'),
    ("load_config ignores PLAYLIST_RENUMBER", CFG,
     '        playlist_renumber=os.getenv("PLAYLIST_RENUMBER", "true").lower() == "true",',
     "        playlist_renumber=True,"),
    ("json_cli writes the renumber default with `or`, losing a chosen False", CLI,
     '            os.environ["PLAYLIST_RENUMBER"] = "true" if (_renumber is None or _renumber) else "false"',
     '            os.environ["PLAYLIST_RENUMBER"] = "true" if (settings.get("playlist_renumber") or True) else "false"'),

    # ── ENGINE ORCHESTRATION ────────────────────────────────────────────────
    # The destructive one: a partial run (a PV-1 selection, an auto-sync) would
    # offer every track it did not cover as a leftover to delete.
    ("the complete-playlist gate on orphans is removed", ENG,
     "        if not complete_playlist:\n            return\n"
     '        mode = getattr(self.organizer, "playlist_prune_orphans", "ask")',
     "        if False:\n            return\n"
     '        mode = getattr(self.organizer, "playlist_prune_orphans", "ask")'),
    ("the complete-playlist gate on renumbering is removed", ENG,
     "        if not complete_playlist:\n            return\n"
     "        entries = [(r.track, r.file_path) for r in results if r.file_path and r.track]",
     "        if False:\n            return\n"
     "        entries = [(r.track, r.file_path) for r in results if r.file_path and r.track]"),
    ('the "off" mode still reports', ENG,
     '        if mode == "off":\n            return\n', ""),
    ('"ask" deletes as well as reports', ENG,
     '        if mode == "auto":\n            removed = self.organizer.prune_playlist_orphans(orphans)',
     '        if mode in ("auto", "ask"):\n            removed = self.organizer.prune_playlist_orphans(orphans)'),
    ("renamed results are not repointed, so the manifest names dead paths", ENG,
     "            new = lookup.get(os.path.normcase(str(r.file_path)))\n            if new:\n                r.file_path = new",
     "            new = lookup.get(os.path.normcase(str(r.file_path)))\n            if False:\n                r.file_path = new"),
]

# A helper the one-phase sabotage needs. Injected with the sabotage and removed
# with it, so the shipped file never carries it.
ONE_PHASE = '''
    def _one_phase_rename_SABOTAGE(self, folder, moves):
        out = {}
        for current, desired in moves:
            try:
                os.replace(extended_path(str(current)), extended_path(str(desired)))
            except OSError:
                continue
            out[str(current)] = str(desired)
        return out
'''


def md5(path: pathlib.Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8", newline="").read()


def nl_of(src: str) -> str:
    """The file's own line ending.

    The anchors below are written with plain \\n, but these files are CRLF in
    this tree (and LF in the website's copy). Without translating, every anchor
    reports "found 0x" and the whole control passes vacuously as "refusing" —
    which is exactly the dead-control failure mode this file exists to avoid.
    """
    return "\r\n" if "\r\n" in src else "\n"


def localise(text: str, nl: str) -> str:
    return text.replace("\n", nl) if nl != "\n" else text


def write(rel: str, text: str) -> None:
    io.open(ROOT / rel, "w", encoding="utf-8", newline="").write(text)


def suite_passes() -> bool:
    return subprocess.run([sys.executable, str(SUITE)], cwd=str(ROOT),
                          capture_output=True).returncode == 0


FILES = sorted({rel for _, rel, _, _ in SABOTAGES})
originals = {rel: (ROOT / rel).read_bytes() for rel in FILES}
before = {rel: md5(ROOT / rel) for rel in FILES}

if not suite_passes():
    print("FATAL: the suite is already failing — fix that before running the control.")
    sys.exit(2)

caught = 0
missed = []
try:
    for label, rel, find, replace in SABOTAGES:
        src = read(rel)
        nl = nl_of(src)
        # A sabotage may need SEVERAL edits at once, when one alone is covered by
        # a redundant guard and so is not a behaviour change at all.
        finds = find if isinstance(find, tuple) else (find,)
        replaces = replace if isinstance(replace, tuple) else (replace,)
        broken = src
        bad = [f for f in finds if broken.count(localise(f, nl)) != 1]
        if bad:
            print(f"  ANCHOR  {label}: {len(bad)} of {len(finds)} anchors not unique — refusing")
            missed.append(label)
            continue
        for f, r in zip(finds, replaces):
            broken = broken.replace(localise(f, nl), localise(r, nl))
        if "_one_phase_rename_SABOTAGE" in "".join(replaces):
            # Give the sabotage the method it calls, or the failure would be an
            # AttributeError rather than the collision it is meant to prove.
            hook = localise("    def _repath(self, renamed: dict[str, str]) -> None:", nl)
            broken = broken.replace(hook, localise(ONE_PHASE + "\n", nl) + hook)
        write(rel, broken)
        if suite_passes():
            print(f"  MISSED  {label}")
            missed.append(label)
        else:
            print(f"  caught  {label}")
            caught += 1
        write(rel, originals[rel].decode("utf-8"))
finally:
    for rel in FILES:
        (ROOT / rel).write_bytes(originals[rel])

restored = all(md5(ROOT / rel) == before[rel] for rel in FILES)
print(f"\n{caught} caught, {len(missed)} missed, of {len(SABOTAGES)}")
print(f"restored byte-identically: {restored}")
print(f"suite green after restore: {suite_passes()}")
sys.exit(0 if (caught == len(SABOTAGES) and restored) else 1)
