r"""
Shared TLS-resilient HTTP session and plain-language network errors for the
mirror adapters (v1.1.8 BUG-2).

BUG-2 was reported against Amazon only, and the fix landed there first. This
lifts the same treatment out of `amazon.py` so the other mirrors get it without
four copies of the same logic drifting apart.

Three things are shared:

1. **A TLS fallback session.** Some users cannot complete a handshake to the
   mirrors from `requests` (OpenSSL) while the same host works fine elsewhere —
   typically a middlebox that dislikes Python's TLS fingerprint, or an
   intercepting proxy. curl_cffi is already bundled and presents a real Chrome
   fingerprint, so it often succeeds where OpenSSL does not. Scoped to
   NON-streaming GETs so streaming download paths keep their `requests`
   response objects and `with ... as r:` usage unchanged.

2. **Proxy visibility.** `requests.Session.trust_env` defaults to True, so a
   system/WinINET or environment proxy is inherited *silently* — and an
   intercepting proxy on 443 is one of the likeliest causes of
   `WRONG_VERSION_NUMBER`. Inheritance is deliberately NOT disabled (plenty of
   users need it); the active proxy is simply logged when TLS fails, so the next
   bug report contains the answer.

3. **Plain-language errors.** A raw
   `SSLError(SSLError(1, '[SSL: WRONG_VERSION_NUMBER] wrong version number'))`
   tells a user nothing and reads like a server outage, which is the wrong
   conclusion: WRONG_VERSION_NUMBER means the bytes coming back are not TLS at
   all, so something on the network path answered instead of the mirror.
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)


class TlsFallbackSession:
    """`requests.Session` that retries once over curl_cffi's TLS stack."""

    def __init__(self, label: str = "Mirror"):
        self._s = requests.Session()
        self._label = label
        self._curl_unavailable = False

    @property
    def headers(self):
        return self._s.headers

    # Forwarded so an adapter can opt out of inheriting system/environment
    # proxies the way a plain requests.Session can. The Apple adapter does
    # (it talks only to our own mirror), and losing that on the way through
    # this wrapper would silently change which proxy its requests take.
    @property
    def trust_env(self):
        return self._s.trust_env

    @trust_env.setter
    def trust_env(self, value):
        self._s.trust_env = value

    def _log_proxy_diagnostics(self, url: str) -> None:
        try:
            from requests.utils import get_environ_proxies
            proxies = get_environ_proxies(url, no_proxy=None)
        except Exception:
            return
        if proxies:
            logger.warning(
                "[%s] A proxy is configured for this connection (%s). If downloads keep "
                "failing with a secure-connection error, this proxy is the most likely cause.",
                self._label, ", ".join(f"{k}={v}" for k, v in proxies.items()),
            )

    def _curl_get(self, url: str, **kwargs):
        if self._curl_unavailable:
            return None
        try:
            from curl_cffi import requests as _curl
        except Exception:
            self._curl_unavailable = True
            return None
        try:
            merged = dict(self._s.headers)
            merged.update(kwargs.pop("headers", None) or {})
            timeout = kwargs.pop("timeout", 30)
            if isinstance(timeout, tuple):
                timeout = max(t for t in timeout if t) if any(timeout) else 30
            kwargs.pop("stream", None)
            resp = _curl.get(url, headers=merged, timeout=timeout,
                             impersonate="chrome124", **kwargs)
            logger.info("[%s] TLS fallback via curl_cffi succeeded for %s", self._label, url)
            return resp
        except Exception as e:
            logger.debug("[%s] curl_cffi TLS fallback also failed for %s: %s", self._label, url, e)
            return None

    def get(self, url, **kwargs):
        try:
            return self._s.get(url, **kwargs)
        except requests.exceptions.SSLError as e:
            if kwargs.get("stream"):
                self._log_proxy_diagnostics(url)
                raise
            logger.warning(
                "[%s] TLS handshake failed for %s (%s) — retrying with an alternate TLS stack",
                self._label, url, str(e)[:120],
            )
            self._log_proxy_diagnostics(url)
            resp = self._curl_get(url, **kwargs)
            if resp is None:
                raise
            return resp

    def post(self, url, **kwargs):
        return self._s.post(url, **kwargs)


def has_http_status(text: str, code: int) -> bool:
    """True when `code` appears as a standalone number in an error message.

    `"401" in err` also matched an ASIN, track id or URL that merely contains those
    digits (B00401XYZ, an Apple id 1404991234), turning an unrelated failure into
    "credentials expired" or "404, do not retry".
    """
    import re
    return re.search(rf"(?<![A-Za-z0-9]){code}(?![A-Za-z0-9])", text or "") is not None


def ensure_complete_transfer(response, written: int, label: str) -> None:
    """Raise if a streamed body is shorter than the Content-Length it announced.

    iter_content() does not always raise when a connection is cut early (it depends
    on the urllib3 version), so a short transfer would otherwise be accepted as a
    finished file. Skipped when the length is absent (relay streams) or the body is
    content-encoded (Content-Length then counts the compressed bytes).
    """
    cl = response.headers.get("Content-Length")
    if not cl or response.headers.get("Content-Encoding"):
        return
    try:
        expected = int(cl)
    except ValueError:
        return
    if written < expected:
        raise RuntimeError(
            f"[{label}] Truncated download: got {written}/{expected} bytes - "
            "the stream was cut short"
        )


def humanize_network_error(message: str, service: str) -> str:
    """Turn a raw TLS/connection error into something a user can act on.

    Returns the input unchanged when it is not a recognised network failure, so
    callers can wrap their own error text safely.
    """
    text = message or ""
    lowered = text.lower()

    if "wrong_version_number" in lowered or "record layer failure" in lowered:
        return (
            f"[{service}] Could not establish a secure connection to the {service} mirror. "
            "The server replied with something that isn't HTTPS — this is almost always a "
            "network issue on your side (a VPN, proxy, corporate firewall, or public Wi-Fi "
            "portal intercepting the connection), not a server outage. Try disabling any "
            "proxy/VPN, or switching network."
        )
    if "certificate verify failed" in lowered or "certificate_verify_failed" in lowered:
        return (
            f"[{service}] The {service} mirror's security certificate could not be verified. "
            "This usually means a proxy, antivirus, or corporate firewall is inspecting HTTPS "
            "traffic. Try disabling HTTPS scanning or switching network."
        )
    if "sslerror" in lowered or "ssl:" in lowered or "tlsv1" in lowered:
        return (
            f"[{service}] A secure-connection (TLS) error occurred while contacting the "
            f"{service} mirror. This is usually a local network, VPN, or proxy problem rather "
            "than a server outage. Try again, or switch network."
        )
    if ("max retries exceeded" in lowered or "connection refused" in lowered
            or "failed to establish a new connection" in lowered
            or "name or service not known" in lowered or "nameresolutionerror" in lowered):
        return (
            f"[{service}] Could not reach the {service} mirror. Check your internet connection — "
            "if you are on a network that blocks unfamiliar domains, a VPN usually fixes this."
        )
    return text


def normalize_mirror_url(raw: str, service: str = "Mirror") -> "str | None":
    r"""Normalize one mirror entry to a usable https URL, or drop it.

    Mirror lists arrive from a remote gist manifest and were never scheme-checked:
    a scheme-less entry reached `requests` as `MissingSchema`, and an `http://`
    entry silently downgraded the connection.

    Loopback hosts deliberately stay on http — the local mirror servers speak
    plaintext, and pointing https:// at a plaintext port is itself one of the few
    things that genuinely produces WRONG_VERSION_NUMBER.
    """
    url = (raw or "").strip().rstrip("/")
    if not url:
        return None
    host_part = url.split("://", 1)[1] if "://" in url else url
    is_loopback = host_part.startswith(("127.0.0.1", "localhost", "[::1]", "0.0.0.0"))
    if "://" not in url:
        url = ("http://" if is_loopback else "https://") + url
    elif url.startswith("http://") and not is_loopback:
        logger.warning(
            "[%s] Mirror %s is configured as plaintext http:// — upgrading to https. "
            "A plaintext URL against a TLS port is a known cause of connection errors.",
            service, url,
        )
        url = "https://" + url[len("http://"):]
    elif not url.startswith(("http://", "https://")):
        logger.warning("[%s] Ignoring mirror entry with unsupported scheme: %s", service, url)
        return None
    return url


def dedupe_mirrors(raw_list, service: str = "Mirror") -> list:
    """Normalize then dedupe. Order matters: the incoming list is deduped on raw
    strings, so `host`, `http://host` and `https://host/` survive as three
    entries and only collapse once normalized — without this the pool treats one
    host as three mirrors, burning retries and skewing failure counts.
    """
    out, seen = [], set()
    for raw in raw_list or []:
        norm = normalize_mirror_url(raw, service)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out
