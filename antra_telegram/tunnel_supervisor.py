"""Keep a Cloudflare Quick Tunnel connected to the local player API.

Quick Tunnels receive a new public origin whenever cloudflared creates a new
tunnel. This supervisor records that origin in the bot's dotenv file and
restarts the bot so newly issued player links always contain the current API
origin.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import time
from pathlib import Path


LOGGER = logging.getLogger(__name__)
QUICK_TUNNEL_URL = re.compile(
    r"https://[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.trycloudflare\.com"
)


def update_dotenv_value(path: Path, key: str, value: str) -> bool:
    """Atomically upsert one dotenv value without exposing other secrets."""

    resolved = path.expanduser().resolve()
    original = resolved.read_text(encoding="utf-8") if resolved.exists() else ""
    rows = original.splitlines()
    replacement = f"{key}={value}"
    changed = False
    found = False
    updated: list[str] = []
    for row in rows:
        if row.startswith(f"{key}="):
            found = True
            if row != replacement:
                changed = True
            updated.append(replacement)
        else:
            updated.append(row)
    if not found:
        updated.append(replacement)
        changed = True
    if not changed:
        return False

    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary = resolved.with_name(f".{resolved.name}.tmp-{os.getpid()}")
    temporary.write_text("\n".join(updated) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(resolved)
    return True


def _restart_bot(label: str) -> None:
    target = f"gui/{os.getuid()}/{label}"
    result = subprocess.run(
        ["launchctl", "kickstart", "-k", target],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        LOGGER.warning("Could not restart %s (exit %s)", label, result.returncode)


def _cloudflared_command(binary: str, origin: str) -> list[str]:
    return [
        binary,
        "tunnel",
        "--no-autoupdate",
        "--protocol",
        "http2",
        "--url",
        origin,
    ]


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    binary = os.getenv("ANTRA_TUNNEL_CLOUDFLARED", "cloudflared")
    origin = os.getenv("ANTRA_TUNNEL_ORIGIN", "http://127.0.0.1:8090")
    env_file = Path(
        os.getenv("ANTRA_TUNNEL_ENV_FILE", ".env.telegram")
    ).expanduser()
    bot_label = os.getenv(
        "ANTRA_TUNNEL_BOT_LABEL",
        "com.glebstepanov.antra-telegram",
    )
    retry_seconds = max(2, int(os.getenv("ANTRA_TUNNEL_RETRY_SECONDS", "5")))
    stopping = False
    child: subprocess.Popen[str] | None = None

    def stop(signum, frame) -> None:
        nonlocal stopping
        stopping = True
        if child is not None and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    while not stopping:
        LOGGER.info("Starting Cloudflare tunnel for %s", origin)
        child = subprocess.Popen(
            _cloudflared_command(binary, origin),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert child.stdout is not None
        for line in child.stdout:
            match = QUICK_TUNNEL_URL.search(line)
            if match:
                public_origin = match.group(0)
                if update_dotenv_value(
                    env_file,
                    "ANTRA_TELEGRAM_PUBLIC_BASE_URL",
                    public_origin,
                ):
                    LOGGER.info("Player API origin updated: %s", public_origin)
                    _restart_bot(bot_label)
            elif "Registered tunnel connection" in line:
                LOGGER.info("Cloudflare tunnel connection registered")
            elif "ERR" in line or "error" in line.casefold():
                LOGGER.warning("cloudflared: %s", line.strip())

        exit_code = child.wait()
        child = None
        if stopping:
            break
        LOGGER.warning(
            "cloudflared exited with code %s; retrying in %s seconds",
            exit_code,
            retry_seconds,
        )
        time.sleep(retry_seconds)


if __name__ == "__main__":
    run()
