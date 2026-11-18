from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Any, Optional

AVAILABLE_TEMPLATE_TOKENS = [
    "title",
    "artist",
    "album_artist",
    "album",
    "year",
    "track",
    "disc",
    "genre",
    "composer",
    "isrc",
    "codec",
    "bitrate",
    "quality",
    # ── v2.0.0 DMX-3 ─────────────────────────────────────────────────────────
    # Only tokens a real field can fill. %bpm% and %artist_id% are NOT
    # here: TrackMetadata carries neither and no fetcher populates them, so they
    # would render empty forever. %size% is not here because the output path is
    # allocated after resolve but BEFORE the download, so the size is unknowable
    # at the moment the filename is chosen.
    "upc",
    "label",
    "explicit",
    "track_total",
    "disc_total",
    "date",
    "playlist",
    "position",
    "owner",
    "main_artists",
    "feat_artists",
    "all_artists",
    "today",
    "type",
    "album_id",
    "track_id",
]

# Private key the organizer stamps into its prefs once per run so {today} is
# pinned for the whole run: an album that straddles midnight must not split
# across two dated folders. Absent (a direct caller) → the current local date.
RUN_DATE_KEY = "_run_date"

DEFAULT_FILENAME_PREFERENCES: dict[str, Any] = {
    "single_track_filename_template": "{artist} - {title}",
    "album_zip_name_template": "{album_artist} - {album} ({year})",
    "album_track_filename_template": "{track} - {title}",
    "folder_structure_template": "{album_artist}/{year} - {album}/",
    "multi_disc_handling": "prefix",
    "track_number_padding": 2,
    # "_" matches Config.illegal_character_replacement, the desktop app, and what
    # the web UI has always claimed. This used to default to "" (strip), so new
    # users silently got stripping while the UI showed underscore. Users who
    # explicitly chose "" keep it — migrate_legacy_templates honours an empty
    # string for this one key (see below).
    "illegal_character_replacement": "_",
    "whitespace_handling": "preserve",
    "filename_conflict_behavior": "skip",
    # ── v2.0.0 DMX-2 — text transforms ──────────────────────────────────────
    # OUTPUT ONLY. These never touch the strings the resolver matches on:
    # nothing here mutates TrackMetadata, so `strip_collab`, `score_similarity`
    # and the identity gate keep seeing the catalogue's original title and
    # artists. Applying them upstream would bring back BUG-1's entire class of
    # wrong-audio-correct-metadata failures.
    #
    # Every default is a no-op EXCEPT remove_duplicate_artists, which matches
    # the one deliberate behaviour change — see its note below.
    "title_casing": "nothing",
    "artist_casing": "nothing",
    "remove_album_version": False,
    # Default ON. This is idempotent for any artist list with no
    # repeats, so the population it affects is exactly the tracks whose output
    # is currently wrong ("Drake, Drake - Title"). It is also identity-safe:
    # LibraryOrganizer._artists_canonical_key builds a SET of normalized names,
    # and primary_artist is artists[0], which first-occurrence dedup preserves.
    "remove_duplicate_artists": True,
    "featured_artists_mode": "nothing",
    "multi_artist_separator": "default",
    "album_various_artists": True,
}

_TOKEN_RE = re.compile(r"\{([a-z_]+)\}", re.IGNORECASE)

# v2.0.0 DMX-5 — what to do when the target file already exists.
#   skip           — never re-download; the global identity dedup applies (default)
#   overwrite      — re-download and replace the existing file
#   append_counter — "keep both": write a second copy as "<stem> (2)"
#   tag_only       — re-tag the existing file in place from fresh metadata,
#                    never re-download it
# The spelling "append_counter" is the web's existing value and is kept so the
# two front ends share one vocabulary. Anything unrecognised degrades to
# "skip", the only mode that can never replace or duplicate a file.
CONFLICT_BEHAVIORS = ("skip", "overwrite", "append_counter", "tag_only")
_CONFLICT_ALIASES = {
    "rename": "append_counter",
    "keep_both": "append_counter",
    "tags_only": "tag_only",
    "retag": "tag_only",
}


def normalize_conflict_behavior(value: Any) -> str:
    mode = str(value or "").strip().lower()
    mode = _CONFLICT_ALIASES.get(mode, mode)
    return mode if mode in CONFLICT_BEHAVIORS else "skip"


def migrate_legacy_templates(
    prefs: Optional[dict[str, Any]] = None,
    *,
    filename_format: str = "default",
    album_folder_structure: str = "standard",
) -> dict[str, Any]:
    merged = deepcopy(DEFAULT_FILENAME_PREFERENCES)
    if prefs:
        for key, value in prefs.items():
            if value is None:
                continue
            if value == "" and key != "illegal_character_replacement":
                continue
            merged[key] = value

    if not prefs or not prefs.get("single_track_filename_template"):
        merged["single_track_filename_template"] = _legacy_single_track_template(filename_format)
    if not prefs or not prefs.get("album_track_filename_template"):
        merged["album_track_filename_template"] = _legacy_album_track_template(filename_format)
    if not prefs or not prefs.get("folder_structure_template"):
        merged["folder_structure_template"] = _legacy_folder_template(album_folder_structure)

    try:
        merged["track_number_padding"] = max(1, int(merged.get("track_number_padding", 2)))
    except Exception:
        merged["track_number_padding"] = 2

    merged["multi_disc_handling"] = merged.get("multi_disc_handling") or "prefix"
    merged["illegal_character_replacement"] = str(merged.get("illegal_character_replacement", ""))
    merged["whitespace_handling"] = merged.get("whitespace_handling") or "preserve"
    merged["filename_conflict_behavior"] = normalize_conflict_behavior(
        merged.get("filename_conflict_behavior")
    )

    # v2.0.0 DMX-2 — clamp every transform to a known value here rather than at
    # each use site, so an unrecognised string from a newer build degrades to
    # the no-op instead of reaching a regex or a casing branch.
    for _key in ("title_casing", "artist_casing"):
        _mode = str(merged.get(_key, "nothing") or "nothing").strip().lower()
        merged[_key] = _mode if _mode in CASING_MODES else "nothing"
    _feat = str(merged.get("featured_artists_mode", "nothing") or "nothing").strip().lower()
    merged["featured_artists_mode"] = _feat if _feat in FEATURED_MODES else "nothing"
    _sep = str(merged.get("multi_artist_separator", "default") or "default").strip().lower()
    merged["multi_artist_separator"] = _sep if _sep in MULTI_ARTIST_SEPARATORS else "default"
    merged["remove_album_version"] = bool(merged.get("remove_album_version", False))
    merged["remove_duplicate_artists"] = bool(merged.get("remove_duplicate_artists", True))
    merged["album_various_artists"] = bool(merged.get("album_various_artists", True))
    return merged


# ── v2.0.0 DMX-2 — text transforms ───────────────────────────────────────────

# "nothing" is first and is the default everywhere: catalogue casing is
# inconsistent between services, but rewriting it is a taste decision the user
# has to opt into, not something to do to an existing library on upgrade.
CASING_MODES: tuple[str, ...] = ("nothing", "lower", "upper", "start", "sentence")

# "nothing" leaves the title alone. "remove" strips a feat. credit that is
# already in the title. "move" is the useful one: it takes the artists BEYOND
# the primary out of the artist tag and puts them in the title instead, which
# is what people who ask for this actually want (one artist per album folder).
FEATURED_MODES: tuple[str, ...] = ("nothing", "remove", "move")

# "default" keeps today's behaviour EXACTLY: FLAC and MP4 get a genuine
# multi-value field (which is the correct representation and what most modern
# scanners prefer) while MP3 gets ", ".join because ID3v2.3 has no clean
# multi-value text. Any other value collapses all three to one joined string.
#
# A `useNullSeparator` option is deliberately NOT offered, for the reason
# recorded under DMX-1: multi-value ID3 text frames only carry a null
# separator in v2.4, and this tagger writes v2.3 ON PURPOSE so Windows
# Explorer and mobile players can read the APIC cover frame. A switch that
# silently breaks cover-art reading is worse than no switch.
MULTI_ARTIST_SEPARATORS: dict[str, str] = {
    "comma": ", ",
    "semicolon": "; ",
    "slash": "/",
}

# Narrow on purpose: "Album Version" only. The broader recording-class markers
# (Live, Remix, Instrumental, ...) are load-bearing for matching — antra's
# version_mismatch() rejects a wrong recording on exactly those words — so
# stripping them from output would make a library where "Song" and
# "Song (Live)" are indistinguishable, so the rule stays this narrow.
_ALBUM_VERSION_RE = re.compile(
    r"""\s*(?:
          [\(\[]\s*album\s+(?:version|ver\.?)\s*[\)\]]   # (Album Version)
        | -\s*album\s+(?:version|ver\.?)\s*$                # - Album Version
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Bracketed form, which is what catalogues overwhelmingly use, plus the
# unbracketed trailing form. Captures the names so `move` can rebuild them.
_FEAT_BRACKETED_RE = re.compile(
    r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+([^\)\]]+?)\s*[\)\]]",
    re.IGNORECASE,
)
_FEAT_TRAILING_RE = re.compile(
    r"\s+(?:feat\.?|ft\.?|featuring)\s+(.+?)\s*$",
    re.IGNORECASE,
)

_WORD_START_RE = re.compile(r"(^|[\s\-\(\[\{/&_\u2013\u2014])([^\W\d_])", re.UNICODE)


def apply_casing(text: str, mode: str) -> str:
    """Recase `text`. An unrecognised mode is a no-op, never a crash."""
    value = text or ""
    mode = (mode or "nothing").strip().lower()
    if not value or mode in ("", "nothing") or mode not in CASING_MODES:
        return value
    if mode == "lower":
        return value.lower()
    if mode == "upper":
        return value.upper()
    if mode == "sentence":
        # Capitalise the FIRST CHARACTER only, and only when it is a letter.
        # An earlier cut walked forward to the first letter anywhere, which
        # turned "2 OF AMERIKAZ MOST WANTED" into "2 Of amerikaz most wanted"
        # — the second word capitalised, reading as a proper noun. A title
        # that starts with a digit or a bracket simply has no first word to
        # capitalise, which is what a word processor does too.
        lowered = value.lower()
        if lowered[:1].isalpha():
            return lowered[:1].upper() + lowered[1:]
        return lowered
    # "start" — title case. str.title() is unusable here: it yields "Don'T" and
    # "Rock'N'Roll" because it treats an apostrophe as a word boundary.
    lowered = value.lower()
    return _WORD_START_RE.sub(lambda m: m.group(1) + m.group(2).upper(), lowered)


def strip_album_version(title: str) -> str:
    value = title or ""
    for _ in range(3):
        stripped = _ALBUM_VERSION_RE.sub("", value).strip()
        if stripped == value:
            break
        value = stripped
    return value or (title or "")


def dedupe_artists(artists: Any) -> list[str]:
    """First-occurrence dedup, case- and whitespace-insensitive.

    Deliberately NOT a substring pass, which would also drop an artist whose
    name is contained in another: that would turn ["Drake", "Drake & Future"]
    into ["Drake"] and silently lose a credited artist.
    """
    seen: set[str] = set()
    out: list[str] = []
    for artist in artists or []:
        text = str(artist or "").strip()
        if not text:
            continue
        key = re.sub(r"\s+", " ", text).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def split_featured(title: str) -> tuple[str, list[str]]:
    """Split a title into (base, [featured artists]) without losing anything.

    Returns the title unchanged and an empty list when there is no credit, so
    the caller can tell "no feat." from "feat. nobody".
    """
    value = title or ""
    names: list[str] = []

    def _take(match: "re.Match[str]") -> str:
        names.append(match.group(1).strip())
        return ""

    base = _FEAT_BRACKETED_RE.sub(_take, value)
    if not names:
        trailing = _FEAT_TRAILING_RE.search(base)
        if trailing:
            names.append(trailing.group(1).strip())
            base = base[: trailing.start()]
    base = re.sub(r"\s{2,}", " ", base).strip().rstrip("-,&/ ").strip()
    parts: list[str] = []
    for name in names:
        for piece in re.split(r"\s*(?:,|&|\band\b|\+)\s*", name):
            piece = piece.strip()
            if piece:
                parts.append(piece)
    return (base or value), parts


def _format_featured(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " & " + names[-1]


def artist_separator(prefs: Optional[dict[str, Any]]) -> Optional[str]:
    """The chosen join string, or None for "keep the native representation"."""
    mode = str((prefs or {}).get("multi_artist_separator", "default") or "default").strip().lower()
    return MULTI_ARTIST_SEPARATORS.get(mode)


def resolve_various_artists(
    album_artist: str,
    artists: list[str],
    prefs: Optional[dict[str, Any]] = None,
) -> str:
    """albumVariousArtists OFF: file a compilation under the track's own artist.

    ON (the default) keeps today's behaviour untouched. Shared by the tag path
    and the filename path so the two can never disagree about which folder a
    Various Artists compilation lands in.
    """
    if (prefs or {}).get("album_various_artists", True):
        return album_artist
    if re.sub(r"\s+", " ", album_artist or "").strip().lower() != "various artists":
        return album_artist
    return artists[0] if artists else album_artist


@dataclass
class TransformedText:
    """Output-only strings derived from a track. TrackMetadata is untouched."""

    title: str = ""
    artists: list[str] = field(default_factory=list)
    album_artists: list[str] = field(default_factory=list)
    # For the containers that support a real multi-value field (FLAC, MP4).
    artist_values: list[str] = field(default_factory=list)
    # For ID3 and anywhere a single string is required.
    artist_joined: str = ""
    album_artist: str = ""


def transform_text(track: Any, prefs: Optional[dict[str, Any]] = None) -> TransformedText:
    """Apply the DMX-2 transforms and return the strings to WRITE.

    Order matters and is chosen so each step sees a clean input: feat. handling
    first (so the base title is what the version strip and the casing act on),
    then the album-version strip, then casing last so it applies to the final
    text rather than to an intermediate that later gets rewritten.
    """
    prefs = prefs or {}
    raw_title = str(getattr(track, "title", "") or "")
    artists = [str(a) for a in (getattr(track, "artists", None) or []) if str(a).strip()]
    album_artists = [str(a) for a in (getattr(track, "album_artists", None) or []) if str(a).strip()]

    if prefs.get("remove_duplicate_artists", True):
        artists = dedupe_artists(artists)
        album_artists = dedupe_artists(album_artists)

    title = raw_title
    feat_mode = str(prefs.get("featured_artists_mode", "nothing") or "nothing").strip().lower()
    if feat_mode in ("remove", "move"):
        base, in_title = split_featured(raw_title)
        if feat_mode == "remove":
            title = base
        else:
            # Everything past the primary artist becomes a title credit, merged
            # with any credit the title already carried so a track tagged both
            # ways does not end up with the same name twice.
            extra = list(artists[1:])
            merged: list[str] = []
            seen: set[str] = set()
            for name in in_title + extra:
                key = re.sub(r"\s+", " ", name).strip().lower()
                if key and key not in seen:
                    seen.add(key)
                    merged.append(name.strip())
            title = f"{base} (feat. {_format_featured(merged)})" if merged else base
            if artists:
                artists = [artists[0]]

    if prefs.get("remove_album_version", False):
        title = strip_album_version(title)

    title_case = str(prefs.get("title_casing", "nothing") or "nothing")
    artist_case = str(prefs.get("artist_casing", "nothing") or "nothing")
    title = apply_casing(title, title_case)
    artists = [apply_casing(a, artist_case) for a in artists]
    album_artists = [apply_casing(a, artist_case) for a in album_artists]

    separator = artist_separator(prefs)
    artist_values = [separator.join(artists)] if (separator and artists) else list(artists)
    artist_joined = (separator or ", ").join(artists)

    if album_artists:
        album_artist = (separator or ", ").join(album_artists)
    else:
        album_artist = artists[0] if artists else str(getattr(track, "primary_artist", "") or "")

    album_artist = resolve_various_artists(album_artist, artists, prefs)

    return TransformedText(
        title=title,
        artists=artists,
        album_artists=album_artists,
        artist_values=artist_values,
        artist_joined=artist_joined,
        album_artist=album_artist,
    )


def build_web_preview_context() -> dict[str, str]:
    return {
        "title": "Come Together",
        "artist": "The Beatles",
        "album_artist": "The Beatles",
        "album": "Abbey Road",
        "year": "1969",
        "track": "07",
        "disc": "1",
        "genre": "Rock",
        "composer": "",
        "isrc": "GBXZZ1969007",
        "codec": "FLAC",
        "bitrate": "24-bit / 96 kHz",
        "quality": "Lossless",
        "upc": "0602547670342",
        "label": "Apple Corps",
        "explicit": "",
        "track_total": "17",
        "disc_total": "1",
        "date": "1969-09-26",
        "playlist": "",
        "position": "",
        "owner": "",
        "main_artists": "The Beatles",
        "feat_artists": "",
        "all_artists": "The Beatles",
        "today": _date.today().isoformat(),
        "type": "album",
        "album_id": "0ETFjACtuP2ADo6LFhL6HN",
        "track_id": "2EqlS6tkEnglzr7tkKAAYD",
    }


def build_single_track_stem(track: Any, prefs: dict[str, Any]) -> str:
    return render_template(
        prefs.get("single_track_filename_template") or DEFAULT_FILENAME_PREFERENCES["single_track_filename_template"],
        build_context(track, prefs, track_number=track.track_number, disc_number=track.disc_number),
        prefs,
    )


def build_track_stem(track: Any, prefs: dict[str, Any], *, track_number: Optional[int] = None, disc_number: Optional[int] = None) -> str:
    return render_template(
        prefs.get("album_track_filename_template") or DEFAULT_FILENAME_PREFERENCES["album_track_filename_template"],
        build_context(track, prefs, track_number=track_number, disc_number=disc_number),
        prefs,
    )


def build_album_zip_name(track: Any, prefs: dict[str, Any]) -> str:
    return render_template(
        prefs.get("album_zip_name_template") or DEFAULT_FILENAME_PREFERENCES["album_zip_name_template"],
        build_context(track, prefs, track_number=track.track_number, disc_number=track.disc_number),
        prefs,
    )


def build_folder_path(track: Any, prefs: dict[str, Any]) -> str:
    rendered = render_template(
        prefs.get("folder_structure_template") or DEFAULT_FILENAME_PREFERENCES["folder_structure_template"],
        build_context(track, prefs, track_number=track.track_number, disc_number=track.disc_number),
        prefs,
        is_path=True,
    )
    normalized = rendered.replace("\\", "/").strip("/")
    return normalized


def _collapse_artist_list(artists: list[str], max_len: int = 60) -> str:
    """Join artists into a string, collapsing to 'A, B & N others' if too long."""
    joined = ", ".join(str(a) for a in artists if a)
    if len(joined) <= max_len or len(artists) <= 2:
        return joined
    # Try dropping artists from the end until we fit
    for keep in range(len(artists) - 1, 1, -1):
        others = len(artists) - keep
        candidate = ", ".join(str(a) for a in artists[:keep]) + f" & {others} others"
        if len(candidate) <= max_len:
            return candidate
    # Fallback: first artist only + others count
    return str(artists[0]) + f" & {len(artists) - 1} others"


def build_context(
    track: Any,
    prefs: dict[str, Any],
    *,
    track_number: Optional[int] = None,
    disc_number: Optional[int] = None,
) -> dict[str, str]:
    # v2.0.0 DMX-2 — filename tokens are rendered from the TRANSFORMED strings.
    # transform_text never mutates the track, so the resolver and the identity
    # gate still see the catalogue's original title and artists.
    tx = transform_text(track, prefs)
    album_artist = _collapse_artist_list(tx.album_artists) if tx.album_artists else ""
    if len(tx.artists) > 1:
        artist = _collapse_artist_list(tx.artists)
    elif tx.artists:
        artist = tx.artists[0]
    else:
        artist = getattr(track, "primary_artist", "") or ""
    genres = getattr(track, "genres", None) or []
    genre = ", ".join([str(value) for value in genres if value])
    quality = _track_quality(track)
    bitrate = _track_bitrate(track)
    codec = _track_codec(track, quality)
    year = getattr(track, "release_year", None)
    disc = disc_number if disc_number is not None else getattr(track, "disc_number", None)
    track_value = track_number if track_number is not None else getattr(track, "track_number", None)

    # The `or artist or ""` fallback is exactly what it was before DMX-2: with
    # no album artist, {album_artist} renders the full (collapsed) track artist
    # list, NOT just the primary. Changing that would rewrite the default
    # folder template for every multi-artist track.
    album_artist_token = resolve_various_artists(
        album_artist or artist or "", tx.artists, prefs
    )
    return {
        "title": str(tx.title or ""),
        "artist": str(artist or ""),
        "album_artist": str(album_artist_token),
        "album": str(getattr(track, "album", "") or ""),
        "year": str(year or ""),
        "track": _format_track_token(
            track_value,
            disc,
            getattr(track, "total_discs", None),
            prefs,
        ),
        "disc": str(disc or ""),
        "genre": genre,
        "composer": str(getattr(track, "composer", "") or ""),
        "isrc": str(getattr(track, "isrc", "") or ""),
        "codec": codec,
        "bitrate": bitrate,
        "quality": quality,
        **_dmx3_tokens(track, prefs, tx),
    }


# ── v2.0.0 DMX-3 — additional tokens ─────────────────────────────────────────

_ISO_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?")

# Which platform id {track_id} means depends on where the track came from: an
# Apple URL's track is identified by its Apple id, not by a Spotify id that an
# enrichment pass may have attached later.
_TRACK_ID_BY_SERVICE: dict[str, str] = {
    "spotify": "spotify_id",
    "apple": "apple_music_id",
    "tidal": "tidal_track_id",
    "deezer": "deezer_track_id",
    "amazon": "amazon_asin",
}


def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip().lower()


def _format_date_token(track: Any) -> str:
    """ISO only, cut to the precision the catalogue actually has.

    Deliberately ignores DMX-1's tag `date_format`: dd/mm/yyyy would put slashes
    (path separators) in a filename, and ISO is the only form that sorts. A
    timestamp such as "2024-04-05T07:00:00Z" is cut to its date — colons are
    illegal in Windows filenames anyway.
    """
    raw = str(getattr(track, "release_date", "") or "").strip()
    match = _ISO_DATE_RE.match(raw)
    if match:
        return "-".join(part for part in match.groups() if part)
    year = getattr(track, "release_year", None)
    return str(year) if year else ""


def _split_artist_roles(tx: "TransformedText") -> tuple[list[str], list[str], list[str]]:
    """(main, featured, all) with NO invented roles.

    Antra has no Main/Featured role data (that would need a per-service API). The
    only honest signal is a credit in the title: names there are featured, the
    remaining track artists are main. A title with no credit yields every artist
    as main and no featured — never a guess from list position.
    """
    _base, featured = split_featured(tx.title)
    feat_keys = {_norm_name(n) for n in featured}
    main = [a for a in tx.artists if _norm_name(a) not in feat_keys]
    everyone: list[str] = []
    seen: set[str] = set()
    for name in list(tx.artists) + featured:
        key = _norm_name(name)
        if key and key not in seen:
            seen.add(key)
            everyone.append(name)
    return main, featured, everyone


def _track_id_token(track: Any) -> str:
    service = str(getattr(track, "source_service", "") or "").strip().lower()
    attr = _TRACK_ID_BY_SERVICE.get(service)
    if attr:
        value = getattr(track, attr, None)
        if value:
            return str(value)
    for attr in ("spotify_id", "apple_music_id", "tidal_track_id", "deezer_track_id", "amazon_asin"):
        value = getattr(track, attr, None)
        if value:
            return str(value)
    return ""


def _dmx3_tokens(track: Any, prefs: dict[str, Any], tx: "TransformedText") -> dict[str, str]:
    main, featured, everyone = _split_artist_roles(tx)

    explicit = getattr(track, "is_explicit", None)
    # Tri-state, as FR-3 renders it: unknown is empty, never "Clean".
    explicit_token = "Explicit" if explicit is True else ("Clean" if explicit is False else "")

    position = getattr(track, "playlist_position", None)
    try:
        width = max(1, int(prefs.get("track_number_padding", 2)))
    except Exception:
        width = 2
    position_token = str(position).zfill(width) if position else ""

    if getattr(track, "playlist_name", None):
        kind = "playlist"
    else:
        kind = str(getattr(track, "request_kind", "") or "").strip().lower()

    return {
        "upc": str(getattr(track, "upc", "") or ""),
        "label": str(getattr(track, "label", "") or ""),
        "explicit": explicit_token,
        "track_total": str(getattr(track, "total_tracks", None) or ""),
        "disc_total": str(getattr(track, "total_discs", None) or ""),
        "date": _format_date_token(track),
        "playlist": str(getattr(track, "playlist_name", "") or ""),
        "position": position_token,
        "owner": str(getattr(track, "playlist_owner", "") or ""),
        "main_artists": _collapse_artist_list(main) if main else "",
        "feat_artists": _collapse_artist_list(featured) if featured else "",
        "all_artists": _collapse_artist_list(everyone) if everyone else "",
        "today": str(prefs.get(RUN_DATE_KEY) or _date.today().isoformat()),
        "type": kind,
        "album_id": str(getattr(track, "album_id", "") or ""),
        "track_id": _track_id_token(track),
    }


def render_template(
    template: str,
    values: dict[str, Any],
    prefs: Optional[dict[str, Any]] = None,
    *,
    is_path: bool = False,
) -> str:
    prefs = migrate_legacy_templates(prefs or {})
    if is_path:
        # Pre-sanitize token values so that slashes *within* values (e.g. an album
        # title like "The United States Of Mind / Phase 2") don't get interpreted as
        # path separators when the rendered string is later split on "/".
        replacement = str(prefs.get("illegal_character_replacement", ""))
        safe_values = {
            k: _stringify(v).replace("/", replacement).replace("\\", replacement)
            for k, v in values.items()
        }
        raw = _TOKEN_RE.sub(lambda m: safe_values.get(m.group(1).lower(), ""), template or "")
    else:
        raw = _TOKEN_RE.sub(lambda m: _stringify(values.get(m.group(1).lower(), "")), template or "")
    collapsed = _collapse_orphaned_separators(raw)
    sanitized = _apply_whitespace_policy(collapsed, prefs.get("whitespace_handling", "preserve"))
    if is_path:
        segments = [sanitize_filename_segment(part, prefs, max_len=120) for part in sanitized.replace("\\", "/").split("/") if part.strip()]
        return "/".join([segment for segment in segments if segment])
    return sanitize_filename_segment(sanitized, prefs, max_len=180)


def sanitize_filename_segment(value: str, prefs: Optional[dict[str, Any]] = None, max_len: Optional[int] = None) -> str:
    prefs = migrate_legacy_templates(prefs or {})
    replacement = str(prefs.get("illegal_character_replacement", ""))
    value = value or ""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', replacement, value)
    value = re.sub(r"\s{2,}", " ", value).strip()
    value = value.strip(". ")
    if max_len and len(value) > max_len:
        value = value[:max_len].rstrip(". ")
    return value or "Unknown"


def _collapse_orphaned_separators(text: str) -> str:
    value = text or ""
    for _ in range(6):
        prev = value
        value = re.sub(r"\(\s*\)", "", value)
        value = re.sub(r"\[\s*\]", "", value)
        value = re.sub(r"\{\s*\}", "", value)
        value = re.sub(r"\s*-\s*-\s*", " - ", value)
        value = re.sub(r"\s*/\s*/\s*", " / ", value)
        value = re.sub(r"\s*\|\s*\|\s*", " | ", value)
        value = re.sub(r"\s{2,}", " ", value)
        value = value.replace("( ", "(").replace(" )", ")")
        value = value.replace("[ ", "[").replace(" ]", "]")
        value = value.replace("{ ", "{").replace(" }", "}")
        value = re.sub(r"^\s*[-/|_,.]+\s*", "", value)
        value = re.sub(r"\s*[-/|_,.]+\s*$", "", value)
        value = value.strip()
        if value == prev:
            break
    return value


def _apply_whitespace_policy(value: str, policy: str) -> str:
    policy = (policy or "preserve").lower()
    if policy == "underscore":
        return value.replace(" ", "_")
    # "dash" is what the desktop Folder Structure modal has always SENT for its
    # "Replace with -" option, while only "hyphen" was accepted here — so that
    # option silently did nothing. Both spellings are honoured rather than
    # renaming one side, because a config.json already carrying "dash" must
    # start working, not keep being ignored.
    if policy in ("hyphen", "dash"):
        return value.replace(" ", "-")
    return value


def _format_track_token(
    track_number: Optional[int],
    disc_number: Optional[int],
    total_discs: Optional[int],
    prefs: dict[str, Any],
) -> str:
    if not track_number:
        return ""
    try:
        width = max(1, int(prefs.get("track_number_padding", 2)))
    except Exception:
        width = 2
    base = str(track_number).zfill(width)
    mode = (prefs.get("multi_disc_handling") or "prefix").lower()
    # "folder" (v2.0.0 DMX-4): the disc lives in a CD<n>/ subfolder, so the
    # number in the filename is the plain track number.
    if mode in ("track_only", "folder"):
        return base
    if mode == "offset":
        lead_disc = disc_number if disc_number and disc_number > 0 else 1
        return f"{lead_disc}{base}"
    multi = (total_discs or 1) > 1
    if multi and disc_number and disc_number > 0:
        if mode in {"prefix", "dash"}:
            return f"{disc_number}-{base}"
    return base


_DISC_FOLDER_RE = re.compile(r"^CD\d+$")


def disc_folder_name(track: Any, prefs: Optional[dict[str, Any]]) -> Optional[str]:
    """The per-disc subfolder a track belongs in, or None (v2.0.0 DMX-4).

    Only in ``multi_disc_handling: folder`` and only on a release that really
    has more than one disc — a single-disc album never gets a lone ``CD1/``.
    ``disc_number >= 2`` also counts when the total is unknown, so a disc-2
    track can never land beside disc 1 with a colliding track number.
    """
    if str((prefs or {}).get("multi_disc_handling") or "prefix").lower() != "folder":
        return None
    try:
        disc = int(getattr(track, "disc_number", None) or 0)
    except (TypeError, ValueError):
        disc = 0
    try:
        total = int(getattr(track, "total_discs", None) or 0)
    except (TypeError, ValueError):
        total = 0
    if total <= 1 and disc < 2:
        return None
    return f"CD{disc if disc > 0 else 1}"


def is_disc_folder_name(name: str) -> bool:
    """True for a folder name disc_folder_name() can produce."""
    return bool(_DISC_FOLDER_RE.match(name or ""))


def _track_quality(track: Any) -> str:
    bit_depth = getattr(track, "bit_depth", None)
    sample_rate_hz = getattr(track, "sample_rate_hz", None)
    quality_kbps = getattr(track, "quality_kbps", None)
    if bit_depth and sample_rate_hz:
        return f"{bit_depth}-bit / {int(sample_rate_hz) // 1000} kHz"
    if quality_kbps:
        return f"{quality_kbps} kbps"
    return ""


def _track_bitrate(track: Any) -> str:
    return _track_quality(track)


def _track_codec(track: Any, quality: str) -> str:
    ext = str(getattr(track, "output_extension", "") or "").lstrip(".")
    if ext:
        return ext.upper()
    if "ALAC" in quality.upper():
        return "ALAC"
    return "FLAC"


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _legacy_single_track_template(filename_format: str) -> str:
    mapping = {
        "title_only": "{title}",
        "artist_title": "{artist} - {title}",
        "title_artist": "{title} - {artist}",
    }
    return mapping.get(filename_format or "default", "{track} - {title}")


def _legacy_album_track_template(filename_format: str) -> str:
    mapping = {
        "title_only": "{title}",
        "artist_title": "{track} - {artist} - {title}",
        "title_artist": "{track} - {title} - {artist}",
    }
    return mapping.get(filename_format or "default", "{track} - {title}")


def _legacy_folder_template(album_folder_structure: str) -> str:
    mode = (album_folder_structure or "standard").lower()
    if mode == "flat":
        return "{album} ({year})/"
    if mode == "year_prefix":
        return "{album_artist}/({year}) {album}/"
    return "{album_artist}/{album} ({year})/"
