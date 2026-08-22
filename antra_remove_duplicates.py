from pathlib import Path
from collections import defaultdict
import csv
import hashlib
import sys

try:
    from mutagen import File as MutagenFile
except ImportError:
    print("ERROR: mutagen is not installed.")
    print("Run: python -m pip install mutagen")
    sys.exit(1)

ROOT = Path(r"C:\Users\sndee\Music\English")
BACKUP_DIR = ROOT / "antra_duplicate_backup"
LOW_QUALITY_DIR = ROOT / "antra_low_quality_duplicates"
LOG_CSV = ROOT / "antra_duplicate_cleanup_log.csv"
AUDIO_EXTENSIONS = {
    ".flac", ".alac", ".m4a", ".mp3", ".aac",
    ".wav", ".aiff", ".aif", ".ogg", ".opus",
    ".wv", ".ape"
}


def sha256_file(path, chunk_size=1024 * 1024):
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def first_tag(tags, names):
    if not tags:
        return ""
    for name in names:
        try:
            value = tags.get(name)
            if value:
                if isinstance(value, list):
                    return str(value[0])
                return str(value)
        except Exception:
            pass
    lowered = {str(k).lower(): v for k, v in tags.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value:
            if isinstance(value, list):
                return str(value[0])
            return str(value)
    return ""


def normalize(text):
    return " ".join(str(text).lower().strip().split())


def inspect_file(path):
    result = {
        "path": str(path),
        "filename": path.name,
        "extension": path.suffix.lower(),
        "size_mb": round(path.stat().st_size / (1024 * 1024), 2),
        "artist": "",
        "title": "",
        "album": "",
        "duration_sec": "",
        "codec": "",
        "sample_rate": "",
        "bit_depth": "",
        "channels": "",
        "bitrate_kbps": "",
        "lossless": "",
        "sha256": "",
    }
    try:
        audio = MutagenFile(path, easy=False)
        if audio is None:
            return result
        tags = getattr(audio, "tags", None)
        info = getattr(audio, "info", None)
        result["artist"] = first_tag(tags, ["artist", "ARTIST", "\xa9ART"])
        result["title"] = first_tag(tags, ["title", "TITLE", "\xa9nam"])
        result["album"] = first_tag(tags, ["album", "ALBUM", "\xa9alb"])
        if info:
            length = getattr(info, "length", None)
            if length:
                result["duration_sec"] = round(float(length), 2)
            sample_rate = getattr(info, "sample_rate", None)
            if sample_rate:
                result["sample_rate"] = sample_rate
            channels = getattr(info, "channels", None)
            if channels:
                result["channels"] = channels
            bitrate = getattr(info, "bitrate", None)
            if bitrate:
                result["bitrate_kbps"] = round(bitrate / 1000)
            bits = getattr(info, "bits_per_sample", None)
            if bits:
                result["bit_depth"] = bits
        codec = getattr(info, "codec", None) if info else None
        if codec:
            result["codec"] = str(codec)
        else:
            result["codec"] = path.suffix.lower().replace(".", "").upper()
        ext = path.suffix.lower()
        if ext in {".flac", ".alac", ".wav", ".aiff", ".aif", ".wv", ".ape"}:
            result["lossless"] = "YES"
        elif ext in {".mp3", ".aac", ".m4a", ".ogg", ".opus"}:
            codec_lower = str(result["codec"]).lower()
            if "alac" in codec_lower or "apple lossless" in codec_lower:
                result["lossless"] = "YES"
            else:
                result["lossless"] = "NO"
        else:
            result["lossless"] = "UNKNOWN"
        result["sha256"] = sha256_file(path)
    except Exception:
        pass
    return result


def quality_score(row):
    score = 0
    if row["lossless"] == "YES":
        score += 1_000_000
    try:
        score += int(row["bit_depth"] or 0) * 10_000
    except Exception:
        pass
    try:
        score += int(row["sample_rate"] or 0)
    except Exception:
        pass
    try:
        score += int(row["bitrate_kbps"] or 0)
    except Exception:
        pass
    try:
        score += int(float(row["size_mb"]) * 10)
    except Exception:
        pass
    return score


def main():
    if not ROOT.exists():
        print(f"ERROR: Folder does not exist: {ROOT}")
        sys.exit(1)

    files = sorted(
        p for p in ROOT.rglob("*")
        if p.is_file()
        and p.suffix.lower() in AUDIO_EXTENSIONS
        and not p.is_relative_to(BACKUP_DIR)
        and not p.is_relative_to(LOW_QUALITY_DIR)
        and p.name not in {"antra_quality_report.csv", "antra_duplicate_candidates.csv", "antra_duplicate_cleanup_log.csv"}
    )

    groups = defaultdict(list)
    for path in files:
        meta = inspect_file(path)
        h = meta.get("sha256")
        if h:
            groups[h].append((path, meta))

    moved = 0
    kept = 0
    rows = []
    BACKUP_DIR.mkdir(exist_ok=True)
    LOW_QUALITY_DIR.mkdir(exist_ok=True)

    for digest, items in sorted(groups.items()):
        if len(items) < 2:
            kept += 1
            continue

        ranked = sorted(items, key=lambda x: quality_score(x[1]), reverse=True)
        best_path, _ = ranked[0]

        for path, _ in ranked[1:]:
            if not path.exists():
                continue

            if path == best_path:
                continue

            dst = LOW_QUALITY_DIR / f"{best_path.stem}__low_quality__{path.name}"
            counter = 1
            while dst.exists():
                dst = LOW_QUALITY_DIR / f"{best_path.stem}__low_quality_{counter}__{path.name}"
                counter += 1

            try:
                path.replace(dst)
            except FileNotFoundError:
                continue

            rows.append({
                "kept_file": str(best_path),
                "moved_file": str(path),
                "backup_path": str(dst),
                "sha256": digest,
            })
            moved += 1

    with LOG_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["kept_file", "moved_file", "backup_path", "sha256"])
        writer.writeheader()
        writer.writerows(rows)

    print("=" * 70)
    print("ANTRA DUPLICATE REMOVAL SUMMARY")
    print("=" * 70)
    print(f"Folder: {ROOT}")
    print(f"Exact duplicate groups processed: {moved}")
    print(f"Files kept: {kept}")
    print(f"Low-quality duplicates moved: {moved}")
    print(f"Low-quality folder: {LOW_QUALITY_DIR}")
    print(f"Log file: {LOG_CSV}")
    print("=" * 70)


if __name__ == "__main__":
    main()
