"""
Spotify OAuth PKCE authentication manager.
Handles first-time browser login and silent token refresh forever after.
Users never need to create a Spotify developer account.
"""
import logging
import os
from typing import Optional

import spotipy
from spotipy.oauth2 import SpotifyPKCE
from spotipy.cache_handler import CacheFileHandler

logger = logging.getLogger(__name__)

SPOTIFY_SCOPES = " ".join([
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",    # liked songs
    "user-follow-read",     # followed artists
])


import time
import requests

# Spotify web-player TOTP credentials.
# Using /api/token + TOTP instead of /get_access_token because the latter
# is Cloudflare IP-blocked for all scripted clients (403 "URL Blocked"),
# even with Chrome TLS impersonation. The /api/token endpoint is not blocked
# and returns isAnonymous=False when the sp_dc cookie is included.
_SP_TOTP_SECRET  = (
    "GM3TMMJTGYZTQNZVGM4DINJZHA4TGOBYGMZTCMRTGEYDSMJRHE4TEOBUG4YTCMRUGQ4D"
    "QOJUGQYTAMRRGA2TCMJSHE3TCMBY"
)
_SP_TOTP_VERSION = 61

WEB_PLAYER_TOKEN_URL = "https://open.spotify.com/api/token"

WEB_PLAYER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin":  "https://open.spotify.com",
    "Referer": "https://open.spotify.com/",
}

class SpotifyWebPlayerAuth:
    """
    Authenticates via Spotify web player sp_dc cookie or a manual access token.
    Priority: Manual Token > sp_dc cookie.
    """

    def __init__(self, sp_dc: str = "", manual_token: str = ""):
        self._sp_dc = sp_dc.strip() if sp_dc else ""
        self._access_token = manual_token.strip() if manual_token else None
        self._token_expiry = 0.0
        if self._access_token:
            # Manual tokens don't have a reliable way to refresh, 
            # so we set a 1-hour expiry and expect a manual update if it fails.
            self._token_expiry = time.time() + 3600 

        self._session = requests.Session()
        # Use strictly ordered headers to mimic a real browser fingerprint
        self._session.headers.clear()
        self._session.headers.update(WEB_PLAYER_HEADERS)

    def get_token(self) -> str:
        """Return valid access token, refreshing if needed."""
        if self._access_token and time.time() < self._token_expiry - 60:
            return self._access_token
        return self._refresh_token()

    def _refresh_token(self) -> str:
        """Exchange sp_dc cookie for web player access token using TOTP mechanism."""
        # Return manual token if still valid
        if self._access_token and time.time() < self._token_expiry - 10:
            return self._access_token

        if not self._sp_dc:
            if self._access_token:
                return self._access_token  # Return manual token even if "expired" as last resort
            raise ValueError("[Spotify] No sp_dc cookie or manual token provided for authentication")

        try:
            import pyotp
        except ImportError:
            raise RuntimeError(
                "[Spotify] pyotp is required for authentication. Run: pip install pyotp"
            )

        totp = pyotp.TOTP(_SP_TOTP_SECRET)
        code = totp.now()
        try:
            r = self._session.get(
                WEB_PLAYER_TOKEN_URL,
                params={
                    "reason":      "init",
                    "productType": "web-player",
                    "totp":        code,
                    "totpVer":     str(_SP_TOTP_VERSION),
                    "totpServer":  code,
                },
                cookies={"sp_dc": self._sp_dc},
                timeout=15,
                allow_redirects=True,
            )
        except Exception as e:
            raise RuntimeError(f"[Spotify] Token request failed: {e}")

        if r.status_code == 200:
            try:
                data = r.json()
            except Exception:
                raise RuntimeError("[Spotify] Could not parse token response")

            if data.get("isAnonymous", True):
                raise RuntimeError(
                    "[Spotify] sp_dc cookie produced an anonymous token — "
                    "it may be expired. Refresh it from your browser."
                )

            self._access_token = data["accessToken"]
            expiry_ms = data.get("accessTokenExpirationTimestampMs", 0)
            self._token_expiry = (
                expiry_ms / 1000 if expiry_ms else time.time() + 3600
            )
            logger.info("[Spotify] Web player token refreshed via TOTP")
            return self._access_token

        if r.status_code == 401:
            raise RuntimeError(
                "[Spotify] sp_dc cookie is invalid or expired. "
                "Refresh it from your browser (DevTools → Application → "
                "Cookies → open.spotify.com → sp_dc)."
            )

        raise RuntimeError(
            f"[Spotify] Token request failed with status {r.status_code}. "
            "Please ensure your sp_dc cookie is valid and not expired."
        )

    def get_headers(self) -> dict:
        """Return request headers with valid Bearer token."""
        token = self.get_token()
        return {
            **WEB_PLAYER_HEADERS,
            "Authorization": f"Bearer {token}",
        }

    def is_valid(self, force_check: bool = False) -> bool:
        """
        Check if the current auth (cookie or token) can produce 
        a valid authenticated session.
        If force_check is true, it performs a real request to Spotify.
        """
        try:
            if not force_check and self._access_token and time.time() < self._token_expiry - 10:
                return True # Token seems fresh enough logically!
            
            # If we don't have a token, or we are forcing a check:
            # First, try to get/refresh the token
            token = self.get_token()
            if not token:
                return False
                
            # Perform a REAL ping to Spotify's lightweight "me" endpoint
            # This verifies the token is not hit by a 429 or 401.
            r = self._session.get(
                "https://api.spotify.com/v1/me",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10
            )
            
            if r.status_code == 200:
                return True
                
            # If we got a 429, log it so the user knows they are blocked
            if r.status_code == 429:
                logger.warning(
                    f"[Spotify] Auth validation blocked (429). "
                    f"Retry-After: {r.headers.get('Retry-After')}s"
                )
            
            return False
        except Exception as e:
            logger.debug(f"[Spotify] Web player auth validation failed: {e}")
            return False

    def force_refresh(self) -> None:
        """Force token refresh on next get_token() call."""
        self._token_expiry = 0.0
        self._access_token = None


class SpotifyAuthManager:

    def __init__(
        self,
        client_id: str,
        redirect_uri: str = "http://127.0.0.1:8888/callback",
        cache_path: str = ".spotify_cache",
    ):
        self._client_id = client_id
        self._redirect_uri = redirect_uri
        self._cache_path = cache_path
        self._sp: Optional[spotipy.Spotify] = None
        # Ensure cache file's parent directory exists
        import pathlib
        cache_file = pathlib.Path(cache_path)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        self._cache_handler = CacheFileHandler(cache_path=str(cache_file))

    def is_authenticated(self) -> bool:
        """Returns True if a valid cached token exists."""
        try:
            auth = self._make_pkce()
            token = auth.get_cached_token()
            return token is not None and not auth.is_token_expired(token)
        except Exception:
            return False

    def authenticate(self) -> spotipy.Spotify:
        """
        Returns authenticated Spotify client.
        First run: opens browser for user to log in.
        Subsequent runs: uses cached refresh token silently.
        Never asks user to log in again after first time.
        """
        if self._sp and self.is_authenticated():
            return self._sp

        auth = self._make_pkce()

        # Opens browser on first run; uses cached refresh token silently after.
        # We do NOT call current_user() here to verify — some accounts may have
        # restricted endpoints. The token itself is proof of authentication.
        self._sp = spotipy.Spotify(auth_manager=auth)
        
        # Force a token fetch/refresh. This will trigger the interactive flow if needed.
        try:
            token_info = auth.get_access_token()
            if not token_info:
                raise RuntimeError("Failed to obtain access token.")
        except Exception as e:
            self._sp = None
            raise RuntimeError(f"Spotify authentication failed: {e}")
            
        logger.debug("[Spotify] Session established via PKCE.")
        return self._sp

    def get_user_display_name(self) -> Optional[str]:
        """Return the logged-in user's display name, or None if not authenticated."""
        if not self.is_authenticated():
            return None
        try:
            sp = self.get_client()
            if sp is None:
                return None
            user = sp.current_user()
            return user.get("display_name") or user.get("id")
        except Exception:
            return None

    def logout(self) -> None:
        """Clear cached token. User will need to log in again."""
        if os.path.exists(self._cache_path):
            os.remove(self._cache_path)
        self._sp = None
        logger.info("[Spotify] Logged out, cache cleared")

    def get_client(self) -> Optional[spotipy.Spotify]:
        """Return client if authenticated, None otherwise. Never opens browser."""
        if not self.is_authenticated():
            return None
        return self._sp or self.authenticate()

    def _make_pkce(self) -> SpotifyPKCE:
        return SpotifyPKCE(
            client_id=self._client_id,
            redirect_uri=self._redirect_uri,
            scope=SPOTIFY_SCOPES,
            cache_handler=self._cache_handler,
            open_browser=True,
        )
