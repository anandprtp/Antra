"""
Antra CLI entry point.

Usage:
    python -m antra <spotify_url> [<spotify_url> ...] [options]
    antra <spotify_url> [<spotify_url> ...] [options]
"""
import argparse
import logging
import os
import sys
from collections import defaultdict

from antra.core.config import load_config
from antra.core.models import BulkDownloadProgress, DownloadStatus, SpotifyLibrary, SpotifyPlaylistSummary
from antra.core.spotify import SpotifyResourceError
from antra.core.service import (
    describe_output_format,
    OUTPUT_FORMAT_CHOICES,
    SOURCE_PREFERENCE_CHOICES,
    RuntimeOptions,
    AntraService,
    describe_source_preference,
    normalize_output_format,
    normalize_source_preference,
)
from antra.utils.cleanup import cleanup_project_junk, find_cleanup_candidates
from antra.utils.inspector import inspect_audio_file
from antra.utils.library_maintenance import (
    dedupe_duplicate_albums,
    dedupe_duplicate_songs,
    find_duplicate_albums,
    find_duplicate_songs,
)
from antra.utils.logging_setup import setup_logging
from antra.utils.runtime import ensure_runtime_environment
from antra.cli.spotify_cli import setup_spotify_cli, handle_spotify_cli

logger = logging.getLogger(__name__)

SOURCE_PROMPT_RANKS = {
    "auto": 1,
    "hifi": 2,
    "amazon": 3,
    "soulseek": 4,
    "jiosaavn": 5,
}

OUTPUT_FORMAT_PROMPT_RANKS = {
    "source": 1,
    "flac": 2,
    "m4a": 2,
    "aac": 3,
    "mp3": 4,
}

SOURCE_PROMPT_VALUES = {
    1: "auto",
    2: "hifi",
    3: "amazon",
    4: "soulseek",
    5: "priority-4",
}

OUTPUT_FORMAT_PROMPT_VALUES = {
    1: "source",
    2: "lossless",
    3: "aac",
    4: "mp3",
}


def _choose_from_rank_group(rank: int, grouped_choices: dict[int, list[str]]) -> str | None:
    options = grouped_choices.get(rank, [])
    if not options:
        return None
    if len(options) == 1:
        return options[0]

    print(f"{rank} includes:")
    for index, choice in enumerate(options, 1):
        print(f"  {index}. {choice}")

    try:
        choice = input("Pick one by number or name: ").strip().lower()
    except EOFError:
        return None

    if not choice:
        return None
    if choice in options:
        return choice
    if choice.isdigit():
        selected_index = int(choice) - 1
        if 0 <= selected_index < len(options):
            return options[selected_index]
    return None


def _choose_numbered_option(
    initial: str | None,
    choices: tuple[str, ...],
    label: str,
    default: str,
    ranks: dict[str, int] | None = None,
    rank_values: dict[int, str] | None = None,
) -> str:
    if initial in choices:
        return initial
    if rank_values and initial in set(rank_values.values()):
        return initial

    if not sys.stdin.isatty():
        return default

    grouped_choices: dict[int, list[str]] = defaultdict(list)
    if ranks:
        for choice in choices:
            grouped_choices[ranks.get(choice, len(grouped_choices) + 1)].append(choice)

    print(f"{label}:")
    if ranks:
        printed_ranks: set[int] = set()
        for index, choice in enumerate(choices, 1):
            display_index = ranks.get(choice, index)
            if display_index in printed_ranks:
                continue
            printed_ranks.add(display_index)
            grouped = grouped_choices.get(display_index, [choice])
            label_text = " / ".join(grouped)
            default_suffix = " (default)" if default in grouped else ""
            print(f"  {display_index}. {label_text}{default_suffix}")
    else:
        for index, choice in enumerate(choices, 1):
            default_suffix = " (default)" if choice == default else ""
            print(f"  {index}. {choice}{default_suffix}")

    try:
        choice = input(f"Enter choice number or name (default: {default}): ").strip().lower()
    except EOFError:
        return default

    if not choice:
        return default
    if choice in choices:
        return choice
    if choice.isdigit():
        selected_rank = int(choice)
        if ranks:
            if rank_values and selected_rank in rank_values:
                return rank_values[selected_rank]
            selected = _choose_from_rank_group(selected_rank, grouped_choices)
            return selected or default
        selected_index = selected_rank - 1
        if 0 <= selected_index < len(choices):
            return choices[selected_index]
    return default

def print_summary(results, elapsed_seconds=None):
    import os
    total = len(results)
    completed = sum(1 for r in results if r.status == DownloadStatus.COMPLETED)
    skipped = sum(1 for r in results if r.status == DownloadStatus.SKIPPED)
    failed = sum(1 for r in results if r.status == DownloadStatus.FAILED)

    total_bytes = 0
    qualities = set()
    for r in results:
        if r.status in (DownloadStatus.COMPLETED, DownloadStatus.SKIPPED) and r.file_path and os.path.exists(r.file_path):
            total_bytes += os.path.getsize(r.file_path)
            if hasattr(r, "quality_label") and r.quality_label:
                qualities.add(r.quality_label)

    print("\n" + "═" * 60)
    print(f"  ANTRA — Complete")
    print("═" * 60)
    print(f"  Tracks added      : {completed} / {total}")
    print(f"  Already in library: {skipped}")
    print(f"  Could not source  : {failed}")

    if total_bytes > 0:
        print(f"  Total size        : {total_bytes / (1024*1024):.1f} MB")
    if elapsed_seconds is not None:
        print(f"  Time taken        : {elapsed_seconds:.0f}s")
    if qualities:
        print(f"  Quality           : {', '.join(qualities)}")

    if failed:
        print("\n  Could not source:")
        for r in results:
            if r.status == DownloadStatus.FAILED:
                print(f"    - {r.track.artist_string} — {r.track.title}")
                if r.error_message:
                    print(f"      Reason: {r.error_message}")
    print("═" * 60 + "\n")


def print_preview(tracks):
    print("\n" + "═" * 60)
    print("  ANTRA — Playlist Preview")
    print("═" * 60)
    for i, track in enumerate(tracks, 1):
        print(f"  {i:02d}. {track.artist_string} — {track.title}")
        if track.album:
            print(f"      Album: {track.album}")
    print("═" * 60 + "\n")


def print_inspection(path: str):
    info = inspect_audio_file(path)
    print("\n" + "═" * 60)
    print("  ANTRA — File Inspection")
    print("═" * 60)
    print(f"  File            : {info['path']}")
    print(f"  Exists          : {info['exists']}")
    print(f"  Extension       : {info['ext']}")
    print(f"  Taggable format : {info['taggable_format']}")
    print(f"  Title           : {info.get('title') or '-'}")
    print(f"  Artist          : {info.get('artist') or '-'}")
    print(f"  Album           : {info.get('album') or '-'}")
    print(f"  Embedded art    : {info['embedded_artwork']}")
    print(f"  Embedded lyrics : {info['embedded_lyrics']}")
    print(f"  Synced lyrics   : {info['synced_lyrics']}")
    print(f"  Sidecar .lrc    : {info['sidecar_lrc']}")
    print(f"  Sidecar .txt    : {info['sidecar_txt']}")
    if info.get("warning"):
        print(f"  Warning         : {info['warning']}")
    if info.get("error"):
        print(f"  Error           : {info['error']}")
    print("═" * 60 + "\n")


def choose_source_preference(initial: str | None) -> str:
    return normalize_source_preference(initial) if initial is not None else "auto"


def choose_output_format(initial: str | None) -> str:
    return normalize_output_format(initial) if initial is not None else "source"


def print_duplicate_albums(groups):
    print("\n" + "═" * 60)
    print("  ANTRA — Duplicate Albums")
    print("═" * 60)
    if not groups:
        print("  No duplicate album folders found.")
        print("═" * 60 + "\n")
        return

    for index, group in enumerate(groups, 1):
        print(f"  {index:02d}. {group.artist} — {group.album}")
        print(f"      Keep   : {group.canonical}")
        for duplicate in group.duplicates:
            print(f"      Delete : {duplicate}")
    print("═" * 60 + "\n")


def print_duplicate_songs(groups):
    print("\n" + "═" * 60)
    print("  ANTRA — Duplicate Songs")
    print("═" * 60)
    if not groups:
        print("  No duplicate songs found.")
        print("═" * 60 + "\n")
        return

    for index, group in enumerate(groups, 1):
        print(f"  {index:02d}. {group.artist} — {group.title}")
        print(f"      Keep   : {group.canonical}")
        for duplicate in group.duplicates:
            print(f"      Delete : {duplicate}")
    print("═" * 60 + "\n")


def print_dedupe_report(report):
    print("\n" + "═" * 60)
    print("  ANTRA — Album Dedupe Complete")
    print("═" * 60)
    print(f"  Groups found            : {report.groups_found}")
    print(f"  Duplicate albums removed: {report.duplicate_albums_removed}")
    print(f"  Tracks moved            : {report.tracks_moved}")
    print(f"  Duplicate tracks deleted: {report.duplicate_tracks_deleted}")
    print("═" * 60 + "\n")


def print_song_dedupe_report(report):
    print("\n" + "═" * 60)
    print("  ANTRA — Song Dedupe Complete")
    print("═" * 60)
    print(f"  Groups found             : {report.groups_found}")
    print(f"  Duplicate songs deleted  : {report.duplicate_tracks_deleted}")
    print(f"  Playlist entries rewritten: {report.playlist_entries_rewritten}")
    print("═" * 60 + "\n")


def print_user_library(library: SpotifyLibrary):
    print("\n" + "═" * 60)
    print(f"  ANTRA — Spotify Library for {library.display_name}")
    print("═" * 60)
    if not library.playlists:
        print("  No playlists or collections found.")
        print("═" * 60 + "\n")
        return

    kind_labels = {
        "playlist": "playlist",
        "liked_songs": "liked",
        "saved_album": "album",
        "followed_artist": "artist",
    }
    for index, playlist in enumerate(library.playlists, 1):
        kind = kind_labels.get(playlist.kind, playlist.kind.replace("_", " "))
        owner = f" - {playlist.owner}" if playlist.owner else ""
        count = f"{playlist.total_tracks} tracks" if playlist.total_tracks else "-"
        print(f"  {index:02d}. [{kind}] {playlist.name}{owner} [{count}]")
    print("═" * 60 + "\n")


def choose_library_selections(library: SpotifyLibrary) -> list[SpotifyPlaylistSummary]:
    if not library.playlists or not sys.stdin.isatty():
        return []

    print_user_library(library)
    try:
        raw = input(
            "Enter item numbers or names separated by commas, or 'all' for everything: "
        ).strip()
    except EOFError:
        return []

    if not raw:
        return []
    if raw.lower() == "all":
        return list(library.playlists)

    selections: list[SpotifyPlaylistSummary] = []
    seen: set[str] = set()
    parts = [part.strip() for part in raw.split(",") if part.strip()]
    for part in parts:
        if part.isdigit():
            index = int(part) - 1
            if 0 <= index < len(library.playlists):
                playlist = library.playlists[index]
                if playlist.selection_key not in seen:
                    selections.append(playlist)
                    seen.add(playlist.selection_key)
            continue
        lowered = part.lower()
        for playlist in library.playlists:
            if playlist.name.lower() != lowered:
                continue
            if playlist.selection_key in seen:
                continue
            selections.append(playlist)
            seen.add(playlist.selection_key)
    return selections


def print_bulk_progress(progress: BulkDownloadProgress):
    position = f"[{progress.playlist_index}/{progress.playlist_total}]"
    playlist_name = progress.playlist.name
    if progress.stage == "fetching":
        print(f"{position} Fetching playlist metadata: {playlist_name}")
    elif progress.stage == "fetched":
        print(f"{position} Found {progress.tracks_total} tracks in {playlist_name}")
    elif progress.stage == "downloading":
        print(f"{position} Building library from: {playlist_name} ({progress.tracks_total} tracks)")
    elif progress.stage == "completed":
        print(
            f"{position} Library updated: {playlist_name} "
            f"({progress.tracks_completed}/{progress.tracks_total} added)"
        )
    elif progress.stage in {"fetch_failed", "failed"}:
        print(f"{position} Could not process: {playlist_name}")
        if progress.message:
            print(f"    {progress.message}")


def print_cleanup_candidates(candidates):
    print("\n" + "═" * 60)
    print("  ANTRA — Cleanup Candidates")
    print("═" * 60)
    if not candidates:
        print("  No safe cleanup candidates found.")
        print("═" * 60 + "\n")
        return

    total_bytes = sum(candidate.size_bytes for candidate in candidates)
    for index, candidate in enumerate(candidates, 1):
        size_mb = candidate.size_bytes / (1024 * 1024)
        print(f"  {index:02d}. [{candidate.kind}] {candidate.path} ({size_mb:.2f} MB)")
    print(f"\n  Total reclaimable: {total_bytes / (1024 * 1024):.2f} MB")
    print("═" * 60 + "\n")


def print_cleanup_report(report):
    print("\n" + "═" * 60)
    print("  ANTRA — Cleanup Complete")
    print("═" * 60)
    print(f"  Candidates removed : {len(report.candidates)}")
    print(f"  Files removed      : {report.files_removed}")
    print(f"  Directories removed: {report.dirs_removed}")
    print(f"  Space reclaimed    : {report.bytes_reclaimed / (1024 * 1024):.2f} MB")
    print("═" * 60 + "\n")


def confirm_cleanup() -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        choice = input("Delete the safe cache/temp/build files listed above? [y/N]: ").strip().lower()
    except EOFError:
        return False
    return choice in {"y", "yes"}


def confirm_song_dedupe() -> bool:
    if not sys.stdin.isatty():
        return False
    choice = input("Delete duplicate songs and keep only the canonical copy? [y/N]: ").strip().lower()
    return choice in {"y", "yes"}


def process_resources(service, resources, options, preview=False):
    all_results = []
    resource_failures = []

    for index, resource in enumerate(resources, 1):
        print(f"   [{index}/{len(resources)}] Resource: {resource}\n")
        try:
            tracks = service.fetch_playlist_tracks(resource, options=options)
        except SpotifyResourceError as e:
            print(f"ERROR: {resource}")
            print(f"  {e}\n")
            resource_failures.append((resource, str(e)))
            continue

        if not tracks:
            print(f"No tracks found for: {resource}\n")
            continue

        if preview:
            print_preview(tracks)
            continue

        results = service.download_tracks(tracks, options=options)
        all_results.extend(results)

    return all_results, resource_failures


def process_library_mode(
    service: AntraService,
    options: RuntimeOptions,
    playlist_names: str | None = None,
    all_playlists: bool = False,
    include_liked_songs: bool = True,
    include_saved_albums: bool = True,
    include_followed_artists: bool = True,
    preview: bool = False,
):
    library = service.get_user_library(
        options=options,
        include_liked_songs=include_liked_songs,
        include_saved_albums=include_saved_albums,
        include_followed_artists=include_followed_artists,
    )

    if not all_playlists and not playlist_names:
        selections = choose_library_selections(library)
        if not selections:
            print_user_library(library)
            return [], [], library
    else:
        selections = service.select_playlists(
            library,
            names_csv=playlist_names,
            all_playlists=all_playlists,
        )

    if not selections:
        print("No matching playlists selected.\n")
        return [], [], library

    if preview:
        tracks, failures = service.fetch_library_selections(
            selections,
            options=options,
            progress_callback=print_bulk_progress,
        )
        print_preview(tracks)
        return tracks, failures, library

    report = service.download_library_selections(
        selections,
        options=options,
        progress_callback=print_bulk_progress,
    )
    return report.results, report.failures, library


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]

    # Detect subcommand mode early so that bare Spotify URLs are never
    # mistaken for an invalid subcommand by argparse.
    _KNOWN_COMMANDS = {"spotify"}
    first_positional = next((a for a in argv if not a.startswith("-")), None)
    is_subcommand_mode = first_positional in _KNOWN_COMMANDS

    parser = argparse.ArgumentParser(
        prog="antra",
    )

    if is_subcommand_mode:
        subparsers = parser.add_subparsers(dest="command")
        setup_spotify_cli(subparsers)
    else:
        # Direct URL / playlist mode — no subcommand routing needed.
        parser.set_defaults(command=None)

    parser.add_argument(
        "playlists",
        nargs="*",
        help=(
            "One or more Spotify URLs or IDs — supports playlists, albums, tracks, "
            "and artist top-tracks. Use 'liked-songs' to download your Liked Songs library."
        ),
    )
    parser.add_argument("--me", action="store_true", help="Authenticate with your Spotify account and access your library")
    parser.add_argument("--all-playlists", action="store_true", help="With --me, download every available playlist in your library")
    parser.add_argument("--playlists", dest="playlist_names", help="With --me, download selected playlists by name, comma-separated")
    parser.add_argument("--liked-songs", action="store_true", help="With --me, include your Liked Songs collection")
    parser.add_argument("--saved-albums", action="store_true", help="With --me, include your Saved Albums collection")
    parser.add_argument("--followed-artists", action="store_true", help="With --me, include your followed artists")
    parser.add_argument("--inspect-file", help="Inspect one downloaded file for embedded artwork/lyrics and sidecars")
    parser.add_argument("-o", "--output", help="Music library directory (default: ./Music)")
    parser.add_argument("--source", choices=SOURCE_PREFERENCE_CHOICES, help="Choose the source to use for downloads")
    parser.add_argument("--format", "--output-format", dest="output_format", choices=OUTPUT_FORMAT_CHOICES, help="Choose the final audio format to save")
    parser.add_argument("--find-duplicate-albums", action="store_true", help="Scan the library and report duplicate album folders")
    parser.add_argument("--dedupe-duplicate-albums", action="store_true", help="Move missing songs into the canonical album folder and delete redundant album folders")
    parser.add_argument("--find-duplicate-songs", action="store_true", help="Scan the library and report duplicate songs")
    parser.add_argument("--dedupe-duplicate-songs", action="store_true", help="Delete duplicate songs and keep one canonical copy")
    parser.add_argument("--find-cleanup-candidates", action="store_true", help="Scan the project for safe cache/temp/build files that can be deleted")
    parser.add_argument("--clean-project-junk", action="store_true", help="Delete safe cache/temp/build files from the project")
    parser.add_argument("--no-lyrics", action="store_true", help="Skip lyrics fetching")
    parser.add_argument("--no-enrich", action="store_true", help="Skip Spotify album enrichment")
    parser.add_argument("--preview", action="store_true", help="Show the playlist tracks without downloading")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show INFO logs from all internal modules")
    args = parser.parse_args(argv)

    ensure_runtime_environment()
    setup_logging(level=logging.DEBUG if args.debug else logging.INFO, verbose=args.verbose)
    cfg = load_config()
    service = AntraService(cfg)

    if args.command == "spotify":
        handle_spotify_cli(args, cfg)
        sys.exit(0)

    if args.inspect_file:
        print_inspection(os.path.abspath(args.inspect_file))
        sys.exit(0)

    if args.find_cleanup_candidates or args.clean_project_junk:
        repo_root = os.getcwd()
        if args.find_cleanup_candidates:
            candidates = find_cleanup_candidates(repo_root)
            print_cleanup_candidates(candidates)
            if candidates and confirm_cleanup():
                print_cleanup_report(cleanup_project_junk(repo_root))
            sys.exit(0)
        print_cleanup_report(cleanup_project_junk(repo_root))
        sys.exit(0)

    if args.find_duplicate_albums or args.dedupe_duplicate_albums or args.find_duplicate_songs or args.dedupe_duplicate_songs:
        output_dir = args.output or cfg.output_dir
        if args.find_duplicate_albums:
            print_duplicate_albums(find_duplicate_albums(output_dir))
            sys.exit(0)
        if args.dedupe_duplicate_albums:
            report = dedupe_duplicate_albums(output_dir)
            print_dedupe_report(report)
            sys.exit(0)
        if args.find_duplicate_songs:
            groups = find_duplicate_songs(output_dir)
            print_duplicate_songs(groups)
            if groups and confirm_song_dedupe():
                report = dedupe_duplicate_songs(output_dir)
                print_song_dedupe_report(report)
            sys.exit(0)
        report = dedupe_duplicate_songs(output_dir)
        print_song_dedupe_report(report)
        sys.exit(0)

    is_dummy_id = cfg.spotify_client_id in ("your_client_id_here", "dummy", "")
    if args.me and (not cfg.spotify_client_id or not cfg.spotify_client_secret or is_dummy_id):
        print("ERROR: SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be configured to use --me.")
        print("  Copy .env.example to .env and fill in your real Developer App credentials.")
        sys.exit(1)

    # Expand 'liked-songs' shorthand into the Playwright sentinel URL
    _LIKED_SENTINELS = {"liked-songs", "liked_songs", "liked", "me:liked"}
    expanded_playlists = [
        "https://open.spotify.com/collection/tracks"
        if p.strip().lower() in _LIKED_SENTINELS
        else p
        for p in (args.playlists or [])
    ]
    args.playlists = expanded_playlists

    if not args.playlists and not args.me:
        parser.error("at least one Spotify URL/ID is required unless --inspect-file is used")

    print("\n" + "═" * 60)
    print("  ANTRA \u2014 Library That Does It All, Automatically")
    print("═" * 60)

    from antra.utils.slskd_onboarding import ensure_slskd
    ensure_slskd(cfg)

    source_preference = choose_source_preference(args.source)
    output_format = choose_output_format(args.output_format)
    options = RuntimeOptions(
        output_dir=args.output or None,
        fetch_lyrics=False if args.no_lyrics else None,
        enrich_album_data=False if args.no_enrich else None,
        source_preference=source_preference,
        output_format=output_format,
    )
    runtime_cfg = service.build_runtime_config(options)
    # Summary lines removed for cleaner CLI output

    import time
    start_time = time.time()

    if args.me:
        try:
            results, resource_failures, _library = process_library_mode(
                service,
                options,
                playlist_names=args.playlist_names,
                all_playlists=args.all_playlists,
                include_liked_songs=True if args.me else args.liked_songs,
                include_saved_albums=True if args.me else args.saved_albums,
                include_followed_artists=True if args.me else args.followed_artists,
                preview=args.preview,
            )
        except SpotifyResourceError as e:
            print(f"ERROR: {e}")
            sys.exit(1)
    else:
        results, resource_failures = process_resources(
            service,
            args.playlists,
            options,
            preview=args.preview,
        )

    if resource_failures and args.preview:
        sys.exit(1)
    if args.preview:
        sys.exit(0)
    if not results and resource_failures:
        sys.exit(1)

    elapsed = time.time() - start_time
    print_summary(results, elapsed_seconds=elapsed)

    if resource_failures:
        print("Resource fetch failures:")
        for failure in resource_failures:
            if isinstance(failure, tuple):
                resource, error = failure
                print(f"  - {resource}")
                print(f"    {error}")
                continue
            print(f"  - {failure.playlist.name}")
            print(f"    {failure.error_message}")
        print()


def run(argv=None):
    try:
        main(argv)
    except KeyboardInterrupt:
        print("\nCancelled.")
        raise SystemExit(130) from None


if __name__ == "__main__":
    run()
