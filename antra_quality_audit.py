from pathlib import Path
from collections import defaultdict
import hashlib
import csv
import sys

try:
    from mutagen import File as MutagenFile
except ImportError:
    print("ERROR: mutagen is not installed.")
    print("Run: python -m pip install mutagen")
    sys.exit(1)

ROOT = Path(r"C:\Users\sndee\Music\English")

AUDIO_EXTENSIONS = {
    ".flac", ".alac", ".m4a", ".mp3", ".aac",
    ".wav", ".aiff", ".aif", ".ogg", ".opus",
    ".wv", ".ape"
}

REPORT = ROOT / "antra_quality_report.csv"
DUPLICATES = ROOT / "antra_duplicate_candidates.csv"


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
        "status": "OK",
        "error": "",
    }

    try:
        audio = MutagenFile(path, easy=False)

        if audio is None:
            result["status"] = "UNREADABLE"
            result["error"] = "Mutagen could not identify the audio file"
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

    except Exception as e:
        result["status"] = "ERROR"
        result["error"] = repr(e)

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
        print(f"ERROR: Folder does not exist:\n{ROOT}")
        sys.exit(1)

    files = sorted(
        p for p in ROOT.rglob("*")
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )

    print("=" * 70)
    print("ANTRA MUSIC LIBRARY QUALITY CHECK")
    print("=" * 70)
    print(f"Folder: {ROOT}")
    print(f"Audio files found: {len(files)}")
    print()

    if not files:
        print("No supported audio files found.")
        return

    rows = []

    for i, path in enumerate(files, 1):
        print(f"[{i}/{len(files)}] {path.name}")
        rows.append(inspect_file(path))

    by_hash = defaultdict(list)
    for row in rows:
        if row["sha256"]:
            by_hash[row["sha256"]].append(row)

    exact_groups = [group for group in by_hash.values() if len(group) > 1]

    by_track = defaultdict(list)

    for row in rows:
        key = (normalize(row["artist"]), normalize(row["title"]))
        if key != ("", ""):
            by_track[key].append(row)

    track_groups = [group for group in by_track.values() if len(group) > 1]

    fieldnames = list(rows[0].keys()) + ["quality_score"]

    with REPORT.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for row in rows:
            output = dict(row)
            output["quality_score"] = quality_score(row)
            writer.writerow(output)

    with DUPLICATES.open("w", newline="", encoding="utf-8-sig") as f:
        fields = [
            "group_type",
            "artist",
            "title",
            "album",
            "path",
            "extension",
            "size_mb",
            "codec",
            "sample_rate",
            "bit_depth",
            "bitrate_kbps",
            "duration_sec",
            "lossless",
            "sha256",
            "quality_score",
        ]

        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()

        group_number = 0

        for group in exact_groups:
            group_number += 1
            for row in sorted(group, key=quality_score, reverse=True):
                writer.writerow({
                    "group_type": f"EXACT_DUPLICATE_{group_number}",
                    "artist": row["artist"],
                    "title": row["title"],
                    "album": row["album"],
                    "path": row["path"],
                    "extension": row["extension"],
                    "size_mb": row["size_mb"],
                    "codec": row["codec"],
                    "sample_rate": row["sample_rate"],
                    "bit_depth": row["bit_depth"],
                    "bitrate_kbps": row["bitrate_kbps"],
                    "duration_sec": row["duration_sec"],
                    "lossless": row["lossless"],
                    "sha256": row["sha256"],
                    "quality_score": quality_score(row),
                })

        for group in track_groups:
            group_number += 1
            ranked = sorted(group, key=quality_score, reverse=True)

            for row in ranked:
                writer.writerow({
                    "group_type": f"SAME_ARTIST_TITLE_{group_number}",
                    "artist": row["artist"],
                    "title": row["title"],
                    "album": row["album"],
                    "path": row["path"],
                    "extension": row["extension"],
                    "size_mb": row["size_mb"],
                    "codec": row["codec"],
                    "sample_rate": row["sample_rate"],
                    "bit_depth": row["bit_depth"],
                    "bitrate_kbps": row["bitrate_kbps"],
                    "duration_sec": row["duration_sec"],
                    "lossless": row["lossless"],
                    "sha256": row["sha256"],
                    "quality_score": quality_score(row),
                })

    print()
    print("=" * 70)
    print("AUDIT COMPLETE")
    print("=" * 70)
    print(f"Total audio files       : {len(rows)}")
    print(f"Exact duplicate groups  : {len(exact_groups)}")
    print(f"Same artist/title groups: {len(track_groups)}")
    print()
    print(f"Full report:")
    print(REPORT)
    print()
    print(f"Duplicate report:")
    print(DUPLICATES)
    print()
    print("NO FILES WERE DELETED OR MOVED.")
    print("=" * 70)


if __name__ == "__main__":
    main()
