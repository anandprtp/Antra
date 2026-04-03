import os
from pathlib import Path

def get_config_dir() -> str:
    """Return %LOCALAPPDATA%\\Antra, creating it if needed."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    p = Path(base) / "Antra"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)
