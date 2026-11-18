"""Issue #72 - every mirror adapter must survive, and explain, a hostile network.

The report: sign-in fails outright, and with a supporter key pasted instead
every mirror dies while a source that is NOT behind *.anandserver.cfd downloads
fine. A VPN fixes it completely. Four of the five mirrors retried over an
alternate TLS stack and said something readable; **Apple did neither**, and put

    [Apple] All mirrors failed. Last error: HTTPSConnectionPool(...
    SSLError(SSLError(1, '[SSL: WRONG_VERSION_NUMBER] wrong version number')))

in front of the user, which reads like a server outage and is the wrong
conclusion. WRONG_VERSION_NUMBER means the bytes that came back were not TLS at
all, so something on the path answered instead of the mirror.

The retry is exercised against a REAL requests.Session raising a REAL
requests.exceptions.SSLError, not a stubbed sentinel, and the curl_cffi half is
substituted so the test needs no network.

Run:  python antra/utils/test_mirror_tls.py
Negative control: antra/utils/test_mirror_tls_negctl.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import requests

from antra.utils.mirror_http import TlsFallbackSession, humanize_network_error

FAILURES: list[str] = []
COUNT = 0


def check(label, got, want):
    global COUNT
    COUNT += 1
    if got != want:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")


def check_true(label, got):
    check(label, bool(got), True)


# The exact text a user pasted into the issue.
REPORTED = ("HTTPSConnectionPool(host='apple.anandserver.cfd', port=443): Max retries "
            "exceeded with url: /api/track/712862708 (Caused by SSLError(SSLError(1, "
            "'[SSL: WRONG_VERSION_NUMBER] wrong version number (_ssl.c:1006)')))")


# ── 1. the message a user is shown ─────────────────────────────────────────

msg = humanize_network_error(REPORTED, "Apple")
check_true("the reported error is rewritten", msg != REPORTED)
check_true("the rewrite is attributed to the service", msg.startswith("[Apple]"))
for leak in ("WRONG_VERSION_NUMBER", "_ssl.c", "HTTPSConnectionPool", "SSLError"):
    check(f"the rewrite does not leak {leak!r}", leak in msg, False)
check_true("the rewrite says it is a local network problem",
           "network" in msg.lower() or "proxy" in msg.lower())

# CONTROL: something that is NOT a network failure must come back untouched,
# or this helper would swallow every real API error behind a VPN suggestion.
for untouched in ("API error 404", "Track not found on Apple Music",
                  "Rate limited (429)", ""):
    check(f"a non-network message is unchanged: {untouched!r}",
          humanize_network_error(untouched, "Apple"), untouched)

# Each recognised class must give DIFFERENT advice, and this is asserted as
# four distinct messages rather than as "it changed".
#
# The first version only checked that each message was rewritten and mentioned
# the network, and the negative control correctly reported two sabotages as
# MISSED: humanize_network_error ends in a generic "a TLS error occurred"
# branch, so switching off the WRONG_VERSION_NUMBER test or the certificate
# test made those cases fall through to it - still rewritten, still mentioning
# the network, still passing. Counting the distinct outputs is what makes the
# branches load-bearing: collapsing any two of them fails here.
wrong_version = humanize_network_error(REPORTED, "Tidal")
cert = humanize_network_error("SSLError: certificate verify failed", "Tidal")
generic_tls = humanize_network_error("SSLError: tlsv1 alert internal error", "Tidal")
dead = humanize_network_error("Max retries exceeded: Name or service not known", "Tidal")

for label, m in (("wrong-version", wrong_version), ("certificate", cert),
                 ("generic TLS", generic_tls), ("unreachable", dead)):
    check_true(f"the {label} case is attributed to the service", m.startswith("[Tidal]"))

check("the four network classes give four different messages",
      len({wrong_version, cert, generic_tls, dead}), 4)
check_true("a certificate failure names the certificate",
           "certificate" in cert.lower())
check_true("only the wrong-version case says the reply was not HTTPS",
           "isn't https" in wrong_version.lower() and "isn't https" not in generic_tls.lower())


# ── 2. the retry, against a real SSLError ──────────────────────────────────

class _BoomSession:
    """Stands in for requests.Session and fails the way the reporter's did."""

    def __init__(self):
        self.headers = {}
        self.trust_env = True
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        raise requests.exceptions.SSLError(REPORTED)


class _Answer:
    status_code = 200

    def json(self):
        return {"ok": True}


def _session(curl_result):
    s = TlsFallbackSession("Apple")
    s._s = _BoomSession()
    s._curl_get = lambda url, **kw: curl_result
    return s


s = _session(_Answer())
resp = s.get("https://apple.anandserver.cfd/api/track/1")
check("a failed handshake is retried over the alternate stack", resp.json(), {"ok": True})
check("the standard stack was tried first", s._s.calls, 1)

# When the alternate stack fails too - which is what happened in the report -
# the original error must propagate rather than being swallowed into a None.
s = _session(None)
try:
    s.get("https://apple.anandserver.cfd/api/track/1")
    check("both stacks failing raises", False, True)
except requests.exceptions.SSLError as e:
    check("both stacks failing re-raises the original SSLError", str(e), REPORTED)

# A streaming GET is deliberately NOT retried: swapping the response object
# under a `with ... as r:` streaming read would break the download path.
s = _session(_Answer())
try:
    s.get("https://apple.anandserver.cfd/api/stream/1", stream=True)
    check("a streaming GET is not retried", False, True)
except requests.exceptions.SSLError:
    check("a streaming GET raises instead of being retried", True, True)

# trust_env must survive the wrapper - the Apple adapter switches it off, and
# losing that would silently change which proxy its requests take.
s = TlsFallbackSession("Apple")
s.trust_env = False
check("trust_env is forwarded to the real session", s._s.trust_env, False)
check("trust_env reads back", s.trust_env, False)


# ── 3. the Apple adapter actually uses it ──────────────────────────────────
#
# Asserted on the live object rather than on the source text, so a future edit
# that swaps the session back to a plain requests.Session is caught.

from antra.sources.apple import AppleAdapter

adapter = AppleAdapter.__new__(AppleAdapter)
AppleAdapter.__init__(adapter, mirrors=["https://apple.anandserver.cfd"])
check("the Apple mirror adapter is on the TLS-resilient session",
      type(adapter._session).__name__, "TlsFallbackSession")
# CONTROL: the four that already had it must still have it, so this is a
# property of every mirror rather than a one-off for Apple.
from antra.sources.deezer_mirror import DeezerMirrorAdapter
from antra.sources.qobuz_mirror import QobuzMirrorAdapter
from antra.sources.tidal_mirror import TidalMirrorAdapter

for cls, url in (
    (TidalMirrorAdapter, "https://tidal.anandserver.cfd"),
    (QobuzMirrorAdapter, "https://qobuz.anandserver.cfd"),
    (DeezerMirrorAdapter, "https://deezer.anandserver.cfd"),
):
    a = cls(url)
    check(f"{cls.__name__} is on the TLS-resilient session",
          type(a._session).__name__, "TlsFallbackSession")


# ── report ─────────────────────────────────────────────────────────────────

if FAILURES:
    print(f"FAIL - {len(FAILURES)} of {COUNT} checks failed:")
    for x in FAILURES:
        print("  -", x)
    sys.exit(1)
print(f"PASS - {COUNT} checks (issue #72 mirror TLS resilience)")
