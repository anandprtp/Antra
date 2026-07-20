from .models import TrackAsset


def render_m3u8(asset: TrackAsset, media_url: str) -> str:
    label = asset.display_name.replace("\r", " ").replace("\n", " ")
    duration = int(asset.duration_seconds) if asset.duration_seconds else -1
    return f"#EXTM3U\n#EXTINF:{duration},{label}\n{media_url}\n"
