"""
Managed slskd bootstrap/start helpers.

This module keeps Antra lightweight by downloading slskd only when needed.
It is intentionally best-effort: failures are logged and do not crash the app.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import secrets
import socket
import subprocess
import time
import zipfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

_LATEST_RELEASE_API = "https://api.github.com/repos/slskd/slskd/releases/latest"
_DEFAULT_BASE_URL = "http://127.0.0.1:5030"


class SlskdBootstrapManager:
    """Download and start a managed slskd daemon for Soulseek source support."""

    def __init__(self, app_name: str = "antra"):
        self._app_name = app_name
        self._root = self._resolve_root_dir()
        self._bin_dir = self._root / "bin"
        self._runtime_dir = self._root / "runtime"
        self._state_file = self._runtime_dir / "state.json"
        self._stdout_log = self._runtime_dir / "slskd.stdout.log"
        self._stderr_log = self._runtime_dir / "slskd.stderr.log"

    def ensure_running(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        timeout_seconds: float = 20.0,
        username: str = "",
        password: str = "",
    ) -> Optional[dict[str, str]]:
        """
        Ensure a managed slskd process is running and reachable.

        Returns a dict suitable for config hydration:
          {"base_url": "...", "api_key": "..."}
        or None if bootstrap/start failed.
        """
        base_url = self._resolve_base_url(base_url)
        api_key = self._read_api_key()

        if self._is_reachable(base_url, api_key=api_key):
            if api_key:
                return {"base_url": base_url, "api_key": api_key}
            else:
                if self._locate_slskd_exe() is not None:
                    logger.warning("[Soulseek] slskd reachable but API key missing. Forcing kill to recover.")
                    self._kill_managed_slskd()
                    time.sleep(1.5)
                elif username and password:
                    # External slskd is running but we have no managed binary or API
                    # key for it.  Since the user provided Soulseek credentials, replace
                    # it with a Antra-managed instance so we can control authentication.
                    logger.info(
                        "[Soulseek] External slskd at %s detected; replacing with managed instance.",
                        base_url,
                    )
                    self._kill_managed_slskd()
                    time.sleep(1.5)
                else:
                    logger.info(
                        "[Soulseek] External slskd detected at %s without a managed API key — "
                        "skipping bootstrap to avoid disrupting it.",
                        base_url,
                    )
                    return None

        exe = self._locate_slskd_exe()
        if exe is None:
            exe = self._download_and_extract_latest()
            if exe is None:
                return None

        api_key = self._read_api_key()
        if not api_key:
            api_key = secrets.token_urlsafe(32)
            self._write_api_key(api_key)

        self._write_config(
            base_url=base_url,
            api_key=api_key,
            username=username,
            password=password,
        )

        # Kill any surviving slskd that may still be holding the port before
        # starting our managed instance with the fresh config.
        if self._is_reachable(base_url):
            logger.debug("[Soulseek] Killing leftover slskd process before restart.")
            self._kill_managed_slskd()
            time.sleep(1.5)

        self._start_process(exe)
        if not self._wait_until_reachable(base_url, timeout_seconds, api_key=api_key):
            logger.warning("[Soulseek] Managed slskd did not become reachable in time.")
            return None
        state = self._read_state()
        state["base_url"] = base_url
        self._write_state(state)
        web_password = self._get_or_create_web_password()
        logger.info(
            "[Soulseek] slskd web UI available at %s — login: username=slskd  password=%s",
            base_url, web_password,
        )
        return {"base_url": base_url, "api_key": api_key}

    def _resolve_root_dir(self) -> Path:
        if os.name == "nt":
            local = os.getenv("LOCALAPPDATA")
            if local:
                return Path(local) / self._app_name / "slskd"
        return Path.home() / ".cache" / self._app_name / "slskd"

    def _platform_asset_suffix(self) -> Optional[str]:
        system = platform.system().lower()
        machine = platform.machine().lower()
        if system == "windows":
            return "win-arm64.zip" if "arm" in machine else "win-x64.zip"
        if system == "linux":
            return "linux-arm64.zip" if "aarch64" in machine or "arm64" in machine else "linux-x64.zip"
        if system == "darwin":
            return "osx-arm64.zip" if "arm" in machine else "osx-x64.zip"
        return None

    def _download_and_extract_latest(self) -> Optional[Path]:
        suffix = self._platform_asset_suffix()
        if not suffix:
            logger.warning("[Soulseek] Unsupported platform for managed slskd bootstrap.")
            return None

        logger.info("[Soulseek] Downloading slskd (%s) for first-run bootstrap...", suffix)
        try:
            rel = requests.get(_LATEST_RELEASE_API, timeout=15)
            rel.raise_for_status()
            data = rel.json()
        except Exception as exc:
            logger.warning(f"[Soulseek] Failed to fetch slskd release metadata: {exc}")
            return None

        assets = data.get("assets", []) if isinstance(data, dict) else []
        chosen = None
        for asset in assets:
            name = str(asset.get("name", ""))
            if name.endswith(suffix):
                chosen = asset
                break
        if not chosen:
            logger.warning(f"[Soulseek] No release asset matched platform suffix: {suffix}")
            return None

        url = chosen.get("browser_download_url")
        name = chosen.get("name", "slskd.zip")
        if not url:
            logger.warning("[Soulseek] Release asset had no download URL.")
            return None

        self._bin_dir.mkdir(parents=True, exist_ok=True)
        zip_path = self._bin_dir / str(name)
        try:
            with requests.get(url, stream=True, timeout=30) as resp:
                resp.raise_for_status()
                with open(zip_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
        except Exception as exc:
            logger.warning(f"[Soulseek] Failed to download slskd archive: {exc}")
            return None

        extract_dir = self._bin_dir / "current"
        extract_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(extract_dir)
        except Exception as exc:
            logger.warning(f"[Soulseek] Failed to extract slskd archive: {exc}")
            return None

        exe = self._locate_slskd_exe()
        if exe is None:
            logger.warning("[Soulseek] slskd executable not found after extraction.")
            return None
        # zipfile.extractall() does not restore Unix permissions from the zip
        # metadata, so the binary lands as non-executable on macOS/Linux.
        if platform.system() != "Windows":
            os.chmod(exe, 0o755)
        return exe

    def _resolve_base_url(self, requested_base_url: str) -> str:
        state = self._read_state()
        saved_base_url = str(state.get("base_url") or "").strip()
        if saved_base_url:
            return saved_base_url

        requested_base_url = (requested_base_url or "").strip()
        if requested_base_url and requested_base_url != _DEFAULT_BASE_URL:
            parsed = urlparse(requested_base_url)
            if parsed.scheme and parsed.netloc:
                return requested_base_url

        port = self._find_free_port(preferred_port=5030)
        default_host = urlparse(_DEFAULT_BASE_URL).hostname or "127.0.0.1"
        return f"http://{default_host}:{port}"

    @staticmethod
    def _find_free_port(preferred_port: int) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            try:
                sock.bind(("127.0.0.1", preferred_port))
                return preferred_port
            except OSError:
                pass

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _locate_slskd_exe(self) -> Optional[Path]:
        root = self._bin_dir / "current"
        if not root.exists():
            return None
        names = {"slskd.exe", "slskd"}
        for path in root.rglob("*"):
            if path.is_file() and path.name.lower() in names:
                return path
        return None

    def _start_process(self, exe: Path) -> None:
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        
        data_dir = self._runtime_dir / "data"
        dl_dir = self._runtime_dir / "downloads"
        inc_dir = self._runtime_dir / "incomplete"
        
        for d in [data_dir, dl_dir, inc_dir]:
            d.mkdir(parents=True, exist_ok=True)
            
        # Ensure wwwroot exists next to the executable, otherwise slskd web server crashes on startup
        (exe.parent / "wwwroot").mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        for k in list(env.keys()):
            if k.startswith("SLSKD_") or k.startswith("SOULSEEK_"):
                env.pop(k, None)

        stdout = open(self._stdout_log, "a", encoding="utf-8")
        stderr = open(self._stderr_log, "a", encoding="utf-8")
        kwargs = {
            "cwd": str(exe.parent),
            "env": env,
            "stdout": stdout,
            "stderr": stderr,
            "stdin": subprocess.DEVNULL,
            "shell": False,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

        cmd = [
            str(exe),
            "--app-dir", str(self._runtime_dir),
            "--downloads", str(dl_dir),
            "--incomplete", str(inc_dir),
            "--no-https"
        ]

        proc = subprocess.Popen(cmd, **kwargs)  # noqa: S603
        state = self._read_state()
        state.update({"pid": proc.pid, "started_at": time.time()})
        self._write_state(state)

    def _wait_until_reachable(self, base_url: str, timeout_seconds: float, api_key: str = "") -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self._is_reachable(base_url, api_key=api_key):
                return True
            time.sleep(0.5)
        return False

    @staticmethod
    def _is_reachable(base_url: str, api_key: str = "") -> bool:
        url = base_url.rstrip("/") + "/api/v0/application"
        try:
            headers = {"X-API-Key": api_key} if api_key else {}
            resp = requests.get(url, headers=headers, timeout=2)
            if api_key:
                return resp.status_code == 200
            return resp.status_code in {200, 401, 403}
        except Exception:
            return False

    def _kill_managed_slskd(self) -> None:
        """Kill orphans if we don't know the API key but daemon is running locally."""
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/f", "/im", "slskd.exe"], capture_output=True, check=False, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                subprocess.run(["pkill", "-9", "-f", "slskd"], capture_output=True, check=False)
            time.sleep(1.0)
        except Exception as exc:
            logger.debug(f"[Soulseek] Failed to kill orphaned slskd process: {exc}")

    def _read_api_key(self) -> Optional[str]:
        state = self._read_state()
        value = state.get("api_key")
        return str(value) if value else None

    def _write_api_key(self, api_key: str) -> None:
        state = self._read_state()
        state["api_key"] = api_key
        self._write_state(state)

    def _read_state(self) -> dict:
        if not self._state_file.exists():
            return {}
        try:
            return json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_state(self, state: dict) -> None:
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def _write_config(
        self,
        base_url: str,
        api_key: str,
        username: str,
        password: str,
    ) -> None:
        self._runtime_dir.mkdir(parents=True, exist_ok=True)

        parsed = urlparse(base_url)
        port = parsed.port or 5030
        downloads_dir = (self._runtime_dir / "downloads").resolve().as_posix()
        incomplete_dir = (self._runtime_dir / "incomplete").resolve().as_posix()
        data_dir = (self._runtime_dir / "data").resolve().as_posix()

        # Generate a stable web UI password stored in state so the user can log in
        web_password = self._get_or_create_web_password()

        config_content = "\n".join(
            [
                f"instance_name: {json.dumps('Antra Managed slskd')}",
                "directories:",
                f"  downloads: {json.dumps(downloads_dir)}",
                f"  incomplete: {json.dumps(incomplete_dir)}",
                f"  data: {json.dumps(data_dir)}",
                "soulseek:",
                f"  username: {json.dumps(username)}",
                f"  password: {json.dumps(password)}",
                "web:",
                f"  port: {port}",
                "  https:",
                "    disabled: true",
                "  authentication:",
                "    disabled: false",
                "    username: slskd",
                f"    password: {json.dumps(web_password)}",
                "    api_keys:",
                "      antra:",
                f"        key: {json.dumps(api_key)}",
                "        role: administrator",
            ]
        ) + "\n"

        config_file = self._runtime_dir / "slskd.yml"
        config_file.write_text(config_content, encoding="utf-8")

    def _get_or_create_web_password(self) -> str:
        state = self._read_state()
        pwd = state.get("web_password")
        if not pwd:
            pwd = secrets.token_urlsafe(12)
            state["web_password"] = pwd
            self._write_state(state)
        return str(pwd)

    def get_web_ui_info(self) -> Optional[dict[str, str]]:
        """Return {url, username, password} for the slskd web UI, or None if not running."""
        state = self._read_state()
        base_url = state.get("base_url")
        web_password = state.get("web_password")
        if not base_url:
            return None
        return {
            "url": str(base_url),
            "username": "slskd",
            "password": str(web_password) if web_password else "",
        }

