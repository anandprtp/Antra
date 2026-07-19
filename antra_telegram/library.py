import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

from mutagen import File as MutagenFile

from .models import TrackAsset


SUPPORTED_AUDIO_EXTENSIONS = frozenset(
    {".flac", ".m4a", ".mp3", ".wav", ".ogg", ".opus", ".aac", ".mp4", ".webm"}
)


def normalize_query(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "").casefold()
    return " ".join(re.findall(r"[\w]+", value, flags=re.UNICODE))


def _first_tag(tags, *names: str) -> str:
    if not tags:
        return ""
    for name in names:
        value = tags.get(name)
        if value:
            first = value[0] if isinstance(value, (list, tuple)) else value
            return str(first).strip()
    return ""


def _fallback_metadata(root: Path, path: Path) -> tuple[str, str, str]:
    stem = re.sub(r"^\s*\d+\s*[-_.]\s*", "", path.stem).strip() or path.stem
    artist = ""
    album = ""
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        parts = path.parts

    if len(parts) >= 4 and parts[0].casefold() == "albums":
        artist, album = parts[1], parts[2]
    elif len(parts) >= 3 and parts[0].casefold() == "playlists":
        album = parts[1]
    elif len(parts) >= 3:
        # Current Antra libraries can use Artist / Album / file without an
        # explicit Albums directory. Taking the two parent components also
        # handles an optional category prefix.
        artist, album = parts[-3], parts[-2]
    return stem, artist, album


def inspect_track(root: Path, path: Path) -> TrackAsset:
    title, artist, album = _fallback_metadata(root, path)
    duration: float | None = None
    try:
        audio = MutagenFile(path, easy=True)
        if audio is not None:
            title = _first_tag(audio.tags, "title") or title
            artist = _first_tag(audio.tags, "artist", "albumartist") or artist
            album = _first_tag(audio.tags, "album") or album
            length = getattr(getattr(audio, "info", None), "length", None)
            duration = float(length) if length is not None else None
    except Exception:
        # A filename/path fallback keeps partially tagged libraries searchable.
        pass
    return TrackAsset(path=path.resolve(), title=title, artist=artist, album=album, duration_seconds=duration)


class LibraryIndex:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self._assets: list[TrackAsset] = []

    @property
    def size(self) -> int:
        return len(self._assets)

    def list_assets(self, limit: int = 20) -> list[TrackAsset]:
        return sorted(
            self._assets,
            key=lambda asset: asset.display_name.casefold(),
        )[: max(0, limit)]

    def refresh(self) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        paths = sorted(
            path
            for path in self.root.rglob("*")
            if path.is_file() and path.suffix.casefold() in SUPPORTED_AUDIO_EXTENSIONS
        )
        self._assets = [inspect_track(self.root, path) for path in paths]
        return self.size

    def add_path(self, path: Path) -> TrackAsset:
        resolved = path.expanduser().resolve()
        resolved.relative_to(self.root)
        asset = inspect_track(self.root, resolved)
        self._assets = [existing for existing in self._assets if existing.path != resolved]
        self._assets.append(asset)
        return asset

    def search(
        self,
        query: str,
        limit: int = 5,
        min_score: float = 0.42,
    ) -> list[TrackAsset]:
        normalized = normalize_query(query)
        if not normalized:
            return []
        query_tokens = set(normalized.split())
        scored: list[tuple[float, str, TrackAsset]] = []

        for asset in self._assets:
            title = normalize_query(asset.title)
            primary = normalize_query(f"{asset.artist} {asset.title}")
            searchable = normalize_query(
                f"{asset.artist} {asset.title} {asset.album} {asset.path.stem}"
            )
            searchable_tokens = set(searchable.split())
            overlap = len(query_tokens & searchable_tokens) / max(1, len(query_tokens))
            ratio = max(
                SequenceMatcher(None, normalized, primary).ratio(),
                SequenceMatcher(None, normalized, title).ratio(),
            )
            contains = 1.0 if normalized in searchable else 0.0
            exact = 1.0 if normalized in {title, primary} else 0.0
            score = (0.45 * overlap) + (0.30 * ratio) + (0.15 * contains) + (0.10 * exact)
            if score >= min_score:
                scored.append((score, asset.display_name.casefold(), asset))

        scored.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in scored[:limit]]

    def find_best(self, query: str, min_score: float = 0.42) -> TrackAsset | None:
        matches = self.search(query, limit=1, min_score=min_score)
        return matches[0] if matches else None
