<p align="center">
  <img src="assets/features-header.svg" width="100%" alt="Antra Features"/>
</p>

<p align="center">
  <a href="README.md">← Back to README</a> &nbsp;·&nbsp;
  <a href="https://github.com/anandprtp/Antra/releases">Download</a> &nbsp;·&nbsp;
  <a href="https://ko-fi.com/antraverse">Support Development</a>
</p>

<br/>

---

## Multi-Source Audio Engine

Antra doesn't rely on a single source. It works through a waterfall of community-run servers, always trying the highest-quality lossless format first — and falls back gracefully when a source is unavailable or rate-limited.

```
Priority chain (per track):

  Community-run APIs  →  Tidal · Qobuz · Amazon Music  (FLAC, up to 24-bit/192kHz)
  Soulseek P2P        →  anything the community has — rare pressings, out-of-print releases
```

Load is distributed evenly across same-tier sources. When a server is temporarily rate-limited, Antra moves it to the back of the queue and continues from the others — no stalls, no dead time.

---

## ISRC-Based Exact Matching

Most tools match by title + artist and often grab the wrong version — a remaster, a radio edit, a regional pressing. Antra uses **ISRC codes** (the unique identifier of every recording) to guarantee you get the exact track from the exact release you requested.

When ISRCs are available, Antra uses them to match against source APIs directly. When they're not, it falls back to a scored similarity search with title-artist weighting.

---

## Explicit Version Preference

If the track you requested is the explicit (unedited) version, Antra will prefer it. Radio edits and censored versions are penalised in the match scoring and skipped when a clean result is the only option — keeping the rest of the queue searching until an explicit source is found, or marking the track as a clean-only result.

Configurable in Settings: **Prefer explicit versions** (on by default).

---

## Hi-Res Awareness

Apple Music's catalog API includes per-track quality hints (`hi-res-lossless`, `lossless`, `atmos`). When a track is known to have a 24-bit master, Antra keeps searching even if a 16-bit lossless result is found — only settling for CD quality if no hi-res source can be located.

---

## Auto-Tagging

Every downloaded file is tagged automatically — no manual editing, no missing artwork, no "Track 01".

| Tag | Source |
|---|---|
| Title, Artist, Album, Track # | Spotify / Apple Music / Amazon metadata |
| Album artwork | Full-resolution cover from the streaming catalog |
| Release date | Full ISO date where available; year as fallback |
| Genre | MusicBrainz lookup via ISRC |
| Lyrics | Genius + Musixmatch fallback |
| ISRC | Embedded for future matching |

Tags are written in the correct format for every container: ID3v2 for MP3, Vorbis comments for FLAC, MP4 atoms for M4A — fully readable by Windows Media Player, VLC, foobar2000, and all major media servers.

---

## Smart Library Organisation

Output is structured the way every media server expects:

```
~/Music/
└── Artist Name/
    └── Album Name (Year)/
        ├── 01 - Track Title.flac
        ├── 02 - Track Title.flac
        └── cover.jpg
```

Multi-disc albums use prefixed numbering (`101`, `201`, ...) so Plex, Navidrome, and Jellyfin can distinguish discs without manual intervention.

### Folder Structure Options

| Mode | Layout |
|---|---|
| **Standard** (default) | `Artist / Album / files` — optimal for Navidrome, Jellyfin, Plex |
| **Flat** | `Album / files` — no artist wrapper, good for manual organisation |

### Filename Format Options

| Mode | Example |
|---|---|
| **Default** | `01 - Track Title.flac` |
| **Title only** | `Track Title.flac` |
| **Artist - Title** | `Artist - Track Title.flac` |
| **Title - Artist** | `Track Title - Artist.flac` |

Both options are set during first-run setup and adjustable later in Settings.

---

## Smart Deduplication

Antra builds an identity index of your library using ISRCs, track IDs, and normalised title+artist keys. Before downloading, it checks if a track already exists — even if it was saved under a different artist folder name or album edition.

### Library Mode Options

| Mode | Behaviour |
|---|---|
| **Smart Dedup** (default) | Skip a track if the same ISRC exists anywhere in your library — saves storage |
| **Full Albums** | Skip only if the file already exists in the same destination folder — lets you own the same track in multiple album contexts |

---

## Artist Discography Download

Search for any artist by Spotify or Apple Music URL. Antra fetches their full discography and presents it grouped by release type.

- Browse **Albums**, **Singles**, **EPs & Compilations** separately
- Bulk-select or deselect entire groups with one click
- Queue individual albums or the full catalogue in one batch

---

## Parallel Download Engine

Antra downloads multiple tracks simultaneously. Playlists and albums that would take minutes sequentially complete in a fraction of the time.

```
Sequential:   track 1 → track 2 → track 3 → ...
Parallel:     track 1 ↘
              track 2 → done
              track 3 ↗
```

---

## Rich Tracklist UI

When a URL is pasted, the full tracklist appears immediately — before any download starts.

Each row shows the track title, artist, duration, and a real-time progress bar as the file downloads. The playlist header displays the cover art, type (ALBUM / PLAYLIST / SINGLE), artist, track count, total duration, and release date — the same layout as a streaming app.

When multiple URLs are queued in one session, a divider with the album cover and title separates each batch so you always know which tracks belong where.

A dedicated log panel (accessible via the 📋 button) shows verbose download output without disrupting the tracklist view.

---

## Source Health Check

Three chips below the URL bar let you check whether the community-run Tidal, Qobuz, and Amazon servers are reachable before you start a download. Clicking a chip runs parallel health probes against all known endpoints and shows a live/total count with per-endpoint status dots. No server URLs are ever displayed.

---

## Library History

Every completed download session is saved to history with its cover art thumbnail, album/playlist title, URL, track count, and timestamp — so you can quickly identify what you've downloaded without opening the folder.

---

## Built-in Spectrogram Analyzer

Not sure if a file is genuinely lossless or an MP3 in a FLAC wrapper? Drop any audio file into the Analyzer to see its full frequency spectrum. A real FLAC recorded from lossless masters looks unmistakably different from a transcoded lossy file.

Supports batch analysis with gallery view, side-by-side comparison, and PNG export.

---

## No Account Required

Antra works out of the box with no logins, no API keys, and no subscription:

- Spotify metadata — anonymous, no credentials
- Apple Music metadata — anonymous catalog access
- Amazon Music metadata — anonymous page parsing

Optionally wire in a Spotify account or Apple Developer token for private playlists and deeper metadata.

---

## Soulseek / P2P Integration

For tracks that aren't available through any streaming-adjacent source — rare albums, limited pressings, out-of-print releases — Antra integrates with the Soulseek P2P network.

**Zero setup:** Antra downloads, configures, and manages the backend automatically. Just provide your Soulseek credentials once on first run.

> The Soulseek network runs on sharing. If you use it, please share back — leave the client running when you can.

---

## Audio Format Options

| Mode | Output |
|---|---|
| **Auto** (default) | Best available — lossless preferred, MP3 fallback if no lossless source exists |
| **Lossless** | FLAC only — track is marked failed rather than falling back to lossy |
| **MP3** | Uses dedicated MP3 sources directly — no FLAC download + transcode |

---

## Platform Support

All builds ship as **single self-contained binaries** — no Python, no runtime, no dependencies to install.

| Platform | Minimum | File |
|---|---|---|
| Windows | 10+ | `Antra.exe` |
| macOS | 12+ (Apple Silicon) | `Antra-macOS.dmg` |
| macOS | 12+ (Intel) | `Antra-macOS-Intel.dmg` |
| Linux | Ubuntu 24.04+ | `Antra-Linux.AppImage` |

---

## Tech Stack

```
Desktop shell   →  Go 1.23 · Wails v2
Frontend UI     →  Svelte · TypeScript · Vite
Download engine →  Python 3.11
IPC             →  newline-delimited JSON over stdout
Packaging       →  PyInstaller · wails build · AppImage · create-dmg
CI/CD           →  GitHub Actions — 4-platform matrix build on tag push
```

---

<p align="center">
  <br/>
  <strong>Antra is free and open source, maintained by one person in their spare time.</strong><br/>
  <em>If it saves you money on streaming, consider keeping it alive.</em>
  <br/><br/>
  <a href="https://ko-fi.com/antraverse">
    <img src="https://img.shields.io/badge/Support_on_Ko--fi-FF5E5B?style=for-the-badge&logo=ko-fi&logoColor=white"/>
  </a>
  &nbsp;
  <a href="https://github.com/anandprtp/Antra">
    <img src="https://img.shields.io/badge/⭐_Star_on_GitHub-FFD700?style=for-the-badge&logo=github&logoColor=black"/>
  </a>
  &nbsp;
  <a href="https://www.reddit.com/r/antraverse/">
    <img src="https://img.shields.io/badge/Community-Reddit-FF4500?style=for-the-badge&logo=reddit&logoColor=white"/>
  </a>
  <br/><br/>
  <sub><a href="README.md">← Back to README</a></sub>
</p>
