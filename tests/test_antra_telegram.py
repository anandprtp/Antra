import asyncio
import concurrent.futures
import hashlib
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer
from telegram.ext import CallbackQueryHandler

from antra.core.config import Config
from antra.core.models import DownloadResult, DownloadStatus, TrackMetadata
from antra.core.resolver import SourceResolver
from antra.core.service import AntraService
from antra.core.youtube_music_fetcher import YouTubeMusicFetcher
from antra.sources.amazon import AmazonAdapter
from antra.sources.youtube import YouTubeAdapter
from antra_telegram.access import AccessStore, AccessStoreError
from antra_telegram.__main__ import build_application
from antra_telegram.config import ConfigError, TelegramConfig
from antra_telegram.delivery import DeliveryKind, choose_delivery
from antra_telegram.jobs import (
    JobCoordinator,
    MusicRequestError,
    MusicResolver,
    youtube_music_input_kind,
)
from antra_telegram.library import LibraryIndex
from antra_telegram.media import MediaRegistry, MediaServer
from antra_telegram.models import PlaylistPreview, PlaylistSession, TrackAsset
from antra_telegram.playlist_sessions import PlaylistSessionStore, PlaylistTooLarge
from antra_telegram.playlist_ui import parse_playlist_callback, render_playlist_page
from antra_telegram.playlists import render_m3u8
from antra_telegram.security import LinkSigner
from antra_telegram.splitter import estimate_segment_seconds
from antra_telegram.storage_db import StorageCatalog
from antra_telegram.telegram_storage import TelegramStorage, copy_part
from antra_telegram.tunnel_supervisor import (
    QUICK_TUNNEL_URL,
    update_dotenv_value,
)


SECRET = b"test-secret-that-is-long-enough-123456"


def test_library_search_uses_filename_and_antra_folder_layout(tmp_path: Path):
    track = tmp_path / "Albums" / "Massive Attack" / "Mezzanine" / "01 - Teardrop.mp3"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"not-real-audio")

    index = LibraryIndex(tmp_path)
    assert index.refresh() == 1
    result = index.find_best("Massive Attack Teardrop")

    assert result is not None
    assert result.title == "Teardrop"
    assert result.artist == "Massive Attack"
    assert result.path == track


def test_library_search_supports_current_artist_album_layout(tmp_path: Path):
    track = tmp_path / "Portishead" / "Dummy" / "02 - Sour Times.flac"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"not-real-audio")

    index = LibraryIndex(tmp_path)
    index.refresh()
    result = index.find_best("Portishead Sour Times")

    assert result is not None
    assert result.artist == "Portishead"
    assert result.album == "Dummy"
    assert index.list_assets() == [result]


def test_library_indexes_webm_audio(tmp_path: Path):
    track = tmp_path / "DJ" / "Live Sets" / "01 - Two Hour Set.webm"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"not-real-audio")

    index = LibraryIndex(tmp_path)

    assert index.refresh() == 1
    assert index.find_best("DJ Two Hour Set") is not None


def test_download_mode_requires_high_confidence_local_match(tmp_path: Path):
    track = tmp_path / "Massive Attack" / "Mezzanine" / "03 - Angel.mp3"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"not-real-audio")
    index = LibraryIndex(tmp_path)
    index.refresh()

    assert index.find_best("Massive Attack Teardrop", min_score=0.78) is None


def test_delivery_matrix(tmp_path: Path):
    mp3 = tmp_path / "song.mp3"
    flac = tmp_path / "song.flac"
    mp3.write_bytes(b"1234")
    flac.write_bytes(b"1234")

    assert choose_delivery(mp3, 4).kind == DeliveryKind.AUDIO
    assert choose_delivery(flac, 4).kind == DeliveryKind.DOCUMENT
    assert choose_delivery(mp3, 3).kind == DeliveryKind.VLC
    assert choose_delivery(mp3, 4, mode="vlc").kind == DeliveryKind.VLC


def test_signed_links_expire_and_m3u8_is_vlc_compatible(tmp_path: Path):
    signer = LinkSigner(SECRET)
    expires = 2_000
    signature = signer.signature("media", "opaque", expires)
    assert signer.verify("media", "opaque", expires, signature, now=1_999)
    assert not signer.verify("media", "opaque", expires, signature, now=2_001)

    asset = TrackAsset(tmp_path / "song.mp3", "Teardrop", "Massive Attack", duration_seconds=243.9)
    playlist = render_m3u8(asset, "https://music.example/media/opaque?sig=x")
    assert playlist == (
        "#EXTM3U\n"
        "#EXTINF:243,Massive Attack — Teardrop\n"
        "https://music.example/media/opaque?sig=x\n"
    )


def test_media_registry_rejects_paths_outside_library(tmp_path: Path):
    root = tmp_path / "library"
    root.mkdir()
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"x")
    registry = MediaRegistry(root, SECRET)

    try:
        registry.register(outside)
    except ValueError:
        pass
    else:
        raise AssertionError("outside path should be rejected")


def test_media_registry_revalidates_symlinks_before_serving(tmp_path: Path):
    root = tmp_path / "library"
    root.mkdir()
    track = root / "track.mp3"
    track.write_bytes(b"inside")
    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"outside")
    registry = MediaRegistry(root, SECRET)
    media_id = registry.register(track)

    track.unlink()
    track.symlink_to(outside)
    assert registry.get(media_id) is None


def test_media_http_supports_head_range_and_expiry(tmp_path: Path):
    async def scenario():
        track = tmp_path / "track.mp3"
        track.write_bytes(b"0123456789")
        asset = TrackAsset(track, "Track", "Artist")
        signer = LinkSigner(SECRET)
        registry = MediaRegistry(tmp_path, SECRET)
        media_id = registry.register(asset)
        server = MediaServer(
            registry,
            signer,
            "https://music.example",
            "127.0.0.1",
            0,
            3600,
        )
        client = TestClient(TestServer(server.create_app()))
        await client.start_server()
        try:
            expires = int(time.time()) + 60
            signature = signer.signature("media", media_id, expires)
            url = f"/media/{media_id}?exp={expires}&sig={signature}"

            response = await client.head(url)
            assert response.status == 200
            assert response.headers["Accept-Ranges"] == "bytes"

            response = await client.get(url, headers={"Range": "bytes=2-5"})
            assert response.status == 206
            assert await response.read() == b"2345"

            playlist_sig = signer.signature("playlist", media_id, expires)
            response = await client.get(
                f"/playlist/{media_id}.m3u8?exp={expires}&sig={playlist_sig}"
            )
            assert response.status == 200
            assert response.content_type == "application/vnd.apple.mpegurl"
            playlist = await response.text()
            assert playlist.startswith("#EXTM3U\n#EXTINF:-1,Artist — Track\n")
            assert f"https://music.example/media/{media_id}?" in playlist

            expired = expires - 120
            expired_sig = signer.signature("media", media_id, expired)
            response = await client.get(
                f"/media/{media_id}?exp={expired}&sig={expired_sig}"
            )
            assert response.status == 403
        finally:
            await client.close()

    asyncio.run(scenario())


def test_identical_jobs_are_coalesced():
    class SlowResolver:
        def __init__(self):
            self.calls = 0

        def resolve(self, query: str):
            self.calls += 1
            time.sleep(0.05)
            return TrackAsset(Path("/tmp/song.mp3"), query)

    async def scenario():
        resolver = SlowResolver()
        coordinator = JobCoordinator(resolver, max_concurrent=1, max_pending=5)
        first, second = await asyncio.gather(
            coordinator.resolve("same song"),
            coordinator.resolve(" same   song "),
        )
        assert first == second
        assert resolver.calls == 1

    asyncio.run(scenario())


def test_youtube_music_track_url_uses_url_pipeline(tmp_path: Path):
    output = tmp_path / "Artist" / "Album" / "01 - Song.mp3"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"not-real-audio")
    track = TrackMetadata(
        title="Song",
        artists=["Artist"],
        album="Album",
        duration_ms=179_000,
        source_service="youtube",
        source_url="https://music.youtube.com/watch?v=91dGIROGAa4",
    )

    class FakeService:
        def __init__(self):
            self.fetches = []
            self.download_options = []

        def search_track(self, query, options=None):
            raise AssertionError("a YouTube Music URL must not use free-text search")

        def fetch_playlist_tracks(self, url, options=None):
            self.fetches.append((url, options))
            return [track]

        def download_tracks(self, tracks, options=None):
            self.download_options.append(options)
            return [
                DownloadResult(
                    track=tracks[0],
                    status=DownloadStatus.COMPLETED,
                    file_path=str(output),
                    source_used="youtube",
                )
            ]

    service = FakeService()
    config = TelegramConfig(
        bot_token="token",
        allowed_user_ids=frozenset({1}),
        library_dir=tmp_path,
        resolve_mode="download",
    )
    library = LibraryIndex(tmp_path)
    resolver = MusicResolver(config, library, service_factory=lambda: service)
    url = "https://music.youtube.com/watch?v=91dGIROGAa4&si=test"

    asset = resolver.resolve(url)

    assert [item[0] for item in service.fetches] == [url]
    assert service.download_options[0].source_preference == "youtube"
    assert service.download_options[0].source_exclusive is True
    assert service.download_options[0].output_format == "mp3"
    assert asset is not None
    assert asset.title == "Song"
    assert asset.artist == "Artist"
    assert asset.duration_seconds == 179


def test_long_youtube_music_track_preserves_source_to_avoid_huge_transcode(tmp_path: Path):
    output = tmp_path / "909 Festival" / "Live" / "01 - Set.webm"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"not-real-audio")
    track = TrackMetadata(
        title="NINA KRAVIZ || 909 FESTIVAL 2025",
        artists=["909 Festival"],
        album="Live",
        duration_ms=7_223_000,
        source_service="youtube",
        source_url="https://music.youtube.com/watch?v=itXEKDh_YB0",
    )

    class FakeService:
        def __init__(self):
            self.options = None

        def fetch_playlist_tracks(self, url, options=None):
            return [track]

        def download_tracks(self, tracks, options=None):
            self.options = options
            return [
                DownloadResult(
                    track=tracks[0],
                    status=DownloadStatus.COMPLETED,
                    file_path=str(output),
                    source_used="youtube",
                )
            ]

    service = FakeService()
    resolver = MusicResolver(
        TelegramConfig(
            bot_token="token",
            allowed_user_ids=frozenset({1}),
            library_dir=tmp_path,
            resolve_mode="download",
            download_format="mp3",
            max_upload_bytes=49_000_000,
        ),
        LibraryIndex(tmp_path),
        service_factory=lambda: service,
    )

    asset = resolver.resolve(
        "https://music.youtube.com/watch?v=itXEKDh_YB0&si=test"
    )

    assert asset is not None
    assert service.options.output_format == "source"
    assert service.options.source_preference == "youtube"
    assert service.options.source_exclusive is True


def test_fast_title_search_downloads_from_youtube_only(tmp_path: Path):
    output = tmp_path / "Artist" / "Album" / "01 - Song.mp3"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"not-real-audio")
    track = TrackMetadata(title="Song", artists=["Artist"], album="Album", duration_ms=180_000)

    class FakeService:
        def __init__(self):
            self.options = None

        def search_track(self, query, options=None):
            return track

        def download_tracks(self, tracks, options=None):
            self.options = options
            return [
                DownloadResult(
                    track=tracks[0],
                    status=DownloadStatus.COMPLETED,
                    file_path=str(output),
                    source_used="youtube",
                )
            ]

    service = FakeService()
    resolver = MusicResolver(
        TelegramConfig(
            bot_token="token",
            allowed_user_ids=frozenset({1}),
            library_dir=tmp_path,
            resolve_mode="download",
            fast_mode=True,
        ),
        LibraryIndex(tmp_path),
        service_factory=lambda: service,
    )

    assert resolver.resolve("Artist Song") is not None
    assert service.options.source_preference == "youtube"
    assert service.options.source_exclusive is True


def test_youtube_music_playlist_url_is_redirected_to_preview_without_download(tmp_path: Path):
    class FakeService:
        def download_playlist(self, url, options=None):
            raise AssertionError("collection URLs must not reach the downloader")

    config = TelegramConfig(
        bot_token="token",
        allowed_user_ids=frozenset({1}),
        library_dir=tmp_path,
        resolve_mode="download",
    )
    resolver = MusicResolver(
        config,
        LibraryIndex(tmp_path),
        service_factory=FakeService,
    )

    try:
        resolver.resolve(
            "https://music.youtube.com/watch?v=video12345&list=PL12345678"
        )
    except MusicRequestError as exc:
        assert "список с кнопками" in str(exc)
    else:
        raise AssertionError("a YouTube Music playlist must use the preview flow")


def test_playlist_preview_fetches_metadata_without_downloading(tmp_path: Path):
    tracks = [
        TrackMetadata(
            title="First",
            artists=["Artist"],
            album="Album",
            playlist_name="Small playlist",
            playlist_position=1,
            request_kind="playlist",
            source_service="youtube",
            source_url="https://music.youtube.com/watch?v=video12345",
        ),
        TrackMetadata(
            title="Second",
            artists=["Artist"],
            album="Album",
            playlist_name="Small playlist",
            playlist_position=2,
            request_kind="playlist",
            source_service="youtube",
            source_url="https://music.youtube.com/watch?v=video67890",
        ),
    ]

    class FakeService:
        def __init__(self):
            self.fetches = []

        def fetch_playlist_tracks(self, url, options=None):
            self.fetches.append(url)
            return tracks

        def download_playlist(self, *args, **kwargs):
            raise AssertionError("preview must not download a playlist")

        def download_tracks(self, *args, **kwargs):
            raise AssertionError("preview must not download tracks")

    service = FakeService()
    config = TelegramConfig(
        bot_token="token",
        allowed_user_ids=frozenset({1}),
        library_dir=tmp_path,
        resolve_mode="download",
    )
    resolver = MusicResolver(
        config,
        LibraryIndex(tmp_path),
        service_factory=lambda: service,
    )
    url = "https://music.youtube.com/playlist?list=PL12345678"

    preview = resolver.preview_playlist(url)

    assert service.fetches == [url]
    assert preview.name == "Small playlist"
    assert [track.title for track in preview.tracks] == ["First", "Second"]


def test_playlist_selected_track_downloads_only_that_track(tmp_path: Path):
    output = tmp_path / "Artist" / "Album" / "02 - Second.mp3"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"not-real-audio")
    selected = TrackMetadata(
        title="Second",
        artists=["Artist"],
        album="Album",
        playlist_name="Small playlist",
        playlist_position=2,
        request_kind="playlist",
        source_service="youtube",
        source_url="https://music.youtube.com/watch?v=video67890",
    )

    class FakeService:
        def __init__(self):
            self.downloaded = []

        def download_tracks(self, tracks, options=None):
            self.downloaded.extend(tracks)
            return [
                DownloadResult(
                    track=tracks[0],
                    status=DownloadStatus.COMPLETED,
                    file_path=str(output),
                    source_used="youtube",
                )
            ]

    service = FakeService()
    config = TelegramConfig(
        bot_token="token",
        allowed_user_ids=frozenset({1}),
        library_dir=tmp_path,
        resolve_mode="download",
    )
    resolver = MusicResolver(config, LibraryIndex(tmp_path), service_factory=lambda: service)

    asset = resolver.resolve_track(selected)

    assert asset is not None and asset.title == "Second"
    assert len(service.downloaded) == 1
    downloaded = service.downloaded[0]
    assert downloaded.source_url == selected.source_url
    assert downloaded.request_kind == "track"
    assert downloaded.playlist_name is None
    assert downloaded.playlist_position is None


def test_playlist_sessions_survive_restart_and_are_owner_bound(tmp_path: Path):
    path = tmp_path / "playlist.sqlite3"
    preview = PlaylistPreview(
        source_url="https://music.youtube.com/playlist?list=PL12345678",
        name="Persistent playlist",
        tracks=(
            TrackMetadata(
                title="First",
                artists=["Artist"],
                album="Album",
                source_service="youtube",
                source_url="https://music.youtube.com/watch?v=video12345",
            ),
        ),
    )
    store = PlaylistSessionStore(path, ttl_seconds=3600, max_tracks=100)
    session = store.create(111, 111, preview, now=1_000)
    assert store.bind_message(session.token, 111, 111, 77)

    restarted = PlaylistSessionStore(path, ttl_seconds=3600, max_tracks=100)
    loaded = restarted.get(session.token, 111, 111, 77, now=1_001)

    assert loaded is not None
    assert loaded.name == "Persistent playlist"
    assert loaded.tracks[0].source_url == preview.tracks[0].source_url
    assert restarted.get(session.token, 222, 111, 77, now=1_001) is None
    assert restarted.get(session.token, 111, 222, 77, now=1_001) is None
    assert restarted.get(session.token, 111, 111, 78, now=1_001) is None
    assert path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(path) as db:
        stored_hash = db.execute("SELECT token_hash FROM playlist_sessions").fetchone()[0]
    assert stored_hash != session.token
    assert session.token.encode() not in path.read_bytes()
    assert restarted.get(session.token, 111, 111, 77, now=4_601) is None


def test_playlist_session_rejects_oversized_playlist(tmp_path: Path):
    preview = PlaylistPreview(
        source_url="https://music.youtube.com/playlist?list=PL12345678",
        name="Too large",
        tracks=tuple(
            TrackMetadata(title=f"Track {index}", artists=["Artist"], album="Album")
            for index in range(3)
        ),
    )
    store = PlaylistSessionStore(tmp_path / "playlist.sqlite3", ttl_seconds=60, max_tracks=2)
    try:
        store.create(1, 1, preview)
    except PlaylistTooLarge:
        pass
    else:
        raise AssertionError("oversized playlists must be rejected")


def test_playlist_ui_paginates_and_callbacks_fit_telegram_limits():
    session = PlaylistSession(
        token="abcdefghijklmnopqrstuv",
        owner_user_id=1,
        chat_id=1,
        message_id=10,
        source_url="https://music.youtube.com/playlist?list=PL12345678",
        name="Twenty five tracks",
        tracks=tuple(
            TrackMetadata(
                title=f"Track {index} with a deliberately long mobile button label",
                artists=["Artist"],
                album="Album",
            )
            for index in range(25)
        ),
        expires_at=10_000,
    )

    text, markup = render_playlist_page(session, page=1, page_size=10)

    assert "страница 2/3" in text
    assert "11. Artist — Track 10" in text
    assert "20. Artist — Track 19" in text
    assert len(text) <= 4096
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert all(len(callback.encode("utf-8")) <= 64 for callback in callbacks)
    assert parse_playlist_callback(callbacks[0]).value == 10
    assert any(callback.endswith(":a") for callback in callbacks)
    largest_text, _ = render_playlist_page(session, page=0, page_size=20)
    assert len(largest_text) <= 4096


def test_playlist_callback_parser_rejects_malformed_data():
    assert parse_playlist_callback("pl:short:t:0") is None
    assert parse_playlist_callback("pl:abcdefghijklmnopqrstuv:x:0") is None
    assert parse_playlist_callback("pl:abcdefghijklmnopqrstuv:t:-1") is None
    assert parse_playlist_callback("pl:abcdefghijklmnopqrstuv:a:1") is None


def test_youtube_music_playlist_classifier_is_strict():
    assert youtube_music_input_kind(
        "https://music.youtube.com/playlist?list=PL12345678"
    ) == "collection"
    assert youtube_music_input_kind(
        "https://music.youtube.com/watch?v=video12345&list=PL12345678"
    ) == "collection"
    assert youtube_music_input_kind(
        "https://music.youtube.com/watch?v=video12345"
    ) == "track"
    assert youtube_music_input_kind(
        "https://music.youtube.com.example.invalid/playlist?list=PL12345678"
    ) is None
    assert youtube_music_input_kind(
        "https://user@music.youtube.com/playlist?list=PL12345678"
    ) == "invalid"


def test_youtube_music_playlist_entries_receive_stable_positions():
    track = YouTubeMusicFetcher()._info_to_track(
        {
            "id": "video12345",
            "title": "Artist - Song",
            "duration": 180,
        },
        playlist_name="Ordered playlist",
        playlist_artwork=None,
        index=7,
    )

    assert track is not None
    assert track.playlist_position == 7
    assert track.source_url == "https://music.youtube.com/watch?v=video12345"


def test_playlist_order_is_preserved_across_album_groups():
    tracks = [
        TrackMetadata("A1", ["Artist A"], "Album A", request_kind="playlist"),
        TrackMetadata("B1", ["Artist B"], "Album B", request_kind="playlist"),
        TrackMetadata("A2", ["Artist A"], "Album A", request_kind="playlist"),
    ]

    result = AntraService._stamp_disc_totals(tracks)

    assert [track.title for track in result] == ["A1", "B1", "A2"]


def test_application_registers_playlist_callback_handler(tmp_path: Path):
    config = TelegramConfig(
        bot_token="token",
        allowed_user_ids=frozenset({1}),
        library_dir=tmp_path / "Music",
        access_db_path=tmp_path / "access.sqlite3",
        playlist_db_path=tmp_path / "playlist.sqlite3",
        web_sessions_db_path=tmp_path / "web-sessions.sqlite3",
        resolve_mode="download",
        link_secret=SECRET,
    )

    application = build_application(config)
    handlers = [handler for group in application.handlers.values() for handler in group]

    assert sum(isinstance(handler, CallbackQueryHandler) for handler in handlers) == 1


def test_youtube_adapter_uses_exact_source_video_without_text_search():
    url = "https://music.youtube.com/watch?v=91dGIROGAa4&si=test"
    track = TrackMetadata(
        title="БЛА БЛА БЛА (feat. Babyface Melo)",
        artists=["uniqe", "nkeeei", "ARTEM SHILOVETS", "Babyface Melo"],
        album="GLAMOUR",
        duration_ms=179_000,
        source_service="youtube",
        source_url=url,
    )

    result = YouTubeAdapter().search(track)

    assert result is not None
    assert result.stream_id == url
    assert result.download_url == url
    assert result.similarity_score == 1.0


def test_youtube_adapter_rejects_spoofed_direct_source_url():
    track = TrackMetadata(
        title="Song",
        artists=["Artist"],
        album="Album",
        source_service="youtube",
        source_url="https://music.youtube.com.example.invalid/watch?v=track",
    )

    assert YouTubeAdapter._is_direct_video_url(track.source_url) is False


def test_lossy_resolver_prioritizes_exact_youtube_source():
    class Adapter:
        always_lossy = False
        is_last_resort = False

        def __init__(self, name, priority):
            self.name = name
            self.priority = priority

        def is_available(self):
            return True

    tidal = Adapter("hifi", 1)
    youtube = Adapter("youtube", 99)
    youtube.always_lossy = True
    youtube.is_last_resort = True
    resolver = SourceResolver([tidal, youtube], preferred_output_format="mp3")
    track = TrackMetadata(
        title="Set",
        artists=["DJ"],
        album="Live",
        source_service="youtube",
        source_url="https://music.youtube.com/watch?v=itXEKDh_YB0",
    )

    ordered = resolver._build_track_resolve_order(track, set())

    assert [adapter.name for adapter in ordered] == ["youtube", "hifi"]


def test_amazon_marketplace_miss_is_not_retried():
    adapter = AmazonAdapter([])

    assert adapter.should_retry_download(
        None,
        RuntimeError(
            "Track not available from the current Amazon marketplace/account"
        ),
    ) is False


def test_large_audio_segment_estimate_keeps_upload_margin():
    segment_seconds = estimate_segment_seconds(
        size_bytes=123_000_000,
        duration_seconds=7_223,
        max_part_bytes=49_000_000,
    )

    estimated_part_bytes = 123_000_000 * segment_seconds / 7_223
    assert 60 <= segment_seconds < 7_223
    assert estimated_part_bytes <= 49_000_000 * 0.83


def test_telegram_storage_archives_restorable_cloud_parts(tmp_path: Path):
    async def scenario():
        source = tmp_path / "Music" / "Artist" / "Album" / "01 - Track.webm"
        source.parent.mkdir(parents=True)
        payload = (b"telegram-storage-payload-" * 11) + b"done"
        source.write_bytes(payload)
        asset = TrackAsset(
            source,
            "Track",
            "Artist",
            "Album",
            duration_seconds=120,
        )
        sent_payloads = []

        class FakeBot:
            async def send_document(self, chat_id, document, **kwargs):
                content = document.read()
                sent_payloads.append(content)
                index = len(sent_payloads)
                return SimpleNamespace(
                    chat_id=chat_id,
                    message_id=100 + index,
                    document=SimpleNamespace(
                        file_id=f"file-{index}",
                        file_unique_id=f"unique-{index}",
                    ),
                )

        catalog = StorageCatalog(tmp_path / "storage.sqlite3")
        storage = TelegramStorage(catalog, part_bytes=41)
        uploaded = await storage.archive(
            FakeBot(),
            asset,
            track_id="opaque-track-id",
            chat_id=123,
        )

        assert uploaded is True
        assert b"".join(sent_payloads) == payload
        assert all(len(part) <= 41 for part in sent_payloads)
        assert catalog.is_ready(
            "opaque-track-id",
            hashlib.sha256(payload).hexdigest(),
        )
        parts = catalog.parts_for("opaque-track-id")
        assert [part.byte_offset for part in parts] == [
            index * 41 for index in range(len(parts))
        ]
        assert [part.file_id for part in parts] == [
            f"file-{index}" for index in range(1, len(parts) + 1)
        ]

        uploaded_again = await storage.archive(
            FakeBot(),
            asset,
            track_id="opaque-track-id",
            chat_id=123,
        )
        assert uploaded_again is False

    asyncio.run(scenario())


def test_copy_part_preserves_exact_byte_range(tmp_path: Path):
    source = tmp_path / "source.bin"
    destination = tmp_path / "part.bin"
    source.write_bytes(bytes(range(100)))

    written, digest = copy_part(
        source,
        destination,
        offset=23,
        length=41,
    )

    assert written == 41
    assert destination.read_bytes() == bytes(range(23, 64))
    assert digest == hashlib.sha256(destination.read_bytes()).hexdigest()


def test_tunnel_supervisor_updates_only_public_origin(tmp_path: Path):
    env_file = tmp_path / ".env.telegram"
    env_file.write_text(
        "ANTRA_TELEGRAM_BOT_TOKEN=private-token\n"
        "ANTRA_TELEGRAM_PUBLIC_BASE_URL=https://old.example\n",
        encoding="utf-8",
    )

    assert update_dotenv_value(
        env_file,
        "ANTRA_TELEGRAM_PUBLIC_BASE_URL",
        "https://music-test.trycloudflare.com",
    )
    assert not update_dotenv_value(
        env_file,
        "ANTRA_TELEGRAM_PUBLIC_BASE_URL",
        "https://music-test.trycloudflare.com",
    )
    assert env_file.read_text(encoding="utf-8") == (
        "ANTRA_TELEGRAM_BOT_TOKEN=private-token\n"
        "ANTRA_TELEGRAM_PUBLIC_BASE_URL=https://music-test.trycloudflare.com\n"
    )
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert QUICK_TUNNEL_URL.search(
        "INF https://music-test.trycloudflare.com is ready"
    )


def test_config_rejects_cloud_storage_parts_over_getfile_limit(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.setenv("ANTRA_TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("ANTRA_TELEGRAM_ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("ANTRA_TELEGRAM_LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("ANTRA_TELEGRAM_STORAGE_ENABLED", "true")
    monkeypatch.setenv("ANTRA_TELEGRAM_STORAGE_PART_BYTES", "19000001")
    monkeypatch.delenv("ANTRA_TELEGRAM_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTRA_TELEGRAM_PLAYER_URL", raising=False)
    monkeypatch.delenv("ANTRA_TELEGRAM_LINK_SECRET", raising=False)

    try:
        TelegramConfig.from_env()
    except ConfigError as exc:
        assert "19000000" in str(exc)
    else:
        raise AssertionError("oversized Telegram storage parts must be rejected")


def test_service_search_track_is_public_and_marks_single_request():
    class FakeSpotify:
        def __init__(self, *args, **kwargs):
            self.query = ""

        def search_track(self, query: str):
            self.query = query
            return TrackMetadata(title="Teardrop", artists=["Massive Attack"], album="Mezzanine")

    service = AntraService(config=Config(), spotify_client_factory=FakeSpotify)
    track = service.search_track("  Massive   Attack Teardrop ")

    assert track is not None
    assert track.request_kind == "track"


def test_config_rejects_forgeable_or_non_https_vlc_settings(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ANTRA_TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("ANTRA_TELEGRAM_ALLOWED_USER_IDS", "1")
    monkeypatch.setenv("ANTRA_TELEGRAM_LIBRARY_DIR", str(tmp_path))
    monkeypatch.setenv("ANTRA_TELEGRAM_PUBLIC_BASE_URL", "http://music.example")
    monkeypatch.setenv(
        "ANTRA_TELEGRAM_LINK_SECRET",
        "replace-with-at-least-32-random-characters",
    )

    try:
        TelegramConfig.from_env()
    except ConfigError:
        pass
    else:
        raise AssertionError("insecure public link settings should be rejected")


def test_config_allows_explicit_first_user_claim_without_allowlist(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ANTRA_TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("ANTRA_TELEGRAM_ALLOWED_USER_IDS", "")
    monkeypatch.setenv("ANTRA_TELEGRAM_CLAIM_FIRST_USER", "true")
    monkeypatch.setenv("ANTRA_TELEGRAM_ACCESS_DB", str(tmp_path / "access.sqlite3"))
    monkeypatch.setenv("ANTRA_TELEGRAM_LIBRARY_DIR", str(tmp_path / "Music"))
    monkeypatch.delenv("ANTRA_TELEGRAM_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTRA_TELEGRAM_LINK_SECRET", raising=False)

    config = TelegramConfig.from_env()

    assert config.allowed_user_ids == frozenset()
    assert config.claim_first_user is True
    assert config.access_db_path == (tmp_path / "access.sqlite3").resolve()


def test_access_store_claim_is_persistent_and_exclusive(tmp_path: Path):
    path = tmp_path / "access.sqlite3"
    store = AccessStore(path, allow_first_claim=True)

    first = store.authorize_or_claim(111)
    assert first.allowed and first.claimed_admin and first.role == "admin"
    assert store.authorize_or_claim(222).allowed is False

    restarted = AccessStore(path, allow_first_claim=True)
    assert restarted.authorize_or_claim(111).allowed is True
    assert restarted.authorize_or_claim(222).allowed is False
    assert restarted.admin_id() == 111
    assert path.stat().st_mode & 0o777 == 0o600


def test_access_store_concurrent_first_claim_has_one_winner(tmp_path: Path):
    path = tmp_path / "access.sqlite3"

    def claim(user_id: int):
        return AccessStore(path, allow_first_claim=True).authorize_or_claim(user_id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (111, 222)))

    assert sum(1 for result in results if result.claimed_admin) == 1
    admin = AccessStore(path, allow_first_claim=True).admin_id()
    assert admin in {111, 222}
    assert AccessStore(path, allow_first_claim=True).authorize_or_claim(admin).allowed


def test_access_store_corruption_fails_closed(tmp_path: Path):
    path = tmp_path / "access.sqlite3"
    path.write_bytes(b"not a sqlite database")
    store = AccessStore(path, allow_first_claim=True)

    try:
        store.authorize_or_claim(111)
    except AccessStoreError:
        pass
    else:
        raise AssertionError("a corrupt owner database must fail closed")


def test_admin_can_create_single_use_member_invite(tmp_path: Path):
    path = tmp_path / "access.sqlite3"
    store = AccessStore(path, allow_first_claim=True)
    store.authorize_or_claim(111)

    token = store.create_invite(111, ttl_seconds=3600, now=1_000)
    joined = store.redeem_invite(222, token, now=1_001)

    assert joined.allowed and joined.joined_by_invite and joined.role == "member"
    assert store.authorize_or_claim(222).allowed
    assert store.redeem_invite(333, token, now=1_002).allowed is False
    assert store.list_members(111) == [(111, "admin"), (222, "member")]
