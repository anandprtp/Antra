from __future__ import annotations

from urllib.parse import urlparse


def http_hostname(value: str) -> str | None:
    """Return a normalized hostname only for ordinary credential-free HTTP URLs."""
    try:
        parsed = urlparse((value or "").strip())
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    return parsed.hostname.casefold().rstrip(".")


def is_exact_http_host(value: str, *allowed_hosts: str) -> bool:
    hostname = http_hostname(value)
    return hostname is not None and hostname in {
        host.casefold().rstrip(".") for host in allowed_hosts
    }


def is_http_host_or_subdomain(value: str, parent_host: str) -> bool:
    hostname = http_hostname(value)
    parent = parent_host.casefold().rstrip(".")
    return hostname == parent or (
        hostname is not None and hostname.endswith(f".{parent}")
    )


def is_amazon_music_host(value: str) -> bool:
    hostname = http_hostname(value)
    return hostname in {
        "music.amazon.com",
        "music.amazon.ca",
        "music.amazon.com.mx",
        "music.amazon.com.br",
        "music.amazon.co.uk",
        "music.amazon.de",
        "music.amazon.fr",
        "music.amazon.it",
        "music.amazon.es",
        "music.amazon.in",
        "music.amazon.co.jp",
        "music.amazon.com.au",
    }
