import base64
import hashlib
import hmac
import time
from urllib.parse import urlencode


class LinkSigner:
    def __init__(self, secret: bytes):
        if len(secret) < 16:
            raise ValueError("link signing secret is too short")
        self._secret = secret

    def signature(self, kind: str, media_id: str, expires_at: int) -> str:
        payload = f"{kind}:{media_id}:{expires_at}".encode("utf-8")
        digest = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def verify(
        self,
        kind: str,
        media_id: str,
        expires_at: int,
        signature: str,
        *,
        now: int | None = None,
    ) -> bool:
        current = int(time.time()) if now is None else now
        if expires_at < current:
            return False
        expected = self.signature(kind, media_id, expires_at)
        return hmac.compare_digest(expected, signature or "")

    def build_url(
        self,
        base_url: str,
        kind: str,
        media_id: str,
        expires_at: int,
    ) -> str:
        suffix = ".m3u8" if kind == "playlist" else ""
        query = urlencode(
            {"exp": expires_at, "sig": self.signature(kind, media_id, expires_at)}
        )
        return f"{base_url.rstrip('/')}/{kind}/{media_id}{suffix}?{query}"
