# Antra — Bug Tracker & Feedback

> This file is read by Claude at the start of every session.
> Status tags: `[OPEN]` · `[IN PROGRESS]` · `[FIXED in vX.X.X]` · `[WONT FIX]` · `[NEEDS INFO]`
> Do not delete entries — update the status tag and add a note instead.

---

## v1.1.3 — Target Issues

---

### BUG-01 · `[FIXED in v1.1.3]` — FFmpeg OpenSSL conflict on Fedora 43

**Type:** Bug
**Platform:** Linux (Fedora 43 Workstation, x86_64)
**Component:** `analyzer.go`, bundled PyInstaller backend (libssl)

**Description:**
Audio Quality Analyzer fails on Fedora 43 because the PyInstaller bundle ships its own `libssl.so.3` (OpenSSL 3.2.0) into `/tmp/_MEI*/`, but the system `libcurl.so.4` (linked against the system OpenSSL) picks it up instead of the system one, causing a version mismatch.

**Error:**
```
/usr/bin/ffmpeg: /tmp/_MEIPUXdDx/libssl.so.3: version 'OPENSSL_3.2.0' not found
(required by /lib64/libcurl.so.4)
```

**Root cause hypothesis:**
PyInstaller extracts bundled `.so` files into `/tmp/_MEI*/` and those leak into the dynamic linker search path, shadowing system libraries for child processes (ffmpeg) that Antra spawns.

**Suggested fix:**
When spawning ffmpeg as a subprocess, set `LD_LIBRARY_PATH` to exclude the PyInstaller temp dir, or explicitly unset it so the child process uses only system libraries. Alternatively, use the system ffmpeg path without inheriting the bundled lib environment.

**Files likely involved:** `antra-wails/analyzer.go`, `antra/utils/runtime.py`

---

### BUG-02 · `[FIXED in v1.1.3]` — Apple Music private playlists return 0 tracks

**Type:** Bug (expected limitation — needs confirmation)
**Platform:** Windows (reported), likely all platforms
**Component:** `antra/core/apple_fetcher.py`, `antra/core/service.py`

**Description:**
Pasting a private Apple Music playlist URL results in `0 / 0` tracks with no error — the download completes silently and successfully but with nothing downloaded.

**Reproduction:**
```
Input: https://music.apple.com/us/playlist/<name>/pl.u-<id>
Result:
  Tracks added       : 0 / 0
  Already in library : 0
  Could not source   : 0
  Total size         : 0 MB
  Time taken         : 2s
```

**Root cause hypothesis:**
`pl.u-` prefix indicates a user-created (private) playlist. Apple Music's public API does not expose private playlists without authentication. The fetcher likely gets an empty or 403 response and silently returns zero tracks instead of surfacing an error.

**Suggested fix:**
Detect `pl.u-` playlist URLs in `apple_fetcher.py` and return an explicit user-facing error: _"This appears to be a private Apple Music playlist. Private playlists cannot be accessed without Apple Music authentication."_ Do not silently succeed with 0 tracks.

**Files likely involved:** `antra/core/apple_fetcher.py`, `antra/core/service.py`, `antra-wails/frontend/src/App.svelte` (error display)

---

### BUG-03 · `[FIXED in v1.1.3]` — Lossless mode downloads MP3s via NetEase/JioSaavn

**Type:** Bug
**Platform:** All
**Component:** `antra/core/resolver.py`, `antra/sources/netease.py`, `antra/sources/jiosaavn.py`

**Description:**
When the user has selected **Lossless Only** mode in settings, the download engine still falls through to NetEase and JioSaavn adapters, which return MP3/AAC files. The lossless constraint is not being enforced at the adapter selection level.

**Expected behavior:**
- `Lossless Only` → only attempt FLAC sources. If no FLAC found, mark track as **failed** (do not fall back to lossy).
- `Auto` → try lossless first, fall back to lossy (current behavior, keep as-is).
- `MP3 / Lossy` → NetEase and JioSaavn are fair game.

**Suggested fix:**
In `resolver.py`, filter the adapter list based on the configured quality mode before attempting resolution. NetEase (`netease.py`) and JioSaavn (`jiosaavn.py`) should be excluded when mode is `lossless`. If the filtered list is exhausted with no result, emit a failed download event rather than falling back.

**Files likely involved:** `antra/core/resolver.py`, `antra/core/config.py` (quality mode enum), `antra/sources/netease.py`, `antra/sources/jiosaavn.py`

---

### BUG-04 · `[FIXED in v1.1.3]` — Settings not saved between sessions

**Type:** Bug
**Platform:** All (confirmed)
**Component:** `antra-wails/app_backend.go`, `antra-wails/frontend/src/App.svelte`

**Description:**
Changes made in the Settings panel are not persisted. After closing and reopening the app, all settings revert to defaults.

**Suggested fix:**
Audit the save/load path in `app_backend.go` — verify that `SaveConfig` is being called on settings change and that the config file path resolves correctly on all platforms. Check that the Svelte settings panel is actually invoking the Go `SaveConfig` binding on change, not just updating local state.

**Files likely involved:** `antra-wails/app_backend.go`, `antra-wails/frontend/src/App.svelte`

---

### BUG-05 · `[FIXED in v1.1.3]` — DAB adapter not in source priority list

**Type:** Change request
**Platform:** All
**Component:** `antra/core/resolver.py`, `antra/core/config.py`, `antra-wails/frontend/src/App.svelte`

**Description:**
The DAB adapter (`sources/dab.py`) is functional but not included in the default source priority order. Requested priority order:

| Priority | Source |
|---|---|
| 1 | Amazon |
| 2 | DAB (`dab.yeet.su`) |
| 2 | HiFi |
| 3 | NetEase |
| ... | (rest unchanged) |

**Suggested fix:**
Add `DabAdapter` to the resolver's default priority chain in `resolver.py` / `config.py` at position 2 (alongside or just after HiFi). Expose it as a toggleable source in the GUI sources selector in `App.svelte`.

**Files likely involved:** `antra/core/resolver.py`, `antra/core/config.py`, `antra-wails/frontend/src/App.svelte`

---

## Feature Requests — v1.1.3

---

### FEAT-01 · `[FIXED in v1.1.3]` — Show album title in Library History viewer

**Type:** Feature request
**Component:** `antra-wails/frontend/src/App.svelte`, `antra-wails/app_backend.go`

**Description:**
The history viewer currently shows the source URL. Replace or supplement with the resolved album/track title so users can identify downloads at a glance without opening the URL.

**Suggested implementation:**
Store `album_title` (already available in `TrackMetadata`) alongside the URL when writing history entries in `app_backend.go`. Display it as the primary label in the history list in `App.svelte`, with the URL as secondary/tooltip text.

**Files likely involved:** `antra-wails/app_backend.go`, `antra-wails/frontend/src/App.svelte`

---

### FEAT-02 · `[FIXED in v1.1.3]` — Separate log panel from download progress

**Type:** Feature request (UI overhaul)
**Component:** `antra-wails/frontend/src/App.svelte`

**Description:**
Currently the text log and per-track download progress bars are in the same scrollable area, requiring constant scrolling to see both. Requested changes:

**Download area (main screen):**
- Show all tracks in the playlist/album at once as a tracklist
- Each row: small cover thumbnail, full track title, all artists, duration, format/quality badge
- Download progress bar per track, shown inline below or alongside each row
- Tracklist visible before download starts; bars fill in as download progresses

**Log panel:**
- Detached from the download area
- Accessible via a floating toggle button on the left or right edge of the screen
- Slides open as an overlay or side panel without disrupting the main tracklist view
- Auto-scrolls to latest log line when open

**Files likely involved:** `antra-wails/frontend/src/App.svelte`

---

### FEAT-03 · `[FIXED in v1.1.3]` — Bulk select/deselect singles separately in discography modal

**Type:** Feature request
**Component:** `antra-wails/frontend/src/App.svelte`

**Description:**
The artist discography modal has Select All / Deselect All buttons that act on the entire list. Users want separate bulk controls for **Albums** and **Singles** independently, e.g.:
- "Select all albums" / "Deselect all albums"
- "Select all singles" / "Deselect all singles"

**Suggested implementation:**
Group the discography list by release type (album vs. single vs. EP) with a header per group. Add a checkbox or toggle button in each group header for bulk select/deselect of that group.

**Files likely involved:** `antra-wails/frontend/src/App.svelte`, potentially `antra-wails/app_backend.go` if discography response needs a `release_type` field added

---

---

## Bug Reports — v1.1.3 (continued)

---

### BUG-06 · `[FIXED in v1.1.3]` — Multi-CD albums use flat track numbering instead of disc-prefixed format

**Type:** Bug
**Platform:** All
**Component:** `antra/utils/tagger.py`, `antra/utils/organizer.py`

**Description:**
When downloading a multi-disc album (e.g. a 2-CD release), Antra writes track filenames as `01.flac`, `02.flac`, ... `n.flac` across all discs. Plex (and most media servers) expect the disc number to be embedded in the track number so discs can be distinguished:

| Expected (Plex-compatible) | What Antra produces |
|---|---|
| `101 - Track Title.flac` (disc 1, track 1) | `01 - Track Title.flac` |
| `112 - Track Title.flac` (disc 1, track 12) | `12 - Track Title.flac` |
| `201 - Track Title.flac` (disc 2, track 1) | `01 - Track Title.flac` ← collision |

The collision on track numbers means Plex cannot distinguish which disc a track belongs to and groups everything into a single unsorted list.

**Expected behavior:**
For multi-disc albums (any album where `disc_number > 1` exists in the metadata), format the track number as `DTTT` where `D` = disc number and `TTT` = zero-padded track number on that disc:
- Disc 1 Track 1 → `101`
- Disc 1 Track 12 → `112`
- Disc 2 Track 1 → `201`

Single-disc albums should keep the current flat `01`, `02` format — no regression.

**Files likely involved:** `antra/utils/organizer.py` (filename construction), `antra/utils/tagger.py` (ID3 `TPOS`/`disc_number` tag must already be set correctly for this to work)

---

### BUG-07 · `[FIXED in v1.1.3]` — Deduplication misses already-downloaded albums when artist folder name differs between sources

**Type:** Bug
**Platform:** All
**Component:** `antra/utils/organizer.py`

**Description:**
When a collaborative album (multiple artists) is downloaded via a Spotify URL, the artist folder name is constructed from the Spotify metadata (e.g. `Future & Metro Boomin`). If the same album was previously downloaded via Apple Music or another source, the folder name may differ slightly (e.g. `Future` only, or `Metro Boomin` only, or different separator/casing). Antra does not recognize these as the same album and downloads duplicates instead of skipping.

**Reproduction:**
Download `https://open.spotify.com/album/3bSNhnaQQXpC639OQ4pMyP` ("We Still Don't Trust You" — Future & Metro Boomin). If the album already exists in the library under a slightly different artist folder name (reported: user had to rename manually), Antra re-downloads instead of detecting it as a duplicate.

**Root cause hypothesis:**
`organizer.py` likely does exact string matching on the artist folder path. Collaborative album artist names are formatted differently across sources (Spotify joins with ` & `, Apple Music may use the primary artist only, Amazon may list differently).

**Suggested fix:**
In the deduplication check in `organizer.py`, normalize artist names before comparing: lowercase, strip punctuation, split on common separators (`&`, `,`, `/`), sort constituent names, and compare the resulting sets. A fuzzy match (e.g. >80% similarity via `difflib`) on the album folder path would also catch minor spelling differences. The album title + track count is a strong secondary signal.

**Files likely involved:** `antra/utils/organizer.py`, potentially `antra/utils/matching.py` (reuse existing similarity helpers)

---

## Feature Requests — v1.1.3 (continued)

---

### FEAT-04 · `[OPEN — v1.1.4]` — ALAC output format option (for Apple Music / iPhone sync)

**Type:** Feature request
**Platform:** All (primarily macOS/Windows users syncing to iPhone via Apple Music)
**Component:** `antra/utils/transcoder.py`, `antra-wails/frontend/src/App.svelte`, `antra-wails/app_backend.go`

**Description:**
Apple Music on iPhone cannot play FLAC files directly — it requires ALAC (Apple Lossless, `.m4a` container). Users who download via Antra and want to sync to their iPhone via Apple Music currently have to manually batch-convert FLAC → ALAC, which is slow and tedious.

**Requested behavior:**
Two related features:

1. **Settings option — Output format**: Add a "Output Format" dropdown in settings with options: `FLAC (default)`, `ALAC (.m4a)`, `MP3`, `AAC`. When `ALAC` is selected, all downloaded files are transcoded to ALAC after download, before being moved to the library folder.

2. **Post-download conversion prompt**: After a download batch completes, offer a button/option: "Convert to ALAC" (or the selected output format). This covers the case where the user downloaded FLAC previously and now wants to convert the batch they just got.

**Implementation notes:**
- FLAC → ALAC is lossless; ffmpeg handles it: `ffmpeg -i input.flac -c:a alac output.m4a`
- All existing ID3/FLAC tags should be re-written to the `.m4a` container (MP4 tags) by `tagger.py`
- `transcoder.py` already has ffmpeg integration; add an `transcode_to_alac()` method
- The output format setting should be stored in config and passed through `engine.py` → `transcoder.py`

**Files likely involved:** `antra/utils/transcoder.py`, `antra/utils/tagger.py`, `antra/core/engine.py`, `antra/core/config.py`, `antra-wails/frontend/src/App.svelte`, `antra-wails/app_backend.go`

---

### FEAT-05 · `[FIXED in v1.1.3]` — Distribute download load evenly across Amazon / DAB / HiFi adapters

**Type:** Feature request / Change request
**Platform:** All
**Component:** `antra/core/resolver.py`

**Description:**
Currently the resolver tries adapters strictly in priority order — Amazon first, every time. Amazon hits its rate limit quickly during batch/playlist downloads, then the resolver falls through to DAB or HiFi for the rest of the batch. This concentrates hammering on Amazon, causes rate-limit delays mid-batch, and underutilises DAB/HiFi capacity.

**Requested behavior:**
Round-robin or randomised distribution across same-priority adapters (Amazon, DAB, HiFi all at priority 2 in the lossless tier) so the load is spread evenly:
- Still respect the quality hierarchy: 24-bit FLAC first across all three, then 16-bit FLAC, then Soulseek if enabled, then fail
- Do **not** round-robin across different quality tiers — quality precedence must be maintained
- Do **not** reduce throughput: the parallel download pool (`ThreadPoolExecutor`) should remain unchanged; only the per-track adapter selection order changes
- When one adapter is rate-limited, deprioritise it temporarily (e.g. move to back of rotation for N seconds) rather than fully skipping it

**Implementation notes:**
In `resolver.py`, for adapters sharing the same priority level, shuffle or rotate the order on each resolution call instead of always trying them in declaration order. A lightweight token-bucket or backoff tracker per adapter would handle the temporary rate-limit deprioritisation.

**Files likely involved:** `antra/core/resolver.py`, potentially `antra/sources/base.py` (rate-limit signalling)

---

### FEAT-06 · `[FIXED in v1.1.3]` — Reduce concurrent download thread count from 5 to 3

**Type:** Change request
**Platform:** All
**Component:** `antra/core/service.py` (or wherever the `ThreadPoolExecutor` max_workers is set)

**Description:**
The current parallel download pool processes 5 tracks simultaneously. User feedback indicates this is too aggressive — it triggers rate limits faster and puts unnecessary load on source adapters.

**Requested change:**
Lower `max_workers` (or equivalent concurrency limit) from `5` to `3`.

**Files likely involved:** `antra/core/service.py`, `antra/core/engine.py` — search for `ThreadPoolExecutor` or `max_workers`

---

### FEAT-07 · `[FIXED in v1.1.3]` — Richer album/playlist metadata header (Amazon Music-style)

**Type:** Feature request (UI)
**Platform:** All
**Component:** `antra-wails/frontend/src/App.svelte`

**Description:**
When a URL is pasted, Antra currently shows the album cover art and title. The user would like the header to display richer metadata in the same style as Amazon Music's album/playlist page:

```
[Cover art]  ALBUM
             We Still Don't Trust You
             Future & Metro Boomin  ·  25 songs  ·  1 hr 28 min  ·  Apr 12 2024
                                    [ULTRA HD badge if applicable]
```

Specifically:
- **Type label** above the title: `ALBUM`, `PLAYLIST`, `SINGLE`, etc. (smaller, muted text)
- **Title** — bold, large
- **Artist(s)** — linked or plain, comma/ampersand separated for collabs
- **Track count**, **total duration**, **release date** — on a single line separated by `·`
- **Quality badge** — e.g. `ULTRA HD`, `LOSSLESS`, `HD` if format metadata is available at fetch time

**Implementation notes:**
Most of this data is already available in the `playlist_loaded` event payload (`tracks` list has `duration_ms`, `artist`, `album`; top-level `title` and `artwork_url` are already used). Release date and total track count can be derived from the existing track list. The type label can be inferred from the URL pattern or a new field in the event.

**Files likely involved:** `antra-wails/frontend/src/App.svelte`, `antra/json_cli.py` (may need to enrich `playlist_loaded` payload with `release_date`, `content_type`)

---

---

## Bug Reports — v1.1.3 (new)

---

### BUG-08 · `[FIXED in v1.1.3]` — Artist name search returns no results

**Type:** Bug (regression)
**Platform:** All
**Component:** `antra/core/spotify.py`, `antra/core/apple_fetcher.py`, `antra/core/service.py`

**Description:**
Searching for an artist by name (e.g. "Metro Boomin", "J Cole") in the Search Artist mode returns "No artists found." despite these being well-known artists. This used to work correctly.

**Root cause hypothesis:**
The default search source is Apple Music (iTunes Search API). If the Spotify TOTP anonymous token no longer works for `api.spotify.com/v1/search` (Spotify may have restricted anonymous access), the primary Spotify search fails silently. The Apple Music fallback in `service.py` calls `AppleFetcher().search_artists()` which uses the iTunes Search API — this should be reliable, but may be failing due to SSL certificate issues in the PyInstaller bundle, network timeout, or the exception being swallowed.

Additionally, `spotify.search_artists()` does not call the already-existing `_search_artists_itunes()` method as a fallback after the anonymous token approach fails.

**Suggested fix:**
1. In `spotify.py search_artists()`, add `_search_artists_itunes()` as a guaranteed fallback when both Spotipy and the anonymous token approach fail.
2. Ensure the iTunes Search API path works reliably in bundled builds.

**Files likely involved:** `antra/core/spotify.py`, `antra/core/service.py`

---

### BUG-09 · `[FIXED in v1.1.3]` — Download speed drops when Amazon adapter hits rate limit

**Type:** Bug / performance issue
**Platform:** All
**Component:** `antra/core/resolver.py`

**Description:**
When downloading a playlist, Amazon Music is tried first (priority 1). Once Amazon hits its rate limit, the download speed noticeably decreases. Observed: Amazon=11 downloads, HiFi=21 downloads, DAB=0 downloads in one session — DAB appears to be getting no downloads at all despite being at the same priority tier as HiFi.

**Root cause:**
Amazon is the sole adapter at priority 1. The current rate-limit cooldown design moves rate-limited adapters to the back of their **tier** — but since Amazon is alone in priority 1, it's still tried first on every `resolve()` call even during its 30-second cooldown. Each call hits Amazon (immediately fails again), which delays every track resolution until Amazon is removed from contention.

The load-balancing shuffle (FEAT-05) only works within priority 2 (HiFi + DAB), so DAB should be getting ~50% of HiFi's load. If DAB is getting 0, it may be a server-side issue with `dab.yeet.su` rather than a code issue.

**Suggested fix:**
In `_build_resolve_order()`, instead of moving rate-limited adapters to the back of their tier, move them to the **end of the entire resolve order** (after all non-rate-limited adapters across all tiers). This way, when Amazon is cooling down, the order becomes `[HiFi, DAB, Soulseek, ..., Amazon(cooling)]` rather than `[Amazon(cooling), HiFi, DAB, ...]`. Amazon is only tried as absolute last resort across all adapters while in cooldown, so downloads proceed at full speed.

**Files likely involved:** `antra/core/resolver.py`

---

## Feature Requests — v1.1.3 (new)

---

### FEAT-08 · `[FIXED in v1.1.3]` — Library Build History: cover art thumbnail + correct card layout

**Type:** Feature request / Bug (UI regression)
**Platform:** All
**Component:** `antra-wails/frontend/src/App.svelte`, `antra-wails/app_backend.go`, `antra/json_cli.py`

**Description:**
History cards in the Library Build History panel should display cover art and use the correct label hierarchy. The backend plumbing (`artwork_url` in `playlist_summary`, `ArtworkUrl` in `HistoryItem`) was added in v1.1.3, but the card layout in `App.svelte` is currently wrong — title and URL are in the wrong order, or the cover art is not rendering.

**Required card layout:**
```
[cover] Album / Playlist Name        ← bold primary label
        https://open.spotify.com/... ← small secondary line
        N tracks · timestamp
```

**Implementation notes:**
- `artwork_url` is already included in `playlist_summary` from `json_cli.py`.
- `ArtworkUrl string` is already on the `HistoryItem` Go struct.
- Fix is purely in `App.svelte`: ensure the thumbnail renders on the left, title is the primary bold label, URL is the smaller secondary line below it. Old history entries without `artwork_url` should fall back to a muted music-note placeholder icon.

**Files likely involved:** `antra-wails/frontend/src/App.svelte`

---

### BUG-10 · `[FIXED in v1.1.3]` — MP3 mode downloads FLAC from Amazon/HiFi then transcodes

**Type:** Bug / UX issue
**Platform:** All
**Component:** `antra/core/resolver.py`, `antra-wails/frontend/src/App.svelte`

**Description:**
When the user selects MP3 as the output format, the resolver still tries Amazon and HiFi first (lossless sources at priority 1–2) and downloads FLAC, which is then transcoded to MP3. This wastes bandwidth and time — JioSaavn (priority 4, AAC 320kbps) and NetEase (priority 4, MP3) already provide lossy files at sufficient quality for the chosen output format. Additionally, the Settings panel shows a separate M4A option which the user wants removed in favour of a cleaner Auto / Lossless / MP3 set.

**Suggested fix:**
1. In `resolver.py`, detect MP3/lossy output mode (`_is_lossy_preferred_mode()`) and reorder `_build_resolve_order()` to put `always_lossy` adapters (JioSaavn, NetEase) before lossless adapters within the non-rate-limited list. Lossless adapters remain as fallback if all lossy sources fail.
2. In `App.svelte` settings, remove the M4A radio option and update descriptions.

**Files likely involved:** `antra/core/resolver.py`, `antra-wails/frontend/src/App.svelte`

---

### FEAT-09 · `[FIXED in v1.1.3]` — Playlist header shows track album art instead of playlist cover

**Type:** Bug / Feature (UI correctness)
**Platform:** All
**Component:** `antra/json_cli.py`, `antra/core/models.py`, `antra/core/spotify.py`, `antra/core/apple_fetcher.py`

**Description:**
When a Spotify or Apple Music **playlist** URL is pasted, the header cover art displayed above the tracklist is the first track's **album artwork** — not the playlist's own cover image. Album and single URLs correctly show the album cover, but playlist covers are often custom images (collages, curated visuals) that are completely different from any individual track's artwork.

**Suggested fix:**
1. Add `playlist_artwork_url: Optional[str] = None` to `TrackMetadata` in `models.py`.
2. In Spotify's `_fetch_playlist()`, fetch the playlist cover image alongside the name: change `fields="name"` to `fields="name,images"` and store `images[0]['url']` on each track as `playlist_artwork_url`.
3. In `_fetch_public_playlist_embed()`, extract `images` from the embed entity response.
4. In Apple's `_fetch_playlist()`, capture the playlist artwork from the Catalog API response.
5. In `json_cli.py`, prefer `tracks[0].playlist_artwork_url` over `tracks[0].artwork_url` when emitting the `playlist_loaded` event.

**Files likely involved:** `antra/core/models.py`, `antra/core/spotify.py`, `antra/core/apple_fetcher.py`, `antra/json_cli.py`

---

---

## Feature Requests — v1.1.3 (new, continued)

---

### FEAT-10 · `[FIXED in v1.1.3]` — Cross-album deduplication opt-out ("Full Albums" mode)

**Type:** Feature request
**Platform:** All
**Component:** `antra/utils/organizer.py`, `antra/core/config.py`, `antra-wails/frontend/src/App.svelte`, `antra-wails/app_backend.go`

**Description:**
Antra's library deduplication system identifies tracks by ISRC, Spotify ID, and normalised title+artist keys. When a track appears on both a "Best Of" compilation and the original studio album, downloading the studio album after the "Best Of" (or vice-versa) causes those shared tracks to be skipped — marked as "Already in library" — because they match existing entries under a different album folder. The result is incomplete studio albums in the library: some tracks are missing because they were only written to the compilation folder.

**Example:**
- Download "Ice Cube — Greatest Hits" → all tracks written to `.../Ice Cube/Greatest Hits/`
- Later download "Ice Cube — AmeriKKKa's Most Wanted" → several tracks skipped because they already exist in the Greatest Hits folder, leaving AmeriKKKa's Most Wanted with gaps.

**User preference:** Download full albums always, even if the same ISRC was previously saved as part of a different album. Each album should be self-contained in its own folder.

**Requested behavior:**
Add a settings toggle: **Library Mode**
- `Smart dedup` (current default): skip a track if the same ISRC/ID already exists anywhere in the library — saves storage.
- `Full albums`: skip only if the file already exists in the **same destination folder** (same album). Cross-album duplicates are allowed — each album is downloaded completely regardless of what the rest of the library contains.

**Implementation notes:**
- The identity key index in `LibraryOrganizer` (`_load_identity_index`) scans all files and builds a global ISRC→path map. In "Full albums" mode, simply skip building/consulting the cross-album index; only check for an existing file in the target directory before writing.
- The setting should be stored in `Config` and passed through to `LibraryOrganizer`.

**Files likely involved:** `antra/utils/organizer.py`, `antra/core/config.py`, `antra-wails/app_backend.go`, `antra-wails/frontend/src/App.svelte`

---

### FEAT-12 · `[FIXED in v1.1.3]` — Use Apple Music `audioTraits` to inform resolver source priority

**Type:** Feature request
**Platform:** All
**Component:** `antra/core/resolver.py`, `antra/core/models.py`

**Description:**
Apple Music's Catalog API returns an `audioTraits` array on every track — values include `lossless`, `hi-res-lossless`, `atmos`, `adm` (Apple Digital Masters). This data is already fetched by `apple_fetcher.py`, stored on `TrackMetadata.audio_traits`, and written to a custom `AUDIO_TRAITS` file tag by `tagger.py`. However it is never *acted on* by the resolver.

**Requested behavior:**
When `audio_traits` on a `TrackMetadata` contains `hi-res-lossless`, the resolver should know a 24-bit master exists and prioritise Qobuz/Tidal (if configured) or HiFi over JioSaavn/NetEase. When it contains only `lossless` (16-bit), CD-quality sources are sufficient. When it contains neither, the track may only be available lossy on streaming and the resolver can skip expensive lossless probes faster.

**Implementation notes:**
- `_quality_tier()` and `_candidate_key()` in `resolver.py` could incorporate `track.audio_traits` as a hint when scoring candidates.
- If `hi-res-lossless` is present and a lossless source returns only 16-bit, keep trying rather than accepting immediately.
- This is an optimisation / quality hint, not a hard gate — if no better source is found, fall back as usual.

**Files likely involved:** `antra/core/resolver.py`, `antra/core/models.py`

---

### FEAT-13 · `[FIXED in v1.1.3]` — Genre and year tags missing from Windows Media Player properties

**Type:** Bug / Feature request
**Platform:** Windows (WMP), likely visible in all tag readers
**Component:** `antra/utils/tagger.py`, `antra/core/engine.py`, `antra/core/isrc_enricher.py`

**Description:**
Downloaded tracks are missing genre and year in Windows Media Player's file properties. The tagger already writes these (`TDRC`/`TCON` for MP3, `date`/`genre` Vorbis for FLAC, `©day`/`©gen` for M4A) — but only when the fields are non-empty on `TrackMetadata`. The fields are often empty because:

1. **Genre**: populated only from Apple Music's `genreNames` (Apple-sourced tracks only) or MusicBrainz via `engine._enrich_genres_if_needed()` — which requires an ISRC. Since `ISRCEnricher` is not wired into the pipeline, Spotify/Amazon/DAB/HiFi tracks arrive without ISRCs and never get genre enrichment.

2. **Year**: sourced from the metadata fetcher. Sources that don't return `release_date` or `release_year` leave it as `None`, so no year tag is written.

**Suggested fix:**
1. Wire `ISRCEnricher` into `service.py` so all tracks get ISRCs (and thus MusicBrainz genre lookup) regardless of source.
2. Ensure `release_year` is always set — fall back to parsing the year from `release_date` string if the source returns only a date.
3. For WMP compatibility, verify that the FLAC `date` Vorbis comment is being read correctly; WMP may expect the `year` key or a plain 4-digit value rather than a full ISO date string.

**Files likely involved:** `antra/core/isrc_enricher.py`, `antra/core/service.py`, `antra/core/engine.py`, `antra/utils/tagger.py`

---

### FEAT-11 · `[FIXED in v1.1.3]` — Prefer explicit (unedited) track versions

**Type:** Feature request
**Platform:** All
**Component:** `antra/core/models.py`, `antra/core/resolver.py`, `antra/core/spotify.py`, `antra/core/config.py`, `antra-wails/frontend/src/App.svelte`

**Description:**
When downloading a track, Antra may resolve to the radio edit or censored version rather than the original explicit/dirty release. This has been observed in practice (e.g. an Ice Cube track downloaded as a heavily censored radio edit). The user wants an option to always prefer the explicit version and reject radio edits.

**Root cause hypothesis:**
The source adapters match tracks by title + artist similarity score. A radio edit and the explicit version share the same title and artist, so the similarity score is identical. The adapter may return whichever version it finds first (often the radio edit, which is more common in streaming catalogs). There is no penalty for returning a censored version.

**Requested behavior:**
Add a setting: **Explicit tracks only** (toggle, default off).
When enabled:
- Add `is_explicit: bool` to `TrackMetadata` (set from Spotify's `explicit` field) and to `SearchResult`.
- In the resolver/adapter matching, if the source metadata indicates `explicit=False` (radio edit) and a result is found that is *not* explicit, apply a similarity score penalty (or skip it outright if better alternatives exist).
- When `is_explicit=True` on the target track, prefer explicit results; only accept a non-explicit result if no explicit version is found.

**Implementation notes:**
- Spotify metadata already exposes `explicit: bool` per track — needs to be stored on `TrackMetadata`.
- Source adapters (HiFi, Amazon, JioSaavn etc.) often include `explicit`/`clean` flags in their search response metadata — these can be surfaced in `SearchResult`.
- The resolver's `_candidate_key()` scoring function can add a bonus for explicit results when the target track is known to be explicit.
- UI: simple checkbox in settings "Prefer explicit versions".

**Files likely involved:** `antra/core/models.py`, `antra/core/spotify.py`, `antra/core/resolver.py`, `antra/core/config.py`, `antra-wails/frontend/src/App.svelte`

---

### BUG-11 · `[FIXED in v1.1.3]` — Multi-disc albums show flat track numbers for Amazon Music and Spotify public path

**Type:** Bug
**Platform:** All
**Component:** `antra/core/amazon_music_fetcher.py`, `antra/core/isrc_enricher.py`

**Description:**
Apple Music always returned correct disc numbers (catalog API provides `discNumber`). Amazon Music's JSON-LD schema.org format has no disc field at all. Spotify's public/anonymous fallback path never filled `disc_number`, so multi-disc albums from both sources produced flat `01, 02... n` numbering instead of `101, 102... 201, 202...`

**Fix:** Added `_assign_disc_numbers_from_html()` to `amazon_music_fetcher.py` (byte-position heuristic matching "Disc X"/"CD X" headers to `<music-horizontal-item` elements in SSR HTML). Extended `ISRCEnricher.enrich_tracks()` to stamp `disc_number` from the Spotify v1 `/tracks` batch response, covering the public Spotify path since all Spotify tracks have `spotify_id`.

**Files modified:** `antra/core/amazon_music_fetcher.py`, `antra/core/isrc_enricher.py`

---

### FEAT-14 · `[FIXED in v1.1.3]` — Source health check panel (Tidal / Amazon / Qobuz)

**Type:** Feature request
**Component:** `antra-wails/app_backend.go`, `antra-wails/frontend/src/App.svelte`, `antra-wails/frontend/wailsjs/go/main/App.js`, `antra-wails/frontend/wailsjs/go/main/App.d.ts`

**Description:**
Users wanted a way to see at a glance whether the community proxy endpoints are reachable without exposing their URLs.

**Implementation:** Three brand-logo chips (Tidal wave chevrons, Amazon smile arrow, Qobuz Q mark) always visible below the URL input. Clicking a chip fires parallel HTTP health probes in Go (`CheckSourceHealth`, 7s timeout, `sync.WaitGroup`) against all known endpoints. The popover shows only a live/total count + a dot grid (each dot = one endpoint, green = alive, red = down, hover = latency in ms). No endpoint URLs are ever shown in the UI.

**Files modified:** `antra-wails/app_backend.go`, `antra-wails/frontend/wailsjs/go/main/App.js`, `antra-wails/frontend/wailsjs/go/main/App.d.ts`, `antra-wails/frontend/src/App.svelte`

---

### FEAT-15 · `[FIXED in v1.1.3]` — Scroll-to-bottom arrow for tracklist and log panel

**Type:** Feature request
**Component:** `antra-wails/frontend/src/App.svelte`

**Description:**
Long playlists/albums required manual scrolling to see newly started tracks at the bottom of the tracklist, and the log panel had no quick way to jump to the latest entry.

**Implementation:** Tracklist wrapped in `.tracklist-wrapper`; `bind:this={tracklistEl}` + `on:scroll={updateTracklistScroll}` track distance from bottom. A circular "↓" button appears when >40px from bottom and scrolls instantly on click. Log panel header gets a matching "↓" button driven by `logAtBottom` state updated in `updateAutoScrollState()`.

**Files modified:** `antra-wails/frontend/src/App.svelte`

---

### FEAT-16 · `[FIXED in v1.1.3]` — Album/playlist title separator in tracklist for multi-URL downloads

**Type:** Feature request
**Component:** `antra-wails/frontend/src/App.svelte`

**Description:**
When multiple URLs were pasted, all tracks appeared in one continuous unseparated list with no indication of where one album ended and the next began.

**Implementation:** When `playlist_loaded` fires and `trackOrder` already has tracks, a `__SEP__${timestamp}` sentinel is inserted into `trackOrder` and `separatorMeta[key] = {title, artwork}` is saved. The `{#each trackOrder}` loop renders a `.tracklist-album-sep` divider row (small cover thumbnail + uppercase album title) for sentinel keys. `separatorMeta` resets on new download start.

**Files modified:** `antra-wails/frontend/src/App.svelte`

---

### FEAT-17 · `[FIXED in v1.1.3]` — Sponsor / Ko-fi prompt

**Type:** Feature request
**Component:** `antra-wails/frontend/src/App.svelte`

**Description:**
No in-app reminder for users to support the project financially.

**Implementation:** A small toast notification (top-right, below header) appears 1.2s after app open, auto-dismisses after ~9s with a fade-upward animation toward the Ko-fi icon. Message is honest and non-pressuring. The existing Ko-fi icon in the header now shows a compact popover tooltip on hover with the same message and a Ko-fi button. Both skipped/hidden during first-run setup.

**Files modified:** `antra-wails/frontend/src/App.svelte`

---

---

## Feature Requests & Bugs — v1.1.3 (new, this session)

---

### BUG-13 · `[FIXED in v1.1.3]` — Managed slskd bootstrap fails with Permission denied on macOS/Linux

**Type:** Bug
**Platform:** macOS, Linux
**Component:** `antra/utils/slskd_manager.py`

**Description:**
After Antra downloads and extracts the slskd binary for the first time, trying to run it fails immediately with:
```
Managed slskd bootstrap failed: [Errno 13] Permission denied: '.../.cache/antra/slskd/bin/current/slskd'
```

**Root cause:**
`zipfile.ZipFile.extractall()` does not restore Unix execute permissions from the zip metadata. The binary lands as `0o644` (non-executable). Windows is unaffected.

**Fix:**
Added `os.chmod(exe, 0o755)` after extraction in `_download_and_extract_latest()`, guarded by `platform.system() != "Windows"`.

**Files modified:** `antra/utils/slskd_manager.py`

---

### FEAT-18 · `[FIXED in v1.1.3]` — Library folder structure & filename format preferences

**Type:** Feature request
**Component:** `antra/utils/organizer.py`, `antra/core/config.py`, `antra-wails/app_backend.go`, `antra-wails/frontend/src/App.svelte`

**Description:**
Not all users want the default Navidrome/Jellyfin-optimised folder layout (`Artist / Album / NN - Title.flac`). Two separate customisation axes are needed, surfaced as a first-run setup step **and** accessible later from a dedicated "Preferences" tab/section (separate from the already-crowded Settings panel).

**Axis 1 — Folder structure:**
- `Standard` (default — current behaviour): `Artist / Album / files` for albums; `Playlists / Playlist Name / files + .m3u` for playlists. Optimal for Navidrome, Jellyfin, Plex.
- `Flat`: skip the artist wrapper — just `Album Name / files` or `Playlist Name / files`. Good for users who organise manually or don't use a media server.

**Axis 2 — Filename format:**
- `Default` (current): `NN - Title.flac` (track number prefix)
- `Title only`: `Title.flac` — no number prefix
- `Artist - Title`: `Artist - Title.flac` — useful for flat folders where artist context is otherwise lost
- `Title - Artist`: `Title - Artist.flac`

**First-run UX:**
A setup screen shown once on first launch (before the main UI) lets the user pick both options with plain-language descriptions and visual examples. A "Keep defaults" button skips without changing anything. Choices are persisted to config and can be changed later in the Preferences panel.

**Implementation notes:**
- Add `folder_structure: str = "standard"` and `filename_format: str = "default"` to `Config` and `load_config()`.
- `LibraryOrganizer._build_path()` and `_format_filename()` should branch on these values.
- Go struct: `FolderStructure string`, `FilenameFormat string`.
- TypeScript `Config` model: `folder_structure?: string`, `filename_format?: string`.
- Frontend: new "Preferences" section in settings (or separate tab), plus first-run setup screen guarded by a `first_run_complete` flag in config.

**Files likely involved:** `antra/utils/organizer.py`, `antra/core/config.py`, `antra/core/service.py`, `antra/json_cli.py`, `antra-wails/app_backend.go`, `antra-wails/frontend/wailsjs/go/models.ts`, `antra-wails/frontend/src/App.svelte`

**Follow-up — beets-style freeform path templates (user request):**
A user familiar with [beets](https://beets.readthedocs.io/en/stable/reference/config.html#path-format-configuration) requested full template-string control over path and filename construction, e.g.:
```
default: $albumartist/($original_year) $albumartist - $album/$disc-$track $title
comp:    Compilations/($original_year) $albumartist - $album/$disc-$track $title
```
This is the power-user layer on top of the preset options above. Suggested approach: implement the radio-button presets first (v1.1.3), then add an "Advanced: custom path template" text field in a later version that, when non-empty, overrides the preset. Available variables would mirror `TrackMetadata` fields: `$artist`, `$albumartist`, `$album`, `$title`, `$track`, `$disc`, `$year`, `$original_year`, `$isrc`, `$format`. A `comp` override (separate template for compilations/VA albums) would be a nice-to-have.

---

### BUG-12 · `[FIXED in v1.1.3]` — ISRCEnricher hits Spotify 429 rate limit, 0/12 ISRCs enriched

**Type:** Bug
**Platform:** All
**Component:** `antra/core/isrc_enricher.py`

**Description:**
When downloading a 12-track album, the ISRC enricher logs a 429 rate-limit response from the Spotify `/tracks` batch endpoint and bails out with 0/12 ISRCs enriched. This causes downstream genre and disc-number stamping (which rely on ISRCs) to fail for the entire batch.

**Observed log:**
```
[Service] Enriching ISRCs for 12/12 tracks via Spotify API
[ISRCEnricher] Enriching 12 tracks using 1 batches across 10 parallel workers...
[ISRCEnricher] Rate limited (429). Skipping batch (MusicBrainz fallback will catch this).
[ISRCEnricher] ISRC coverage achieved: 0/12
```

**Root cause hypothesis:**
The enricher uses 10 parallel workers sending concurrent requests to the anonymous Spotify `/tracks` endpoint. Even a single batch of 12 tracks with 10 workers likely fires multiple simultaneous requests, triggering the 429. The current error handling skips the batch entirely on the first 429 with no retry or backoff.

**Suggested fix:**
1. Reduce parallel workers for the ISRC enricher (1–2 is sufficient; these are batch requests not per-track).
2. On a 429 response, wait and retry with exponential backoff (1–3 attempts) before giving up.
3. Fall back to sequential processing (one batch at a time, no parallelism) if the first parallel attempt 429s.

**Files likely involved:** `antra/core/isrc_enricher.py`

---

### FEAT-19 · `[FIXED in v1.1.3]` — Artist search: show profile image + sort discography by release date

**Type:** Feature request / Bug
**Platform:** All
**Component:** `antra-wails/frontend/src/App.svelte`, `antra-wails/app_backend.go`, `antra/core/spotify.py`, `antra/core/apple_fetcher.py`

**Description:**
Two related improvements to the artist search flow:

1. **Artist search results** currently show name and a match percentage but no image. Each result should display the artist's profile/cover photo (already returned by Spotify and iTunes Search API alongside the name) as a small thumbnail to the left of the name.

2. **Discography modal** lists albums/singles/EPs but in no defined order. The list should be sorted descending by release year/date so the most recent release appears at the top. Within the same year, sort alphabetically by album name.

**Implementation notes:**
- Spotify's artist search response includes `images[]`; the first image is suitable as a thumbnail. iTunes Search API returns `artworkUrl100`. Both are already fetched — just not forwarded to the frontend.
- Add `image_url: Optional[str]` to the artist search result model/struct and populate it from both search paths.
- Go `ArtistResult` struct: add `ImageUrl string json:"image_url,omitempty"`.
- In `App.svelte` artist results list, show a 36×36px rounded-square thumbnail before the artist name (fallback: a muted person icon).
- Discography sort: in `App.svelte`, sort the `discography` array by `year` descending before rendering; stable-sort so same-year entries stay in server-defined order, then secondary sort by `name` ascending.

**Files likely involved:** `antra/core/spotify.py`, `antra/core/apple_fetcher.py`, `antra-wails/app_backend.go`, `antra-wails/frontend/wailsjs/go/main/App.js`, `antra-wails/frontend/wailsjs/go/main/App.d.ts`, `antra-wails/frontend/src/App.svelte`

---

### FEAT-20 · `[FIXED in v1.1.3]` — Health check chips: use real brand icons instead of hand-drawn SVGs

**Type:** Polish / UI
**Platform:** All
**Component:** `antra-wails/frontend/src/App.svelte`, `antra-wails/frontend/public/` (or embedded assets)

**Description:**
The three source health chips (Tidal, Amazon, Qobuz) currently use hand-drawn inline SVG approximations of brand logos. The user has provided actual brand icon files in the project root:
- `tidal_logo_icon_147227.webp` — Tidal
- `amazon-music.jpg` — Amazon Music
- `qobuz_icon.png` — Qobuz

Replace the SVG placeholders in the health chip buttons and in the health popover modal header with these real icons. Icons should be moved to `antra-wails/frontend/public/icons/` so Vite can serve them as static assets. The chips should look polished and premium — icon + source name, consistent sizing, brand-appropriate accent colours retained for border/glow effects.

**Files likely involved:** `antra-wails/frontend/src/App.svelte`, `antra-wails/frontend/public/icons/` (new asset copies)

---

### FEAT-21 · `[FIXED in v1.1.3]` — Ko-fi toast: update copy + keep visible while hovering

**Type:** Bug + Polish
**Platform:** All
**Component:** `antra-wails/frontend/src/App.svelte`

**Description:**
Two issues with the Ko-fi support prompt:

1. **Copy**: The current toast/hover text says "No AI used" — remove that claim. Replace with messaging focused on the real effort and ongoing maintenance: _"Real effort goes into maintaining Antra and keeping it free. If it saves you time, consider supporting continued development."_

2. **Toast hover persistence**: The toast auto-dismisses after ~9s via a CSS animation. If the user moves their mouse over the toast before it disappears, it should pause the dismiss timer and stay visible for as long as the cursor is hovering. Once the cursor leaves, the countdown resumes (or it dismisses immediately after a short grace period). Currently the toast vanishes mid-hover which is jarring and prevents clicking the Ko-fi link.

**Implementation notes:**
- For hover persistence: replace the pure CSS animation auto-dismiss with a JS `setTimeout` that starts on mount. Add `on:mouseenter` / `on:mouseleave` handlers on the toast element to `clearTimeout` / restart the timer respectively.
- Keep the ~9s default dismiss time for non-hover case.

**Files likely involved:** `antra-wails/frontend/src/App.svelte`

---

### BUG-14 · `[FIXED in v1.1.3]` — ISRCEnricher 429 loop adds 14s delay with 0 benefit

**Type:** Performance / regression
**Platform:** All
**Component:** `antra/core/service.py`, `antra/core/isrc_enricher.py`

**Description:**
Every download batch triggers `_enrich_isrcs()` in `service.py`, which calls `GET /v1/tracks` with an anonymous TOTP token. Spotify consistently rate-limits (429) this endpoint. The enricher retries 3 times (2s + 4s + 8s = 14s total delay) and ultimately achieves 0 ISRCs. This adds dead time before every download starts.

**Fix:** Removed `_enrich_isrcs(tracks)` call from `fetch_playlist_tracks()` in `service.py`. ISRCs are still populated for authenticated Spotify and Apple Music catalog paths.

**Files modified:** `antra/core/service.py`

---

### BUG-15 · `[FIXED in v1.1.3]` — Truncated HiFi download permanently blocks fallback to other sources

**Type:** Bug
**Platform:** All
**Component:** `antra/core/engine.py`

**Description:**
When HiFi returns a truncated FLAC file, the engine permanently excludes HiFi and re-resolves. For tracks where the featured artist is in the title (e.g. "YouUgly (with Westside Gunn)"), Amazon and DAB return `None` from search — the parenthetical kills their title matching. Result: HiFi excluded, Amazon/DAB find nothing, track fails immediately. The `[FAIL]` message shows the HiFi truncation error with no indication that Amazon/DAB were tried.

**Fix:** In `engine.py`, truncated downloads are now routed to `rate_limited_adapters` instead of `excluded_adapters`. Amazon/DAB are tried first; if both return nothing, the engine's existing "retry rate-limited as last resort" logic gives HiFi one more attempt. If HiFi truncates again, it is permanently excluded.

**Files modified:** `antra/core/engine.py`

---

### BUG-16 · `[FIXED in v1.1.3]` — Health check popover shows old SVG icons instead of real brand images

**Type:** Bug (UI regression)
**Platform:** All
**Component:** `antra-wails/frontend/src/App.svelte`

**Description:**
The source health chip buttons (below the URL bar) correctly display real brand images (Tidal, Amazon Music, Qobuz). But when a chip is clicked and the health popover opens, its header shows the old hand-drawn inline SVG approximations instead of the same brand images.

**Fix:** Replaced the three SVG blocks in the popover header with `<img>` tags pointing to `/icons/tidal.webp`, `/icons/amazon-music.jpg`, and `/icons/qobuz.png` — matching the chips exactly.

**Files modified:** `antra-wails/frontend/src/App.svelte`

---

### BUG-18 · `[FIXED in v1.1.3]` — Flat folder structure places files under Albums/Playlists subdirectories

**Type:** Bug
**Platform:** All
**Component:** `antra/utils/organizer.py`

**Description:**
With Folder Structure set to "Flat", albums still landed under `Music/Albums/Album Name/` and playlists under `Music/Playlists/Playlist Name/` instead of directly inside the music root.

**Fix:** `get_output_path()` now uses `self.root / album_dir` and `self.root / playlist_dir` when `folder_structure == "flat"`. `write_playlist_manifest` also uses `manifest_root = self.root` in flat mode so the `.m3u` lands at `root/Name.m3u`.

**Files modified:** `antra/utils/organizer.py`

---

### BUG-19 · `[FIXED in v1.1.3]` — Artist-Title and Title-Artist filename formats omit track number, causing wrong sort order

**Type:** Bug
**Platform:** All
**Component:** `antra/utils/organizer.py`

**Description:**
`artist_title` and `title_artist` filename formats produced bare `Artist - Title.flac` with no track-number prefix. File managers sorted by name alphabetically, not by track order. Only the `default` format had the `NN - ` prefix.

**Fix:** All three named formats now prepend the track number: `01 - Artist - Title.flac`, `01 - Title - Artist.flac`. Multi-disc prefix (`101 - ...`) applied consistently. Only `title_only` remains numberless. FEATURES.md table updated.

**Files modified:** `antra/utils/organizer.py`, `FEATURES.md`

---

### BUG-20 · `[FIXED in v1.1.3]` — Soulseek transfers complete in slskd but Antra shows "waiting" / endless re-downloads

**Type:** Bug
**Platform:** macOS, Windows
**Component:** `antra/sources/soulseek.py`

**Description:**
After slskd completed downloads, Antra kept showing "waiting for soulseek transfer" indefinitely. On Windows, the same files were endlessly re-downloaded by slskd.

**Root causes:**
1. `_candidate_download_paths` never included `{username}/` in the path (slskd layout is `{downloads_dir}/{username}/{remote_path}`), so file-existence checks failed even when `localPath` wasn't populated.
2. slskd auto-removes completed transfers from its active queue. `get_all_downloads(includeRemoved=False)` never returned them, leaving Antra waiting for a transfer that was already gone.
3. Both failures caused the 10-minute timeout → engine retried via Soulseek from a different peer → slskd re-downloaded → cycle repeated.

**Fix:** Username-prefixed path variants are now generated first. A one-shot `get_all_downloads(includeRemoved=True)` pass fires after 20s with no transfer match, catching auto-removed completed transfers. `username` is threaded through all path-resolution methods.

**Files modified:** `antra/sources/soulseek.py`

---

### BUG-21 · `[FIXED in v1.1.3]` — slskd process stays running after Antra quits on macOS/Linux

**Type:** Bug
**Platform:** macOS, Linux
**Component:** `antra-wails/app.go`

**Description:**
`app.go`'s `shutdown()` only killed `slskd.exe` via `taskkill` on Windows. On macOS and Linux, the slskd subprocess continued running after Antra closed.

**Fix:** On non-Windows, `shutdown()` now reads the PID from `~/.cache/antra/slskd/runtime/state.json` (written by `SlskdBootstrapManager`) and kills it. Falls back to `pkill -f slskd` if the state file is missing or the PID is stale.

**Files modified:** `antra-wails/app.go`

---

### BUG-22 · `[FIXED in v1.1.3]` — Soulseek-only mode reports "No source adapters available"

**Type:** Bug
**Platform:** All
**Component:** `antra-wails/frontend/src/App.svelte`, `antra/core/service.py`, `antra/json_cli.py`

**Description:**
When the user disables all source groups in Settings and leaves only Soulseek enabled, the download fails immediately:
```
[OK] Dab adapter enabled (free FLAC via Qobuz proxy)
[OK] NetEase adapter enabled (320kbps MP3, Chinese catalog)
No source adapters available. Check your configuration.
Tracks added : 0 / 0
```
Two problems visible in the log:
1. DAB and NetEase are reported as enabled even though the HiFi group was unchecked — the sources toggle is not filtering them out.
2. Soulseek itself is not being initialized at all, likely because slskd credentials/URL are not being passed through when it is the only adapter selected.

**Reproduction:**
Settings → Sources → uncheck Hi-Fi group → leave Soulseek checked → download any album.

**Root cause hypothesis:**
The `sources_enabled` list in the frontend may not map the Soulseek adapter correctly when it is the sole remaining source. Additionally the `toggleSourceGroup` logic may not be correctly intersecting the enabled list with the full adapter set, so non-Soulseek adapters still reach `service.py` as enabled. Separately, Soulseek bootstrap (`SlskdBootstrapManager.ensure_running()`) may be skipped when `SLSKD_BASE_URL` is absent and only Soulseek is selected.

**Files likely involved:** `antra-wails/frontend/src/App.svelte`, `antra/core/service.py`, `antra/json_cli.py`

---

### BUG-23 · `[FIXED in v1.1.3]` — Artist-Title filename format setting not respected; files download as default 01-Title

**Type:** Bug
**Platform:** All (confirmed)
**Component:** `antra/utils/organizer.py`, `antra/json_cli.py`, `antra-wails/app_backend.go`

**Description:**
Despite selecting "Artist - Title" in the Filename Format settings, downloaded files are still named `01 - Track Title.flac` (the default format). The setting appears to save correctly in the UI but is not reaching the `LibraryOrganizer`.

**Reproduction:**
Settings → Filename Format → select "Artist - Title" → Save → download any album → files appear as `01 - Title.flac`.

**Root cause hypothesis:**
The `filename_format` value may not be serialized into the config JSON sent to the Python backend via `json_cli.py`, or `load_config()` / `build_engine()` in `service.py` may not be passing it through to `LibraryOrganizer.__init__()`. The Go→Python config mapping in `json_cli.py` should set `FILENAME_FORMAT` from the received JSON, but this pipe may be broken.

**Files likely involved:** `antra/json_cli.py`, `antra/core/service.py`, `antra/utils/organizer.py`, `antra-wails/app_backend.go`

---

### BUG-17 · `[FIXED in v1.1.3]` — Public user-created Apple Music playlists blocked by overly aggressive pl.u- guard

**Type:** Bug (regression from BUG-02 fix)
**Platform:** All
**Component:** `antra/core/apple_fetcher.py`

**Description:**
BUG-02's fix added an early guard that raises `ValueError` for any playlist ID starting with `pl.u-`, treating all user-created playlists as private. However, `pl.u-` only means *user-created*, not *private*. Public user-created playlists (shareable links) are fully accessible via Apple's Catalog API using an anonymous developer token. Example: `https://music.apple.com/sg/playlist/uplifting-trance/pl.u-EdAVklWuaB6xjMN` is a public playlist that was blocked.

**Fix:** Removed the early `pl.u-` guard. The Catalog API is now attempted for all playlist types. The RSS fallback is explicitly skipped for `pl.u-` playlists (it only works for Apple-curated playlists). If the Catalog API returns no tracks for a `pl.u-` playlist, a specific error tells the user to check the playlist is set to public/shareable.

**Files modified:** `antra/core/apple_fetcher.py`

---

### BUG-26 · `[FIXED in v1.1.3]` — Filename format setting ignored; multi-disc numbering not universal

**Type:** Bug
**Platform:** All (confirmed Windows)
**Component:** `antra/utils/organizer.py`, `antra/json_cli.py`

**Description:**
Two related issues with filename formatting:

1. **Format not applied.** Selecting `title_artist` filename format (or any non-default format) plus `flat` folder structure in Settings and saving still produces files named `01 - Track Title.flac` — the default format. The setting reaches the backend (BUG-23 was fixed to pass it through), but something in `_format_filename()` in `organizer.py` is not branching correctly.

2. **Multi-disc numbering not universal.** The `101`, `102`, `201`, `202`... disc-prefixed numbering (Plex-compatible format where `D * 100 + track_number`) was implemented for the `default` filename format. It was also requested for Apple Music in a prior fix, but the user confirms it should apply **to all filename formats** — `default`, `artist_title`, `title_artist`, and `title_only`. Currently, other formats may still produce `01 - ...` even on multi-disc albums.

**Reproduction:**
- Settings → Filename Format → `Title - Artist` + Folder Structure → `Flat` → Save → download any Spotify album
- Files appear as `01 - Track Title.flac` (default format, flat structure ignored)
- On a 2-disc album: disc 2 track 1 still appears as `01 - ...` instead of `201 - ...`

**Expected behavior:**
- `title_artist` → `01 - Title - Artist.flac` (or `101 - Title - Artist.flac` on disc 1 of multi-disc)
- `artist_title` → `01 - Artist - Title.flac`
- Disc prefix (`101`, `201`...) applies across ALL format options when `total_discs > 1`

**Root cause hypothesis:**
`_format_filename()` in `organizer.py` may have a logic error in the branching — the `filename_format` value from config may not match the string check exactly, or the env var set in `json_cli.py` is being overwritten/not propagated correctly after BUG-24's fix.

**Files likely involved:** `antra/utils/organizer.py` (`_format_filename()`), `antra/json_cli.py`

---

### BUG-27 · `[FIXED in v1.1.3]` — Soulseek-only mode: slskd finds tracks but downloads stall / fail

**Type:** Bug
**Platform:** All (confirmed)
**Component:** `antra/sources/soulseek.py`, `antra/utils/slskd_manager.py`

**Description:**
When only Soulseek is selected as the download source, tracks start downloading (the engine shows `📥 [Downloading]`) but then stall — eventually failing after retries. The slskd service itself is able to find tracks and download them, but Antra either can't detect the completed transfer or the download never actually completes.

**Observed log:**
```
[OK] Managed slskd bootstrap is ready.
[Sources] Active after group filter: soulseek
[1/15] Metro Boomin, John Legend — On Time (with John Legend)
📥 [Downloading] [1/15] On Time (with John Legend) by Metro Boomin, John Legend (FLAC 24-bit/96kHz)
🔁 [Retry 2] [1/15] On Time (with John Legend) (FLAC 24-bit/96kHz)
🔁 [Retry 3] [1/15] On Time (with John Legend) (FLAC 24-bit/96kHz)
```

**Additional context:**
- Some tracks ([3/15]) fail immediately with "No matching source found" — these have complex collaboration credits in the title (`feat. X & with Y` combined) that likely break the Soulseek search query.
- Other tracks start but stall after being found — possibly the BUG-20 path-resolution issue still has an edge case, or Soulseek peers are slow/disconnecting mid-transfer.
- User wants to inspect slskd directly via its web UI. **The slskd web UI runs locally at `http://localhost:5030`** — this URL should be surfaced somewhere in the Antra UI (log panel or settings) so users can open it to inspect transfer state.

**Suggested fixes:**
1. Surface the slskd local URL (`http://localhost:5030` or whatever port is configured) in the log panel when Soulseek is active, so users can inspect transfer state in the browser.
2. Strip complex collaboration credits (`feat. X & with Y`, `(with X feat. Y)`) from the Soulseek search query the same way odesli.py does with `_COLLAB_RE`.
3. Investigate whether BUG-20's `includeRemoved=True` one-shot pass is still missing any edge cases for slow peers.

**Files likely involved:** `antra/sources/soulseek.py`, `antra/utils/slskd_manager.py`, `antra/json_cli.py` (for slskd URL log line), `antra-wails/frontend/src/App.svelte` (optional: slskd link in UI)

---

### BUG-28 · `[FIXED in v1.1.3]` — Full Albums mode skips tracks that appear on both a standard and deluxe/Shady edition of the same album

**Type:** Bug
**Platform:** All (confirmed)
**Component:** `antra/utils/organizer.py`
**Library mode:** Full Albums

**Description:**
When downloading an artist's discography (Eminem — `https://music.apple.com/us/artist/eminem/111051`), tracks that appear on a standard edition of an album were correctly downloaded. Later in the same 26-track batch, the same tracks appeared again (from a deluxe/"Shady Edition") and were incorrectly skipped as "already downloaded" even though the user expected them to be downloaded into a separate album folder.

**Observed log:**
```
[11/26] Eminem — Houdini
📥 [Downloading] [11/26] Houdini by Eminem (FLAC 24-bit)
[✓] Added to library: Eminem - Houdini
...
[24/26] Eminem — Houdini
[SKIP] Skipping (already downloaded): Houdini
[—] Already in library: Eminem - Houdini

[25/26] Tobey — SKIP
[26/26] Somebody Save Me — SKIP
```

**Root cause hypothesis:**
Two possible causes:

1. **Same album name → same target folder**: The standard edition and deluxe/Shady edition may have the same `album` metadata value (or normalize to the same folder name after sanitization). In Full Albums mode, `is_already_downloaded()` only checks the target path — if the path is `Eminem/The Death of Slim Shady/Houdini.flac` for both editions, the second one is correctly skipped by Full Albums logic. But the user expected a separate `The Death of Slim Shady (Shady Edition)/` folder.

2. **In-session dedup across albums in the same batch**: The identity key cache built in-session (not from disk) may be matching the track by ISRC or title+artist across two different album folders, even in Full Albums mode.

**Expected behavior:**
If two editions of an album have different album names (e.g., "The Death of Slim Shady" vs. "The Death of Slim Shady (Shady Edition)"), they should resolve to different target folders and each be downloaded independently — even if they share tracks. Full Albums mode should allow this.

**Suggested fix:**
1. Check whether the `album` field on the two Houdini track instances is actually different (i.e., do they come from differently-named albums). If yes, the folder name sanitization may be collapsing them — the parenthetical `(Shady Edition)` suffix may be stripped or ignored.
2. If they do have the same `album` value (Apple Music returns the same album name for both editions), then this is a metadata issue: the fetcher needs to distinguish standard vs. deluxe editions by checking the album's `contentRating` or edition suffix.
3. As a simpler fix: when `full_albums=True`, ensure the in-session identity cache is NOT populated during the current download (only disk-scan dedup, not cross-album within the same batch), so tracks from a new album in the batch are never flagged against tracks downloaded earlier in the same session.

**Files likely involved:** `antra/utils/organizer.py` (`is_already_downloaded()`, `_track_identity_keys()`), `antra/core/apple_fetcher.py` (album metadata for deluxe/standard editions)

---
