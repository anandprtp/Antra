from __future__ import annotations

import re
from copy import deepcopy
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
]

DEFAULT_FILENAME_PREFERENCES: dict[str, Any] = {
    "single_track_filename_template": "{artist} - {title}",
    "album_zip_name_template": "{album_artist} - {album} ({year})",
    "album_track_filename_template": "{track} - {title}",
    "folder_structure_template": "{album_artist}/{year} - {album}/",
    "multi_disc_handling": "prefix",
    "track_number_padding": 2,
    "illegal_character_replacement": "",
    "whitespace_handling": "preserve",
    "filename_conflict_behavior": "skip",
}

_TOKEN_RE = re.compile(r"\{([a-z_]+)\}", re.IGNORECASE)


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
    merged["filename_conflict_behavior"] = merged.get("filename_conflict_behavior") or "skip"
    return merged


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


def build_context(
    track: Any,
    prefs: dict[str, Any],
    *,
    track_number: Optional[int] = None,
    disc_number: Optional[int] = None,
) -> dict[str, str]:
    album_artist = ""
    album_artists = getattr(track, "album_artists", None) or []
    if album_artists:
        album_artist = ", ".join([str(value) for value in album_artists if value])
    artist = getattr(track, "primary_artist", "") or ""
    genres = getattr(track, "genres", None) or []
    genre = ", ".join([str(value) for value in genres if value])
    quality = _track_quality(track)
    bitrate = _track_bitrate(track)
    codec = _track_codec(track, quality)
    year = getattr(track, "release_year", None)
    disc = disc_number if disc_number is not None else getattr(track, "disc_number", None)
    track_value = track_number if track_number is not None else getattr(track, "track_number", None)

    return {
        "title": str(getattr(track, "title", "") or ""),
        "artist": str(artist or ""),
        "album_artist": str(album_artist or artist or ""),
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
    }


def render_template(
    template: str,
    values: dict[str, Any],
    prefs: Optional[dict[str, Any]] = None,
    *,
    is_path: bool = False,
) -> str:
    prefs = migrate_legacy_templates(prefs or {})
    raw = _TOKEN_RE.sub(lambda m: _stringify(values.get(m.group(1).lower(), "")), template or "")
    collapsed = _collapse_orphaned_separators(raw)
    sanitized = _apply_whitespace_policy(collapsed, prefs.get("whitespace_handling", "preserve"))
    if is_path:
        segments = [sanitize_filename_segment(part, prefs) for part in sanitized.replace("\\", "/").split("/") if part.strip()]
        return "/".join([segment for segment in segments if segment])
    return sanitize_filename_segment(sanitized, prefs)


def sanitize_filename_segment(value: str, prefs: Optional[dict[str, Any]] = None) -> str:
    prefs = migrate_legacy_templates(prefs or {})
    replacement = str(prefs.get("illegal_character_replacement", ""))
    value = value or ""
    value = re.sub(r'[<>:"/\\|?*\x00-\x1F]', replacement, value)
    value = re.sub(r"\s{2,}", " ", value).strip()
    value = value.strip(". ")
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
    if policy == "hyphen":
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
    if mode == "track_only":
        return base
    if mode == "offset":
        lead_disc = disc_number if disc_number and disc_number > 0 else 1
        return f"{lead_disc}{base}"
    multi = (total_discs or 1) > 1
    if multi and disc_number and disc_number > 0:
        if mode in {"prefix", "dash"}:
            return f"{disc_number}-{base}"
    return base


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
