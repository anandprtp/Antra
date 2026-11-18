"""Negative control for test_mirror_tls.py (issue #72).

Sabotage 1 is the reported bug put back: the Apple mirror adapter on a plain
requests.Session, with no retry and no readable error.

Run:  python antra/utils/test_mirror_tls_negctl.py
"""
import hashlib
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SUITE = os.path.join(ROOT, "antra", "utils", "test_mirror_tls.py")

APPLE = os.path.join(ROOT, "antra", "sources", "apple.py")
HTTP = os.path.join(ROOT, "antra", "utils", "mirror_http.py")

SABOTAGES = [
    ("1. THE REPORTED BUG: Apple back on a plain session, no retry", APPLE,
     '        self._session = TlsFallbackSession("Apple")\n        self._session.trust_env = False',
     '        self._session = requests.Session()\n        self._session.trust_env = False'),

    ("2. the alternate TLS stack is never tried", HTTP,
     '            resp = self._curl_get(url, **kwargs)\n            if resp is None:\n                raise\n            return resp',
     '            raise'),

    ("3. a streaming GET is retried too, swapping the response object", HTTP,
     '            if kwargs.get("stream"):\n                self._log_proxy_diagnostics(url)\n                raise',
     '            if False:\n                self._log_proxy_diagnostics(url)\n                raise'),

    ("4. WRONG_VERSION_NUMBER is no longer recognised", HTTP,
     '    if "wrong_version_number" in lowered or "record layer failure" in lowered:',
     '    if False:'),

    ("5. every message is rewritten, swallowing real API errors", HTTP,
     '    text = message or ""\n    lowered = text.lower()',
     '    text = message or ""\n    return f"[{service}] Something went wrong."\n    lowered = text.lower()'),

    ("6. a certificate failure gets the same advice as an unreachable host", HTTP,
     '    if "certificate verify failed" in lowered or "certificate_verify_failed" in lowered:',
     '    if False:'),

    ("7. trust_env stops reaching the real session", HTTP,
     '    @trust_env.setter\n    def trust_env(self, value):\n        self._s.trust_env = value',
     '    @trust_env.setter\n    def trust_env(self, value):\n        pass'),
]


def read(p):
    with open(p, "rb") as f:
        return f.read()


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
    before = {p: hashlib.md5(v).hexdigest() for p, v in originals.items()}

    caught = 0
    for label, path, old, new in SABOTAGES:
        raw = originals[path]
        text = raw.decode("utf-8")
        nl = "\r\n" if b"\r\n" in raw else "\n"
        o, n = old.replace("\n", nl), new.replace("\n", nl)
        if text.count(o) != 1:
            print(f"  ??   {label}\n       ANCHOR NOT FOUND ({text.count(o)} matches)")
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
            print(f"  OK   {label}\n       {first.strip()[:140]}")
        else:
            print(f"  MISS {label}  <-- the suite did NOT catch this")

    ok = all(hashlib.md5(read(p)).hexdigest() == before[p] for p in originals)
    print(f"\nall files restored byte-identically: {ok}")
    print(f"{caught} of {len(SABOTAGES)} sabotages caught")
    code, _ = run_suite()
    print(f"suite passes again after restore: {code == 0}")
    return 0 if (caught == len(SABOTAGES) and ok and code == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
