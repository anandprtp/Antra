import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from antra_telegram.access import AccessStore
from antra_telegram.bot import TelegramMusicBot
from antra_telegram.media import MediaRegistry, MediaServer
from antra_telegram.models import TrackAsset
from antra_telegram.security import LinkSigner
from antra_telegram.storage_db import StorageCatalog
from antra_telegram.telegram_storage import TelegramStorage
from antra_telegram.web_sessions import (
    PlayerStateConflict,
    WebSessionStore,
    validate_player_state,
)


SECRET = b"player-api-test-secret-that-is-long-enough"


def test_bot_player_link_uses_one_time_launch_instead_of_bearer_token(
    tmp_path: Path,
):
    sessions = WebSessionStore(tmp_path / "web.sqlite3")
    bot = TelegramMusicBot.__new__(TelegramMusicBot)
    bot.config = SimpleNamespace(
        player_url="https://player.example",
        public_base_url="https://music.example",
        web_session_ttl_seconds=2_592_000,
    )
    bot.web_session_store = sessions

    url = bot._player_url(777, "opaque-track")
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query))

    assert parsed.scheme == "https"
    assert parsed.netloc == "player.example"
    assert parsed.path == "/open"
    assert parsed.fragment == ""
    assert set(query) == {"launch"}
    launch = sessions.consume_launch(query["launch"])
    assert launch is not None
    assert launch.user_id == 777
    assert launch.media_id == "opaque-track"
    assert sessions.consume_launch(query["launch"]) is None


def test_web_sessions_are_hashed_expiring_and_revocable(tmp_path: Path):
    path = tmp_path / "web.sqlite3"
    store = WebSessionStore(path, default_ttl_seconds=60)

    token = store.issue(123, role="admin", now=1_000)
    identity = store.authenticate(token, now=1_059)

    assert identity is not None
    assert identity.user_id == 123
    assert identity.role == "admin"
    assert identity.expires_at == 1_060
    assert store.authenticate(token, now=1_061) is None
    assert path.stat().st_mode & 0o777 == 0o600

    with sqlite3.connect(path) as connection:
        stored_hash = connection.execute(
            "SELECT token_hash FROM web_sessions"
        ).fetchone()[0]
    assert stored_hash != token
    assert token.encode("utf-8") not in path.read_bytes()

    second = store.issue(123, now=2_000)
    assert store.revoke(second, now=2_001)
    assert not store.revoke(second, now=2_002)
    assert store.authenticate(second, now=2_001) is None


def test_player_state_is_persistent_owner_scoped_and_revision_checked(tmp_path: Path):
    path = tmp_path / "web.sqlite3"
    store = WebSessionStore(path)
    draft = validate_player_state(
        {
            "queue_ids": ["one", "two"],
            "current_id": "two",
            "position_ms": 42_000,
            "paused": False,
            "shuffle": True,
            "repeat_mode": "all",
            "revision": 0,
        }
    )

    saved = store.save_player_state(
        123,
        draft,
        expected_revision=0,
        now=1_000,
    )

    assert saved.revision == 1
    assert saved.updated_at == 1_000
    assert WebSessionStore(path).get_player_state(123) == saved
    assert store.get_player_state(456).revision == 0
    assert store.get_player_state(456).queue_ids == ()

    with pytest.raises(PlayerStateConflict) as conflict:
        store.save_player_state(123, draft, expected_revision=0, now=1_001)
    assert conflict.value.current == saved


@pytest.mark.parametrize(
    "payload",
    [
        {"queue_ids": "not-a-list"},
        {"queue_ids": ["one"], "current_id": "missing"},
        {"queue_ids": ["one"], "position_ms": -1},
        {"queue_ids": ["one"], "paused": 1},
        {"queue_ids": ["one"], "repeat_mode": "sometimes"},
        {"queue_ids": ["one"], "revision": True},
    ],
)
def test_player_state_validation_rejects_malformed_payloads(payload):
    with pytest.raises(ValueError):
        validate_player_state(payload)


def test_media_registry_lists_stable_opaque_ids_without_paths(tmp_path: Path):
    track = tmp_path / "Artist" / "Album" / "01 - Song.mp3"
    track.parent.mkdir(parents=True)
    track.write_bytes(b"audio")
    registry = MediaRegistry(tmp_path, SECRET)

    assert registry.refresh() == 1
    first = registry.list_assets()
    assert len(first) == 1
    media_id, asset = first[0]
    assert "/" not in media_id
    assert asset.title == "Song"

    assert registry.refresh() == 1
    assert registry.list_assets()[0][0] == media_id


def test_player_api_auth_catalog_state_cors_and_signed_range(tmp_path: Path):
    async def scenario():
        first = tmp_path / "Massive Attack" / "Mezzanine" / "01 - Teardrop.mp3"
        first.parent.mkdir(parents=True)
        first.write_bytes(b"0123456789")
        second = tmp_path / "Portishead" / "Dummy" / "02 - Roads.mp3"
        second.parent.mkdir(parents=True)
        second.write_bytes(b"abcdefghij")

        registry = MediaRegistry(tmp_path, SECRET)
        registry.refresh()
        sessions = WebSessionStore(tmp_path / "player.sqlite3")
        token = sessions.issue(777, role="admin")
        access = AccessStore(
            tmp_path / "access.sqlite3",
            static_allowed_user_ids=frozenset({777}),
        )
        server = MediaServer(
            registry,
            LinkSigner(SECRET),
            "https://music.example",
            "127.0.0.1",
            0,
            3600,
            web_session_store=sessions,
            cors_allowed_origins=("https://player.example",),
            web_link_ttl_seconds=120,
            player_base_url="https://player.example",
            access_store=access,
        )
        client = TestClient(TestServer(server.create_app()))
        await client.start_server()
        headers = {
            "Authorization": f"Bearer {token}",
            "Origin": "https://player.example",
        }
        try:
            response = await client.get(
                "/api/v1/health",
                headers={"Origin": "https://player.example"},
            )
            assert response.status == 200
            assert await response.json() == {"status": "ok", "tracks": 2}

            launch_token = sessions.issue_launch(777, role="admin")
            response = await client.post(
                "/api/v1/player-launch",
                headers={"Origin": "https://player.example"},
                json={"launch": launch_token},
            )
            assert response.status == 200
            launch_payload = await response.json()
            launched = urlsplit(launch_payload["url"])
            launched_fragment = dict(parse_qsl(launched.fragment))
            assert launched.scheme == "https"
            assert launched.netloc == "player.example"
            assert launched.query == ""
            assert "api" not in launched_fragment
            assert sessions.authenticate(launched_fragment["token"]).user_id == 777

            response = await client.post(
                "/api/v1/player-launch",
                headers={"Origin": "https://player.example"},
                json={"launch": launch_token},
            )
            assert response.status == 401

            response = await client.get("/api/v1/tracks")
            assert response.status == 401
            assert response.headers["WWW-Authenticate"] == "Bearer"

            response = await client.get(
                "/api/v1/tracks",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Origin": "https://evil.example",
                },
            )
            assert response.status == 403

            response = await client.options(
                "/api/v1/player-state",
                headers={
                    "Origin": "https://player.example",
                    "Access-Control-Request-Method": "PUT",
                    "Access-Control-Request-Headers": "Authorization, Content-Type",
                },
            )
            assert response.status == 204
            assert (
                response.headers["Access-Control-Allow-Origin"]
                == "https://player.example"
            )

            response = await client.get(
                "/api/v1/tracks?q=massive+teardrop&limit=10",
                headers=headers,
            )
            assert response.status == 200
            assert (
                response.headers["Access-Control-Allow-Origin"]
                == "https://player.example"
            )
            payload = await response.json()
            assert payload["total"] == 1
            assert payload["limit"] == 10
            track = payload["items"][0]
            assert track["title"] == "Teardrop"
            assert track["artist"] == "Massive Attack"
            assert track["size_bytes"] == 10
            assert track["mime_type"] == "audio/mpeg"
            assert track["availability"] == "ready"
            assert track["stream_url"].startswith(
                f"https://music.example/api/v1/tracks/{track['id']}/stream?"
            )
            assert str(tmp_path) not in json.dumps(payload)

            response = await client.get("/api/v1/me", headers=headers)
            assert response.status == 200
            assert await response.json() == {
                "user_id": 777,
                "role": "admin",
                "session_expires_at": sessions.authenticate(token).expires_at,
            }

            response = await client.get("/api/v1/player-state", headers=headers)
            assert response.status == 200
            state = await response.json()
            assert state["queue_ids"] == []
            assert state["revision"] == 0

            state.update(
                {
                    "queue_ids": [track["id"]],
                    "current_id": track["id"],
                    "position_ms": 12_345,
                    "paused": False,
                }
            )
            response = await client.put(
                "/api/v1/player-state",
                headers=headers,
                json=state,
            )
            assert response.status == 200
            saved = await response.json()
            assert saved["revision"] == 1
            assert saved["position_ms"] == 12_345

            response = await client.put(
                "/api/v1/player-state",
                headers=headers,
                json=state,
            )
            assert response.status == 409
            conflict = await response.json()
            assert conflict["error"] == "revision_conflict"
            assert conflict["current"]["revision"] == 1

            stream = urlsplit(track["stream_url"])
            stream_target = stream.path + "?" + stream.query
            response = await client.head(
                stream_target,
                headers={"Origin": "https://player.example"},
            )
            assert response.status == 200
            assert response.headers["Accept-Ranges"] == "bytes"

            response = await client.get(
                stream_target,
                headers={
                    "Origin": "https://player.example",
                    "Range": "bytes=2-5",
                },
            )
            assert response.status == 206
            assert await response.read() == b"2345"

            query = dict(parse_qsl(stream.query))
            query["sig"] = "tampered"
            response = await client.get(
                stream.path + "?" + urlencode(query),
                headers={"Origin": "https://player.example"},
            )
            assert response.status == 403

            access.static_allowed_user_ids = frozenset()
            response = await client.get("/api/v1/me", headers=headers)
            assert response.status == 401
            assert sessions.authenticate(token) is None
        finally:
            await client.close()

    asyncio.run(scenario())


def test_archived_track_survives_restart_and_restores_on_stream(tmp_path: Path):
    async def scenario():
        library = tmp_path / "Music"
        source = library / "Artist" / "Album" / "01 - Track.mp3"
        source.parent.mkdir(parents=True)
        payload = b"telegram-backed-audio-payload"
        source.write_bytes(payload)
        asset = TrackAsset(source, "Track", "Artist", "Album", 120)
        cloud_parts: dict[str, bytes] = {}

        class FakeBot:
            async def send_document(self, chat_id, document, **kwargs):
                file_id = f"file-{len(cloud_parts) + 1}"
                cloud_parts[file_id] = document.read()
                return SimpleNamespace(
                    chat_id=chat_id,
                    message_id=len(cloud_parts),
                    document=SimpleNamespace(
                        file_id=file_id,
                        file_unique_id=f"unique-{file_id}",
                    ),
                )

            async def get_file(self, file_id):
                class File:
                    async def download_to_drive(self, custom_path):
                        Path(custom_path).write_bytes(cloud_parts[file_id])

                return File()

        bot = FakeBot()
        first_registry = MediaRegistry(library, SECRET)
        media_id = first_registry.register(asset)
        catalog = StorageCatalog(tmp_path / "storage.sqlite3")
        storage = TelegramStorage(catalog, part_bytes=8)
        await storage.archive(
            bot,
            asset,
            track_id=media_id,
            chat_id=777,
        )
        source.unlink()

        restarted_registry = MediaRegistry(library, SECRET)
        restarted_registry.refresh()
        stored = catalog.ready_track(media_id)
        assert stored is not None
        cache_path = (
            library
            / ".antra-telegram-cache"
            / media_id
            / stored.filename
        )
        restarted_registry.register_stored(
            media_id,
            TrackAsset(
                cache_path,
                stored.title,
                stored.artist,
                stored.album,
                stored.duration_seconds,
                source="telegram",
            ),
        )
        sessions = WebSessionStore(tmp_path / "sessions.sqlite3")
        token = sessions.issue(777)
        server = MediaServer(
            restarted_registry,
            LinkSigner(SECRET),
            "https://music.example",
            "127.0.0.1",
            0,
            3600,
            web_session_store=sessions,
            telegram_storage=storage,
            storage_bot_provider=lambda: bot,
        )
        client = TestClient(TestServer(server.create_app()))
        await client.start_server()
        try:
            response = await client.get(
                "/api/v1/tracks",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status == 200
            track = (await response.json())["items"][0]
            assert track["availability"] == "archived"

            stream = urlsplit(track["stream_url"])
            response = await client.get(
                stream.path + "?" + stream.query,
                headers={"Range": "bytes=2-8"},
            )
            assert response.status == 206
            assert await response.read() == payload[2:9]
            assert cache_path.read_bytes() == payload
        finally:
            await client.close()

    asyncio.run(scenario())


def test_player_proxy_uses_one_origin_without_exposing_credentials(tmp_path: Path):
    async def scenario():
        seen_headers: dict[str, str | None] = {}

        async def frontend(request: web.Request) -> web.Response:
            seen_headers["authorization"] = request.headers.get("Authorization")
            seen_headers["cookie"] = request.headers.get("Cookie")
            seen_headers["forwarded_host"] = request.headers.get("X-Forwarded-Host")
            seen_headers["forwarded_proto"] = request.headers.get("X-Forwarded-Proto")
            return web.Response(
                text="<title>Antra Player</title>",
                content_type="text/html",
                headers={"Set-Cookie": "frontend-secret=must-not-escape"},
            )

        upstream_app = web.Application()
        upstream_app.router.add_get("/{tail:.*}", frontend)
        upstream = TestServer(upstream_app)
        await upstream.start_server()

        registry = MediaRegistry(tmp_path, SECRET)
        sessions = WebSessionStore(tmp_path / "player.sqlite3")
        server = MediaServer(
            registry,
            LinkSigner(SECRET),
            "https://music.example",
            "127.0.0.1",
            0,
            3600,
            web_session_store=sessions,
            player_upstream_url=str(upstream.make_url("")).rstrip("/"),
        )
        client = TestClient(TestServer(server.create_app()))
        await client.start_server()
        try:
            response = await client.get(
                "/",
                headers={
                    "Authorization": "Bearer browser-session",
                    "Cookie": "private=cookie",
                    "Host": "music.example",
                },
            )
            assert response.status == 200
            assert "Antra Player" in await response.text()
            assert "Set-Cookie" not in response.headers
            assert seen_headers == {
                "authorization": None,
                "cookie": None,
                "forwarded_host": "music.example",
                "forwarded_proto": "https",
            }

            response = await client.get("/api/v1/health")
            assert response.status == 200
            assert await response.json() == {"status": "ok", "tracks": 0}

            response = await client.get("/api/v1/does-not-exist")
            assert response.status == 404
            assert await response.json() == {"error": "not_found"}

            response = await client.post("/")
            assert response.status == 405
        finally:
            await client.close()
            await upstream.close()

    asyncio.run(scenario())
