"""slskd_onboarding.generate_config must round-trip any username/password through YAML.

Run:  python antra/utils/test_slskd_config.py   (needs PyYAML; skips cleanly without it)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
try:
    import yaml
except ImportError:
    print("SKIP: PyYAML not installed")
    sys.exit(0)

from antra.utils import slskd_onboarding as so

tmp = Path(tempfile.mkdtemp(prefix="slskd"))
so.SLSKD_CONFIG_DIR = tmp / "cfg"
so.SLSKD_DOWNLOADS_DIR = tmp / "dl"

FAIL = []
CASES = [
    ("plain", "user", "secret"),                       # control: the simple case must still work
    ("hash", "user", "pa #ss"),
    ("colon", "user", "a: b"),
    ("quote", 'us"er', "it's \"q\""),
    ("backslash", "u\\v", "p\\w"),
    ("leading", "user", "- starts like a list"),
    ("yaml-bool", "user", "yes"),
    ("unicode", "üser", "pässwörd"),
    ("newline-injection", "user", "x\n  authentication:\n    disabled: true"),
]
for label, user, pw in CASES:
    cfg = yaml.safe_load(so.generate_config(user, pw, "abc123", 5030).read_text())
    ok = (cfg["soulseek"]["username"] == user and cfg["soulseek"]["password"] == pw
          and cfg["web"]["authentication"]["disabled"] is False
          and cfg["web"]["port"] == 5030
          and cfg["web"]["authentication"]["api_keys"]["antra"]["key"] == "abc123")
    if not ok:
        FAIL.append(label)

# Control: the OLD bare interpolation must fail the hash and newline cases, or this suite proves nothing.
def old(pw):
    return yaml.safe_load(f"soulseek:\n  password: {pw}\nweb:\n  authentication:\n    disabled: false\n")
if old("pa #ss")["soulseek"]["password"] == "pa #ss":
    FAIL.append("control: old format should have truncated at ' #'")
if old("x\n  authentication:\n    disabled: true")["soulseek"]["password"] == "x\n  authentication:\n    disabled: true":
    FAIL.append("control: old format should not round-trip a newline")

print("FAIL: " + ", ".join(FAIL) if FAIL else "OK")
sys.exit(1 if FAIL else 0)
