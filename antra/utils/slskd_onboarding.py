"""
Native Soulseek (slskd) setup and integration manager.
"""
import getpass
import json
import logging
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import time
import zipfile
import tarfile
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

from antra.core.config import Config, REPO_ROOT

logger = logging.getLogger(__name__)

SLSKD_DIR = Path.home() / ".antra" / "slskd"
SLSKD_BIN_DIR = SLSKD_DIR / "bin"
SLSKD_CONFIG_DIR = SLSKD_DIR / "config"
SLSKD_DOWNLOADS_DIR = SLSKD_DIR / "downloads"


def is_slskd_reachable(base_url: str, api_key: str = "") -> bool:
    """Check if the slskd API is up and running."""
    if not base_url:
        return False
    endpoint = f"{base_url.rstrip('/')}/api/v0/application"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key
        
    try:
        req = Request(endpoint, headers=headers)
        with urlopen(req, timeout=2.0) as resp:
            return resp.status == 200
    except Exception:
        return False


def find_free_port() -> int:
    """Finds an available local port dynamically."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def get_latest_slskd_release_url() -> str:
    """Fetch the latest release download URL matching the current OS."""
    system = platform.system().lower()
    arch = platform.machine().lower()
    
    if system == "windows":
        target = "win-x64.zip"
    elif system == "darwin":
        if arch in ["arm64", "aarch64"]:
            target = "osx-arm64.zip"
        else:
            target = "osx-x64.zip"
    elif system == "linux":
        if arch in ["arm64", "aarch64"]:
            target = "linux-arm64.zip"
        elif arch in ["armv7l"]:
            target = "linux-arm.zip"
        else:
            target = "linux-x64.zip"
    else:
        raise RuntimeError(f"Unsupported OS: {system}")

    req = Request("https://api.github.com/repos/slskd/slskd/releases/latest", headers={"User-Agent": "Antra"})
    with urlopen(req, timeout=10.0) as resp:
        data = json.loads(resp.read().decode())
    
    for asset in data.get("assets", []):
        name = asset.get("name", "").lower()
        if "slskd" in name and target in name:
            return asset.get("browser_download_url")
            
    raise RuntimeError(f"Could not find a valid slskd release for '{target}'.")


def download_and_extract_slskd():
    """Download and extract the slskd binary to the local system."""
    url = get_latest_slskd_release_url()
    logger.info(f"Downloading slskd from {url}...")
    
    SLSKD_BIN_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = SLSKD_DIR / url.split("/")[-1]
    
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=30.0) as resp, open(archive_path, "wb") as f:
        shutil.copyfileobj(resp, f)
        
    logger.info("Extracting slskd binary...")
    if archive_path.suffix == ".zip":
        with zipfile.ZipFile(archive_path, 'r') as zip_ref:
            for member in zip_ref.infolist():
                extracted_path = (SLSKD_BIN_DIR / member.filename).resolve()
                if not extracted_path.is_relative_to(SLSKD_BIN_DIR.resolve()):
                    raise RuntimeError(f"Zip slip vulnerability detected for {member.filename}")
                zip_ref.extract(member, SLSKD_BIN_DIR)
    elif archive_path.suffix == ".gz":
        with tarfile.open(archive_path, "r:gz") as tar_ref:
            for member in tar_ref.getmembers():
                extracted_path = (SLSKD_BIN_DIR / member.name).resolve()
                if not extracted_path.is_relative_to(SLSKD_BIN_DIR.resolve()):
                    raise RuntimeError(f"Tar slip vulnerability detected for {member.name}")
                tar_ref.extract(member, SLSKD_BIN_DIR, set_attrs=False)
            
    # Cleanup archive
    archive_path.unlink()
    
    # Make executable on Linux/Mac
    if platform.system().lower() != "windows":
        executable = SLSKD_BIN_DIR / "slskd"
        if executable.exists():
            executable.chmod(0o755)


def generate_config(username, password, api_key, port):
    """Generate the slskd.yml configuration file."""
    SLSKD_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SLSKD_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    
    config_content = f"""
soulseek:
  username: {username}
  password: {password}
shares:
  directories:
    downloads: {SLSKD_DOWNLOADS_DIR.absolute().as_posix()}
web:
  port: {port}
  https:
    disabled: true
  authentication:
    disabled: false
    api_keys:
      antra:
        key: {api_key}
        role: administrator
"""
    config_file = SLSKD_CONFIG_DIR / "slskd.yml"
    config_file.write_text(config_content.strip())
    return config_file


def save_env_variables(base_url, api_key):
    """Write or append the generated config variables to .env."""
    env_file = REPO_ROOT / ".env"
    env_copy = ""
    
    if env_file.exists():
        env_copy = env_file.read_text()
        
    lines = env_copy.splitlines()
    new_lines = []
    
    for line in lines:
        if line.startswith("SLSKD_BASE_URL=") or line.startswith("SLSKD_API_KEY="):
            continue
        new_lines.append(line)
        
    new_lines.append(f"SLSKD_BASE_URL={base_url}")
    new_lines.append(f"SLSKD_API_KEY={api_key}")
    
    env_file.write_text("\n".join(new_lines) + "\n")
    logger.info(f"Saved slskd configuration to {env_file}")


def start_slskd(config_dir: Path):
    """Launch slskd binary in the background seamlessly."""
    system = platform.system().lower()
    
    if system == "windows":
        executable = str(SLSKD_BIN_DIR / "slskd.exe")
        creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)
        subprocess.Popen(
            [executable, "--app-dir", str(config_dir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags
        )
    else:
        executable = str(SLSKD_BIN_DIR / "slskd")
        subprocess.Popen(
            [executable, "--app-dir", str(config_dir)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )


def _interactive_setup():
    print("  Soulseek provides lossless FLAC downloads without subscriptions.")
    choice = input("  Set up Soulseek integration now? [y/N]: ").strip().lower()
    if choice not in {"y", "yes"}:
        return None, None
        
    print("\n  Enter your Soulseek network credentials.")
    print("  If you don't have an account, enter a new username and password.")
    
    while True:
        username = input("  Username: ").strip()
        if not username:
            continue
        password = getpass.getpass("  Password: ").strip()
        if not password:
            print("  Password cannot be empty.")
            continue
        return username, password

def ensure_slskd(cfg: Config):
    """
    Ensure slskd is natively running. Setup an instance if none is available.
    Mutates cfg.soulseek_base_url and cfg.soulseek_api_key on success.
    """
    if not cfg.soulseek_auto_bootstrap:
        return
        
    # 1. Check if the daemon is already running (either manual or managed)
    if cfg.soulseek_base_url and is_slskd_reachable(cfg.soulseek_base_url, cfg.soulseek_api_key):
        return

    # 2. Check if we already have a managed setup downloaded and configured
    config_file = SLSKD_CONFIG_DIR / "slskd.yml"
    if config_file.exists() and SLSKD_BIN_DIR.exists():
        # Daemon is offline, attempt to silently restart it using saved config port
        # Assume cfg.soulseek_base_url has the local port if they used the native setup.
        if cfg.soulseek_base_url:
            print(f"  [slskd] Resuming native background process at {cfg.soulseek_base_url}...")
            start_slskd(SLSKD_CONFIG_DIR)
            
            # Wait for it to wake up
            for _ in range(15):
                if is_slskd_reachable(cfg.soulseek_base_url, cfg.soulseek_api_key):
                    return
                time.sleep(1)
        # If it still isn't awake, or we somehow lost the port in .env, fallthrough to setup

    # 3. Setup is required (missing config or broken)
    username, password = _interactive_setup()
    if not username or not password:
        return
        
    print("\n  [slskd] Downloading native binary...")
    try:
        download_and_extract_slskd()
    except Exception as e:
        print(f"  [slskd] Setup failed: Failed to download binary ({e})")
        return
        
    port = find_free_port()
    api_key = secrets.token_hex(16)
    base_url = f"http://127.0.0.1:{port}"
    
    generate_config(username, password, api_key, port)
    
    print(f"  [slskd] Starting background process on port {port}...")
    start_slskd(SLSKD_CONFIG_DIR)
    
    up = False
    for _ in range(15):
        if is_slskd_reachable(base_url, api_key):
            up = True
            break
        time.sleep(1)
        
    if not up:
        print("  [slskd] Warning: API took too long to become responsive. Adapter may fail.")
        
    save_env_variables(base_url, api_key)
    cfg.soulseek_base_url = base_url
    cfg.soulseek_api_key = api_key
    
    print("  [slskd] Successfully configured!")
