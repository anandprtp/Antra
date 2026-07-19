import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from antra_telegram.bot import TelegramMusicBot
from antra_telegram.media import MediaRegistry, MediaServer
from antra_telegram.security import LinkSigner
from antra_telegram.web_sessions import (
    PlayerStateConflict,
    WebSessionStore,
    validate_player_state,
)


SECRET = b"player-api-test-secret-that-is-long-enough"


def test_bot_player_link_keeps_credentials_in_fragment(tmp_path: Path):
    sessions = WebSessionStore(tmp_path / "web.sqlite3")
    bot = TelegramMusicBot.__new__(TelegramMusicBot)
    bot.config = SimpleNamespace(
        player_url="https://player.example",
        public_base_url="https://music.example",
    )
    bot.web_session_store = sessions

    url = bot._player_url(777, "opaque-track")
    parsed = urlsplit(url)
    fragment = dict(parse_qsl(parsed.fragment))

    assert parsed.scheme == "https"
    assert parsed.netloc == "player.example"
    assert parsed.query == ""
    assert fragment["api"] == "https://music.example"
    assert fragment["track"] == "opaque-track"
    assert sessions.authenticate(fragment["token"]).user_id == 777


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

            assert sessions.revoke(token)
            response = await client.get("/api/v1/me", headers=headers)
            assert response.status == 401
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
