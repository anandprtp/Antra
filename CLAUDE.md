## Instructions for Claude

- **Always read this entire file before touching any code**
- After every change, update the Changelog under `## In Progress` with: what changed, why, and which files were modified
- Never remove or edit old changelog entries — append only
- When a version is released, mark it with the release date and open a new `## In Progress` block
- If you fix or close a Known Issue, mark it `[FIXED in vX.X.X]` inline — do not delete it
- If a new issue is discovered during a session, add it to Known Issues immediately
- Before starting work, confirm out loud: current version, what's in progress, and which known issues are being targeted

---

## Session Start Checklist

When a new session begins, Claude must confirm:

1. **Current version** — what is released vs. what's in progress
2. **In Progress block** — what has already been changed this cycle
3. **Targeted issues** — which Known Issues or BUGS.md items are being worked on today
4. **Files likely to be touched** — based on the above, which files are in scope

---

# Antra

A desktop music library downloader that resolves Spotify/Apple Music/Amazon Music URLs and downloads lossless audio from a prioritized chain of sources. The GUI is built with Wails (Go + Svelte), the download engine is Python, and the two communicate via a bundled PyInstaller binary that speaks a newline-delimited JSON protocol over stdout.

**Current version: v1.1.3** (released 2026-04-19)
**Next version: v1.1.4** (in progress)

---

## Tech Stack

| Layer | Technology |
|---|---|
| Desktop shell | Go 1.23, Wails v2.12.0 |
| Frontend UI | Svelte 3, TypeScript, Vite 3 |
| Download engine | Python 3.11 |
| Packaging | PyInstaller (backend), `wails build` (app), AppImage (Linux), create-dmg (macOS) |
| CI/CD | GitHub Actions — 4 platform builds triggered on `push tags v*` |
| Spotify metadata | spotipy, TOTP-based anonymous token (`pyotp`), Spotify partner GraphQL API |
| Audio tagging | mutagen |
| Format transcoding | ffmpeg (bundled via imageio-ffmpeg) |
| Track matching | Odesli / Songwhip / amazon.com scraper |
| Lyrics | lyricsgenius, Musixmatch |
| Soulseek | slskd (auto-bootstrapped), slskd-api |

### Python runtime dependencies (`requirements-runtime.txt`)
`spotipy`, `yt-dlp`, `mutagen`, `requests`, `python-dotenv`, `pycryptodomex`, `Pillow`, `imageio-ffmpeg`, `slskd-api`, `lyricsgenius`, `pyncm`, `pyotp`

---

## Project Structure

```
Antra/
├── antra/                        # Python download engine (importable package)
│   ├── json_cli.py               # Entry point called by Go — speaks newline-JSON on stdout
│   ├── core/
│   │   ├── service.py            # AntraService: fetch tracks + download_tracks orchestration
│   │   ├── engine.py             # DownloadEngine: resolve→download→tag→organize per track
│   │   ├── resolver.py           # SourceResolver: tries adapters in priority order
│   │   ├── models.py             # TrackMetadata, SearchResult, DownloadResult, etc.
│   │   ├── config.py             # Config dataclass + load_config() from env vars
│   │   ├── spotify.py            # SpotifyClient: metadata, TOTP token, partner GraphQL API
│   │   ├── spotfetch_fetcher.py  # SpotFetchFetcher: multi-mirror proxy for no-auth Spotify metadata
│   │   ├── apple_fetcher.py      # AppleFetcher: Apple Music metadata + artist search
│   │   ├── amazon_music_fetcher.py # AmazonMusicFetcher: schema.org JSON-LD parsing
│   │   ├── soundcloud_fetcher.py # SoundCloud metadata
│   │   ├── events.py             # EngineEvent, EngineEventType (emitted during download)
│   │   ├── control.py            # DownloadController: cancellation token
│   │   └── exceptions.py        # Custom exception types
│   ├── sources/                  # Download adapters (each implements BaseAdapter)
│   │   ├── base.py               # BaseAdapter ABC + RateLimitedError
│   │   ├── amazon.py             # Amazon Music mirror adapter
│   │   ├── apple.py              # Apple Music mirror adapter
│   │   ├── hifi.py               # HiFi community FLAC adapter
│   │   ├── dab.py                # DAB Music (dab.yeet.su) adapter
│   │   ├── qobuz.py              # Qobuz + Qobuz proxy adapter
│   │   ├── deezer.py             # Deezer (ARL token) adapter
│   │   ├── tidal.py              # Tidal adapter
│   │   ├── soulseek.py           # Soulseek/slskd adapter
│   │   ├── jiosaavn.py           # JioSaavn (India, AAC 320) adapter
│   │   ├── netease.py            # NetEase Cloud Music adapter (Chinese catalog)
│   │   └── odesli.py             # Odesli/Songwhip/amazon.com ASIN resolver
│   └── utils/
│       ├── tagger.py             # FileTagger: writes ID3/FLAC/MP4 tags + artwork
│       ├── organizer.py          # LibraryOrganizer: dedup + folder structure
│       ├── transcoder.py         # AudioTranscoder: ffmpeg format conversion
│       ├── matching.py           # String similarity, track matching score
│       ├── lyrics.py             # LyricsFetcher: Musixmatch + Genius
│       ├── runtime.py            # get_ffmpeg_exe(), get_ffprobe_exe()
│       ├── logging_setup.py      # Log config
│       └── musicbrainz.py        # MusicBrainz lookup utilities
│
├── antra-wails/                  # Go/Wails desktop shell
│   ├── main.go                   # Wails app bootstrap
│   ├── app.go                    # App struct, startup/shutdown, ffmpeg path caching
│   ├── app_backend.go            # All bound methods: StartDownload, GetArtistDiscography,
│   │                             #   SearchArtists, Spotify auth, config, history, etc.
│   ├── analyzer.go               # ProbeFile + GenerateSpectrogram (calls bundled ffprobe/ffmpeg)
│   ├── backend_runtime.spec      # PyInstaller spec for bundling Python backend
│   ├── proc_windows.go           # hideProcess() for Windows (no console window)
│   ├── proc_unix.go              # hideProcess() stub for Unix
│   ├── runtime_assets_windows.go # Embeds bundled backend path logic for Windows
│   ├── runtime_assets_other.go   # Same for non-Windows
│   ├── wails.json                # Wails project config
│   └── frontend/
│       ├── src/App.svelte        # Single-page UI: all screens, modals, download flow
│       ├── src/main.ts           # Svelte entry point
│       ├── vite.config.ts        # Vite build config
│       └── wailsjs/go/main/      # Auto-generated Go→JS bindings (App.js, App.d.ts, models.ts)
│
├── BUGS.md                       # Current open issues and user feedback (read each session)
├── .github/workflows/release.yml # CI: builds all 4 platforms and creates GitHub Release
├── requirements-runtime.txt      # Python packages bundled into PyInstaller backend
├── requirements-desktop.txt      # Adds pyinstaller + wheel on top of runtime deps
├── .env.example                  # Documents all supported environment variables
└── build_desktop.py              # Local desktop build helper script
```

---

## How to Run / Build / Deploy

### Development (run from source)

```bash
# 1. Install Python deps
pip install -r requirements-runtime.txt

# 2. Run the Wails dev server (hot-reload)
cd antra-wails
wails dev
```

The Go app will find `antra/json_cli.py` by walking parent directories — no special setup needed.

### Production build (local)

```bash
cd antra-wails

# Build Python backend binary first
pip install -r ../requirements-desktop.txt
pyinstaller backend_runtime.spec --distpath runtime/backend --noconfirm

# Build Wails app (embeds the backend binary)
wails build -clean                            # macOS / Windows
wails build -clean -tags webkit2_41           # Linux
```

### Release (GitHub Actions)

Push a tag: `git tag v1.x.x && git push origin v1.x.x`

The workflow (`.github/workflows/release.yml`) builds:
- `build-windows` → `Antra.exe` (windows-latest)
- `build-linux` → `Antra-Linux.AppImage` (ubuntu-24.04)
- `build-macos` → `Antra-macOS.dmg` (macos-latest, Apple Silicon)
- `build-macos-intel` → `Antra-macOS-Intel.dmg` (macos-15-intel)

All 4 artifacts are uploaded to the GitHub Release automatically.

### Config storage (runtime)

| Platform | Path |
|---|---|
| Windows | `%LOCALAPPDATA%\Antra\config.json` |
| macOS | `~/Library/Application Support/Antra/config.json` |
| Linux | `~/.local/share/Antra/config.json` |

Environment variables (`.env` or system env) override config — see `.env.example`.

---

## Known Issues / TODOs

> Status tags: `[OPEN]` · `[IN PROGRESS]` · `[FIXED in vX.X.X]`
> Do not delete entries — update the status tag instead.

- `[OPEN]` **SpotFetch mirrors** listed in `config.py` (`sp.vov.li`, `sp.rnb.su`, etc.) are community servers — they may go down. Primary path now uses TOTP + Spotify partner API directly, so mirrors are fallback only.
- `[OPEN]` **TOTP secret** (`_SP_TOTP_SECRET` in `spotify.py`) and partner GraphQL hashes may need updating if Spotify rotates them. Sourced from the SpotiFLAC project.
- `[OPEN]` **Amazon Music mirrors** in `config.py` are community-run; same caveat as SpotFetch mirrors.
- `[OPEN]` **Soulseek seeding** (`soulseek_seed_after_download`) is implemented in `soulseek.py` but not exposed in the GUI settings panel yet.
- `[OPEN]` **Spotify OAuth PKCE flow** (`spotify_auth.py`) is wired up but the GUI only exposes sp_dc cookie / token methods — full OAuth login is CLI-only.
- `[OPEN]` **History is unbounded** — no trimming or pagination in the UI (`App.svelte`).
- `[OPEN]` **Locale Spotify URLs** (`open.spotify.com/intl-es/artist/...`) — `isArtistUrl` in `App.svelte` normalizes artist URLs only; album/playlist locale variants pass through to Python fine, but frontend normalization is incomplete.
- `[OPEN]` **NetEase source** (`sources/netease.py`) was added in v1.1.2 but is not yet toggled in the GUI sources selector (`App.svelte`).
- `[FIXED in v1.1.3]` **FEAT-18** — Library folder structure & filename format preferences: first-run setup screen + Preferences panel. Two axes: folder layout (`standard` vs `flat`) and filename format (`default`, `title-only`, `artist-title`, `title-artist`). See BUGS.md.
- `[FIXED in v1.1.3]` **BUG-12** — ISRCEnricher hits Spotify 429 on first batch with 10 parallel workers, bails with 0/12 ISRCs enriched. Fix: reduce workers, add retry/backoff. See `antra/core/isrc_enricher.py`.
- `[FIXED in v1.1.3]` **FEAT-19** — Artist search results should show profile image thumbnail; discography modal should be sorted newest-first by release year. See BUGS.md.
- `[FIXED in v1.1.3]` **FEAT-20** — Health check chips: replace hand-drawn SVG logos with real brand icons (files provided: `tidal_logo_icon_147227.webp`, `amazon-music.jpg`, `qobuz_icon.png`). See BUGS.md.
- `[FIXED in v1.1.3]` **FEAT-21** — Ko-fi toast: replace "No AI used" copy with maintenance-effort messaging; fix toast vanishing while hovered (should pause auto-dismiss on hover). See BUGS.md.
- `[FIXED in v1.1.3]` **BUG-26** — Filename format setting ignored after BUG-23 fix; multi-disc `101`/`201` numbering applies only to `default` format, not to `artist_title`/`title_artist`. See BUGS.md.
- `[FIXED in v1.1.3]` **BUG-27** — Soulseek-only mode: tracks found by slskd but downloads stall/fail; complex collaboration credits in title break Soulseek search query; slskd web UI URL not surfaced in Antra. See BUGS.md.
- `[FIXED in v1.1.3]` **BUG-28** — Full Albums mode skips tracks from a deluxe/Shady edition that were already downloaded from the standard edition in the same batch — both editions map to the same album folder name (parenthetical suffix stripped), or the in-session identity cache cross-deduplicates across albums. See BUGS.md.

---

## Changelog

### In Progress — v1.1.4

- **FEAT-04 — ALAC output format option (Apple Music / iPhone sync)**: Add `ALAC (.m4a)` as a selectable output format. FLAC → ALAC conversion via ffmpeg is lossless; `tagger.py` re-writes tags into the MP4 container. Exposes as a new option in the Format Preference settings alongside Auto / Lossless / MP3.
  **Files likely to be modified:** `antra/utils/transcoder.py`, `antra/utils/tagger.py`, `antra/core/engine.py`, `antra/core/config.py`, `antra-wails/frontend/src/App.svelte`, `antra-wails/app_backend.go`

---

### v1.1.3 — released 2026-04-19

- **Disc-prefixed filenames universal — all formats, all sources**: `_format_filename()` in `organizer.py` previously only applied `101/201` disc-prefix numbering when `total_discs > 1` or `disc_number >= 2` (i.e. only for detected multi-disc albums). Single-disc albums and albums where the source doesn't populate disc info still produced flat `01 - Title.flac`. Changed: `default`, `artist_title`, and `title_artist` formats now always use `disc_number or 1` as the disc prefix — so single-disc tracks become `101/102/103` and multi-disc tracks become `101.../201...` universally, regardless of source. `title_only` keeps its existing behaviour (no number at all, except disc prefix added only when a second disc is explicitly present, to avoid filename collisions).
  **Files modified:** `antra/utils/organizer.py`

- **slskd web UI credentials surfaced in Settings**: `GetSlskdWebUIInfo()` Go binding reads the managed instance's `state.json` and returns the web UI URL + generated credentials. The Settings panel Soulseek section now shows a small info box with the URL, username (`slskd`), and generated password once slskd has been bootstrapped — so users don't need to dig through hidden system files to log into the web UI. The password is stable (generated once on first bootstrap, stored in `state.json`).
  **Files modified:** `antra-wails/app_backend.go`, `antra-wails/frontend/wailsjs/go/main/App.js`, `antra-wails/frontend/wailsjs/go/main/App.d.ts`, `antra-wails/frontend/src/App.svelte`

- **BUG-26 fix — `title_only` format lacked disc prefix on multi-disc albums; format always logged**: `_format_filename()` in `organizer.py` returned bare `Title` for `title_only` even on multi-disc albums, while BUG-26 requires `101 - Title.flac` / `201 - Title.flac` when `total_discs > 1`. Fixed: added disc-prefix branch to the `title_only` case (same guard as the other formats: `is_multi_disc and track.disc_number and track_number`). The propagation chain (`json_cli.py` env vars → `load_config()` → `LibraryOrganizer`) was already correct after BUG-23+24; the remaining code issue was only the `title_only` case. Also removed the `!= "default"` guard on the `[Config]` log line in `json_cli.py` so the active format and folder structure are always printed at startup — users can now verify in the log panel that their settings are being applied.
  **Files modified:** `antra/utils/organizer.py`, `antra/json_cli.py`

- **BUG-27 fix — Soulseek collaboration credits in title break search; slskd web UI surfaced**: (1) Added module-level `_COLLAB_RE` to `soulseek.py` (same pattern as `odesli.py`: matches `(with X)`, `(feat. X)`, `(ft. X)`, `(featuring X)` in any bracket style). `search()` now strips collab credits from the track title before building the slskd query — `"On Time (with John Legend) - Metro Boomin"` becomes `"On Time - Metro Boomin"`, which matches far more shared files. Logs the stripping at INFO level so users can see it in the log panel. Refactored the polling+scoring body into a private `_run_search()` helper so `search()` can also attempt a title-only fallback (`"On Time"`) when collab credits were stripped but the primary query still finds nothing; the fallback does NOT run for ordinary zero-result searches to avoid doubling search latency. (2) `_write_config()` in `slskd_manager.py` adds `username: slskd` + a stable generated `web_password` (stored in `state.json`) to the slskd YAML `web.authentication` block; logs the web UI URL and credentials at INFO level when slskd starts, so users can open the browser UI to inspect transfer state.
  **Files modified:** `antra/sources/soulseek.py`, `antra/utils/slskd_manager.py`

- **BUG-27 bootstrap fix — external slskd at port 5030 prevented managed instance from starting**: Two failure modes in `ensure_running()` in `slskd_manager.py`: (a) when a non-Antra slskd is running at 5030 and the user has provided Soulseek credentials, the bootstrap previously returned `None` immediately ("skipping to avoid disrupting"), leaving no adapter — now the external instance is killed and replaced with an Antra-managed one; (b) `_start_process()` was called without first checking whether any slskd was still holding the port (stale session, previous crash, etc.) — added a `_is_reachable` check before `_start_process()` that kills any survivor so the new process can bind. Both paths add a `time.sleep(1.5)` grace period after `_kill_managed_slskd()` to allow the OS to release the port.
  **Files modified:** `antra/utils/slskd_manager.py`

- **BUG-28 fix — Full Albums mode skips deluxe/edition tracks that share tracks with the standard edition**: Root cause: `_fetch_album()` in `apple_fetcher.py` has two paths — an iTunes API path and a Catalog API path. The iTunes path correctly overrides each track's `album` with the parent collection's `collectionName` (line 284). The Catalog API path returned early without this override, so tracks' `albumName` attributes from the Catalog API (which may reflect the canonical album name, not the edition variant) were used unchanged — causing both the standard and Shady Edition to produce the same album folder name → same target path → second edition skipped as "already downloaded". Fixed: Catalog API path now extracts `name`, `releaseDate`, and `artistName` from the parent album attributes and stamps them onto each track (matching the iTunes path's override logic). Also improves the log line to include the album name.
  **Files modified:** `antra/core/apple_fetcher.py`

- **BUG-25 fix — Amazon adapter fails to find tracks with "(with X)" collaboration credits in title**: `OdesliEnricher._try_songwhip()` stripped `feat./ft./featuring` from the title slug before lookup but not `(with X)` — Songwhip received `jid/youugly-with-westside-gunn` instead of `jid/youugly` and returned 404. `_search_amazon()` also searched with the full collaboration-credited title, hurting match quality. Fixed: added `_COLLAB_RE` that strips `(with X)`, `(feat. X)`, `(ft. X)`, `(featuring X)` from title before slugging; the no-collab slug is now tried first in Songwhip. Same regex applied to the Amazon product search query and to the title-match check (so "YouUgly" matches a result titled "YouUgly (with Westside Gunn)" and vice-versa).
  **Files modified:** `antra/sources/odesli.py`

- **Resolver logging improvement — adapter "no match" messages now visible at INFO level**: Previously `resolver.py` logged "returned no result" at DEBUG level in normal (non-preserve_input_order) mode, meaning the log panel never showed which adapters were tried and found nothing. Upgraded to INFO so users can see e.g. `[Resolver] amazon — no match found for: YouUgly` and understand why the fallback chain is progressing.
  **Files modified:** `antra/core/resolver.py`

- **BUG-24 fix — NameError: `logger` not defined in `main()` crashes every download**: The BUG-23 fix added a `logger.info(...)` call at line 444 of `json_cli.py` inside `main()`, but `logger` is only defined inside `_setup_logging()` — never assigned in `main()`'s scope. Any download attempt hit this `NameError` immediately after loading config. Fixed by replacing the `logger.info()` call with the standard `print(json.dumps({...}))` pattern used throughout `main()`.
  **Files modified:** `antra/json_cli.py`

- **BUG-23 fix — Filename format setting not reaching LibraryOrganizer**: `json_cli.py` set `FOLDER_STRUCTURE`, `FILENAME_FORMAT`, and `LIBRARY_MODE` env vars only when the config value was truthy (`if settings.get(...)`). If the value was absent from the config file (e.g. existing `config.json` from before FEAT-18 was merged, where the field wasn't present), the env var was never set and `load_config()` could pick up a stale value from `load_dotenv(override=True)` (which runs at import time before `json_cli.py` can set anything). Fixed by unconditionally setting all three env vars, falling back to their respective defaults (`"standard"`, `"default"`, `"smart_dedup"`). Added a log line at startup that prints the active `filename_format` and `folder_structure` when non-default, so users can confirm in the log panel what value is actually in use.
  **Files modified:** `antra/json_cli.py`

- **BUG-22 fix — Soulseek-only mode reports "No source adapters available"**: Root cause: when the user unchecks the HiFi source group without having ever explicitly toggled the Soulseek switch, `soulseek_enabled` stays `false` (its default). `json_cli.py` mapped `soulseek_enabled: false` → `SLSKD_AUTO_BOOTSTRAP=false`, so the slskd bootstrap block in `service.py` was skipped and no Soulseek adapter was added. The HiFi adapters were then correctly removed by the group filter, leaving 0 adapters. Fixed in `json_cli.py`: when building `SLSKD_AUTO_BOOTSTRAP`, also check if `"soulseek"` is in `sources_enabled` — if so, force bootstrap on regardless of `soulseek_enabled`. Added a post-filter warning in `service.py` when Soulseek-only mode ends up with no adapters (guiding the user to check credentials), and an informational log line showing which adapters are active after filtering (clarifies the misleading pre-filter `[OK]` messages for adapters that get removed).
  **Files modified:** `antra/json_cli.py`, `antra/core/service.py`

- **FEAT-19 fix — Artist search profile images + discography sort**: Artist search results via the Apple Music / iTunes path now show profile photo thumbnails. `search_artists()` in `apple_fetcher.py` already called `_fetch_artist_artwork()` internally but never used its output — fixed by adding a parallel `ThreadPoolExecutor` (up to 4 workers) to fetch each result's photo from the Apple Music Catalog API after the iTunes search returns; silently no-ops per-artist when the developer token is absent or the request fails. The Svelte UI already renders `artist.artwork_url` with a 🎤 fallback, so no frontend change needed for search results. Discography modal: each release-type group (`{#each}` on Albums/Singles/EPs) now sorts by year descending then name ascending inline: `.sort((a,b) => (b.year??0)-(a.year??0) || a.name.localeCompare(b.name))`.
  **Files modified:** `antra/core/apple_fetcher.py`, `antra-wails/frontend/src/App.svelte`

- **FEAT-08 fix — Library Build History card layout**: Rewrote history card to match the spec: 44×44px artwork thumbnail on the left (fallback: muted 🎵 placeholder); title as bold primary label; URL as small muted secondary line (shown only when a title exists); third line "N tracks · [failed count if >0] · date" consolidated inside the text block so all three lines align with the artwork. Removed the detached full-width date row and the verbose `↓ N × N - N Total: N` stats row. Source chips now only render when at least one source exists.
  **Files modified:** `antra-wails/frontend/src/App.svelte`

- **BUG-20 fix — Soulseek transfers complete in slskd but Antra shows "waiting" / endless re-downloads**: Three root causes fixed. (1) `_candidate_download_paths` never included the `{username}/` prefix that slskd prepends to all downloads (`{downloads_dir}/{username}/{remote_path}`), so file-existence checks always failed after `localPath` wasn't set. Fixed by generating username-prefixed candidates first. (2) slskd auto-removes completed transfers from its active queue; `get_all_downloads(includeRemoved=False)` never returned them. Added a one-shot `includeRemoved=True` pass after `NO_MATCH_REMOVED_CHECK_AFTER` (20s) when no transfer has been matched — detects fast-peer / small-file completions that were cleaned up before the first poll. (3) When path resolution fails after a real completion, the engine timed out and re-enqueued the same track from a new Soulseek peer, repeating indefinitely. Fixing (1) and (2) eliminates the timeout, which breaks the re-enqueue cascade. Also added `username` parameter threading through `_resolve_download_path → _candidate_download_path → _candidate_download_paths` so callers don't have to rely on the `dl` dict alone.
  **Files modified:** `antra/sources/soulseek.py`

- **BUG-21 fix — slskd process not killed on Antra quit (macOS/Linux)**: `app.go` shutdown only ran `taskkill slskd.exe` on Windows. Fixed: on macOS/Linux, reads the PID saved by `SlskdBootstrapManager` in `~/.cache/antra/slskd/runtime/state.json` and kills it via `os.FindProcess().Kill()`; falls back to `pkill -f slskd` if the state file is missing or the kill fails.
  **Files modified:** `antra-wails/app.go`

- **BUG-18 fix — Flat folder structure places files under Albums/Playlists subdirs instead of root**: In flat mode, `get_output_path()` was still routing albums to `self.albums_root / album_dir` and playlists to `self.playlists_root / playlist_dir` (i.e. `D:/Music/Albums/...` and `D:/Music/Playlists/...`). Fixed to use `self.root / album_dir` and `self.root / playlist_dir` when `folder_structure == "flat"`, so paths resolve to `D:/Music/Album Name/` directly. `write_playlist_manifest` updated the same way so the `.m3u` also lands at `root/Name.m3u` in flat mode.
  **Files modified:** `antra/utils/organizer.py`

- **BUG-19 fix — `artist_title` and `title_artist` filename formats omit track number, causing file-manager sort by name instead of track order**: The `default` format always prefixed `NN - `, but `artist_title` and `title_artist` produced bare `Artist - Title.flac` with no number, so file managers sorted alphabetically. Fixed `_format_filename()` so all three numbered formats (`default`, `artist_title`, `title_artist`) prepend the track/disc number when available. New examples: `01 - J. Cole - m y . l i f e.flac`, `01 - m y . l i f e - J. Cole.flac`. Only `title_only` remains numberless. Updated FEATURES.md filename format table accordingly.
  **Files modified:** `antra/utils/organizer.py`, `FEATURES.md`

- **BUG-16 fix — Health check popover shows old SVG icons instead of real brand images**: The three SVG icon blocks in the source health popover header (`healthPopoverSource === 'hifi'/'amazon'/'dab'`) were the original hand-drawn approximations from before FEAT-20. Replaced them with `<img>` tags pointing to `/icons/tidal.webp`, `/icons/amazon-music.jpg`, and `/icons/qobuz.png` at 26×26px — matching the chips exactly.
  **Files modified:** `antra-wails/frontend/src/App.svelte`

- **BUG-17 fix — Public user-created Apple Music playlists (pl.u-) blocked**: The BUG-02 fix added an overly aggressive early guard that raised `ValueError` for all `pl.u-` playlist IDs, treating user-created as synonymous with private. Apple's Catalog API fully supports public user-created playlists — `pl.u-` is only a naming convention. Removed the early guard. Added an explicit skip of the RSS fallback for `pl.u-` playlists (the RSS/iTunes lookup only works for Apple-curated playlists). If the Catalog API returns no tracks for a `pl.u-` playlist, the error now instructs the user to check that the playlist is set to public/shareable rather than blaming the `pl.u-` prefix.
  **Files modified:** `antra/core/apple_fetcher.py`

- **BUG-14 fix — ISRCEnricher adds 14s delay with 0 ISRCs**: Removed `_enrich_isrcs(tracks)` call from `fetch_playlist_tracks()` in `service.py`. The enricher was calling `GET /v1/tracks` with an anonymous TOTP token, hitting Spotify 429 on every batch, retrying 3 times (2+4+8 = 14s), and achieving 0 ISRCs. ISRCs remain populated for authenticated Spotify and Apple Music catalog paths; the unauthenticated path is simply too rate-limited to be usable.
  **Files modified:** `antra/core/service.py`

- **BUG-15 fix — Truncated downloads: global adapter deprioritization + per-track deferred retry**: Two-part fix for HiFi returning truncated FLACs system-wide (all parallel workers were independently queuing on a broken adapter, discovering the truncation one at a time). (1) `resolver._mark_rate_limited()` now accepts an optional `cooldown_seconds` parameter (default unchanged at 30s). When the engine detects a truncated download, it immediately calls `self.resolver._mark_rate_limited(adapter.name, cooldown_seconds=120)` — this signals ALL parallel workers to stop queuing on that adapter globally for 120s, so they go straight to DAB instead. (2) The engine also routes truncated downloads to `rate_limited_adapters` (not `excluded_adapters`) so the adapter gets one last per-track retry as a true last resort — handles tracks like "YouUgly (with Westside Gunn)" where DAB/Amazon can't find the track and HiFi is the only source that can.
  **Files modified:** `antra/core/engine.py`, `antra/core/resolver.py`

- **FEAT-18 — Library folder structure & filename format preferences**: Added two new config axes surfaced in both the first-run setup screen and the Settings panel. **Axis 1 — Folder structure:** `standard` (default, `Artist / Album / files`, Navidrome/Jellyfin/Plex compatible) vs `flat` (`Album / files`, no artist wrapper). **Axis 2 — Filename format:** `default` (`NN - Title`), `title_only` (`Title`), `artist_title` (`Artist - Title`), `title_artist` (`Title - Artist`). Implementation: added `folder_structure: str = "standard"` and `filename_format: str = "default"` to `Config` dataclass with `FOLDER_STRUCTURE` / `FILENAME_FORMAT` env vars in `load_config()`. Refactored `LibraryOrganizer.__init__()` to accept both params; extracted `_format_filename(track, track_number)` helper that replaces the inline filename construction in `get_output_path()` — multi-disc disc-prefix logic is preserved in `default` mode only. `build_engine()` in `service.py` passes both params to `LibraryOrganizer`. `json_cli.py` maps `folder_structure` / `filename_format` from the Go config JSON to env vars, and the bare `LibraryOrganizer(cfg.output_dir)` construction at startup now passes both params. Go `Config` struct: added `FolderStructure string` and `FilenameFormat string`. TypeScript `Config` model updated with both fields. `App.svelte`: added defaults in initial state and `onMount` guards; added "Folder Structure" and "Filename Format" sections in the Settings panel (between Library Mode and Music Folder); added the same two sections to the first-run setup screen (between Music Library Folder and the Soulseek toggle).
  **Files modified:** `antra/core/config.py`, `antra/utils/organizer.py`, `antra/core/service.py`, `antra/json_cli.py`, `antra-wails/app_backend.go`, `antra-wails/frontend/wailsjs/go/models.ts`, `antra-wails/frontend/src/App.svelte`

- **FEAT-11 fix — Prefer explicit track versions (avoid radio edits / clean versions)**: Added `is_explicit: Optional[bool] = None` to both `TrackMetadata` and `SearchResult` in `models.py`. Spotify's `_parse_track()` sets it from `track.get("explicit")` (authenticated path). Apple Music's `_catalog_item_to_metadata()` sets it from `attrs.get("contentRating")` — `"explicit"` → True, `"clean"` → False, absent → None. Added `prefer_explicit: bool = True` to `Config` and `PREFER_EXPLICIT` env var in `load_config()`. In `resolver.py`: added `import re`, module-level `_CLEAN_VERSION_RE` pattern (matches "radio edit", "clean version", "edited version", "censored"), `prefer_explicit` param to `__init__()`, `_explicit_penalty()` method (returns -0.20 when target is explicit but result is confirmed clean or title matches the pattern), `_result_looks_clean()` helper. Updated `_candidate_key()` to accept optional `track` and incorporate `_explicit_penalty()`. Updated `_accepts_result_immediately()` to accept `track` and return `False` for clean results when target is explicit (keeps searching rather than accepting first clean hit). Updated all call sites in `resolve()` to pass `track`. The fast-path 24-bit and lossless early-exit guards also skip clean results. `service.py` passes `prefer_explicit` to `SourceResolver`. `json_cli.py` maps `prefer_explicit` from config JSON. Go struct: `PreferExplicit *bool`. TypeScript model and Svelte default updated. Settings: "Prefer explicit versions" checkbox added in the Format Preference section.
  **Files modified:** `antra/core/models.py`, `antra/core/spotify.py`, `antra/core/apple_fetcher.py`, `antra/core/resolver.py`, `antra/core/config.py`, `antra/core/service.py`, `antra/json_cli.py`, `antra-wails/app_backend.go`, `antra-wails/frontend/wailsjs/go/models.ts`, `antra-wails/frontend/src/App.svelte`

- **FEAT-12 fix — Use Apple Music `audioTraits` to hint resolver source priority**: Added `_track_wants_hires(track)` helper to `SourceResolver` (returns True when `"hi-res-lossless"` is in `track.audio_traits`). Modified the 16-bit lossless early-exit path in `resolve()`: when the track is known hi-res but the result is only 16-bit, the resolver now logs a message and continues searching for a 24-bit source instead of accepting immediately. The existing 24-bit fast-path exit is unaffected (still exits on tier=4). Modified `_candidate_key()`: when the track is hi-res and the result is 24-bit, adds `+0.05` to similarity — biases the final candidate selection toward hi-res when multiple adapters each return a result. Net effect: for hi-res tracks (e.g. most modern Apple Music releases), the resolver will prefer a 24-bit Amazon/Qobuz result over a 16-bit HiFi/DAB result when both are available.
  **Files modified:** `antra/core/resolver.py`

- **FEAT-14 — Source health check panel**: Added three clickable brand-logo chips below the URL input (always visible in both URL mode and artist search mode): Tidal (wave chevron SVG, cyan), Amazon (smile arrow SVG, orange), Qobuz (Q circle SVG, purple). Clicking a chip triggers `CheckSourceHealth(key)` in Go, which fires parallel HTTP probes (7-second timeout, `sync.WaitGroup`) against all known endpoints: 19 HiFi/Tidal endpoints (GET `/search/?s=test`), 4 Amazon mirror endpoints (GET `/`), 1 DAB endpoint (GET `/search?q=test`). Returns a JSON `SourceHealthResult` with `source`, `total`, `live`, and an `[]EndpointStatus` array (`url`, `alive`, `latency_ms`). Chip SVG dims and count turns red when zero endpoints are live. Clicking opens a health popover showing only the live/total count (large number) and a dot grid (one dot per endpoint — green = alive, dark red = down; hover shows latency only). Endpoint URLs are never exposed in the UI. `CheckSourceHealth` Go binding added to `App.js` and `App.d.ts`.
  **Files modified:** `antra-wails/app_backend.go`, `antra-wails/frontend/wailsjs/go/main/App.js`, `antra-wails/frontend/wailsjs/go/main/App.d.ts`, `antra-wails/frontend/src/App.svelte`

- **FEAT-15 — Scroll-to-bottom arrow for tracklist and log panel**: Added `tracklistEl` ref and `tracklistAtBottom` state — when user scrolls more than 40px from the bottom of the tracklist, a floating circular "↓" button appears in the bottom-right of the tracklist area; clicking it scrolls to the bottom. Same pattern for the log panel: added `logAtBottom` state updated by `updateAutoScrollState()`; a "↓" button appears in the log panel header when the user is not at the bottom, clicking calls `scrollToBottom(true)` (forced instant scroll).
  **Files modified:** `antra-wails/frontend/src/App.svelte`

- **BUG-11 fix — Multi-disc album numbering for Amazon Music and Spotify public path**: Amazon Music's JSON-LD schema.org format does not include disc numbers; added `_assign_disc_numbers_from_html()` in `amazon_music_fetcher.py` — uses byte-position heuristic to find "Disc X"/"CD X" header text interleaved with `<music-horizontal-item` elements in the SSR HTML, assigning `disc_number` and resetting `track_number` to 1-based per disc. Applied to both `_fetch_album()` (JSON-LD path) and `_parse_tracklist_page()` (paginated path). Spotify public fallback path never filled `disc_number`; extended `ISRCEnricher.enrich_tracks()` (in `isrc_enricher.py`) to also stamp `disc_number` from the Spotify v1 `/tracks` batch response — since all Spotify tracks have `spotify_id`, this covers the public path automatically.
  **Files modified:** `antra/core/amazon_music_fetcher.py`, `antra/core/isrc_enricher.py`

- **FEAT-17 — Sponsor toast + Ko-fi hover tooltip**: On startup (skipped during first-run setup), a toast notification appears in the top-right corner after 1.2 seconds and auto-dismisses after ~9 seconds. The toast has a Ko-fi icon, a short honest message ("Antra is hand-crafted — not AI-generated..."), a "Support on Ko-fi" button, and an × close button. Dismissing animates the toast upward and fades it out (toward the Ko-fi icon position). The existing Ko-fi icon button in the header now has a `kofi-wrap` relative container; hovering it shows a compact inline tooltip popover with the same message and a Ko-fi button. `sponsorToastTimer` is stored so the auto-dismiss is cancelled if the user manually closes it first.
  **Files modified:** `antra-wails/frontend/src/App.svelte`

- **FEAT-16 — Multi-URL album/playlist title separator in tracklist**: When `playlist_loaded` fires and `trackOrder` already has tracks from a previous URL, a `__SEP__${timestamp}` sentinel key is inserted into `trackOrder` and the corresponding `{title, artwork}` pair is saved in `separatorMeta`. The tracklist `{#each}` loop checks `trackName.startsWith('__SEP__')` and renders a `.tracklist-album-sep` divider row (small cover thumbnail + uppercase album/playlist title) instead of a track row. `separatorMeta` is reset to `{}` at the start of each new download batch (both `startDownload` and the discography modal handler) so separators don't persist across sessions.
  **Files modified:** `antra-wails/frontend/src/App.svelte`

- **FEAT-10 fix — Full Albums library mode (cross-album dedup opt-out)**: Added `library_mode` setting with two values: `smart_dedup` (default — skip track if the same ISRC/ID exists anywhere in the library) and `full_albums` (skip only if the file already exists in the exact target folder, allowing the same track in multiple album folders). In `config.py`: added `library_mode: str = "smart_dedup"` field and `LIBRARY_MODE` env var in `load_config()`. In `organizer.py`: added `full_albums: bool = False` param to `__init__()` — when `True`, skips `_build_identity_index()` at startup (avoids false positives from other albums; faster init) and `is_already_downloaded()` skips the cross-library identity key lookup, only checking the canonical target path. In `service.py`: reads `cfg.library_mode` and passes `full_albums=True` when building `LibraryOrganizer`. In `json_cli.py`: maps `library_mode` from Go config JSON to `LIBRARY_MODE` env var. Go struct: added `LibraryMode string` field. TypeScript `Config` model: added `library_mode?: string`. Settings UI: added "Library Mode" section with Smart Dedup / Full Albums radio options (with plain-language descriptions) between Format Preference and Music Folder.
  **Files modified:** `antra/core/config.py`, `antra/utils/organizer.py`, `antra/core/service.py`, `antra/json_cli.py`, `antra-wails/app_backend.go`, `antra-wails/frontend/wailsjs/go/models.ts`, `antra-wails/frontend/src/App.svelte`

- **FEAT-13 fix — Genre and year tags now reliably populated (Windows Media Player compatible)**: Wired `ISRCEnricher` into `service.py` — added `_enrich_isrcs(tracks)` static method that checks for tracks with `spotify_id` but no `isrc` and calls `ISRCEnricher().enrich_tracks()` to bulk-fill ISRCs (and `release_date`/`release_year`) via the Spotify v1 `/tracks` endpoint using an anonymous TOTP token. This is called in `fetch_playlist_tracks()` just before `_stamp_disc_totals()`. Tracks that already have ISRCs (Apple Music catalog API, authenticated Spotify) are unaffected. Now that more tracks have ISRCs, `engine._enrich_genres_if_needed()` → MusicBrainz lookup fires for a much larger fraction of downloads, filling in the `genre` tag. Additionally fixed `tagger.py` for all three formats (FLAC, MP3, MP4): instead of writing only `release_year` (the integer), the tagger now writes `release_date` when available (full ISO string, e.g. "2024-04-12") and falls back to `str(release_year)` — this makes the year tag readable in Windows Media Player and standard tag readers that expect an ISO date string in TDRC / Vorbis `date` / MP4 `©day`.
  **Files modified:** `antra/core/service.py`, `antra/utils/tagger.py`

- **BUG-10 fix — MP3 mode now uses JioSaavn/NetEase directly instead of downloading FLAC**: Added `_is_lossy_preferred_mode()` to `SourceResolver` (returns True for `mp3`, `aac`, `m4a` output formats). Updated `_build_resolve_order()` with a `lossy_preferred` branch: when active, working (non-cooling) adapters are split into `lossy_normal` (adapters with `always_lossy=True` — JioSaavn and NetEase) and `lossless_normal` (Amazon, HiFi, DAB, Soulseek). `lossy_normal` comes first (preserving priority order within each group, shuffled within each tier), then `lossless_normal` as fallback. Cooling adapters still go at the very end regardless. In practice for MP3 mode: order becomes `[JioSaavn/NetEase, ..., Amazon, HiFi, DAB, Soulseek, ...(cooling)]`. Removed the M4A radio option from the Format Preference settings in `App.svelte`; options are now Auto / Lossless / MP3. Updated descriptions: Auto describes "Best available — lossless preferred, MP3 fallback"; Lossless clarified as "FLAC only — skip if unavailable"; MP3 notes it uses JioSaavn/NetEase directly.
  **Files modified:** `antra/core/resolver.py`, `antra-wails/frontend/src/App.svelte`

- **FEAT-09 fix — Playlist header now shows playlist cover instead of first-track album art**: Added `playlist_artwork_url: Optional[str] = None` field to `TrackMetadata` in `models.py`. In Spotify's `_fetch_playlist()`, changed `_fetch_playlist_name()` to `_fetch_playlist_meta()` (new helper that fetches `fields="name,images"` and returns `(name, artwork_url)` tuple); stamped `playlist_artwork_url` on every track in the authenticated path. In `_fetch_public_playlist_embed()`, extracted the playlist cover from the embed entity (`images`/`visuals.headerImage.sources`) and passed it as `playlist_artwork_url` when constructing each `TrackMetadata`. In Apple's `_fetch_playlist_name()` → refactored to `_fetch_playlist_meta()` that also reads `attributes.artwork` (resolves `{w}/{h}` template at full resolution) and returns `(name, artwork_url)` tuple; stamped `playlist_artwork_url` on each track in `_playlist_via_catalog_api()`. In `json_cli.py`, changed `_artwork_early` to prefer `playlist_artwork_url` over `artwork_url` for both the `playlist_loaded` event and the `playlist_summary` event.
  **Files modified:** `antra/core/models.py`, `antra/core/spotify.py`, `antra/core/apple_fetcher.py`, `antra/json_cli.py`

- **FEAT-08 fix — Library Build History now shows cover art thumbnails**: Added `artwork_url` field to `playlist_summary` JSON event in `json_cli.py` (uses same `playlist_artwork_url`-first logic as `playlist_loaded`; included in error-path summary too). Added `ArtworkUrl string` field (`json:"artwork_url,omitempty"`) to `HistoryItem` Go struct in `app_backend.go`. Updated `HistoryItem` TypeScript class in `models.ts` to include `artwork_url?: string`. In `App.svelte` history card, added a 40×40px rounded thumbnail to the left of each item (fallback: 🎵 icon in a muted box); old history entries without `artwork_url` gracefully fall back to the icon.
  **Files modified:** `antra/json_cli.py`, `antra-wails/app_backend.go`, `antra-wails/frontend/wailsjs/go/models.ts`, `antra-wails/frontend/src/App.svelte`

- **BUG-09 fix — Rate-limited adapters (e.g. Amazon) no longer block downloads**: Changed `_build_resolve_order()` in `resolver.py`: instead of moving rate-limited adapters to the back of their own tier, they are now moved to the END of the entire resolve order (after all non-rate-limited adapters across all tiers). When Amazon (priority 1, sole adapter in its tier) hits its rate limit, the new order becomes `[HiFi, DAB, Soulseek, ..., Amazon(cooling)]` — downloads proceed at full speed via HiFi/DAB and Amazon is only retried as an absolute last resort. Quality hierarchy is preserved among all non-cooling adapters (they are still ordered by priority tier). Previously, Amazon's 30-second cooldown was ineffective because it was the only adapter at priority 1, so it was always tried first even while cooling.
  **Files modified:** `antra/core/resolver.py`

- **BUG-08 fix — Artist name search fallback to iTunes**: In `spotify.py search_artists()`, replaced the final `return []` with a call to the already-existing `_search_artists_itunes()` method. Previously, when both the Spotipy credentials path and the anonymous TOTP token path failed (e.g. Spotify rotated their API), the method returned empty with no further fallback — resulting in "No artists found" even though `service.py` separately calls `AppleFetcher().search_artists()`. Now `search_artists()` itself always guarantees an iTunes fallback, making the Spotify search self-contained and reliable regardless of whether the Spotify API is accessible.
  **Files modified:** `antra/core/spotify.py`

- **FEAT-07 — Amazon Music-style album/playlist metadata header**: Enriched `playlist_loaded` event in `json_cli.py` with four new fields: `content_type` (ALBUM/PLAYLIST/SINGLE — inferred from URL pattern and track data), `artists_string` (album-level artists formatted as "A & B"), `release_date` (parsed from `release_date` field as "Apr 12 2024", falling back to `release_year`), and `quality_badge` (LOSSLESS/AAC/MP3 mapped from `cfg.output_format`). Added three helper functions before `main()`. Frontend changes: added 6 state vars (`playlistArtists`, `playlistReleaseDate`, `playlistContentType`, `playlistQualityBadge`, `playlistTotalDurationMs`, `playlistTotalTracks`) and `formatDuration()` helper; updated `playlist_loaded` handler to populate them (total duration computed client-side from `tracks[].duration_ms` sum); cleared all new vars in both `startDownload()` and the discography download handler. Replaced the simple `<img> + <span>` header with a two-column layout: larger cover art (76px) on the left; on the right — muted type label, bold title, artist line, info line (N songs · X hr Y min · date) separated by `·`, and an accent-colored quality badge. Updated all associated CSS classes.
  **Files modified:** `antra/json_cli.py`, `antra-wails/frontend/src/App.svelte`

- **FEAT-06 — Reduce concurrent downloads from 5 to 3**: Changed `max_workers` default in `EngineConfig` from `5` to `3`. Reduces simultaneous adapter pressure and rate-limit exposure without affecting the download pipeline otherwise.
  **Files modified:** `antra/core/engine.py`

- **FEAT-05 — Even load distribution across same-priority adapters (Amazon / HiFi / DAB)**: Added `_build_resolve_order(excluded)` to `SourceResolver` — groups adapters by priority, shuffles within each group on every `resolve()` call (non-rate-limited first, cooling-down last), then flattens back to a list. This replaces the previous fixed `self.adapters` iteration, so Amazon no longer takes 100% of the load just because it has priority=1 when HiFi and DAB also sit at priority 2. Quality hierarchy is fully preserved: priority groups are still processed in ascending-priority order; shuffling only affects ordering within a group. Added `_mark_rate_limited()` / `_is_rate_limited()` with a 30-second cooldown: when `adapter.search()` raises `RateLimitedError`, the adapter is moved to the back of its group for 30 s instead of being skipped globally — it stays available as a last resort within its tier. `RateLimitedError` is now explicitly caught in the search try/except (previously it fell into the generic exception handler and was silently swallowed). `preserve_input_order` paths are unchanged. Thread-safe: shuffle operates on a per-call local list; cooldown dict is guarded by a lock.
  **Files modified:** `antra/core/resolver.py`

- **BUG-07 fix — Deduplication misses collaborative albums with different artist folder names**: The identity key system used `primary_artist` (just `artists[0]`) for title-based dedup. This fails when sources represent the same collaboration differently — e.g. Spotify stores `["Future", "Metro Boomin"]` (primary = "future") but an existing file tagged by another tool has `artist = ["Future & Metro Boomin"]` (primary normalizes to "future metro boomin"). Added `_artists_canonical_key(artists)` static method to `organizer.py`: splits each artist string on common separators (`&`, `,`, `/`, `feat.`), normalizes each part, sorts them — so all representations of the same artist set produce an identical key. Added a `title_artists:{title}:{canonical_artists}` identity key in both `_track_identity_keys()` (for the incoming track) and `_identity_keys_from_values()` (for files scanned from disk), so cross-source dedup now works regardless of artist order, separator style, or combined-vs-split tagging.
  **Files modified:** `antra/utils/organizer.py`

- **BUG-06 fix — Multi-CD disc-prefixed filenames for Plex**: Added `total_discs: Optional[int]` field to `TrackMetadata` in `models.py`. Added `_stamp_disc_totals()` static method in `service.py` — groups fetched tracks by `album_id` (or album+artist fallback), computes `max(disc_number)` per group, and writes it back to every track before the list is returned from `fetch_playlist_tracks()`. Refactored `fetch_playlist_tracks()` from early-return style to if/elif/else so `_stamp_disc_totals()` runs on a single exit path. Updated filename construction in `organizer.py`: when `total_discs > 1` (or `disc_number >= 2` as a context-free fallback), formats as `DTT` (e.g. `101 - Title.flac`, `201 - Title.flac`) instead of the old `D-TT` or flat `TT` format. Single-disc albums are unaffected.
  **Files modified:** `antra/core/models.py`, `antra/core/service.py`, `antra/utils/organizer.py`

- **BUG-04 follow-up — Source toggle logic fix**: `toggleSourceGroup()` was silently a no-op when `sources_enabled = []` (all on) because filtering an empty array always returns `[]`. Fixed by materializing the full explicit list `['hifi', 'soulseek']` before mutating, so unchecking a group correctly produces e.g. `['hifi']` instead of leaving the empty array unchanged.
  **Files modified:** `antra-wails/frontend/src/App.svelte`

- **FEAT-03 — Bulk select/deselect by group in discography modal**: Each release-type group header (Albums, Singles, EPs & Compilations) now has inline **All** and **None** buttons so users can bulk select or deselect just that group independently. The global Select All / Deselect All buttons at the top remain for full-list control.
  **Files modified:** `antra-wails/frontend/src/App.svelte`

- **FEAT-02 — Separate log panel + playlist header + full pre-populated tracklist**: (1) `json_cli.py` now emits a `playlist_loaded` event immediately after `fetch_playlist_tracks` returns — before any individual downloads start. This event carries `title`, `artwork_url`, and the full `tracks` list (`artist`, `title`, `duration_ms` per track). (2) Frontend handles `playlist_loaded`: sets `playlistTitle`/`playlistArtwork` and pre-populates the entire `trackOrder` + `activeTracks` in "Waiting..." state so the full tracklist appears the moment metadata is fetched. (3) A playlist header (cover art + title) is shown above the tracklist whenever `playlistTitle` or `playlistArtwork` is set. Replaced the combined `.terminal` scroll area with a scrollable tracklist (color-coded rows, inline progress bars) and a floating **📋 Log** button that slides in a right-edge log panel overlay. `process_ended` only clears on cancel; tracks persist until the next download.
  **Files modified:** `antra/json_cli.py`, `antra-wails/frontend/src/App.svelte`

- **FEAT-01 — Show album/playlist title in Library History**: Added `title` field to `playlist_summary` in `json_cli.py` — derived from `tracks[0].playlist_name` (playlists) or `tracks[0].album` (albums), empty string on error. Added `Title string` to `HistoryItem` in `app_backend.go`. Updated the history card in `App.svelte`: title shown as primary bold label (with ellipsis overflow), URL shown as smaller secondary line below it. Old history entries (no `title` field) fall back to showing the URL as primary label.
  **Files modified:** `antra/json_cli.py`, `antra-wails/app_backend.go`, `antra-wails/frontend/src/App.svelte`

- **BUG-05 fix — DAB adapter not in source priority list / NetEase lossy flag**: Bumped `DabAdapter.priority` from 3 → 2 in `sources/dab.py` so DAB ties with HiFi in the free-lossless tier (Amazon=1, HiFi+DAB=2, Soulseek=3). Set `NetEaseAdapter.always_lossy = True` in `sources/netease.py` — NetEase's freely streamable tier is MP3 only; lossless requires a VIP account this adapter doesn't have, so it should be skipped entirely in lossless-only mode (the resolver's existing `always_lossy` guard now handles it). Updated the Sources toggle label in `App.svelte` from "Hi-Fi (Amazon, Tidal proxy)" to "Hi-Fi (Amazon, HiFi, DAB)" to reflect all three adapters in the group. Note: this also supersedes the BUG-03 resolver fallback fix — with `always_lossy = True` on NetEase, it never reaches `best_result` in lossless mode at all.
  **Files modified:** `antra/sources/dab.py`, `antra/sources/netease.py`, `antra-wails/frontend/src/App.svelte`

- **BUG-04 fix — Settings not saved between sessions**: Both the overlay click and the header "Close" button in the settings modal called `showSettings = false` without invoking `SaveConfig` — only the dedicated "Save Settings" button at the bottom actually persisted. Changed both paths to call `saveSettings()` instead. Renamed header button to "Save & Close" to make intent clear. Removed the now-redundant dedicated "Save Settings" button at the bottom. Also bumped the version label in the settings footer to v1.1.3.
  **Files modified:** `antra-wails/frontend/src/App.svelte`

- **BUG-03 fix — Lossless mode downloads MP3s via NetEase/JioSaavn**: Fixed the last-resort fallback in `resolver.py` (`_is_lossless_only_mode` branch, lines 277-284). Previously it returned the lossy `best_result` (e.g. a NetEase MP3) whenever no lossless source cleared the acceptance threshold. Now it checks `best_result.is_lossless`: low-confidence lossless results are still returned (correct format even if uncertain match), but lossy results are rejected and `None` is returned so the engine marks the track as failed. JioSaavn was already excluded in lossless mode via `always_lossy = True`; NetEase intentionally keeps `always_lossy = False` since it can theoretically return lossless — the result-level flag is the right gate.
  **Files modified:** `antra/core/resolver.py`

- **BUG-02 fix — Apple Music private playlists return 0 tracks silently**: Fixed `_RE_PLAYLIST` regex in `apple_fetcher.py` to include `-` in the character class (`[a-zA-Z0-9-]+`) so user-created playlist IDs like `pl.u-Jkl8kP18MWDjD6Z` are captured correctly instead of truncating at the hyphen. Added an early guard in `_fetch_playlist()`: if `playlist_id` starts with `pl.u-`, raises `ValueError` immediately with a user-facing message instead of silently exhausting all fetchers and returning 0 tracks.
  **Files modified:** `antra/core/apple_fetcher.py`

- **BUG-01 fix — FFmpeg OpenSSL conflict on Fedora 43**: Added `get_clean_subprocess_env()` to `antra/utils/runtime.py`. Strips the PyInstaller `_MEIPASS` temp dir from `LD_LIBRARY_PATH` (and `LD_PRELOAD`) before spawning any ffmpeg/ffprobe child process on Linux. Applied to `--probe` and `--spectrogram` subprocess calls in `antra/json_cli.py`, and to the ffmpeg transcoding call in `antra/utils/transcoder.py`.
  **Files modified:** `antra/utils/runtime.py`, `antra/json_cli.py`, `antra/utils/transcoder.py`

- **BUG-12 fix — ISRCEnricher hits Spotify 429 rate limit, achieving 0/12 ISRCs**: Root cause was `max_workers=10` firing too many parallel requests against the anonymous Spotify `/tracks` endpoint. Fixed: reduced default `max_workers` from 10 → 2; added exponential backoff retry on 429 (waits 2s/4s/8s across up to 3 attempts before giving up); for single-batch cases (≤50 tracks) bypasses the thread pool entirely and calls `fetch_batch` directly; extracted `_apply_results()` helper to deduplicate result stamping logic.
  **Files modified:** `antra/core/isrc_enricher.py`

- **FEAT-20 — Health check chips: real brand icons**: Replaced hand-drawn inline SVG approximations in the three source health chips (Tidal/Amazon/Qobuz) with real brand images. Icons copied to `antra-wails/frontend/public/icons/` (tidal.webp, amazon-music.jpg, qobuz.png) so Vite serves them as static assets at `/icons/...`. The `<img>` tags use `object-fit: contain` at 20×20px; `opacity` drops to 0.2 when all endpoints are down (replaces the previous per-SVG color-swap approach). Added `.health-chip-icon` CSS rule.
  **Files modified:** `antra-wails/frontend/src/App.svelte`, `antra-wails/frontend/public/icons/` (new asset files)

- **BUG-13 fix — Managed slskd bootstrap fails with Permission denied on macOS/Linux**: `zipfile.ZipFile.extractall()` does not restore Unix execute permissions from zip metadata, so the extracted slskd binary lands as non-executable (`0o644`). Added `os.chmod(exe, 0o755)` after extraction on non-Windows platforms so the binary is immediately runnable without a manual `chmod +x`.
  **Files modified:** `antra/utils/slskd_manager.py`

- **FEAT-21 — Ko-fi toast: copy update + hover persistence**: Updated toast title from "Antra is hand-crafted" to "Support Antra"; body text now says "Real effort goes into maintaining Antra and keeping it free. If it saves you time, consider supporting continued development." (removes "AI-generated" claim). Same copy applied to the Ko-fi header tooltip. Fixed hover persistence: moved `sponsorToastTimer` initialization inside the `showSponsorToast = true` callback so the 9s countdown starts only when the toast actually appears; added `on:mouseenter` (clears timer) and `on:mouseleave` (restarts with 3s grace) to the toast div so it stays visible while the cursor is over it.
  **Files modified:** `antra-wails/frontend/src/App.svelte`

---

### v1.1.2 — released 2026-04-15

**Files modified:** `antra/core/spotify.py`, `antra/core/spotfetch_fetcher.py`, `antra/core/service.py`, `antra/core/config.py`, `antra/core/models.py`, `antra/core/amazon_music_fetcher.py`, `antra/sources/amazon.py`, `antra/sources/odesli.py`, `antra/sources/netease.py`, `antra/utils/tagger.py`, `antra/utils/transcoder.py`, `antra/utils/organizer.py`, `antra/core/engine.py`, `antra/core/resolver.py`, `antra/core/apple_fetcher.py`, `antra/json_cli.py`, `antra/sources/base.py`, `antra/sources/qobuz.py`, `antra/sources/soulseek.py`, `antra-wails/app_backend.go`, `antra-wails/frontend/src/App.svelte`, `antra-wails/frontend/wailsjs/go/main/App.js`, `antra-wails/frontend/wailsjs/go/main/App.d.ts`, `antra-wails/frontend/wailsjs/go/models.ts`, `requirements-runtime.txt`, `.github/workflows/release.yml`

- **Spotify TOTP token**: Added `_get_totp_access_token()` in `spotify.py` using `pyotp` + Spotify's internal `/api/token?reason=init&productType=web-player&totp=...` endpoint. Eliminates dependency on third-party SpotFetch proxies for metadata.
- **Spotify partner GraphQL API**: Added `_fetch_album_via_partner_api()` in `spotify.py` — paginates album tracks via `api-partner.spotify.com` with persisted query hashes. Primary no-auth fallback when SpotFetch proxies are down.
- **SpotFetch multi-mirror pool**: `spotfetch_fetcher.py` now accepts a list of base URLs, tries them in order, skips on DNS failure, short-circuits on 404.
- **`_fetch_spotfetch_tracks` 3-stage fallback** in `service.py`: (1) SpotFetch mirrors, (2) partner GraphQL API, (3) public HTML scrapers.
- **Amazon Music JSON-LD parsing**: `amazon_music_fetcher.py` now parses `schema.org/MusicAlbum` JSON-LD embedded in every page, replacing the broken web-component scraper. Extracts track ASINs directly.
- **Direct ASIN passthrough**: `TrackMetadata.amazon_asin` field added to `models.py`. When an Amazon Music URL is the source, track ASINs flow directly to `AmazonAdapter.search()`, bypassing Odesli.
- **Odesli resolver ordering fixed** in `odesli.py`: Odesli → Songwhip → amazon.com scraper (was: scraper first, causing wrong ASINs like old CD pressings).
- **Amazon 404 no-retry**: `amazon.py` `should_retry_download()` now returns `False` for 404 errors, eliminating 3× retry delay.
- **Version label**: `Antra v1.1.2` added to settings panel footer in `App.svelte`.
- **`pyotp>=2.9.0`** added to `requirements-runtime.txt`.
- **pyncm version fixed**: pinned to `>=1.8.0` (was `>=2.0.0` which doesn't exist on PyPI).
- **CI runner fix**: switched Intel macOS build from deprecated `macos-13` / `macos-12` to `macos-15-intel`.

---

### v1.1.1 — released 2026-04-10

- Restored all source adapters and fetchers for working builds after they were removed from the public repo in v1.1.0.

---

### v1.1.0 — released 2026-04-10

- **Artist Search** — search for artists by Spotify/Apple Music links and download their full discography or individual albums.
- **Faster Downloads** — parallel download engine (`ThreadPoolExecutor`) for batch and playlist downloads.
- **Analyzer Fixed** — spectrogram analyzer now works out of the box; ffmpeg path auto-detected from bundled backend.
- **macOS & Linux** — official builds added for Apple Silicon, macOS Intel, and Linux AppImage.
- **`SearchArtists` Go binding** added to `app_backend.go` and `antra/json_cli.py --search-artists`.
- **`GetArtistDiscography` Go binding** added; Svelte discography modal for selecting albums before download.
- Fixed analyzer requiring manual `ffmpeg` PATH configuration.
- Improved source fallback reliability.
