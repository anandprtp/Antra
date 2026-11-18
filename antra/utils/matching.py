"""
String similarity helpers for track matching.
"""
import re
from difflib import SequenceMatcher
from typing import Optional


def _as_text(value) -> str:
    """API fields arrive as null or numbers; the scorer must not raise on them."""
    if isinstance(value, str):
        return value
    return "" if value is None else str(value)


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse spaces."""
    text = _as_text(text).lower()
    text = re.sub(r"\(.*?\)|\[.*?\]", "", text)   # Remove parenthetical suffixes
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# Matches collaboration credits that catalog search engines don't index:
# "(with X)", "[with X]", "(feat. X)", "[feat. X]", "(ft. X)", "(featuring X)", etc.
_COLLAB_CREDIT_RE = re.compile(
    r"\s*[\(\[](with|feat\.?|ft\.?|featuring)\s+[^\)\]]+[\)\]]",
    re.IGNORECASE,
)


def strip_collab(title: str) -> str:
    """Remove collaboration credits from a track title before sending to catalog
    search APIs. Catalogs index tracks under the clean title only — including
    '(with Travis Scott)' or '[feat. Future]' breaks text search.

    Used for search query construction only; raw title is still used for
    similarity scoring so the match is validated against the full title.
    """
    return _COLLAB_CREDIT_RE.sub("", title).strip()


def string_similarity(a: str, b: str) -> float:
    """0.0–1.0 similarity between two strings after normalization."""
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# ── Version / recording-class markers ────────────────────────────────────────
# normalize() strips ALL parentheticals before similarity scoring, which makes
# "Song (Instrumental)" and "Song" score 1.0 against each other. That is the
# single biggest cause of wrong-audio matches: instrumentals, karaoke covers
# and live versions sail through with a perfect title score. These markers are
# extracted from the title's suffix segments — parentheticals, brackets, and
# text after a " - " separator — BEFORE normalization, and compared as sets.
# A mismatch means the two titles refer to different recordings of the song.
#
# Deliberately NOT markers (same recording, different packaging):
#   remaster/remastered, deluxe, anniversary, bonus, expanded, mono/stereo,
#   single version, album version, radio edit (handled by _CLEAN_VERSION_RE
#   in the resolver), feat./with credits (handled by strip_collab).
_VERSION_MARKER_CLASSES: dict[str, "re.Pattern[str]"] = {
    "instrumental": re.compile(r"\b(instrumental|inst|off vocal|karaoke|backing track)\b", re.IGNORECASE),
    "tribute":      re.compile(r"\b(tribute|cover|originally performed|in the style of)\b", re.IGNORECASE),
    "acapella":     re.compile(r"\b(acapella|a cappella|vocals? only)\b", re.IGNORECASE),
    "live":         re.compile(r"\b(live|unplugged|concert)\b", re.IGNORECASE),
    "acoustic":     re.compile(r"\bacoustic\b", re.IGNORECASE),
    "remix":        re.compile(r"\b(remix|flip|bootleg|rework|re-?edit)\b", re.IGNORECASE),
    "altered":      re.compile(r"\b(sped ?up|slowed|reverb|nightcore|8[- ]?bit|chopped|screwed|music box|lullaby)\b", re.IGNORECASE),
    "demo":         re.compile(r"\b(demo|rough mix|early version)\b", re.IGNORECASE),
    "rerecording":  re.compile(r"\b(taylor'?s version|re-?recorded|re-?recording)\b", re.IGNORECASE),
    "orchestral":   re.compile(r"\b(orchestral|orchestra version|piano version|strings version)\b", re.IGNORECASE),
}

# Suffix segments: (...) or [...] anywhere, plus everything after " - ".
_TITLE_SUFFIX_SEGMENT_RE = re.compile(r"\(([^)]*)\)|\[([^\]]*)\]|\s[-–—]\s(.+)$")


def version_markers(title: str) -> frozenset[str]:
    """Extract the set of recording-class markers from a title's suffix
    segments. 'Stand Get Up (inst)' → {'instrumental'};
    'The Boxer (Live at Central Park)' → {'live'}; 'Stand Get Up' → {}."""
    if not title:
        return frozenset()
    segments: list[str] = []
    for match in _TITLE_SUFFIX_SEGMENT_RE.finditer(title):
        segments.extend(g for g in match.groups() if g)
    if not segments:
        return frozenset()
    blob = " ".join(segments)
    found = set()
    for cls, pattern in _VERSION_MARKER_CLASSES.items():
        if pattern.search(blob):
            found.add(cls)
    return frozenset(found)


# ── Mix / edit designators ─────────────────────────────────────────────────────
# _VERSION_MARKER_CLASSES groups titles into recording CLASSES, which is enough
# for "is this a karaoke cover" but not for dance music, where the mix name IS
# the recording's identity and two mixes share a class. Measured on the reported
# playlist (BUG-MIX-1), with only the class check in place:
#   "My Feeling (Daddy'S Prime Time Edit)" vs "My Feeling"            -> 1.000
#   "Need To Feel Loved - 12'' Club Mix" vs "... (Adam K & Soha Vocal Mix)" -> 0.850
#   "Can't Get Enough! - Vocal Club Mix" vs "Can't Get Enough!"       -> 0.809
# all above every acceptance threshold, because normalize() deletes the
# parenthetical outright and "club mix" / "vocal mix" / "edit" are in no class.
#
# So the designator is compared as CONTENT, not class: the distinguishing words
# of the mix name, with structural and packaging words removed.
_MIX_KEYWORD_RE = re.compile(
    r"\b(mix|mixes|mixx?es|edit|edits|dub|version|cut|remix|remixes|rework|re-?edit|vip)\b",
    re.IGNORECASE,
)

# Words that carry no identity: the structural noun itself, connectives, the
# release-packaging vocabulary, and years. Dropping these is what makes
# "(Deluxe Version)" identical to a plain title, and "Full Intention 12\"Remix"
# identical to "Full Intention 12\"Remix [Remastered 2021]" (measured: Qobuz
# returns exactly that for the Spotify title).
_DESIGNATOR_NOISE = frozenset({
    "mix", "mixes", "mixxes", "edit", "edits", "version", "versions", "cut",
    "remix", "remixes", "rework", "reedit", "mixed", "inch", "re",
    "the", "a", "an", "of", "and", "by", "from", "on", "in",
    "feat", "ft", "featuring", "with",
    "remaster", "remastered", "remastering", "deluxe", "anniversary", "bonus",
    "expanded", "mono", "stereo", "explicit", "clean", "digital", "reissue",
    "edition", "special", "limited", "hd", "hq",
})

# Designators that name the STANDARD release rather than a distinct one.
# Catalogues routinely omit them ("Song - Radio Edit" is just "Song" on Tidal),
# so treating them as "no designator" avoids failing tracks that are in fact
# the right recording. A genuinely different length is still caught by the
# resolver's duration guard.
_DESIGNATOR_CANONICAL = frozenset({
    "radio", "single", "album", "original", "main", "standard", "full",
    "short", "normal", "default",
})

_YEAR_RE = re.compile(r"^(19|20)\d\d$")


def mix_designator(title: str) -> frozenset[str]:
    """The distinguishing words of a title's mix/edit designator.

    'Can't Get Enough! - Vocal Club Mix'          -> {'vocal', 'club'}
    'Missing - Todd Terry Remix'                  -> {'todd', 'terry'}
    "Don't Go - Original Mix"                     -> frozenset()   (canonical)
    'Good Life (Remastered)'                      -> frozenset()   (packaging)
    'My Feeling'                                  -> frozenset()

    Only suffix segments that actually contain a mix/edit keyword are
    considered, so 'Love On My Mind (feat. Amanda Wilson)' and
    '(Live at Wembley)' are left to strip_collab / _VERSION_MARKER_CLASSES.
    """
    if not title:
        return frozenset()
    tokens = set()
    for match in _TITLE_SUFFIX_SEGMENT_RE.finditer(title):
        for segment in (g for g in match.groups() if g):
            if not _MIX_KEYWORD_RE.search(segment):
                continue
            for word in re.split(r"[^\w]+", segment.lower()):
                if not word or word in _DESIGNATOR_NOISE or _YEAR_RE.match(word):
                    continue
                tokens.add(word)
    if not tokens or tokens <= _DESIGNATOR_CANONICAL:
        return frozenset()
    return frozenset(tokens)


def _strip_designator_segments(title):
    """Drop the mix/edit designator from a title so the SONG can be compared on
    its own. normalize() removes "(Vocal Club Mix)" but not "- Vocal Club Mix",
    so without this the same mix written in the two styles scores only 0.671 --
    below ACCEPT_THRESHOLD -- while the designator sets are identical. The
    designator itself is compared separately by mix_designator()."""
    if not title:
        return title

    def _drop(match):
        segment = next((g for g in match.groups() if g), "")
        return "" if _MIX_KEYWORD_RE.search(segment) else match.group(0)

    stripped = _TITLE_SUFFIX_SEGMENT_RE.sub(_drop, title).strip()
    return stripped or title


def designator_mismatch(query_title: str, result_title: str) -> bool:
    """True when the two titles name different mixes/edits of the same song —
    including the case where one names a mix and the other names none."""
    return mix_designator(query_title) != mix_designator(result_title)


def version_mismatch(query_title: str, result_title: str) -> bool:
    """True when the two titles carry different recording-class markers —
    e.g. one is an instrumental/karaoke/live/remix and the other is not.
    Symmetric: a query for '(inst)' must not match the vocal version either.
    Also true when the two titles name different mixes/edits (BUG-MIX-1)."""
    if version_markers(query_title) != version_markers(result_title):
        return True
    # Same recording class, different mix (BUG-MIX-1): 'Vocal Club Mix' and
    # 'Extended Mix' are both simply 'not a remix' to the class check.
    return designator_mismatch(query_title, result_title)


# Similarity scores are capped at this value when the recording class differs.
# Below every acceptance threshold in the resolver and all adapters.
VERSION_MISMATCH_CAP = 0.40

# Ceiling for a result whose artist is clearly not the requested one. See the
# comment at the cap site in score_similarity() for why it must stay below 0.55.
WRONG_ARTIST_CAP = 0.50


# Artist names that identify karaoke/tribute/cover acts. A result from one of
# these with a clean title ("God's Plan" by "Piano Tribute Players") carries no
# title marker at all — the artist name is the only signal it's the wrong
# recording. Only applied when the QUERY artists don't match the pattern too.
_TRIBUTE_ARTIST_RE = re.compile(
    r"\b(karaoke|tribute|cover(s| band| version)?|in the style of|made famous|"
    r"vitamin string quartet|rockabye baby|kidz bop|party tyme|ameritz|"
    r"8[- ]?bit arcade|lullaby|music box|sleep baby|piano dreamers)\b",
    re.IGNORECASE,
)


def _is_tribute_artist(result_artist: str, query_artists: list[str]) -> bool:
    if not result_artist or not _TRIBUTE_ARTIST_RE.search(result_artist):
        return False
    # If the requested artist is itself a karaoke/tribute act, it's a match, not a trap.
    return not any(_TRIBUTE_ARTIST_RE.search(a or "") for a in query_artists)


_ARTIST_SPLIT_RE = re.compile(r",|&|/|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|\bwith\b|\bvs\.?\b|\bx\b", re.IGNORECASE)


def _artist_tokens(value: str) -> list[str]:
    """Split a credit string into individual act names, normalized."""
    if not value:
        return []
    parts = [normalize(p) for p in _ARTIST_SPLIT_RE.split(value)]
    return [p for p in parts if p]


def _names_one_inside_the_other(a: str, b: str) -> bool:
    """True when normalized `a` appears inside `b` (or vice versa) on whole-word
    boundaries. Boundaries matter: 'queen' must NOT match 'queens of the stone
    age', and 'air' must not match 'airbourne'."""
    if not a or not b:
        return False
    for needle, haystack in ((a, b), (b, a)):
        if needle == haystack:
            return True
        if re.search(r"(?:^|\s)" + re.escape(needle) + r"(?:\s|$)", haystack):
            return True
    return False


def artist_match_score(query_artists: list, result_artist: str) -> float:
    """0.0-1.0 confidence that `result_artist` names the act in `query_artists`.

    SequenceMatcher over the raw joined credit strings is the single most
    re-patched weakness in this module -- the changelog records it being worked
    around at a CALL SITE four separate times (CARRY-5, CARRY-6 among them)
    because a CORRECT artist buried in a long credit list is underrated by
    character ratio alone. Measured:

      "Arijit Singh" vs "Pritam, Shashwat Singh, Antara Mitra, Arijit Singh &
                         Amitabh Bhattacharya"            -> ratio 0.300
      "Arijit Singh" vs "Vishal & Shekhar, Arijit Singh"   -> ratio 0.615

    The first falls under the wrong-artist cap even though the artist is named
    verbatim, and the crude rescue for it used to be a "title_score >= 0.90 means
    trust the title alone" bypass of that cap -- which is precisely how a
    DIFFERENT band's identically-titled song got through (Fastball's "Boomerang",
    "Knock It Down" and "Seattle" each scored 0.64-0.73 against strangers). An
    exact title match is no evidence at all about the artist, so that bypass
    could never have been sound.

    Splitting the credit into acts and asking "is this act named?" answers the
    buried-artist case directly, so the bypass is no longer needed to carry it.
    """
    result_artist = (result_artist or "").strip()
    if not result_artist:
        return 0.0
    result_norm = normalize(result_artist)
    result_tokens = _artist_tokens(result_artist)
    best = 0.0
    for raw in query_artists or []:
        if not raw:
            continue
        query_norm = normalize(raw)
        if not query_norm:
            continue
        # Named verbatim, as a whole word, in either direction.
        if _names_one_inside_the_other(query_norm, result_norm):
            return 1.0
        for q_tok in _artist_tokens(raw) or [query_norm]:
            for r_tok in result_tokens or [result_norm]:
                if _names_one_inside_the_other(q_tok, r_tok):
                    return 1.0
                best = max(best, string_similarity(q_tok, r_tok))
        best = max(best, string_similarity(query_norm, result_norm))
    return best


def score_similarity(
    query_title: str,
    query_artists: list[str],
    result_title: str,
    result_artist: str,
) -> float:
    # One malformed catalogue item (null title/artist) used to raise out of every
    # adapter's search loop and abort the whole search for that track.
    query_title, result_title, result_artist = (
        _as_text(query_title), _as_text(result_title), _as_text(result_artist))
    query_artists = [_as_text(a) for a in (query_artists or [])]
    # Recording-class gate: normalize() strips parentheticals, so without this
    # an instrumental/karaoke/live/remix version scores identically to the
    # original. Capped below every acceptance threshold, checked on the RAW
    # titles before any normalization. A karaoke/tribute act name on the result
    # artist counts as a version mismatch even when the title itself is clean.
    mismatched_version = version_mismatch(query_title, result_title) or _is_tribute_artist(
        result_artist, query_artists
    )

    def _capped(score: float) -> float:
        return min(score, VERSION_MISMATCH_CAP) if mismatched_version else score

    # Compare the song without its mix/edit designator; the designator is
    # scored separately above via version_mismatch (BUG-MIX-1).
    title_score = string_similarity(
        _strip_designator_segments(query_title),
        _strip_designator_segments(result_title),
    )

    # Artist vs channel name
    artist_score = artist_match_score(query_artists, result_artist)

    # Artist name appearing anywhere in the video title (T-Series, Sony etc.).
    # This is a CONTAINMENT test, not a character ratio. SequenceMatcher between
    # an artist name and an unrelated song title manufactures artist confidence
    # out of coincidental letters -- measured, "Fastball" vs "Seattle" = 0.400,
    # contributing 0.320 and lifting a stranger's track to 0.728, over both
    # FALLBACK_FLOOR (0.60) and ACCEPT_THRESHOLD (0.70). The case this exists for
    # ("Song Name - Artist Name [Official Video]") is containment, so require it.
    # The artist has to appear in the part of the result title that the query
    # title does NOT account for, or the signal is just the title match wearing
    # an artist's hat. An EPONYMOUS song is the case that exposes it: querying
    # "Chicago" by Chicago against a stranger's track also titled "Chicago", the
    # artist name is "in the title" only because the artist IS the title, which
    # lifted best_artist_score to 0.800, skipped the wrong-artist cap entirely and
    # scored the stranger 0.920. Self-titled songs are common (Chicago, Garbage,
    # Bad Company...), so that disabled the cap for a whole class of track.
    # Removing the query title first leaves "Stan - Eminem (Official Video)" minus
    # "Stan" -> " - eminem (official video)", which still names the artist, while
    # "Chicago" minus "Chicago" -> "" names nobody.
    title_artist_score = 0.0
    if result_title:
        residual = normalize(result_title)
        query_title_norm = normalize(query_title)
        if query_title_norm:
            residual = residual.replace(query_title_norm, " ")
        residual = re.sub(r"\s+", " ", residual).strip()
        for a in query_artists or []:
            a_norm = normalize(a or "")
            if a_norm and residual and _names_one_inside_the_other(a_norm, residual):
                title_artist_score = 1.0
                break

    best_artist_score = max(artist_score, title_artist_score * 0.8)

    composite = 0.60 * title_score + 0.40 * best_artist_score

    # Fallback: if title alone is a very strong match, don't let
    # a label channel name kill the result
    if title_score >= 0.55 and composite < 0.35:
        return _capped(title_score * 0.75)

    # Hard cap: if the artist is clearly wrong (score < 0.45), cap the composite
    # below LOSSLESS_ACCEPT_THRESHOLD (0.55) so a perfect title match on a
    # common song title (e.g. "White Christmas") doesn't pull in the wrong artist.
    # Only bypass this when the result has no artist info at all (empty string).
    #
    # There used to be a second exception here: title_score >= 0.90 skipped the
    # cap outright, on the reasoning that such a title is "distinctive enough to
    # trust alone". It is not -- an exact title match says nothing whatever about
    # the artist, and an identically-titled song by a DIFFERENT act is exactly
    # when it fires. Measured 2026-10-06 on Fastball's "Make Your Mama Proud":
    # "Boomerang" vs a stranger's scored 0.673, "Knock It Down" 0.642 and
    # "Seattle" 0.728 -- all over FALLBACK_FLOOR (0.60), and "Seattle" over
    # ACCEPT_THRESHOLD (0.70) too. CARRY-6 recorded the same bypass carrying
    # Queen & David Bowie's compilation over a "Various Artists" query at 0.714
    # and had to add a separate _artist_plausible() guard at that call site.
    # The buried-artist case it was really protecting ("Arijit Singh" inside a
    # five-name credit list) is now handled properly inside artist_match_score(),
    # so the bypass has nothing left to carry and is gone. The web tree never had
    # it; the two trees now agree.
    # WRONG_ARTIST_CAP must be STRICTLY below every acceptance gate a capped
    # result could otherwise clear. Measured 2026-10-06, the gates at or under
    # 0.55 are resolver.LOSSLESS_ACCEPT_THRESHOLD (0.55, compared with >=),
    # qobuz_mirror.MIN_SIMILARITY (0.55) and soulseek.MIN_SIMILARITY (0.55).
    # There was a second, "softer" tier here that capped a title_score >= 0.75
    # match at 0.55 instead -- which is not below 0.55, so a wrong-artist
    # lossless result at exactly the cap passed all three of those gates. Since
    # every other gate (FALLBACK_FLOOR 0.60, LOSSY 0.65, ACCEPT 0.70) sits above
    # both values, admitting wrong-artist lossless results was the tier's ONLY
    # observable effect, so it is gone and the single cap matches the web tree.
    if best_artist_score < 0.45 and result_artist.strip():
        return _capped(min(composite, WRONG_ARTIST_CAP))

    return _capped(composite)


def duration_close(expected_s: float, actual_s: float, tolerance: int = 10) -> bool:
    """Return True if durations are within an adaptive tolerance window.

    Cross-service catalogs often disagree by a few extra seconds because of
    leading silence, trailing fade-outs, regional edits, or metadata rounding.
    A hard 5-second cutoff is too strict for long tracks and DJ mixes, so we
    keep the caller-provided tolerance as a floor and expand it slightly for
    longer recordings.
    """
    try:
        expected = float(expected_s)
        actual = float(actual_s)
    except (TypeError, ValueError):
        return False

    longer = max(expected, actual)
    adaptive_tolerance = min(30.0, longer * 0.045)
    effective_tolerance = max(float(tolerance), adaptive_tolerance)
    return abs(expected - actual) <= effective_tolerance

def has_severe_duration_mismatch(expected_ms, candidate_ms) -> bool:
    """True when a candidate's length is so far from the requested one that it is
    a different recording (a 420 s extended mix offered for a 262 s radio edit).

    The same rule the mirror adapters apply (shorter than 80% and by 20 s or
    more, or longer than 130% and by 45 s or more). Unknown on either side is
    never severe. This is a HARD reject: as a x0.8 score discount it did nothing
    for an exact title match (1.0 x 0.8 == MIN_SIMILARITY, so it still passed).
    """
    try:
        expected_s = float(expected_ms) / 1000.0
        candidate_s = float(candidate_ms) / 1000.0
    except (TypeError, ValueError):
        return False
    if expected_s <= 0 or candidate_s <= 0:
        return False
    shorter = candidate_s < expected_s * 0.8 and (expected_s - candidate_s) >= 20
    longer = candidate_s > expected_s * 1.3 and (candidate_s - expected_s) >= 45
    return shorter or longer


def search_match_score(query: str, artist: str, title: str) -> float:
    """How well a catalogue search hit answers the query. 0.0-1.0 (issue #58).

    Best of "title" and "artist title". Scoring the title alone would rank
    `queen bohemian rhapsody` badly against the song it plainly names; scoring
    the joined string alone would do the same to a bare `bohemian rhapsody`.
    The max of the two handles either spelling.

    This is a display/ranking hint for the search UI only - it never gates a
    download, so it deliberately does NOT use the mix-designator machinery that
    score_similarity applies when deciding whether two recordings are the same.
    """
    q = normalize(query or "")
    best = string_similarity(q, normalize(title or ""))
    if artist:
        best = max(best, string_similarity(q, normalize(f"{artist} {title}")))
    return round(best, 3)

