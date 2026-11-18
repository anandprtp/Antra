"""json_cli._write_config_atomic: config.json must never be left truncated.

Run:  python antra/utils/test_config_atomic.py
"""
import json
import os
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from antra import json_cli

FAIL = []


def check(label, ok):
    if not ok:
        FAIL.append(label)


d = tempfile.mkdtemp(prefix="cfg")
p = os.path.join(d, "config.json")
original = {"antra_api_key": "ak_keepme", "n": 1}
with open(p, "w", encoding="utf-8") as f:
    json.dump(original, f)

# Control: a normal write replaces the content and leaves no temp file.
json_cli._write_config_atomic(p, {"antra_api_key": "ak_new", "é": "ü"}, ensure_ascii=False, indent=2)
check("content replaced", json.load(open(p, encoding="utf-8")) == {"antra_api_key": "ak_new", "é": "ü"})
check("no temp files left", os.listdir(d) == ["config.json"])

# The bug: a failure mid-write must leave the ORIGINAL intact (old code left it empty).
with open(p, "w", encoding="utf-8") as f:
    json.dump(original, f)


class Boom:
    pass


try:
    json_cli._write_config_atomic(p, {"x": Boom()})   # not JSON-serialisable -> dump raises mid-write
    check("serialisation error must propagate", False)
except TypeError:
    pass
check("original survives a failed write", json.load(open(p, encoding="utf-8")) == original)
check("failed write leaves no temp file", os.listdir(d) == ["config.json"])

# Failure at the final swap must also keep the original and clean up.
with mock.patch("os.replace", side_effect=OSError("denied")):
    try:
        json_cli._write_config_atomic(p, {"x": 1})
    except OSError:
        pass
check("original survives a failed replace", json.load(open(p, encoding="utf-8")) == original)
check("failed replace leaves no temp file", os.listdir(d) == ["config.json"])

# Callers actually use it (a helper nobody calls fixes nothing).
src = open(json_cli.__file__, encoding="utf-8").read()
check("no direct truncating config writes remain",
      'open(config_path, "w"' not in src)

if os.name != "nt":
    check("mode is owner-only", (os.stat(p).st_mode & 0o777) == 0o600)

print("FAIL:" if FAIL else "OK", *FAIL, sep="\n  ")
sys.exit(1 if FAIL else 0)
