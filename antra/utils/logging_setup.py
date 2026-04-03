"""
Logging configuration for Antra.
Writes to console (colored) and a rotating file log.
"""
import logging
import os
from pathlib import Path
from logging.handlers import RotatingFileHandler

import sys
if sys.platform == "win32":
    if getattr(sys, "stdout", None) and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if getattr(sys, "stderr", None) and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class SafeConsoleHandler(logging.StreamHandler):
    """Console handler that degrades gracefully on narrow Windows encodings."""

    def emit(self, record):
        try:
            msg = self.format(record)
            stream = self.stream
            encoding = getattr(stream, "encoding", None) or "utf-8"
            payload = (msg + self.terminator).encode(encoding, errors="replace")
            buffer = getattr(stream, "buffer", None)
            if buffer is not None:
                buffer.write(payload)
                buffer.flush()
            else:
                stream.write(payload.decode(encoding, errors="replace"))
                stream.flush()
        except Exception:
            self.handleError(record)


class SuppressConsoleNoiseFilter(logging.Filter):
    """Hide noisy third-party/API fallback logs from console while keeping file logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "spotipy.client":
            return False
        if record.name == "antra.core.spotify":
            message = record.getMessage()
            if "Spotify track API unavailable" in message:
                return False
        return True

def _default_log_dir() -> str:
    cwd = Path.cwd()
    if not getattr(sys, "frozen", False):
        return str(cwd)

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return str(Path(base) / "Antra" / "logs")

    return str(Path.home() / ".antra" / "logs")


def setup_logging(log_dir: str | None = None, level: int = logging.INFO, verbose: bool = False):
    target_dir = log_dir or _default_log_dir()
    os.makedirs(target_dir, exist_ok=True)
    log_path = os.path.join(target_dir, "antra.log")

    fmt = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    # Console format: just the message — no logger name, no level noise.
    console_fmt = "%(message)s"

    handlers = [RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")]
    if getattr(sys, "stderr", None) is not None:
        console_handler = SafeConsoleHandler()
        if not verbose:
            console_handler.setFormatter(logging.Formatter(fmt=console_fmt, datefmt=datefmt))
            console_handler.addFilter(SuppressConsoleNoiseFilter())
        else:
            console_handler.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
            level = logging.DEBUG
        handlers.insert(0, console_handler)

    # Root logger — suppress everything by default
    logging.getLogger().setLevel(logging.WARNING)
    logging.basicConfig(level=level, handlers=handlers)

    if not verbose:
        # Suppress noisy internals — but let the key loggers show INFO to the user
        for logger_name in [
            "antra.sources.hifi",
            "antra.sources.yams",
            "antra.sources.dab",
            "antra.sources.jiosaavn",
            "antra.sources.amazon",
            "antra.sources.apple",
            "antra.sources.youtube",
            "antra.sources.qobuz",
            "antra.core.resolver",
            "urllib3",
        ]:
            logging.getLogger(logger_name).setLevel(logging.WARNING)

        # Key loggers: show INFO so the user sees download progress and auth status
        for logger_name in [
            "antra.core.engine",
            "antra.core.spotify_auth",
            "antra.core.spotify",
        ]:
            logging.getLogger(logger_name).setLevel(logging.INFO)

        # Service [OK] adapter init messages are internal noise — suppress in normal mode
        logging.getLogger("antra.core.service").setLevel(logging.WARNING)
    else:
        for lib in ("spotipy", "urllib3", "requests", "yt_dlp"):
            logging.getLogger(lib).setLevel(logging.WARNING)
