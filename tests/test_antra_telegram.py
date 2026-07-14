import asyncio
import concurrent.futures
import os
import time
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from antra.core.config import Config
from antra.core.models import DownloadResult, DownloadStatus, TrackMetadata
from antra.core.service import AntraService
from antra.sources.youtube import YouTubeAdapter
from antra_telegram.access import AccessStore, AccessStoreError
from antra_telegram.config import ConfigError, TelegramConfig
from antra_telegram.delivery import DeliveryKind, choose_delivery
from antra_telegram.jobs import JobCoordinator, MusicRequestError, MusicResolver
from antra_telegram.library import LibraryIndex
from antra_telegram.media import MediaRegistry, MediaServer
from antra_telegram.models import TrackAsset
from antra_telegram.playlists import render_m3u8
from antra_telegram.security import LinkSigner


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
    )

    class FakeService:
        def __init__(self):
            self.urls = []

        def search_track(self, query, options=None):
            raise AssertionError("a YouTube Music URL must not use free-text search")

        def download_playlist(self, url, options=None):
            self.urls.append(url)
            return [
                DownloadResult(
                    track=track,
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

    assert service.urls == [url]
    assert asset is not None
    assert asset.title == "Song"
    assert asset.artist == "Artist"
    assert asset.duration_seconds == 179


def test_youtube_music_playlist_url_is_rejected_without_download(tmp_path: Path):
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
        resolver.resolve("https://music.youtube.com/watch?v=track&list=playlist")
    except MusicRequestError as exc:
        assert "один трек" in str(exc)
    else:
        raise AssertionError("a YouTube Music playlist must be rejected")


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
