from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class DeliveryKind(Enum):
    AUDIO = "audio"
    DOCUMENT = "document"
    VLC = "vlc"


@dataclass(frozen=True)
class DeliveryDecision:
    kind: DeliveryKind
    size_bytes: int


def choose_delivery(path: Path, max_upload_bytes: int, mode: str = "auto") -> DeliveryDecision:
    size = path.stat().st_size
    suffix = path.suffix.casefold()
    if mode == "vlc" or size > max_upload_bytes:
        return DeliveryDecision(DeliveryKind.VLC, size)
    if suffix in {".mp3", ".m4a"}:
        return DeliveryDecision(DeliveryKind.AUDIO, size)
    return DeliveryDecision(DeliveryKind.DOCUMENT, size)
