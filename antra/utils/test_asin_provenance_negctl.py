"""Negative control for test_asin_provenance.py.

Puts each BUG-ASIN-1 fix back, one at a time, in place, and asserts the suite
notices. Without this, "all checks passed" could mean the checks never depended
on the change.

Sabotage 1 is the reported product-scraper bug verbatim, 6 and 7 are the
unverified-ASIN bug, 8 is the title-trust bypass, and 12 is the fix-by-disabling
cheat (refuse everything), which the suite's paired controls must reject.

Each run clears __pycache__ before invoking the suite: several mutations are the
same byte length as the original, so size+mtime caching would otherwise let
CPython import a .pyc compiled from source no longer on disk (the trap recorded
in CLAUDE.md under OPS-APL-4 / stale bytecode).

A pristine copy of every touched file is kept on disk, so an interrupted run
self-heals on the next start rather than leaving the tree sabotaged.

Run:  python antra/utils/test_asin_provenance_negctl.py
"""
import hashlib
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SUITE = os.path.join(ROOT, "antra", "utils", "test_asin_provenance.py")

TARGETS = {
    "odesli": os.path.join(ROOT, "antra", "sources", "odesli.py"),
    "amazon": os.path.join(ROOT, "antra", "sources", "amazon.py"),
    "matching": os.path.join(ROOT, "antra", "utils", "matching.py"),
}
PRISTINE_DIR = os.path.join(ROOT, "antra", "utils", ".negctl_pristine_asin")


def md5(path):
    with open(path, "rb") as fh:
        return hashlib.md5(fh.read()).hexdigest()


def self_heal():
    """Restore from a pristine copy left behind by an interrupted run."""
    if not os.path.isdir(PRISTINE_DIR):
        return
    for name, path in TARGETS.items():
        saved = os.path.join(PRISTINE_DIR, name + ".py")
        if os.path.isfile(saved) and md5(saved) != md5(path):
            shutil.copyfile(saved, path)
            print(f"  self-heal: restored {name}.py from a previous interrupted run")
    shutil.rmtree(PRISTINE_DIR, ignore_errors=True)


self_heal()
os.makedirs(PRISTINE_DIR, exist_ok=True)
ORIGINAL = {}
for _name, _path in TARGETS.items():
    with open(_path, "rb") as _fh:
        ORIGINAL[_name] = _fh.read()
    shutil.copyfile(_path, os.path.join(PRISTINE_DIR, _name + ".py"))
BASE_MD5 = {n: md5(p) for n, p in TARGETS.items()}


def write(name, data):
    with open(TARGETS[name], "wb") as fh:
        fh.write(data)


def restore():
    for name, data in ORIGINAL.items():
        write(name, data)


def drop_pycache():
    for sub in ("sources", "utils", "core"):
        d = os.path.join(ROOT, "antra", sub, "__pycache__")
        shutil.rmtree(d, ignore_errors=True)


def mutate(name, old, new):
    """Replace `old` with `new` in the file, tolerating CRLF/LF. Returns False if
    nothing changed, which the caller reports as a NO-OP rather than a pass."""
    text = ORIGINAL[name].decode("utf-8")
    for ending in ("\r\n", "\n"):
        o = old.replace("\n", ending)
        if o in text:
            write(name, text.replace(o, new.replace("\n", ending), 1).encode("utf-8"))
            return True
    return False


def run_suite():
    drop_pycache()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        p = subprocess.run([sys.executable, SUITE], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return "HUNG", ""
    return ("FAILED" if p.returncode != 0 else "PASSED"), (p.stdout + p.stderr)


SABOTAGES = []


def sabotage(label, name, old, new):
    SABOTAGES.append((label, name, old, new))


# 1. THE REPORTED BUG: substring title compare, artist ignored.
sabotage("1  product scraper: bare substring title compare, artist ignored",
         "odesli",
         "        score = max(\n"
         "            score_similarity(title, artists, product_title, \"\"),\n"
         "            score_similarity(title_clean, artists, product_title, \"\"),\n"
         "        )\n"
         "        if score < _AMAZON_PRODUCT_MIN_SCORE:\n"
         "            continue",
         "        tl, ptl = (title or \"\").lower(), product_title.lower()\n"
         "        score = 1.0 if (tl in ptl or ptl in tl) else 0.0\n"
         "        if score < 0.5:\n"
         "            continue")

# 2. THE REPORTED BUG: "first result wins" fallback restored.
sabotage("2  product scraper: unconditional first-result fallback restored",
         "odesli",
         "        if best is None or score > best[2]:\n"
         "            best = (asin, product_title, score)\n"
         "    return best",
         "        if best is None or score > best[2]:\n"
         "            best = (asin, product_title, score)\n"
         "    if best is None and pairs:\n"
         "        return (pairs[0][0], pairs[0][1], 0.0)\n"
         "    return best")

# 3. ISBN guard removed.
sabotage("3  product scraper: ISBN-shaped ASIN guard removed",
         "odesli",
         "        if not asin or _ISBN_ASIN_RE.match(asin):\n            continue",
         "        if not asin:\n            continue")

# 4. HTML entities no longer unescaped -> the good match breaks.
sabotage("4  product scraper: HTML entities no longer unescaped",
         "odesli",
         "        product_title = html.unescape((product_title or \"\").strip())",
         "        product_title = (product_title or \"\").strip()")

# 5. An empty formats list no longer means "not music".
sabotage("5  probe: empty formats no longer means 'not a music track'",
         "amazon",
         "        if not formats and not duration_ms:\n"
         "            return self._ASIN_NOT_MUSIC, None",
         "        if not formats and not duration_ms:\n"
         "            return self._ASIN_UNKNOWN, None")

# 6. THE REPORTED BUG: any music ASIN accepted, duration never compared.
sabotage("6  verify: duration no longer compared (any music ASIN accepted)",
         "amazon",
         "        if verdict == self._ASIN_MUSIC and probed_ms and track.duration_ms:\n"
         "            if duration_close(track.duration_ms / 1000.0, probed_ms / 1000.0, tolerance=8):\n"
         "                return 0.90, probed_ms",
         "        if verdict == self._ASIN_MUSIC and probed_ms and track.duration_ms:\n"
         "            if True:\n"
         "                return 0.90, probed_ms")

# 7. THE PRODUCTION BUG: verification skipped entirely.
# 7. THE PRODUCTION BUG: verification skipped entirely, the pre-fix behaviour.
#    NB an earlier version of this sabotage only replaced the NOT_MUSIC
#    early-return with `if False:`, which left the duration branch and the final
#    reject in place -- so nothing was actually trusted and the suite reported it
#    MISSED, correctly. The bypass has to happen before any check runs.
sabotage("7  verify: guessed ASINs trusted unconditionally at 0.90",
         "amazon",
         "        verdict, probed_ms = self._probe_asin_is_music(asin)\n"
         "        if verdict == self._ASIN_NOT_MUSIC:",
         "        return 0.90, None\n"
         "        verdict, probed_ms = self._probe_asin_is_music(asin)\n"
         "        if verdict == self._ASIN_NOT_MUSIC:")

# 8. THE REPORTED BUG: the title-trust bypass restored.
sabotage("8  matching: title_score >= 0.90 bypass of the wrong-artist cap restored",
         "matching",
         "    if best_artist_score < 0.45 and result_artist.strip():\n"
         "        return _capped(min(composite, WRONG_ARTIST_CAP))",
         "    if best_artist_score < 0.45 and result_artist.strip():\n"
         "        if title_score >= 0.90 and not mismatched_version:\n"
         "            pass\n"
         "        else:\n"
         "            return _capped(min(composite, WRONG_ARTIST_CAP))")

# 9. The cap raised back to the old "softer" tier, which is not below 0.55.
sabotage("9  matching: WRONG_ARTIST_CAP raised back to 0.55",
         "matching",
         "WRONG_ARTIST_CAP = 0.50",
         "WRONG_ARTIST_CAP = 0.55")

# 10. The fabricated character-ratio artist signal restored.
sabotage("10 matching: title_artist_score back to a raw character ratio",
         "matching",
         "    title_artist_score = 0.0\n"
         "    if result_title:\n"
         "        residual = normalize(result_title)",
         "    title_artist_score = max(\n"
         "        (string_similarity(a, result_title) for a in query_artists), default=0.0,\n"
         "    )\n"
         "    if False:\n"
         "        residual = normalize(result_title)")

# 11. Artist comparison back to a raw ratio -> the buried-artist controls must fail.
sabotage("11 matching: artist_match_score back to a raw joined-string ratio",
         "matching",
         "    artist_score = artist_match_score(query_artists, result_artist)",
         "    artist_score = max(\n"
         "        (string_similarity(a, result_artist) for a in query_artists), default=0.0,\n"
         "    )")

# 12. FIX-BY-DISABLING: refuse every product card. The paired controls must catch it.
sabotage("12 product scraper: refuses everything (fix-by-disabling cheat)",
         "odesli",
         "    best = None\n    for asin, product_title in pairs or []:",
         "    best = None\n    for asin, product_title in []:")

# 13. Whole-word boundary dropped -> 'Queen' matches 'Queens of the Stone Age'.
sabotage("13 matching: artist containment no longer on whole-word boundaries",
         "matching",
         "        if re.search(r\"(?:^|\\s)\" + re.escape(needle) + r\"(?:\\s|$)\", haystack):\n"
         "            return True",
         "        if needle in haystack:\n            return True")

# 14. FIX-BY-DISABLING: refuse every guessed ASIN. Paired controls must catch it.
sabotage("14 verify: refuses every guessed ASIN (fix-by-disabling cheat)",
         "amazon",
         "        verdict, probed_ms = self._probe_asin_is_music(asin)\n"
         "        if verdict == self._ASIN_NOT_MUSIC:",
         "        verdict, probed_ms = self._probe_asin_is_music(asin)\n"
         "        if True:")


# 15. The residual-title guard removed: containment tested against the whole
#     result title again, so an eponymous song satisfies it by itself.
sabotage("15 matching: artist-in-title tested against the full title, not the residual",
         "matching",
         "        if query_title_norm:\n"
         "            residual = residual.replace(query_title_norm, \" \")",
         "        if False:\n"
         "            residual = residual.replace(query_title_norm, \" \")")


# 16. The mirror-resolve path left unverified again (the gap the first fix missed).
sabotage("16 verify: MIRROR-resolved ASIN used unverified", "amazon",
         "                _mirror_verified = self._verify_guessed_asin(track, amazon_id, \"mirror resolve\")",
         "                _mirror_verified = (0.95, None)")


# -------------------------------------------------------------- baseline ----
# Interpret nothing until the suite passes against the UNSABOTAGED tree. A
# harness fault (wrong interpreter, bad path, import error) otherwise reports
# every sabotage as CAUGHT while testing nothing at all.
print("baseline (unsabotaged tree must PASS):")
restore()
status, out = run_suite()
if status != "PASSED":
    restore()
    shutil.rmtree(PRISTINE_DIR, ignore_errors=True)
    print(f"  FATAL: baseline is {status}, not PASSED - refusing to interpret results")
    print(out[-2500:])
    sys.exit(2)
print("  OK baseline passes\n")

caught, missed, noop = [], [], []
try:
    for label, name, old, new in SABOTAGES:
        restore()
        if not mutate(name, old, new):
            noop.append(label)
            print(f"  NO-OP  {label} - anchor not found, this sabotage tests NOTHING")
            continue
        status, out = run_suite()
        if status in ("FAILED", "HUNG"):
            caught.append(label)
            print(f"  OK     {status[:6]} {label}")
        else:
            missed.append(label)
            print(f"  MISSED {label}")
finally:
    restore()

ok = True
for name, path in TARGETS.items():
    same = md5(path) == BASE_MD5[name]
    print(f"{os.path.basename(path)} restored md5-identically: {same}")
    ok = ok and same
shutil.rmtree(PRISTINE_DIR, ignore_errors=True)
drop_pycache()

print(f"\n{len(caught)} caught, {len(missed)} missed, {len(noop)} no-op")
if missed or noop or not ok:
    sys.exit(1)
