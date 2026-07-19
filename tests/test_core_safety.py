import pytest
import concurrent.futures
import json
from urllib.parse import urlsplit

from antra.core import endpoint_manifest
from antra.core import metadata_enricher
from antra.core.amazon_music_fetcher import is_amazon_music_url
from antra.core.apple_fetcher import is_apple_music_url
from antra.core.models import AudioFormat, SearchResult, TrackMetadata
from antra.core.resolver import SourceResolver
from antra.core.service import AntraService, RuntimeOptions
from antra.core.soundcloud_fetcher import is_soundcloud_url
from antra.core.youtube_music_fetcher import is_youtube_music_url
from antra.utils.organizer import LibraryOrganizer


@pytest.mark.parametrize(
    ("checker", "valid"),
    [
        (is_youtube_music_url, "https://music.youtube.com/watch?v=abc123"),
        (is_apple_music_url, "https://music.apple.com/us/album/name/123"),
        (is_soundcloud_url, "https://soundcloud.com/artist/track"),
        (is_amazon_music_url, "https://music.amazon.co.uk/albums/ABC123"),
    ],
)
def test_music_url_checks_accept_only_the_intended_host(checker, valid):
    assert checker(valid)
    assert not checker(f"http://127.0.0.1/internal?next={valid}")
    assert not checker(f"https://user:password@{valid.split('://', 1)[1]}")
    parsed = urlsplit(valid)
    assert not checker(
        f"https://{parsed.hostname}.evil.example{parsed.path}"
        f"?{parsed.query}",
    )


@pytest.mark.parametrize(
    "value",
    [
        "javascript:music.youtube.com/watch?v=abc123",
        "//music.youtube.com/watch?v=abc123",
        "not a URL containing music.youtube.com",
    ],
)
def test_youtube_music_url_rejects_non_http_inputs(value):
    assert not is_youtube_music_url(value)


def test_library_identity_preserves_non_latin_titles_and_artists(tmp_path):
    organizer = LibraryOrganizer(str(tmp_path))

    cyrillic = organizer._track_identity_keys(
        TrackMetadata("Плакала", ["Казка"], "Карма"),
    )
    japanese = organizer._track_identity_keys(
        TrackMetadata("夜に駆ける", ["ヨアソビ"], "The Book"),
    )

    assert any("плакала" in key and "казка" in key for key in cyrillic)
    assert any("夜に駆ける" in key and "ヨアソビ" in key for key in japanese)


class _Adapter:
    always_lossy = False
    is_last_resort = False

    def __init__(self, name: str, score: float, priority: int = 1):
        self.name = name
        self.priority = priority
        self.score = score

    def is_available(self):
        return True

    def search(self, track):
        return SearchResult(
            source=self.name,
            title=track.title,
            artists=track.artists,
            album=track.album,
            duration_ms=track.duration_ms,
            audio_format=AudioFormat.FLAC,
            quality_kbps=None,
            is_lossless=True,
            download_url="https://audio.example/track",
            stream_id="track",
            similarity_score=self.score,
        )


def test_preserved_resolver_order_still_moves_cooling_sources_last():
    cooling = _Adapter("cooling", 0.9)
    healthy = _Adapter("healthy", 0.9)
    resolver = SourceResolver(
        [cooling, healthy],
        preserve_input_order=True,
    )
    resolver._mark_rate_limited("cooling", cooldown_seconds=60)

    assert [
        adapter.name for adapter in resolver._build_resolve_order(set())
    ] == ["healthy", "cooling"]


def test_resolver_rejects_best_result_below_acceptance_threshold():
    resolver = SourceResolver(
        [_Adapter("weak", 0.4)],
        preferred_output_format="mp3",
    )
    track = TrackMetadata(
        "Common title",
        ["Correct artist"],
        "Album",
        duration_ms=180_000,
    )

    assert resolver.resolve(track) is None


def test_album_source_affinity_learns_success_and_forgets_failure():
    first = _Adapter("first", 0.9)
    second = _Adapter("second", 0.9)
    resolver = SourceResolver([first, second])
    track = TrackMetadata(
        "Song",
        ["Artist"],
        "Album",
        album_id="album-1",
    )
    result = first.search(track)

    resolver.record_album_source_success(
        track,
        "first",
        result,
        actual_bit_depth=24,
    )

    assert resolver._preferred_album_adapter_name(track, set()) == "first"
    assert resolver._album_adapter_proven_hires(track, "first")

    resolver.record_album_source_failure(track, "first")
    assert resolver._preferred_album_adapter_name(track, set()) is None


def test_exclusive_youtube_build_skips_remote_endpoint_discovery(monkeypatch):
    def unexpected_call(*args, **kwargs):
        raise AssertionError("exclusive YouTube must not fetch provider manifests")

    monkeypatch.setattr(
        "antra.core.endpoint_manifest.load_endpoint_manifest",
        unexpected_call,
    )
    monkeypatch.setattr(
        "antra.core.service._fetch_gist_apple_mirror",
        unexpected_call,
    )
    service = AntraService()
    config = service.build_runtime_config(
        RuntimeOptions(
            source_preference="youtube",
            source_exclusive=True,
        )
    )

    adapters = service.build_adapters(config)

    assert [adapter.name for adapter in adapters] == ["youtube"]


def test_endpoint_manifest_is_process_cached_and_written_private(
    monkeypatch,
    tmp_path,
):
    calls = 0

    def fetch(_url):
        nonlocal calls
        calls += 1
        return {"hifi": ["https://hifi.example"], "api_key": "secret"}

    monkeypatch.setattr(endpoint_manifest, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(
        endpoint_manifest,
        "_CACHE_PATH",
        tmp_path / "endpoint_manifest_cache.json",
    )
    monkeypatch.setattr(endpoint_manifest, "_fetch_remote_manifest", fetch)
    endpoint_manifest._PROCESS_CACHE.clear()

    first = endpoint_manifest.load_endpoint_manifest("https://manifest.example")
    second = endpoint_manifest.load_endpoint_manifest("https://manifest.example")

    assert first is second
    assert calls == 1
    assert endpoint_manifest._CACHE_PATH.stat().st_mode & 0o777 == 0o600


def test_concurrent_organizer_state_updates_are_atomic(tmp_path):
    organizer = LibraryOrganizer(str(tmp_path))
    tracks = [
        TrackMetadata(f"Песня {index}", ["Артист"], "Альбом")
        for index in range(20)
    ]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda item: organizer.mark_downloaded(
                    item[1],
                    str(tmp_path / f"{item[0]}.mp3"),
                ),
                enumerate(tracks),
            )
        )

    state = json.loads((tmp_path / ".antra_state.json").read_text("utf-8"))
    assert len(state) >= len(tracks)


def test_metadata_enrichment_honors_lyrics_policy_and_fetches_once(monkeypatch):
    for name in (
        "_enrich_from_deezer",
        "_enrich_from_itunes",
        "_enrich_from_musicbrainz",
        "_upgrade_artwork",
    ):
        monkeypatch.setattr(
            metadata_enricher,
            name,
            lambda *args, **kwargs: None,
        )
    metadata_enricher._enrich_cache.clear()

    class Lyrics:
        def __init__(self):
            self.calls = 0

        def fetch(self, track):
            self.calls += 1
            return "plain", "synced"

    disabled = Lyrics()
    metadata_enricher.MetadataEnricher.enrich(
        TrackMetadata("No lyrics", ["Artist"], "Album"),
        fetch_lyrics=False,
        lyrics_fetcher=disabled,
    )
    assert disabled.calls == 0

    enabled = Lyrics()
    track = TrackMetadata("With lyrics", ["Artist"], "Album")
    metadata_enricher.MetadataEnricher.enrich(
        track,
        fetch_lyrics=True,
        lyrics_fetcher=enabled,
    )
    assert enabled.calls == 1
    assert track.lyrics == "plain"
    assert track.synced_lyrics == "synced"
