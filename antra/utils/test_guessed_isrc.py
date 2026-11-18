"""BUG-MIX-1 parts 2 and 3: a GUESSED ISRC is a guess, and ISRCs are not capped.

Covers:
  * antra.core.isrc_enricher.guessed_isrc_is_acceptable - the bar a text-search
    hit must clear before the track borrows its ISRC (used by the website's
    Deezer fallback in Antra-Web/main.py).
  * SourceResolver._passes_identity_gate - an isrc_match on a track whose ISRC
    was GUESSED must still be checked against the title, instead of being
    treated as proof of identity.
  * AntraService.enrich_tracks_for_download - above the album-enrichment cap,
    ISRCs must still be fetched.

Run:  python antra/utils/test_guessed_isrc.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from antra.core.isrc_enricher import guessed_isrc_is_acceptable
from antra.core.models import SearchResult, TrackMetadata
from antra.core.resolver import SourceResolver

failures = []


def check(cond, label):
    if not cond:
        failures.append(label)


# ---------------------------------------------------------------- the bar ----
# (track title, artists, duration_ms, hit title, hit artist, hit duration_s, ok)
GUESS_CASES = [
    # Same mix, different bracket style, duration agrees -> safe.
    ("Can't Get Enough! - Vocal Club Mix", ["Soulsearcher"], 412973,
     "Can't Get Enough! (Vocal Club Mix)", "Soulsearcher", 410, True),
    ("Saltwater", ["Chicane"], 202533, "Saltwater", "Chicane", 202, True),
    # The reported failure: the plain version of a mix-titled track.
    ("Can't Get Enough! - Vocal Club Mix", ["Soulsearcher"], 412973,
     "Can't Get Enough!", "Soulsearcher", 214, False),
    ("My Feeling (Daddy'S Prime Time Edit)", ["Junior Jack"], 228960,
     "My Feeling", "Junior Jack", 229, False),
    # Right title, wrong length -> different recording.
    ("Saltwater", ["Chicane"], 202533, "Saltwater", "Chicane", 402, False),
    # Deezer sometimes omits duration; that is a reject, never a pass.
    ("Saltwater", ["Chicane"], 202533, "Saltwater", "Chicane", None, False),
    ("Saltwater", ["Chicane"], None, "Saltwater", "Chicane", 202, False),
    # Same title, different act.
    ("Anthem", ["NJOI"], 258426, "Anthem", "Filthy Karaoke Band", 258, False),
]

for title, artists, dur_ms, h_title, h_artist, h_dur, want in GUESS_CASES:
    got = guessed_isrc_is_acceptable(title, artists, dur_ms, h_title, h_artist, h_dur)
    check(got is want,
          "guessed_isrc_is_acceptable(%r -> %r, %ss) = %s, wanted %s"
          % (title, h_title, h_dur, got, want))


# ------------------------------------------------- the gate in the resolver ---
def _track(title, isrc, guessed, duration_ms=412973):
    t = TrackMetadata(title=title, artists=["Soulsearcher"], album="Album")
    t.duration_ms = duration_ms
    t.isrc = isrc
    t.isrc_guessed = guessed
    return t


def _result(title, duration_ms=412000):
    return SearchResult(
        source="tidal_mirror",
        title=title,
        artists=["Soulsearcher"],
        album="Album",
        duration_ms=duration_ms,
        audio_format="FLAC",
        quality_kbps=1411,
        is_lossless=True,
        download_url=None,
        stream_id="1",
        similarity_score=1.0,
        isrc_match=True,
    )


resolver = SourceResolver([])
gate = resolver._passes_identity_gate

WRONG = _result("Can't Get Enough!")
RIGHT = _result("Can't Get Enough! (Vocal Club Mix)")
QUERY = "Can't Get Enough! - Vocal Club Mix"

# A catalogue ISRC stays authoritative: this is the shipped behaviour that stops
# the marker check rejecting a catalogue that titles the same recording plainly.
check(gate(_track(QUERY, "GBCPZ9900001", False), WRONG) is True,
      "a catalogue ISRC must still short-circuit the marker check")
# A guessed ISRC must not.
check(gate(_track(QUERY, "GBCPZ9900001", True), WRONG) is False,
      "a GUESSED ISRC let the wrong mix through the identity gate")
# ...and must not reject the right one either.
check(gate(_track(QUERY, "GBCPZ9900001", True), RIGHT) is True,
      "a guessed ISRC rejected the matching mix")
# The unconditional duration guard is untouched.
check(gate(_track(QUERY, "GBCPZ9900001", False), _result(QUERY, 120000)) is False,
      "severe duration mismatch must be rejected regardless of ISRC")


# --------------------------------------------- ISRCs are not capped by size ---
from antra.core.service import AntraService

service = AntraService()
seen = {}


def _fake_enrich(tracks):
    seen["n"] = len(tracks)


service._enrich_isrcs = _fake_enrich
service.build_runtime_config = lambda options=None: types.SimpleNamespace(enrich_album_data=True)
service.validate_config = lambda cfg: None

big = [_track("Track %d" % i, None, False) for i in range(250)]
service.enrich_tracks_for_download(big, "https://open.spotify.com/playlist/x")
check(seen.get("n") == 250,
      "over the album-enrichment cap, ISRC enrichment must still run (got %r)" % (seen.get("n"),))

total = len(GUESS_CASES) + 4 + 1
if failures:
    print("FAIL (%d of %d checks)" % (len(failures), total))
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("OK - %d checks passed" % total)
