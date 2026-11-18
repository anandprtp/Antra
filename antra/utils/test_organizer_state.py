"""The dedup state file must survive parallel workers and a kill mid-write.

Run:  python antra/utils/test_organizer_state.py
"""
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from antra.core.models import TrackMetadata
from antra.utils import organizer as org_mod
from antra.utils.organizer import LibraryOrganizer, STATE_FILE

FAIL = []


def check(label, ok):
    if not ok:
        FAIL.append(label)


def make():
    root = tempfile.mkdtemp(prefix="st")
    return LibraryOrganizer(root, library_index_enabled=False), Path(root)


def track(i):
    return TrackMetadata(title=f"Song {i}", artists=["Band"], album="Al", album_artists=["Band"], isrc=f"USAB{i:08d}")


# 1. control: a normal save round-trips
o, root = make()
o.mark_failed(track(0), "no source")
saved = json.loads((root / STATE_FILE).read_text(encoding="utf-8"))
check("control: state written and parseable", any(k.startswith("FAILED:") for k in saved))
check("no .tmp left after a save", not (root / (STATE_FILE + ".tmp")).exists())

# 2. parallel workers: every save must leave a complete, parseable file; no lost entries
o, root = make()
errors = []


def worker(base):
    try:
        for i in range(base, base + 40):
            o.mark_failed(track(i), "x")
            data = (root / STATE_FILE).read_text(encoding="utf-8")
            json.loads(data)                      # a torn write would raise here
    except Exception as e:
        errors.append(repr(e))


threads = [threading.Thread(target=worker, args=(b * 100,)) for b in range(6)]
[t.start() for t in threads]
[t.join() for t in threads]
check("parallel saves never produced a torn/unparseable file: " + "; ".join(errors[:2]), not errors)
final = json.loads((root / STATE_FILE).read_text(encoding="utf-8"))
check("final state contains every worker's entries", len(final) >= 6 * 40 * 1)
check("no .tmp left after parallel saves", not (root / (STATE_FILE + ".tmp")).exists())

# 3. the failure mode of the old code: the live dict changing while it is serialised
o, root = make()
stop = threading.Event()


def mutator():
    # bounded: an unbounded insert loop makes every save serialise a huge dict
    n = 0
    while not stop.is_set():
        o._state[f"FAILED:k{n % 3000}"] = "r"
        o._state[f"FAILED:t{n}"] = "r"
        o._state.pop(f"FAILED:t{n}", None)
        n += 1


m = threading.Thread(target=mutator)
m.start()
logged = []
orig = org_mod.logger.warning
org_mod.logger.warning = lambda msg, *a, **k: logged.append(msg)
try:
    for _ in range(200):
        o._save_state()
finally:
    stop.set()
    m.join()
    org_mod.logger.warning = orig
check("saving while another thread mutates the state never fails", not logged)

# 4. a kill mid-write must not lose the previous state
o, root = make()
o.mark_failed(track(1), "first")
before = (root / STATE_FILE).read_text(encoding="utf-8")
real_replace = os.replace


def die(*a, **k):
    raise OSError("killed")


org_mod.os.replace = die
try:
    o.mark_failed(track(2), "second")
finally:
    org_mod.os.replace = real_replace
check("previous state intact when the swap fails", (root / STATE_FILE).read_text(encoding="utf-8") == before)

# 5. a corrupt state file is kept for diagnosis, not silently overwritten
root = Path(tempfile.mkdtemp(prefix="cr"))
(root / STATE_FILE).write_text('{"FAILED:isrc:X": "half', encoding="utf-8")
o2 = LibraryOrganizer(str(root), library_index_enabled=False)
check("corrupt state is preserved as .corrupt", (root / (STATE_FILE + ".corrupt")).exists())
check("organizer starts with an empty state", not o2._state)

print("FAIL:\n  " + "\n  ".join(FAIL) if FAIL else "OK")
sys.exit(1 if FAIL else 0)
