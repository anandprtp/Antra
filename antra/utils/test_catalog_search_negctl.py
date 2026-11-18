"""Negative control for test_catalog_search.py (issue #58).

A passing suite proves nothing unless it can fail. Each sabotage below is
applied IN PLACE to the real source file, the suite is re-run, and the file is
restored and md5-checked. Sabotage 1 is the reported bug put back: a song search
that hands back the album's URL instead of the track's, so clicking one song
downloads the whole album.

Run:  python antra/utils/test_catalog_search_negctl.py
"""
import hashlib
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SUITE = os.path.join(ROOT, "antra", "utils", "test_catalog_search.py")

APPLE = os.path.join(ROOT, "antra", "core", "apple_fetcher.py")
MATCH = os.path.join(ROOT, "antra", "utils", "matching.py")
SPOT = os.path.join(ROOT, "antra", "core", "spotify.py")
SERVICE = os.path.join(ROOT, "antra", "core", "service.py")

# (label, file, old, new)
SABOTAGES = [
    # The first draft of this sabotage swapped trackViewUrl for collectionViewUrl
    # and was correctly reported MISSED: measured on 4 live queries, iTunes
    # returns the two IDENTICAL for entity=song, so it changed no behaviour at
    # all. Dropping the query string is the real failure mode - it turns the
    # song's URL into its album's, which is the whole bug.
    ("1. THE REPORTED BUG: the ?i= is lost, so a song URL becomes its album's", APPLE,
     '            url = _clean_itunes_url(s.get("trackViewUrl") or "")',
     '            url = _clean_itunes_url((s.get("trackViewUrl") or "").split("?")[0])'),

    ("2. the uo= tracking parameter is left on the queued URL", APPLE,
     '    for sep in ("&uo=", "?uo="):',
     '    for sep in ():'),

    ("3. artwork is left at the 100x100 thumbnail", APPLE,
     '    return re.sub(r"/\\d+x\\d+bb\\.jpg$", f"/{size}x{size}bb.jpg", url)',
     '    return url'),

    ("4. rows with no usable URL are kept instead of dropped", APPLE,
     '            if not title or not url:\n                continue\n            artist = s.get("artistName") or ""',
     '            artist = s.get("artistName") or ""'),

    ("5. results are re-sorted locally, losing iTunes' relevance order", APPLE,
     '                "source": "apple",\n            })\n        return out\n\n    def search_albums',
     '                "source": "apple",\n            })\n        out.sort(key=lambda r: r["match_score"])\n        return out\n\n    def search_albums'),

    ("6. the limit is passed through unclamped", APPLE,
     '        limit = max(1, min(int(limit or 25), 200))',
     '        limit = int(limit or 25)'),

    ("7. scoring uses the title alone, not the best of title / artist+title", MATCH,
     '    if artist:\n        best = max(best, string_similarity(q, normalize(f"{artist} {title}")))',
     '    if artist:\n        pass'),

    ("8. a Spotify row with no id is kept, producing a URL to nowhere", SPOT,
     '        if not name or not track_id:\n            return None',
     '        if not name:\n            return None'),

    ("9. an unknown search kind reaches argparse instead of becoming 'song'", SERVICE,
     '        if kind not in ("song", "album"):\n            kind = "song"',
     '        if kind not in ("song", "album"):\n            pass'),

    ("10. the artist kind no longer routes to the artist search", SERVICE,
     '        if kind == "artist":\n            return self.search_artists(query, source=source)',
     '        if kind == "artist":\n            kind = "song"'),

    ("11. Spotify rows are left unscored, so every badge reads 0%", SERVICE,
     '            if not r.get("match_score"):',
     '            if False:'),

    ("12. a Spotify failure returns nothing at all instead of Apple rows", SERVICE,
     '        if not rows:\n            # Apple is the fallback',
     '        if False:\n            # Apple is the fallback'),
]


def read(p):
    with open(p, "rb") as f:
        return f.read()


def md5(p):
    return hashlib.md5(read(p)).hexdigest()


def run_suite():
    r = subprocess.run([sys.executable, SUITE], capture_output=True, text=True, cwd=ROOT)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main():
    code, out = run_suite()
    if code != 0:
        print("REFUSING: the suite does not pass before any sabotage.\n" + out)
        return 1
    print("baseline: suite passes\n")

    originals = {p: read(p) for p in {s[1] for s in SABOTAGES}}
    before = {p: md5(p) for p in originals}

    caught = 0
    for label, path, old, new in SABOTAGES:
        raw = originals[path]
        text = raw.decode("utf-8")
        nl = "\r\n" if b"\r\n" in raw else "\n"
        o = old.replace("\n", nl)
        n = new.replace("\n", nl)
        if text.count(o) != 1:
            print(f"  ?? {label}\n     ANCHOR NOT FOUND ({text.count(o)} matches) - sabotage not applied")
            continue
        with open(path, "wb") as f:
            f.write(text.replace(o, n, 1).encode("utf-8"))
        try:
            code, out = run_suite()
        finally:
            with open(path, "wb") as f:
                f.write(raw)
        if code != 0:
            caught += 1
            first = next((l for l in out.splitlines() if l.strip().startswith("- ")), "")
            print(f"  OK   {label}\n       {first.strip()}")
        else:
            print(f"  MISS {label}  <-- the suite did NOT catch this")

    ok = True
    for p in originals:
        if md5(p) != before[p]:
            print(f"!! {p} was NOT restored byte-identically")
            ok = False
    print(f"\nall files restored byte-identically: {ok}")
    print(f"{caught} of {len(SABOTAGES)} sabotages caught")

    code, _ = run_suite()
    print(f"suite passes again after restore: {code == 0}")
    return 0 if (caught == len(SABOTAGES) and ok and code == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
