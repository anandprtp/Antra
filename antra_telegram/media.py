import asyncio
import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path
from collections.abc import Callable
from urllib.parse import quote, urlencode, urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from .access import AccessStore, AccessStoreError
from .library import SUPPORTED_AUDIO_EXTENSIONS, inspect_track, normalize_query
from .models import TrackAsset
from .playlists import render_m3u8
from .security import LinkSigner
from .telegram_storage import TelegramStorage, TelegramStorageError
from .web_sessions import (
    PlayerStateConflict,
    WebIdentity,
    WebSessionStore,
    WebSessionStoreError,
    validate_player_state,
)

LOGGER = logging.getLogger(__name__)

_PLAYER_PROXY_SESSION = web.AppKey("antra_player_proxy_session", ClientSession)
_PROXY_BLOCKED_REQUEST_HEADERS = frozenset(
    {
        "authorization",
        "connection",
        "content-length",
        "cookie",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_PROXY_BLOCKED_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "content-length",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "set-cookie",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


class MediaRegistry:
    """Maps stable opaque IDs to files contained by the configured library root."""

    def __init__(self, root: Path, secret: bytes):
        self.root = root.expanduser().resolve()
        self._secret = secret
        self._assets: dict[str, TrackAsset] = {}
        self._stored_assets: dict[str, TrackAsset] = {}
        self._lock = threading.RLock()

    def _id_for(self, path: Path) -> str:
        relative = path.resolve().relative_to(self.root).as_posix().encode("utf-8")
        digest = hmac.new(self._secret, relative, hashlib.sha256).digest()[:18]
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def register(self, asset_or_path: TrackAsset | Path) -> str:
        if isinstance(asset_or_path, TrackAsset):
            asset = asset_or_path
        else:
            asset = inspect_track(self.root, asset_or_path)
        resolved = asset.path.expanduser().resolve()
        resolved.relative_to(self.root)
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        media_id = self._id_for(resolved)
        registered = TrackAsset(
            path=resolved,
            title=asset.title,
            artist=asset.artist,
            album=asset.album,
            duration_seconds=asset.duration_seconds,
            source=asset.source,
        )
        with self._lock:
            self._assets[media_id] = registered
            self._stored_assets.pop(media_id, None)
        return media_id

    def register_stored(self, media_id: str, asset: TrackAsset) -> None:
        resolved = asset.path.expanduser().resolve()
        resolved.relative_to(self.root)
        registered = TrackAsset(
            path=resolved,
            title=asset.title,
            artist=asset.artist,
            album=asset.album,
            duration_seconds=asset.duration_seconds,
            source=asset.source or "telegram",
        )
        with self._lock:
            self._stored_assets[media_id] = registered
            self._assets[media_id] = registered

    def refresh(self) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        refreshed: dict[str, TrackAsset] = {}
        for path in sorted(self.root.rglob("*")):
            if ".antra-telegram-cache" in path.parts:
                continue
            if path.is_file() and path.suffix.casefold() in SUPPORTED_AUDIO_EXTENSIONS:
                asset = inspect_track(self.root, path)
                resolved = asset.path.expanduser().resolve()
                try:
                    resolved.relative_to(self.root)
                except ValueError:
                    continue
                if not resolved.is_file():
                    continue
                refreshed[self._id_for(resolved)] = TrackAsset(
                    path=resolved,
                    title=asset.title,
                    artist=asset.artist,
                    album=asset.album,
                    duration_seconds=asset.duration_seconds,
                    source=asset.source,
                )
        with self._lock:
            for media_id, asset in self._stored_assets.items():
                refreshed.setdefault(media_id, asset)
            self._assets = refreshed
        return len(refreshed)

    def get(
        self,
        media_id: str,
        *,
        allow_missing: bool = False,
    ) -> TrackAsset | None:
        with self._lock:
            asset = self._assets.get(media_id)
        if asset is None:
            return None
        try:
            resolved = asset.path.resolve(strict=not allow_missing)
            resolved.relative_to(self.root)
        except (FileNotFoundError, ValueError):
            return None
        if not allow_missing and not resolved.is_file():
            return None
        return TrackAsset(
            path=resolved,
            title=asset.title,
            artist=asset.artist,
            album=asset.album,
            duration_seconds=asset.duration_seconds,
            source=asset.source,
        )

    def list_assets(self) -> list[tuple[str, TrackAsset]]:
        with self._lock:
            media_ids = tuple(self._assets)
        assets = [
            (media_id, asset)
            for media_id in media_ids
            if (asset := self.get(media_id, allow_missing=True)) is not None
        ]
        return sorted(
            assets,
            key=lambda item: (
                item[1].artist.casefold(),
                item[1].album.casefold(),
                item[1].title.casefold(),
                item[0],
            ),
        )


class MediaServer:
    def __init__(
        self,
        registry: MediaRegistry,
        signer: LinkSigner,
        public_base_url: str,
        bind_host: str,
        bind_port: int,
        link_ttl_seconds: int,
        web_session_store: WebSessionStore | None = None,
        cors_allowed_origins: tuple[str, ...] | list[str] = (),
        web_link_ttl_seconds: int = 3600,
        player_upstream_url: str = "",
        player_base_url: str = "",
        access_store: AccessStore | None = None,
        telegram_storage: TelegramStorage | None = None,
        storage_bot_provider: Callable[[], object] | None = None,
    ):
        if web_link_ttl_seconds <= 0:
            raise ValueError("web_link_ttl_seconds must be positive")
        self.registry = registry
        self.signer = signer
        self.public_base_url = public_base_url.rstrip("/")
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.link_ttl_seconds = link_ttl_seconds
        self.web_session_store = web_session_store
        self.cors_allowed_origins = frozenset(
            _normalize_origin(origin) for origin in cors_allowed_origins
        )
        self.web_link_ttl_seconds = web_link_ttl_seconds
        self.player_upstream_url = (
            _normalize_origin(player_upstream_url)
            if player_upstream_url
            else ""
        )
        self.player_base_url = (
            _normalize_origin(player_base_url)
            if player_base_url
            else self.public_base_url
        )
        self.access_store = access_store
        self.telegram_storage = telegram_storage
        self.storage_bot_provider = storage_bot_provider
        self._runner: web.AppRunner | None = None

    def configure_storage_bot(
        self,
        provider: Callable[[], object],
    ) -> None:
        self.storage_bot_provider = provider

    def create_app(self) -> web.Application:
        def apply_cors_headers(
            response: web.StreamResponse,
            origin: str,
        ) -> None:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = _append_vary(
                response.headers.get("Vary"),
                "Origin",
            )
            response.headers["Access-Control-Allow-Methods"] = (
                "GET, HEAD, POST, PUT, OPTIONS"
            )
            response.headers["Access-Control-Allow-Headers"] = (
                "Authorization, Content-Type"
            )
            response.headers["Access-Control-Max-Age"] = "600"

        @web.middleware
        async def cors_middleware(request: web.Request, handler):
            origin = request.headers.get("Origin")
            if origin and not self._origin_allowed(request, origin):
                return web.json_response(
                    {"error": "origin_not_allowed"},
                    status=403,
                )
            if request.method == "OPTIONS":
                response: web.StreamResponse = web.Response(status=204)
            else:
                try:
                    response = await handler(request)
                except web.HTTPException as exc:
                    if origin:
                        apply_cors_headers(exc, origin)
                    raise
            if origin:
                apply_cors_headers(response, origin)
            return response

        app = web.Application(
            client_max_size=256 * 1024,
            middlewares=[cors_middleware],
        )
        if self.player_upstream_url:
            app.cleanup_ctx.append(self._player_proxy_context)
        app.router.add_get("/playlist/{media_id}.m3u8", self._playlist)
        app.router.add_get("/media/{media_id}", self._media)
        if self.web_session_store is not None:
            app.router.add_get("/api/v1/health", self._api_health)
            app.router.add_get("/api/v1/tracks", self._api_tracks)
            app.router.add_get("/api/v1/me", self._api_me)
            app.router.add_post(
                "/api/v1/player-launch",
                self._api_player_launch,
            )
            app.router.add_get("/api/v1/player-state", self._api_player_state)
            app.router.add_put("/api/v1/player-state", self._api_save_player_state)
            app.router.add_get(
                "/api/v1/tracks/{media_id}/stream",
                self._api_stream,
            )
        app.router.add_route("*", "/api/{tail:.*}", self._api_fallback)
        app.router.add_route("*", "/media/{tail:.*}", self._backend_not_found)
        app.router.add_route("*", "/playlist/{tail:.*}", self._backend_not_found)
        if self.player_upstream_url:
            app.router.add_route("*", "/{tail:.*}", self._player_proxy)
        return app

    async def _player_proxy_context(self, app: web.Application):
        app[_PLAYER_PROXY_SESSION] = ClientSession(
            timeout=ClientTimeout(total=60, connect=10),
            auto_decompress=False,
        )
        try:
            yield
        finally:
            await app[_PLAYER_PROXY_SESSION].close()

    async def start(self) -> None:
        if not self.public_base_url:
            return
        # Signed stream URLs are credentials; keep them out of process logs.
        self._runner = web.AppRunner(self.create_app(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.bind_host, self.bind_port)
        await site.start()

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def playlist_url(self, asset: TrackAsset, *, now: int | None = None) -> str:
        if not self.public_base_url:
            raise RuntimeError("ANTRA_TELEGRAM_PUBLIC_BASE_URL is not configured")
        media_id = self.registry.register(asset)
        current = int(time.time()) if now is None else now
        return self.signer.build_url(
            self.public_base_url,
            "playlist",
            media_id,
            current + self.link_ttl_seconds,
        )

    def _authorize(
        self,
        request: web.Request,
        kind: str,
        *,
        allow_missing: bool = False,
    ) -> TrackAsset:
        media_id = request.match_info["media_id"]
        try:
            expires_at = int(request.query.get("exp", "0"))
        except ValueError as exc:
            raise web.HTTPForbidden(text="invalid link") from exc
        if not self.signer.verify(kind, media_id, expires_at, request.query.get("sig", "")):
            raise web.HTTPForbidden(text="expired or invalid link")
        asset = self.registry.get(media_id, allow_missing=allow_missing)
        if asset is None:
            raise web.HTTPNotFound(text="media not found")
        return asset

    async def _playlist(self, request: web.Request) -> web.Response:
        asset = self._authorize(
            request,
            "playlist",
            allow_missing=self.telegram_storage is not None,
        )
        media_id = request.match_info["media_id"]
        expires_at = int(request.query["exp"])
        media_url = self.signer.build_url(
            self.public_base_url,
            "media",
            media_id,
            expires_at,
        )
        body = render_m3u8(asset, media_url)
        return web.Response(
            text=body,
            content_type="application/vnd.apple.mpegurl",
            charset="utf-8",
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": f'inline; filename="{media_id}.m3u8"',
            },
        )

    async def _media(self, request: web.Request) -> web.StreamResponse:
        media_id = request.match_info["media_id"]
        asset = self._authorize(
            request,
            "media",
            allow_missing=self.telegram_storage is not None,
        )
        asset = await self._materialize(media_id, asset)
        return self._file_response(asset)

    async def _materialize(
        self,
        media_id: str,
        asset: TrackAsset,
    ) -> TrackAsset:
        if asset.path.is_file():
            return asset
        if self.telegram_storage is None or self.storage_bot_provider is None:
            raise web.HTTPNotFound(text="media not found")
        try:
            restored = await self.telegram_storage.restore(
                self.storage_bot_provider(),
                track_id=media_id,
                destination=asset.path,
            )
        except TelegramStorageError as exc:
            LOGGER.exception(
                "Telegram storage restore failed for media %s",
                media_id,
            )
            raise web.HTTPServiceUnavailable(
                text="archived media is temporarily unavailable",
            ) from exc
        self.registry.register_stored(media_id, restored)
        return restored

    def _file_response(self, asset: TrackAsset) -> web.FileResponse:
        content_type = mimetypes.guess_type(asset.path.name)[0] or "application/octet-stream"
        response = web.FileResponse(asset.path, headers={"Cache-Control": "private, no-store"})
        response.content_type = content_type
        response.headers["Content-Disposition"] = _content_disposition(asset.path.name)
        return response

    def _origin_allowed(self, request: web.Request, origin: str) -> bool:
        try:
            normalized = _normalize_origin(origin)
        except ValueError:
            return False
        request_origin = _normalize_origin(f"{request.scheme}://{request.host}")
        public_origin = (
            _url_origin(self.public_base_url)
            if self.public_base_url
            else request_origin
        )
        return normalized in self.cors_allowed_origins or normalized in {
            request_origin,
            public_origin,
        }

    async def _require_web_identity(self, request: web.Request) -> WebIdentity:
        if self.web_session_store is None:
            raise web.HTTPServiceUnavailable(text="web sessions are not configured")
        authorization = request.headers.get("Authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if separator != " " or scheme.casefold() != "bearer" or not token:
            raise web.HTTPUnauthorized(
                text="bearer session required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            identity = await asyncio.to_thread(
                self.web_session_store.authenticate,
                token,
            )
        except WebSessionStoreError as exc:
            raise web.HTTPServiceUnavailable(text="session store unavailable") from exc
        if identity is None:
            raise web.HTTPUnauthorized(
                text="invalid or expired session",
                headers={"WWW-Authenticate": "Bearer"},
            )
        if self.access_store is not None:
            try:
                decision = await asyncio.to_thread(
                    self.access_store.authorize_existing,
                    identity.user_id,
                )
            except AccessStoreError as exc:
                raise web.HTTPServiceUnavailable(
                    text="access store unavailable",
                ) from exc
            if not decision.allowed:
                await asyncio.to_thread(
                    self.web_session_store.revoke_user,
                    identity.user_id,
                )
                raise web.HTTPUnauthorized(
                    text="access revoked",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            identity = WebIdentity(
                identity.user_id,
                decision.role or identity.role,
                identity.expires_at,
            )
        return identity

    def _stream_url(
        self,
        request: web.Request,
        media_id: str,
        *,
        now: int | None = None,
    ) -> tuple[str, int]:
        current = int(time.time()) if now is None else now
        expires_at = current + self.web_link_ttl_seconds
        signature = self.signer.signature("web-stream", media_id, expires_at)
        base_url = self.public_base_url or f"{request.scheme}://{request.host}"
        query = urlencode({"exp": expires_at, "sig": signature})
        encoded_id = quote(media_id, safe="")
        return (
            f"{base_url.rstrip('/')}/api/v1/tracks/{encoded_id}/stream?{query}",
            expires_at,
        )

    def _track_payload(
        self,
        request: web.Request,
        media_id: str,
        asset: TrackAsset,
    ) -> dict[str, object]:
        stream_url, expires_at = self._stream_url(request, media_id)
        try:
            size_bytes: int | None = asset.path.stat().st_size
        except OSError:
            size_bytes = None
        return {
            "id": media_id,
            "title": asset.title,
            "artist": asset.artist,
            "album": asset.album,
            "duration_seconds": asset.duration_seconds,
            "source": asset.source,
            "format": asset.path.suffix.casefold().lstrip("."),
            "mime_type": (
                mimetypes.guess_type(asset.path.name)[0]
                or "application/octet-stream"
            ),
            "size_bytes": size_bytes,
            "availability": "ready" if asset.path.is_file() else "archived",
            "stream_url": stream_url,
            "stream_expires_at": expires_at,
        }

    async def _api_health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "tracks": len(self.registry.list_assets()),
            },
            headers={"Cache-Control": "no-store"},
        )

    async def _api_tracks(self, request: web.Request) -> web.Response:
        await self._require_web_identity(request)
        query = " ".join(request.query.get("q", "").split())
        if len(query) > 200:
            raise web.HTTPBadRequest(text="query is too long")
        try:
            limit = int(request.query.get("limit", "50"))
            offset = int(request.query.get("offset", "0"))
        except ValueError as exc:
            raise web.HTTPBadRequest(text="invalid pagination") from exc
        if limit < 1 or limit > 100 or offset < 0:
            raise web.HTTPBadRequest(text="invalid pagination")

        entries = self.registry.list_assets()
        if query:
            entries = _search_entries(entries, query)
        total = len(entries)
        selected = entries[offset : offset + limit]
        return web.json_response(
            {
                "items": [
                    self._track_payload(request, media_id, asset)
                    for media_id, asset in selected
                ],
                "total": total,
                "limit": limit,
                "offset": offset,
            },
            headers={"Cache-Control": "private, no-store"},
        )

    async def _api_me(self, request: web.Request) -> web.Response:
        identity = await self._require_web_identity(request)
        return web.json_response(
            {
                "user_id": identity.user_id,
                "role": identity.role,
                "session_expires_at": identity.expires_at,
            },
            headers={"Cache-Control": "private, no-store"},
        )

    async def _api_player_launch(self, request: web.Request) -> web.Response:
        assert self.web_session_store is not None
        try:
            body = await request.json()
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest(text="invalid player launch") from exc
        launch_token = body.get("launch") if isinstance(body, dict) else None
        if (
            not isinstance(launch_token, str)
            or not launch_token
            or len(launch_token) > 128
        ):
            raise web.HTTPBadRequest(text="invalid player launch")
        try:
            launch = await asyncio.to_thread(
                self.web_session_store.consume_launch,
                launch_token,
            )
            if launch is None:
                raise web.HTTPUnauthorized(text="invalid or expired player launch")
            session_token = await asyncio.to_thread(
                self.web_session_store.issue,
                launch.user_id,
                role=launch.role,
            )
        except WebSessionStoreError as exc:
            raise web.HTTPServiceUnavailable(text="session store unavailable") from exc

        fragment_values = {"token": session_token}
        if launch.media_id:
            fragment_values["track"] = launch.media_id
        return web.json_response(
            {
                "url": f"{self.player_base_url}/#{urlencode(fragment_values)}",
            },
            headers={"Cache-Control": "private, no-store"},
        )

    async def _api_player_state(self, request: web.Request) -> web.Response:
        identity = await self._require_web_identity(request)
        assert self.web_session_store is not None
        try:
            state = await asyncio.to_thread(
                self.web_session_store.get_player_state,
                identity.user_id,
            )
        except WebSessionStoreError as exc:
            raise web.HTTPServiceUnavailable(text="player state unavailable") from exc
        return web.json_response(
            state.to_dict(),
            headers={"Cache-Control": "private, no-store"},
        )

    async def _api_save_player_state(self, request: web.Request) -> web.Response:
        identity = await self._require_web_identity(request)
        assert self.web_session_store is not None
        try:
            body = await request.json()
            state = validate_player_state(
                body,
                max_queue_items=self.web_session_store.max_queue_items,
            )
            saved = await asyncio.to_thread(
                self.web_session_store.save_player_state,
                identity.user_id,
                state,
                expected_revision=state.revision,
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise web.HTTPBadRequest(text="invalid player state") from exc
        except PlayerStateConflict as exc:
            return web.json_response(
                {
                    "error": "revision_conflict",
                    "current": exc.current.to_dict(),
                },
                status=409,
            )
        except WebSessionStoreError as exc:
            raise web.HTTPServiceUnavailable(text="player state unavailable") from exc
        return web.json_response(
            saved.to_dict(),
            headers={"Cache-Control": "private, no-store"},
        )

    async def _api_stream(self, request: web.Request) -> web.StreamResponse:
        media_id = request.match_info["media_id"]
        asset = self._authorize(
            request,
            "web-stream",
            allow_missing=self.telegram_storage is not None,
        )
        asset = await self._materialize(media_id, asset)
        return self._file_response(asset)

    async def _api_fallback(self, request: web.Request) -> web.Response:
        if request.method == "OPTIONS":
            return web.Response(status=204)
        return web.json_response({"error": "not_found"}, status=404)

    async def _backend_not_found(self, request: web.Request) -> web.Response:
        return web.json_response({"error": "not_found"}, status=404)

    async def _player_proxy(self, request: web.Request) -> web.Response:
        if request.method not in {"GET", "HEAD"}:
            raise web.HTTPMethodNotAllowed(request.method, ("GET", "HEAD"))

        connection_headers = {
            item.strip().casefold()
            for item in request.headers.get("Connection", "").split(",")
            if item.strip()
        }
        blocked_headers = _PROXY_BLOCKED_REQUEST_HEADERS | connection_headers
        headers = {
            name: value
            for name, value in request.headers.items()
            if name.casefold() not in blocked_headers
        }
        headers["X-Forwarded-Host"] = request.host
        headers["X-Forwarded-Proto"] = (
            urlsplit(self.public_base_url).scheme or request.scheme
        )
        target_url = f"{self.player_upstream_url}{request.path_qs}"
        try:
            async with app_session(request).request(
                request.method,
                target_url,
                headers=headers,
                allow_redirects=False,
            ) as upstream:
                upstream_connection_headers = {
                    item.strip().casefold()
                    for item in upstream.headers.get("Connection", "").split(",")
                    if item.strip()
                }
                blocked_response_headers = (
                    _PROXY_BLOCKED_RESPONSE_HEADERS
                    | upstream_connection_headers
                )
                response_headers = {
                    name: value
                    for name, value in upstream.headers.items()
                    if name.casefold() not in blocked_response_headers
                }
                body = b"" if request.method == "HEAD" else await upstream.read()
                return web.Response(
                    body=body,
                    status=upstream.status,
                    reason=upstream.reason,
                    headers=response_headers,
                )
        except (ClientError, asyncio.TimeoutError):
            LOGGER.exception("Web player upstream request failed")
            return web.json_response(
                {"error": "player_unavailable"},
                status=502,
            )


def _content_disposition(filename: str) -> str:
    cleaned = "".join(char for char in filename if 32 <= ord(char) != 127)
    ascii_name = cleaned.encode("ascii", "ignore").decode("ascii") or "audio"
    ascii_name = ascii_name.replace("\\", "_").replace('"', "_")
    encoded_name = quote(cleaned, safe="")
    return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}"


def app_session(request: web.Request) -> ClientSession:
    return request.app[_PLAYER_PROXY_SESSION]


def _normalize_origin(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("invalid origin")
    if parsed.username or parsed.password or parsed.path not in {"", "/"}:
        raise ValueError("invalid origin")
    if parsed.query or parsed.fragment:
        raise ValueError("invalid origin")
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"


def _url_origin(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("invalid URL origin")
    if parsed.username or parsed.password:
        raise ValueError("invalid URL origin")
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"


def _append_vary(existing: str | None, value: str) -> str:
    values = [item.strip() for item in (existing or "").split(",") if item.strip()]
    if value.casefold() not in {item.casefold() for item in values}:
        values.append(value)
    return ", ".join(values)


def _search_entries(
    entries: list[tuple[str, TrackAsset]],
    query: str,
) -> list[tuple[str, TrackAsset]]:
    normalized = normalize_query(query)
    if not normalized:
        return entries
    query_tokens = set(normalized.split())
    scored: list[tuple[float, str, str, TrackAsset]] = []
    for media_id, asset in entries:
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
        score = (0.50 * overlap) + (0.35 * ratio) + (0.15 * contains)
        if score >= 0.42:
            scored.append((score, asset.display_name.casefold(), media_id, asset))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [(media_id, asset) for _, _, media_id, asset in scored]
