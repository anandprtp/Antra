"""BUG-ASIN-1: a GUESSED Amazon ASIN is a guess, and an exact title is not an artist.

Reported 2026-10-06 against the website: downloading Fastball's "Make Your Mama
Proud" (open.spotify.com/album/47V331pHZNjONayt1RUmTo) delivered two songs by
other bands plus "some Western thing" for "Seattle", all three tagged as the
Fastball album. Measured, the album carries NO ISRC on any of its 14 tracks, so
every track fell through to cross-platform/scraped resolution.

Three defects, all covered here:

  1. antra.sources.odesli._pick_amazon_product_asin - the amazon.com PRODUCT
     scraper accepted a card on a bare substring compare of the titles with the
     artist IGNORED, then fell back to "first result wins" with no check at all.
     Live, it returned a boomerang TOY for "Boomerang", an address STAMP for
     "Seattle", a BOOK for "Eater", and the SAME ASIN for two different songs.

  2. AmazonAdapter._probe_asin_is_music / _verify_guessed_asin - a guessed ASIN
     was used to build a SearchResult out of the REQUESTED title, artists and
     duration, stamped similarity_score=0.90. Self-consistent with the query by
     construction, so the resolver's identity gates passed it trivially.

  3. matching.score_similarity - a "title_score >= 0.90 means trust the title
     alone" bypass overrode the wrong-artist cap, and title_artist_score
     manufactured artist confidence from character overlap between an artist name
     and an unrelated song title. Together these scored a STRANGER'S recording
     at 0.642-0.728, over FALLBACK_FLOOR (0.60) and, for "Seattle", over
     ACCEPT_THRESHOLD (0.70).

Every check that asserts a rejection is paired with a control that must come out
the other way, because "reject everything" would otherwise pass this file.

Run:  python antra/utils/test_asin_provenance.py
"""
import inspect
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from antra.sources.amazon import AmazonAdapter
from antra.sources.odesli import _ISBN_ASIN_RE, _pick_amazon_product_asin
from antra.core.models import TrackMetadata
from antra.core.resolver import (
    ACCEPT_THRESHOLD,
    FALLBACK_FLOOR,
    LOSSLESS_ACCEPT_THRESHOLD,
)
from antra.sources.base import RateLimitedError
from antra.utils.matching import WRONG_ARTIST_CAP, artist_match_score, score_similarity

failures = []


def check(cond, label):
    if not cond:
        failures.append(label)


# Assert the module under test is THIS tree's copy. The repo root also holds
# Antra-Web/antra, and importing the wrong one is how a suite passes while
# measuring nothing (recorded in CHANGELOG_ARCHIVE under CARRY-6).
import antra.sources.odesli as _odesli_mod
import antra.utils.matching as _matching_mod

_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _mod in (_odesli_mod, _matching_mod):
    check(
        os.path.abspath(_mod.__file__).startswith(os.path.join(_root, "antra") + os.sep),
        f"{_mod.__name__} resolved outside this tree ({_mod.__file__})",
    )


# ---------------------------------------------------------------------------
# 1. The amazon.com product scraper
# ---------------------------------------------------------------------------
# Verbatim (asin, product_title) pairs scraped from the live amazon.com /
# amazon.in search pages on 2026-10-06 for this album. HTML entities are left
# exactly as the page served them, since unescaping them is part of the fix.
BOOMERANG_PAGE = [
    ("B0DK529JWK", "Professional Flyback Returning Boomerang for Kids &amp; Adults - Large V-Shape A"),
    ("B0DK529JWK", "KiddyBuddy Glow in The Dark Lightning Boomerang Kids Outdoor Toy - Best Outside "),
    ("B0CZHR9PPB", "USB Rechargeable Flying Ball with Built-in RGB Lights | 360 Rotating Boomerang "),
    ("B0DMT46W8W", "Flying Spinner Hand Controlled Boomerang Hover UFO Flying Ball Toy for Kids &amp"),
    ("B0HFNYSY85", "Decathlon Turnball Rubber Fastball - Green"),
]
SEATTLE_PAGE = [
    ("B0GXVNC4T7", "Customized Handheld Seattle Fastball Duo Hand Address Stamp - Craft Stamp"),
    ("B0F8MXFB92", "Seattle Fastball Pre-Inked Address Stamp - Return Address Stamper"),
    ("B0F8MZSSDH", "Seattle Fastball Slim Pre Inked Return Address Stamp Custom Made for Mail"),
    ("B0G5DG3W7Z", "Driveline Baseball PlyoCare Balls - Weighted Pitching Training Balls + Free 16-W"),
]
EATER_PAGE = [
    ("0857667734", "Smoke Eaters"),
    ("B07ZLF2SLG", "Funny Meat Eaters, Smoke Brisket not Meth - Men Womens T-Shirt"),
    ("B0D1VXDZZ8", "Denco Warsaw Fastball Cleaner Aerosol, 15.5oz, 36-Pack"),
]
KNOCK_PAGE_IN = [
    ("B094HBB6WJ", "Firstly Traders Pack of 3 (45 mm) Dark Crazy Balls Sport and Outdoor Games for B"),
    ("B0948R8T37", "Sponsored"),
]
NOTHING_PAGE = [
    ("B006B82Y3G", "Nothing [Explicit]"),
    ("B08731Q7TG", "Nothing Is Left"),
    ("B004IH40Q6", "Nothing Comes from Nothing"),
    ("B0H9M3GGT4", "Fastball or Nothing Baseball Pitcher Design T-Shirt"),
]
ALTAMONT_PAGE = [
    ("B006B833AE", "Altamont"),
    ("B0CVJ5RZZ1", "Sponsored"),
    ("B0GTJXC74D", "SELECT Club Soccer Ball"),
]
SHE_COMES_PAGE = [
    ("B006B82V4I", "She Comes &#x27;Round"),
    ("B01EDA9W0Q", "She Comes &#x27;Round (Album Version)"),
]

ARTISTS = ["Fastball"]


def pick(pairs, title):
    return _pick_amazon_product_asin(pairs, title, title, ARTISTS)


# --- the reported failures: every one of these must now be refused -----------
check(pick(BOOMERANG_PAGE, "Boomerang") is None,
      "a page of boomerang TOYS still yields an ASIN for 'Boomerang'")
check(pick(EATER_PAGE, "Eater") is None,
      "the book 'Smoke Eaters' still yields an ASIN for 'Eater'")
check(pick(KNOCK_PAGE_IN, "Knock It Down") is None,
      "a page of bouncy balls still yields an ASIN for 'Knock It Down'")

# The Seattle page is the sharpest case: three cards CONTAIN the artist name
# ("Seattle Fastball ... Address Stamp"), so an artist-substring test would have
# accepted them. Only the scoring gate rejects them.
check(pick(SEATTLE_PAGE, "Seattle") is None,
      "an address STAMP titled 'Seattle Fastball ...' still yields an ASIN")

# --- controls that MUST come out the other way -------------------------------
# Without these, deleting the scraper entirely would pass every check above.
got = pick(ALTAMONT_PAGE, "Altamont")
check(got is not None and got[0] == "B006B833AE",
      f"the genuine 'Altamont' card is no longer found (got {got!r})")

got = pick(NOTHING_PAGE, "Nothing")
check(got is not None and got[0] == "B006B82Y3G",
      f"the genuine 'Nothing [Explicit]' card is no longer found (got {got!r})")

got = pick(SHE_COMES_PAGE, "She Comes 'Round")
check(got is not None and got[0] == "B006B82V4I",
      f"HTML entities broke the match for \"She Comes 'Round\" (got {got!r})")

# A genuine card ranked BELOW junk must still win: the old code returned the
# first plausible hit in page order, so position must not decide this.
mixed = list(SEATTLE_PAGE) + [("B006B82ZZZ", "Seattle")]
got = pick(mixed, "Seattle")
check(got is not None and got[0] == "B006B82ZZZ",
      f"a genuine card below junk in page order is not picked (got {got!r})")

# --- no "first result wins" fallback ----------------------------------------
check(pick([("B0GTJXC74D", "SELECT Club Soccer Ball")], "Altamont") is None,
      "a single implausible result is still returned as a fallback")
check(pick([], "Altamont") is None, "an empty page returns something")
check(pick(None, "Altamont") is None, "a None page raises or returns something")

# --- ISBN-shaped ASINs are print products ----------------------------------
check(bool(_ISBN_ASIN_RE.match("0857667734")), "ISBN-shaped ASIN not recognised")
check(not _ISBN_ASIN_RE.match("B0DK529JWK"), "a normal ASIN is misread as an ISBN")
check(pick([("0857667734", "Altamont")], "Altamont") is None,
      "an ISBN-shaped ASIN is accepted even with a perfectly matching title")

# A version mismatch must still be capped by score_similarity and so refused.
check(pick([("B006B8FAKE", "Altamont (Karaoke Version)")], "Altamont") is None,
      "a karaoke product card is accepted for the original")


# ---------------------------------------------------------------------------
# 2. ASIN verification against the mirror
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code, payload=None, raise_json=False):
        self.status_code = status_code
        self._payload = payload
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._payload


class StubAdapter(AmazonAdapter):
    """Real AmazonAdapter with only the HTTP layer and the page fallback stubbed,
    so the verdict logic under test is the shipped one."""

    def __init__(self, response):
        self._response = response
        self._probe_calls = []
        self._page_result = None
        self._mirrors = ["https://mirror.test"]
        self._current_mirror = "https://mirror.test"
        self._mirror_failures = {}

        class _S:
            def get(_self, url, **kw):
                self._probe_calls.append(url)
                if isinstance(response, Exception):
                    raise response
                return response

        self._session = _S()

    def _get_working_mirror(self, force_rotate=False):
        return "https://mirror.test"

    def _verify_scraped_asin_any_store(self, track, asin):
        return self._page_result


def trk(duration_ms=177000, title="Boomerang"):
    return TrackMetadata(title=title, artists=ARTISTS, album="Make Your Mama Proud",
                         duration_ms=duration_ms)


# Real payload shapes, copied from https://amazon.anandserver.cfd/api/probe/
MUSIC_PAYLOAD = {"asin": "B006B831U6", "formats": ["opus", "opus-ld", "flac-16"],
                 "duration_ms": 128960, "best": {"bit_depth": 16, "sample_rate": 44100}}
JUNK_PAYLOAD = {"asin": "B0DK529JWK", "formats": [], "duration_ms": None,
                "quality": {}, "best": None}

a = StubAdapter(FakeResponse(200, JUNK_PAYLOAD))
check(a._probe_asin_is_music("B0DK529JWK") == (a._ASIN_NOT_MUSIC, None),
      "an empty formats list is not read as 'not a music track'")

a = StubAdapter(FakeResponse(200, MUSIC_PAYLOAD))
check(a._probe_asin_is_music("B006B831U6") == (a._ASIN_MUSIC, 128960),
      "a real music payload is not read as music with its duration")

a = StubAdapter(FakeResponse(404, {"detail": "Track not available in this marketplace"}))
check(a._probe_asin_is_music("B006B831U6")[0] == a._ASIN_UNKNOWN,
      "a 404 is treated as a verdict rather than as 'cannot tell' - a marketplace "
      "miss is not a statement about what the ASIN is")

a = StubAdapter(FakeResponse(200, None, raise_json=True))
check(a._probe_asin_is_music("B006B831U6")[0] == a._ASIN_UNKNOWN,
      "unparseable JSON is not treated as unknown")

a = StubAdapter(RuntimeError("connection reset"))
check(a._probe_asin_is_music("B006B831U6")[0] == a._ASIN_UNKNOWN,
      "a transport error is not treated as unknown")

a = StubAdapter(FakeResponse(429, {}))
try:
    a._probe_asin_is_music("B006B831U6")
    check(False, "a 429 from the probe does not raise RateLimitedError")
except RateLimitedError:
    pass

a = StubAdapter(FakeResponse(200, MUSIC_PAYLOAD))
check(a._probe_asin_is_music("") == (a._ASIN_UNKNOWN, None)
      and not a._probe_calls,
      "an empty ASIN still costs a request")

# --- _verify_guessed_asin -------------------------------------------------
a = StubAdapter(FakeResponse(200, JUNK_PAYLOAD))
check(a._verify_guessed_asin(trk(), "B0DK529JWK", "Odesli") is None,
      "a non-music ASIN is still accepted")

# duration agrees (128960 ms vs a 129000 ms track) -> accept
a = StubAdapter(FakeResponse(200, MUSIC_PAYLOAD))
got = a._verify_guessed_asin(trk(129000, "Knock It Down"), "B006B831U6", "Odesli")
check(got is not None and abs(got[0] - 0.90) < 1e-9 and got[1] == 128960,
      f"a duration-corroborated ASIN is no longer accepted at 0.90 with its probed "
      f"duration (got {got!r})")

# the control that matters most: REAL music, WRONG length -> reject. Without it,
# the guard would only be testing "is this an audio file".
a = StubAdapter(FakeResponse(200, MUSIC_PAYLOAD))
a._page_result = None
check(a._verify_guessed_asin(trk(187000, "Altamont"), "B006B831U6", "Odesli") is None,
      "a real music ASIN of the wrong length (129s vs 187s) is still accepted")

# unknown probe, page fallback declines -> reject
a = StubAdapter(FakeResponse(503, {}))
a._page_result = None
check(a._verify_guessed_asin(trk(), "B0DK529JWK", "Odesli") is None,
      "an unverifiable ASIN is still accepted")

# unknown probe, but the page check corroborates -> accept (the fallback works)
a = StubAdapter(FakeResponse(503, {}))
a._page_result = (0.88, 176500)
got = a._verify_guessed_asin(trk(), "B006B82XXX", "Odesli")
check(got == (0.88, 176500),
      f"the page-scrape fallback no longer rescues a verifiable ASIN (got {got!r})")

# music, but the TRACK has no duration to compare -> accept, below ACCEPT_THRESHOLD
a = StubAdapter(FakeResponse(200, MUSIC_PAYLOAD))
a._page_result = None
got = a._verify_guessed_asin(trk(None), "B006B831U6", "Odesli")
check(got is not None and got[0] < ACCEPT_THRESHOLD,
      f"a duration-less track yields a confidence that can short-circuit the chain "
      f"(got {got!r}, ACCEPT_THRESHOLD={ACCEPT_THRESHOLD})")


# --- the MIRROR-RESOLVE path is a guess too ---------------------------------
# `_resolve_via_mirror` asks the mirror to resolve a Spotify id inside Amazon's
# own marketplace index, which reads authoritative. It is not: measured on
# production 2026-10-06 it answered ASIN **B01ESAYUKI (probed duration 207 s)**
# for Fastball's **177 s** "Boomerang" -- a 30 s disagreement against three
# independent sources (Spotify 177 s, Qobuz 176 s, the album itself) -- and
# search() returned it at 0.90 with the REQUESTED duration echoed back. It was
# the one ASIN route still exempt from verification after the first fix, and
# amazon.py's own comment records why ("the mirror's own chain uses it too").
_src = inspect.getsource(AmazonAdapter.search)
_mirror_idx = _src.find("_resolve_via_mirror")
_odesli_idx = _src.find("_odesli.resolve")
check(_mirror_idx >= 0, "search() no longer calls _resolve_via_mirror")
check("_verify_guessed_asin" in _src,
      "search() never calls _verify_guessed_asin")
# The verify call must appear between the mirror resolve and the Odesli branch,
# i.e. the mirror's own answer is checked rather than only the fallback's.
_between = _src[_mirror_idx:_odesli_idx] if 0 <= _mirror_idx < _odesli_idx else ""
check("_verify_guessed_asin" in _between,
      "the MIRROR-resolved ASIN is still used unverified - the verify call only "
      "guards the Odesli fallback")
# ...and the converse: the Odesli branch must be guarded independently of the
# mirror one. Asserting only "the source mentions _verify_guessed_asin somewhere"
# let a sabotage that stripped the Odesli guard pass once the mirror guard
# existed, so each branch is pinned on its own.
_after_odesli = _src[_odesli_idx:] if _odesli_idx >= 0 else ""
check("_verify_guessed_asin" in _after_odesli,
      "the ODESLI-resolved ASIN is still used unverified - the verify call only "
      "guards the mirror resolve")
# A rejected mirror ASIN must also drop the ready stream payload, or download()
# would stream the refused ASIN's bytes anyway.
check(re.search(r"(resolved_payload|_mirror_payload)\s*=\s*None", _src) is not None,
      "a rejected mirror ASIN does not clear the pre-resolved stream payload")


# ---------------------------------------------------------------------------
# 3. score_similarity: the wrong-artist hole
# ---------------------------------------------------------------------------
# The cap has to sit below every gate a capped result could otherwise clear.
check(WRONG_ARTIST_CAP < LOSSLESS_ACCEPT_THRESHOLD,
      f"WRONG_ARTIST_CAP ({WRONG_ARTIST_CAP}) is not below LOSSLESS_ACCEPT_THRESHOLD "
      f"({LOSSLESS_ACCEPT_THRESHOLD})")
check(WRONG_ARTIST_CAP < FALLBACK_FLOOR,
      f"WRONG_ARTIST_CAP ({WRONG_ARTIST_CAP}) is not below FALLBACK_FLOOR ({FALLBACK_FLOOR})")
for _name, _mod in (("qobuz_mirror", "antra.sources.qobuz_mirror"),
                    ("soulseek", "antra.sources.soulseek")):
    try:
        _m = __import__(_mod, fromlist=["MIN_SIMILARITY"])
        check(WRONG_ARTIST_CAP < _m.MIN_SIMILARITY,
              f"WRONG_ARTIST_CAP ({WRONG_ARTIST_CAP}) is not below "
              f"{_name}.MIN_SIMILARITY ({_m.MIN_SIMILARITY})")
    except Exception:
        pass

# The three tracks the reporter named, against real same-titled strangers.
WRONG_ARTIST = [
    ("Boomerang", "The Summer Set"),
    ("Boomerang", "Imagine Dragons"),
    ("Knock It Down", "Keri Hilson"),
    ("Seattle", "Perry Como"),
    ("Seattle", "Public Image Ltd"),
    ("White Christmas", "Michael Buble"),
]
for title, stranger in WRONG_ARTIST:
    s = score_similarity(title, ARTISTS if title != "White Christmas" else ["Bing Crosby"],
                         title, stranger)
    check(s < LOSSLESS_ACCEPT_THRESHOLD,
          f"{title!r} by {stranger!r} still scores {s:.3f} >= "
          f"{LOSSLESS_ACCEPT_THRESHOLD} and would be accepted as lossless")

# CARRY-6 recorded this exact pair at 0.714 via the bypass and had to add a
# separate guard at its call site.
check(score_similarity("Greatest Hits", ["Various Artists"],
                       "Greatest Hits", "Queen & David Bowie") < LOSSLESS_ACCEPT_THRESHOLD,
      "the CARRY-6 'Greatest Hits' / 'Queen & David Bowie' pair is still accepted")

# --- controls: the cap must not swallow correct matches ---------------------
check(score_similarity("Boomerang", ARTISTS, "Boomerang", "Fastball") == 1.0,
      "an exact title+artist match no longer scores 1.0")

# The buried-artist case is what the deleted bypass was really protecting. It is
# now handled by artist_match_score, so these MUST stay high.
BURIED_ARTIST = [
    ("Arijit Singh", "Pritam, Shashwat Singh, Antara Mitra, Arijit Singh & Amitabh Bhattacharya"),
    ("Arijit Singh", "Vishal & Shekhar, Arijit Singh"),
    ("Arijit Singh", "Pritam, Arijit Singh & Amitabh Bhattacharya"),
    ("Dua Lipa", "Calvin Harris & Dua Lipa"),
    ("Drake", "Drake, 21 Savage"),
    ("Wizkid", "Drake feat. Wizkid & Kyla"),
]
for query, credit in BURIED_ARTIST:
    check(artist_match_score([query], credit) == 1.0,
          f"artist_match_score({query!r}, {credit!r}) is not 1.0 - the artist IS named")
    s = score_similarity("Kesariya", [query], "Kesariya", credit)
    check(s >= ACCEPT_THRESHOLD,
          f"a correct artist buried in a credit list now scores {s:.3f} < "
          f"{ACCEPT_THRESHOLD} and would be rejected ({query!r} in {credit!r})")

# ...and the containment must be on whole words, or it becomes its own bug.
for query, other in (("Queen", "Queens of the Stone Age"),
                     ("Air", "Airbourne"),
                     ("Various Artists", "Queen & David Bowie"),
                     ("Fastball", "Perry Como")):
    check(artist_match_score([query], other) < 1.0,
          f"artist_match_score({query!r}, {other!r}) == 1.0 - substring match is not "
          f"on whole-word boundaries")

# An empty result artist still bypasses the cap: an adapter that reports no
# artist at all is not evidence of a WRONG artist.
check(score_similarity("Seattle", ARTISTS, "Seattle", "") > WRONG_ARTIST_CAP,
      "a result with no artist information at all is now capped as a wrong artist")

# A version mismatch must still dominate.
check(score_similarity("Boomerang", ARTISTS, "Boomerang (Karaoke Version)", "Fastball") <= 0.40,
      "the version-mismatch cap no longer dominates")

# --- the fabricated artist signal ------------------------------------------
# title_artist_score used to be a raw SequenceMatcher ratio between the query
# ARTIST and the result TITLE, discounted by 0.8. Comparing two already-capped
# scores cannot see that, because the cap hides it -- the danger is the ratio
# lifting best_artist_score OVER 0.45 and skipping the cap ENTIRELY. Measured
# ratios that did so: "Chicago"/"Chicago" 1.000 -> 0.800, "Garbage"/"Garbage"
# 1.000, "America"/"Africa" 0.769 -> 0.615, "Heart"/"Heartbreaker" 0.588 ->
# 0.471. Every one of those scored a stranger's recording at 0.788-0.920, over
# ACCEPT_THRESHOLD. Eponymous songs are common, so the cap was disabled for a
# whole class of track.
FABRICATED = [
    ("Chicago", "Chicago", "Some Other Band", "eponymous song, ratio 1.000"),
    ("Garbage", "Garbage", "Unrelated Act", "eponymous song, ratio 1.000"),
    ("Heartbreaker", "Heart", "Mariah Carey", "artist is a prefix of the title"),
    ("Africa", "America", "Toto", "artist merely resembles the title"),
]
for title, artist, stranger, why in FABRICATED:
    s = score_similarity(title, [artist], title, stranger)
    check(s < LOSSLESS_ACCEPT_THRESHOLD,
          f"{title!r} by {stranger!r} requested as {artist!r} scores {s:.3f} >= "
          f"{LOSSLESS_ACCEPT_THRESHOLD} - character overlap between the artist name "
          f"and the song title is still manufacturing artist confidence ({why})")

# Controls, or the four checks above would pass by breaking eponymous songs
# outright: the RIGHT artist for an eponymous song must still be a perfect match.
for title in ("Chicago", "Garbage"):
    check(score_similarity(title, [title], title, title) == 1.0,
          f"an eponymous song {title!r} by its own band no longer scores 1.0")

# ...and the case title_artist_score exists for -- the artist genuinely named in
# a result title alongside the song -- must still be credited.
check(score_similarity("Stan", ["Eminem"], "Stan - Eminem (Official Video)", "SomeChannel")
      > WRONG_ARTIST_CAP,
      "an artist named in the result TITLE (the YouTube case) is no longer credited")
check(score_similarity("Kesariya", ["Arijit Singh"],
                       "Kesariya Full Song | Arijit Singh", "T-Series") > WRONG_ARTIST_CAP,
      "an artist named in a label-channel result title is no longer credited")


# ---------------------------------------------------------------------------
if failures:
    print(f"FAIL ({len(failures)} of many checks)")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("OK - all checks passed "
      "(product scraper, ASIN verification, wrong-artist cap, buried artist)")
