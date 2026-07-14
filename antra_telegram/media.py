import base64
import hashlib
import hmac
import mimetypes
import time
from pathlib import Path
from urllib.parse import quote

from aiohttp import web

from .library import SUPPORTED_AUDIO_EXTENSIONS, inspect_track
from .models import TrackAsset
from .playlists import render_m3u8
from .security import LinkSigner


class MediaRegistry:
    """Maps stable opaque IDs to files contained by the configured library root."""

    def __init__(self, root: Path, secret: bytes):
        self.root = root.expanduser().resolve()
        self._secret = secret
        self._assets: dict[str, TrackAsset] = {}

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
        self._assets[media_id] = TrackAsset(
            path=resolved,
            title=asset.title,
            artist=asset.artist,
            album=asset.album,
            duration_seconds=asset.duration_seconds,
            source=asset.source,
        )
        return media_id

    def refresh(self) -> int:
        self.root.mkdir(parents=True, exist_ok=True)
        self._assets.clear()
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and path.suffix.casefold() in SUPPORTED_AUDIO_EXTENSIONS:
                self.register(path)
        return len(self._assets)

    def get(self, media_id: str) -> TrackAsset | None:
        asset = self._assets.get(media_id)
        if asset is None:
            return None
        try:
            resolved = asset.path.resolve(strict=True)
            resolved.relative_to(self.root)
        except (FileNotFoundError, ValueError):
            return None
        if not resolved.is_file():
            return None
        return TrackAsset(
            path=resolved,
            title=asset.title,
            artist=asset.artist,
            album=asset.album,
            duration_seconds=asset.duration_seconds,
            source=asset.source,
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
    ):
        self.registry = registry
        self.signer = signer
        self.public_base_url = public_base_url.rstrip("/")
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.link_ttl_seconds = link_ttl_seconds
        self._runner: web.AppRunner | None = None

    def create_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/playlist/{media_id}.m3u8", self._playlist)
        app.router.add_get("/media/{media_id}", self._media)
        return app

    async def start(self) -> None:
        if not self.public_base_url:
            return
        self._runner = web.AppRunner(self.create_app())
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

    def _authorize(self, request: web.Request, kind: str) -> TrackAsset:
        media_id = request.match_info["media_id"]
        try:
            expires_at = int(request.query.get("exp", "0"))
        except ValueError as exc:
            raise web.HTTPForbidden(text="invalid link") from exc
        if not self.signer.verify(kind, media_id, expires_at, request.query.get("sig", "")):
            raise web.HTTPForbidden(text="expired or invalid link")
        asset = self.registry.get(media_id)
        if asset is None:
            raise web.HTTPNotFound(text="media not found")
        return asset

    async def _playlist(self, request: web.Request) -> web.Response:
        asset = self._authorize(request, "playlist")
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
        asset = self._authorize(request, "media")
        content_type = mimetypes.guess_type(asset.path.name)[0] or "application/octet-stream"
        response = web.FileResponse(asset.path, headers={"Cache-Control": "private, no-store"})
        response.content_type = content_type
        response.headers["Content-Disposition"] = _content_disposition(asset.path.name)
        return response


def _content_disposition(filename: str) -> str:
    cleaned = "".join(char for char in filename if 32 <= ord(char) != 127)
    ascii_name = cleaned.encode("ascii", "ignore").decode("ascii") or "audio"
    ascii_name = ascii_name.replace("\\", "_").replace('"', "_")
    encoded_name = quote(cleaned, safe="")
    return f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded_name}"
