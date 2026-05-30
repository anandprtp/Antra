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

Antra resolves links from Spotify, Apple Music, Amazon Music, Tidal, Qobuz, and Deezer. In lossless mode it queries all active lossless-capable sources in parallel and picks the result with the highest bit depth and sample rate — not just the first match found. Lossy formats (AAC, MP3) use dedicated lossy sources first; lossless adapters are only tried as a last resort.

```
Source chain (per track):

  Antra mirror servers  →  Tidal · Qobuz · Amazon Music · Deezer · Apple Music
  Built-in fallbacks    →  lossy and source-specific fallback adapters
  Soulseek P2P          →  anything the community has, including rare and out-of-print releases
```

Free tier works out of the box with rate-limited downloads. Support on Ko-fi to receive a 30-day supporter key with unlimited downloads and 2-3 concurrent downloads.

---

## Download Source Selector

Force all downloads through a single service instead of letting the resolver decide. Six options: **Auto** (default), **Tidal**, **Qobuz**, **Apple Music**, **Amazon Music**, **Deezer**. When a specific source is selected, only adapters in that service family are used and no fallback to other services occurs.

Accessible from the pill-style selector on the main screen — no need to open Settings.

---

## ISRC-Based Exact Matching

Most tools match by title and artist and often grab the wrong version: a remaster, a radio edit, a regional pressing. Antra uses **ISRC codes** (the unique identifier of every recording) to guarantee you get the exact track from the exact release you requested.

When ISRCs are available, Antra uses them to match against source APIs directly. When they are not, it falls back to a scored similarity search with title-artist weighting.

---

## Explicit Version Preference

If the track you requested is the explicit (unedited) version, Antra will prefer it. Radio edits and censored versions are penalised in the match scoring and skipped when a clean result is the only option, keeping the rest of the queue searching until an explicit source is found.

Configurable in Settings: **Prefer explicit versions** (on by default).

---

## Strict Matching Mode

Opt-in safety mode for niche music. When enabled, Antra requires stronger confidence for non-ISRC matches and applies tighter post-download duration validation, preferring a clean failure over a risky wrong-audio save. Default is off so current behaviour is unchanged unless you opt in.

---

## Hi-Res Awareness

Tidal and Qobuz expose per-track bit depth and sample rate in their search results. When a track has a hi-res master available (e.g. 24-bit/96kHz from Tidal, up to 24-bit/192kHz from Qobuz), Antra keeps searching all lossless-capable sources and selects the highest-resolution result — ranked by bit depth first, then sample rate. CD quality (16-bit/44.1kHz) is only used if no hi-res source can be located.

Qobuz URLs also support a **strict 24-bit mode**: if no Qobuz account can produce a genuine 24-bit stream, the request fails cleanly instead of silently saving 16-bit audio under a 24-bit request.

---

## Auto-Tagging

Every downloaded file is tagged automatically. No manual editing, no missing artwork, no "Track 01".

| Tag | Source |
|---|---|
| Title, Artist, Album, Track # | Spotify / Apple Music / Amazon metadata |
| Album artwork | Full-resolution cover from the streaming catalog |
| Release date | Full ISO date where available; year as fallback |
| Genre | Deezer album-level genre via ISRC, with MusicBrainz fallback |
| Composer | Sourced from Qobuz, Tidal, or Deezer metadata |
| Disc number | Correct disc tagging for multi-disc albums |
| Lyrics | LRCLIB synced lyrics, Genius / Musixmatch fallback |
| ISRC | Embedded for future matching |

Tags are written in the correct format for every container: ID3v2 for MP3, Vorbis comments for FLAC, MP4 atoms for M4A — fully readable by Windows Media Player, VLC, foobar2000, and all major media servers.

---

## Smart Library Organisation

Output is structured the way every media server expects:

```
~/Music/
  Artist Name/
    Album Name (Year)/
      101 - Track Title.flac
      102 - Track Title.flac
      cover.jpg
```

Downloads land directly inside the library root — no intermediate `Albums/` or `Playlists/` subfolders. All tracks use disc-prefixed numbering (`101`, `102`, ..., `201`, `202`, ...) so Plex, Navidrome, and Jellyfin always know which disc a track belongs to.

### Template-Based Filenames and Folders

Set your own naming scheme using tokens. Three template fields in Folder Settings:

| Template | Default | Example output |
|---|---|---|
| Single track filename | `{artist} - {title}` | `The Beatles - Come Together.flac` |
| Album track filename | `{track} - {title}` | `07 - Come Together.flac` |
| Folder structure | `{album_artist}/{year} - {album}` | `The Beatles/1969 - Abbey Road/` |

Available tokens: `{title}` `{artist}` `{album_artist}` `{album}` `{year}` `{track}` `{disc}` `{genre}` `{composer}` `{isrc}` `{codec}` `{bitrate}` `{quality}`

Each template field shows a live preview as you type. Click any token chip to insert it at the cursor position.

### Multi-Disc Handling

| Mode | Example |
|---|---|
| **Disc prefix** (default) | `2-05 - Track.flac` |
| **Offset 101/201** | `205 - Track.flac` |
| **Track only** | `05 - Track.flac` |

---

## Smart Deduplication

Antra builds an identity index of your library using ISRCs, track IDs, and normalised title+artist keys. Before downloading, it checks if a track already exists, even if it was saved under a different artist folder name or album edition.

### Library Mode Options

| Mode | Behaviour |
|---|---|
| **Smart Dedup** (default) | Skip a track if the same ISRC exists anywhere in your library. Saves storage. |
| **Full Albums** | Skip only if the file already exists in the same destination folder. Lets you own the same track across multiple album contexts. |

---

## Local Library Import

Already have a music collection? **Import Files** and **Import Folder** buttons below the URL bar move local audio into your library using the same structure, tagging, and deduplication as downloads.

- **Reads existing tags first**: title, artist, album, ISRC, track/disc numbers, release date, and genres are pulled from FLAC/MP3/MP4 metadata. When tags are missing, Antra infers fields from the file path (e.g. `Artist - Title.flac`, or `Artist/Album/01 - Track.flac`).
- **Deduplicated against your library**: imports run through the same ISRC + canonical-title index as downloads, so re-importing a folder that's already partly in your library skips the duplicates.
- **Quality-aware replacement**: if you import a higher-quality version of a track you already own (FLAC vs MP3, 24-bit vs 16-bit, higher bitrate), Antra replaces the older file in place.
- **Sidecar lyrics**: matching `.lrc` and `.txt` files next to each audio file are copied alongside.
- **Supported formats**: FLAC, ALAC, MP3, M4A/MP4, AAC, WAV, AIFF, OGG, Opus.

---

## Artist Discography Download
## Themes

11 built-in themes accessible from the full-screen theme picker (🎨 button in the header).

**Antra Originals:** Antra (default deep teal), Ember (warm amber), Ocean (deep navy), Graphite (monochrome), Sunset (magenta and gold), Linen (light mode)

**Service-Inspired:** Spotify, TIDAL, Qobuz, Deezer, Apple Music, Amazon Music

Each card shows three colour swatches, a name, and a description. Selecting a theme applies it instantly and saves it to config so it restores on next launch.

---

## Album Availability Studio

Paste a Spotify or Deezer album link to see country-by-country availability on a live world map. Shows full-access markets, partial access, region locks, label, UPC, and confirmed track counts. Useful for checking if a release is available in a specific territory before downloading.

Accessible from the 🌍 button in the header.

---

## Artist Discography Download

Search for any artist by Spotify or Apple Music URL. Antra fetches their full discography and presents it grouped by release type.

- Browse **Albums**, **Singles**, **EPs and Compilations** separately
- Bulk-select or deselect entire groups with one click
- Queue individual albums or the full catalogue in one batch

---

## Parallel Download Engine

Antra downloads 2 tracks concurrently by default. Playlists and albums that would take minutes sequentially complete in a fraction of the time.

```
Sequential:   track 1 → track 2 → track 3 → ...
Parallel:     track 1 ↘
              track 2 → done
              track 3 ↗
```

---

## Rich Tracklist UI

When a URL is pasted, the full tracklist appears immediately before any download starts. For playlists with 1000+ tracks, rows appear progressively as pages load — you are not waiting for the full fetch to complete.

Each row shows the track title, artist, duration, and a real-time progress bar as the file downloads. The playlist header displays the cover art, type (ALBUM / PLAYLIST / SINGLE), artist, track count, total duration, and release date in the same layout as a streaming app.

When multiple URLs are queued in one session, a divider with the album cover and title separates each batch so you always know which tracks belong where.

A dedicated log panel (accessible via the 📋 button) shows verbose download output without disrupting the tracklist view.

---

## Source Health Check

Chips below the URL bar show the live status of each source from a public status endpoint. Green means online and active; darker red means currently unavailable. Antra checks the status on startup and continues working normally if the endpoint is unreachable.

---

## Library History

Every completed download session is saved to history with its cover art thumbnail, album/playlist title, URL, track count, and timestamp, so you can quickly identify what you have downloaded without opening the folder.

---

## Built-in Audio Analyzer

Not sure if a file is genuinely lossless or an MP3 in a FLAC wrapper? Drop any audio file into the Analyzer.

**Spectrogram:** Full frequency spectrum view. A real FLAC recorded from lossless masters looks unmistakably different from a transcoded lossy file.

**Stats panel:**

| Metric | What it tells you |
|---|---|
| Peak dBFS | Loudest sample in the file |
| RMS dBFS | Average loudness |
| True Peak dBTP | Inter-sample peak (important for streaming delivery) |
| Integrated Loudness | LUFS over the full file |
| Loudness Range | LRA — dynamic range |
| Frequency Cutoff | Highest frequency with meaningful content |

**Quality badges** are assigned automatically: FLAC/ALAC files with a cutoff below 14 kHz are flagged as "Fake Lossless"; below 17 kHz as "Likely Transcode"; below 19.5 kHz as "Suspect Quality".

Supports batch analysis with gallery view, per-item removal, and a Clear All button.

---

## Soulseek / P2P Integration

For tracks not available through any streaming-adjacent source (rare albums, limited pressings, out-of-print releases), Antra integrates with the Soulseek P2P network.

**Zero setup.** Antra downloads, configures, and manages the backend automatically. Just provide your Soulseek credentials once on first run.

> The Soulseek network runs on sharing. If you use it, please share back and leave the client running when you can.

---

## Spotify Podcast Downloads

Antra can download any Spotify podcast episode or entire show directly using your own Spotify account cookie — no external server, no third-party proxy.

### Account requirement

A **free Spotify account is sufficient**. Podcast audio is not gated behind Spotify Premium. The 320 kbps OGG Vorbis format is available to all logged-in users.

> The only exception is **subscriber-only episodes** — episodes paywalled by the podcast creator (separate from Spotify Premium). Those will fail with "no audio files available."

### Supported URLs

| URL type | Example |
|---|---|
| Single episode | `https://open.spotify.com/episode/4rOoJ6Egrf8K2IrywzwOMk` |
| Full show (all episodes) | `https://open.spotify.com/show/0ofXAdFIQQRsCYj9754UFx` |

### Setup: getting your sp_dc cookie

1. Open **[open.spotify.com](https://open.spotify.com)** in any browser while logged in
2. Open **DevTools** (F12 on Chrome/Edge, Cmd+Option+I on macOS)
3. Go to **Application** tab → **Cookies** → `https://open.spotify.com`
4. Find the cookie named **`sp_dc`** and copy its value (starts with `AQ...`)
5. In Antra, open **Settings → Spotify Podcasts** and paste it into the **sp_dc cookie** field

The cookie is valid for approximately one year.

### Output and tagging

Episodes are saved inside your configured Music folder:

```
~/Music/
  Podcasts/
    Show Name/
      2024-03-15 - Episode Title.ogg
      2024-03-22 - Another Episode.ogg
```

### Rate limiting

To protect your Spotify account from being flagged, Antra applies automatic rate limiting: a 3-7 second random delay between each episode and a 50 episodes/hour hard cap.

---

## Audio Format Options

| Mode | Output |
|---|---|
| **FLAC 24-bit** | Highest available lossless source, prioritising hi-res where available. |
| **FLAC 16-bit** | CD-quality lossless, with 16-bit sources preferred over 24-bit where possible. |
| **ALAC** | Apple-compatible lossless output. |
| **AAC** | Native AAC sources first, lossless only as fallback when needed. |
| **MP3** | Native MP3/lossy sources first, lossless only as fallback when needed. |
| **Auto** | Best available source with lossless preferred. |

---

## Platform Support

All builds ship as **single self-contained binaries** with no Python, no runtime, and no dependencies to install.

| Platform | Minimum | File |
|---|---|---|
| Windows | 10+ | `Antra.exe` |
| macOS | 12+ (Apple Silicon) | `Antra-macOS.dmg` |
| macOS | 12+ (Intel) | `Antra-macOS-Intel.dmg` |
| Linux | Any | `Antra-Linux.AppImage` |

---

## Tech Stack

```
Desktop shell   →  Go 1.23 · Wails v2
Frontend UI     →  Svelte · TypeScript · Vite
Download engine →  Python 3.11
IPC             →  newline-delimited JSON over stdout
Packaging       →  PyInstaller · wails build · AppImage · create-dmg
CI/CD           →  GitHub Actions, 4-platform matrix build on tag push
```

---

<p align="center">
  <br/>
  <strong>Antra is free to use, maintained by one person in their spare time.</strong><br/>
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
  <a href="https://t.me/antraaverse">
    <img src="https://img.shields.io/badge/Community-Telegram-26A5E4?style=for-the-badge&logo=telegram&logoColor=white"/>
  </a>
  <br/><br/>
  <sub><a href="README.md">← Back to README</a></sub>
</p>
