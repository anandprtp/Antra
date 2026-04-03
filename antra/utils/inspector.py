import os
from typing import Any

from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4


def inspect_audio_file(path: str) -> dict[str, Any]:
    ext = os.path.splitext(path)[1].lower()
    base, _ = os.path.splitext(path)

    info: dict[str, Any] = {
        "path": path,
        "exists": os.path.exists(path),
        "ext": ext,
        "embedded_artwork": False,
        "embedded_lyrics": False,
        "synced_lyrics": False,
        "sidecar_lrc": os.path.exists(base + ".lrc"),
        "sidecar_txt": os.path.exists(base + ".txt"),
        "album": None,
        "title": None,
        "artist": None,
        "taggable_format": ext in {".mp3", ".flac", ".m4a", ".mp4"},
    }
    if not info["exists"]:
        return info

    try:
        if ext == ".mp3":
            tags = ID3(path)
            info["embedded_artwork"] = bool(tags.getall("APIC"))
            info["embedded_lyrics"] = bool(tags.getall("USLT"))
            info["synced_lyrics"] = bool(tags.getall("SYLT"))
            title = tags.get("TIT2")
            artist = tags.get("TPE1")
            album = tags.get("TALB")
            info["title"] = title.text[0] if title and getattr(title, "text", None) else None
            info["artist"] = artist.text[0] if artist and getattr(artist, "text", None) else None
            info["album"] = album.text[0] if album and getattr(album, "text", None) else None
        elif ext == ".flac":
            audio = FLAC(path)
            info["embedded_artwork"] = bool(audio.pictures)
            info["embedded_lyrics"] = bool(audio.get("lyrics"))
            info["title"] = _first(audio.get("title"))
            info["artist"] = _first(audio.get("artist"))
            info["album"] = _first(audio.get("album"))
        elif ext in {".m4a", ".mp4"}:
            audio = MP4(path)
            info["embedded_artwork"] = bool(audio.tags.get("covr"))
            info["embedded_lyrics"] = bool(audio.tags.get("\xa9lyr"))
            info["title"] = _first(audio.tags.get("\xa9nam"))
            info["artist"] = _first(audio.tags.get("\xa9ART"))
            info["album"] = _first(audio.tags.get("\xa9alb"))
        else:
            info["warning"] = "Unsupported container for embedded tagging in Antra"
    except Exception as exc:
        info["error"] = str(exc)

    return info


def _first(values):
    if not values:
        return None
    return values[0]
