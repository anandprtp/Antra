<div align="center">

<pre>
    ___            __
   /   |  ____  / /__________ _
  / /| | / __ \/ __/ ___/ __ `/
 / ___ |/ / / / /_/ /  / /_/ /
/_/  |_/_/ /_/\__/_/   \__,_/
</pre>

**Your music. Offline. Lossless.**

Paste a supported streaming link — Antra builds your music library automatically with the highest quality audio available.

![Windows](https://img.shields.io/badge/Windows-10%2B-0078D6?style=for-the-badge&logo=windows&logoColor=white)
![Version](https://img.shields.io/badge/Version-1.0.0-00ffcc?style=for-the-badge)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)
![Free](https://img.shields.io/badge/Free-Forever-ff69b4?style=for-the-badge)

### [⬇ Download Latest Release](https://github.com/anandprtp/antra/releases)

![Antra App Screenshot](assets/screenshots/app-preview.png)

</div>

---

## Features

| Feature | Description |
|---|---|
| 🎵 **Multi-Source** | Sources audio from various supported streaming platforms |
| 🎧 **Lossless First** | Prioritizes highest quality lossless formats before falling back to high-quality lossy |
| 🏷️ **Auto-Tagged** | Every track gets proper metadata — title, artist, album, artwork, lyrics |
| 📁 **Auto-Organized** | Files are sorted into Artist → Album folder structure automatically |
| 🖥️ **Navidrome Ready** | Output is fully compatible with Navidrome, Jellyfin, and Plex |
| 🔬 **Quality Analyzer** | Spectrogram analyzer to verify the actual quality of any audio file |
| 🔍 **Smart Matching** | ISRC-based matching ensures you always get the exact right track |
| 🌐 **No Account Needed** | Works out of the box — no premium streaming service login required |
| ⚡ **P2P Alternative** | Optional peer-to-peer integration for rare, hi-res, and out-of-print releases |

---

## Audio Quality Priority

Antra works through a waterfall of supported platforms, always trying the highest quality lossless formats first, before falling back to lossy formats. 

Audio is sourced from licensed streaming services. No piracy infrastructure is involved.

---

## Soulseek/slskd Integration

Antra features deeply integrated, auto-managed Soulseek P2P routing powered by `slskd`, allowing it to source rare, high-resolution, and out-of-print FLAC releases completely effortlessly.

- **Zero-Setup Daemon**: Antra automatically downloads, configures, and manages a native `slskd` instance in the background. No external setup required.
- **API-Driven**: Communicates directly with the `slskd` API for seamless background searches and queuing—no separate UI needed.
- **Painless Onboarding**: Simply provide your network credentials on the first run, and Antra permanently integrates P2P as a lossless fallback source.

*(Note: You need a free network account to use this feature.)*

---

## Requirements

- **Windows 10 or later** (64-bit)
- **No installation required** — Antra ships as a single self-contained `.exe`
- Optional: [ffmpeg](https://ffmpeg.org/download.html) in PATH for format conversion

---

## Installation

1. Download `Antra.exe` from [Releases](https://github.com/anandprtp/antra/releases)
2. Run it — no installation, no Python, no dependencies
3. On first launch, select your Music Library folder
4. Paste a playlist URL and click **Add to Library**

---

## Usage

### Desktop App

Paste any supported URL into the input bar and press **Add to Library**.

Supported URL formats:
```
https://music-platform.example.com/playlist/...
https://music-platform.example.com/album/...
```

### CLI

```bash
# Download a playlist
python -m antra https://music-platform.example.com/playlist/...

# Download an album
python -m antra https://music-platform.example.com/album/...

# Preview tracks without downloading
python -m antra <url> --preview

# Force lossless output only
python -m antra <url> --format lossless
```

---

## Configuration

Copy `.env.example` to `.env` and fill in what you need:

```env
# Soulseek/slskd Integration (optional — auto-configured on first run)
SOULSEEK_USERNAME=
SOULSEEK_PASSWORD=

# Output directory (default: ~/Music)
ANTRA_OUTPUT_DIR=
```

---

## Building from Source

Requirements: Python 3.11+, Go 1.23+, Node.js 18+, PyInstaller, Wails v2

```bash
git clone https://github.com/anandprtp/antra
cd antra
pip install -r requirements-desktop.txt
python build_desktop.py
```

Output: `antra-wails/build/bin/Antra.exe`

---

## FAQ

<details>
<summary>Is this free?</summary>

Yes. Antra is completely free and open source, forever. No subscriptions, no trial periods, no feature gates.
</details>

<details>
<summary>Do I need a premium streaming account?</summary>

No. Antra works without any premium account.
</details>

<details>
<summary>Where does the audio come from?</summary>

Audio is sourced from licensed streaming services via their APIs. Antra finds the best quality version available from multiple sources and downloads it.
</details>

<details>
<summary>Why does Windows Defender flag the .exe?</summary>

This is a false positive. The executable is compressed with UPX and bundled with PyInstaller, which antivirus tools sometimes flag. If you're concerned, build from source — all code is auditable here.
</details>

<details>
<summary>What audio formats does Antra output?</summary>

By default, Antra keeps the source format (FLAC, ALAC, M4A, or MP3). You can force lossless-only, M4A, or MP3 in Settings or via `--format`.
</details>

<details>
<summary>Does Antra work with Navidrome or Jellyfin?</summary>

Yes. The folder structure (`Artist/Album/Track.flac`) and embedded metadata are fully compatible with Navidrome, Jellyfin, and Plex.
</details>

---

## Support Development

If Antra saves you time, consider supporting ongoing development:

<a href="#">
  <img src="https://img.shields.io/badge/Buy%20Me%20a%20Coffee-FFDD00?style=for-the-badge&logo=buy-me-a-coffee&logoColor=black" alt="Buy Me A Coffee" />
</a>

*(link coming soon)*

---

## Disclaimer

Antra is an independent open-source tool and is **not affiliated with, endorsed by, or connected to** any streaming service.

This project is intended for **personal, private use only**. You are responsible for ensuring your use complies with the Terms of Service of the respective platforms and applicable laws in your jurisdiction.

The software is provided "as is" without warranty of any kind. The author assumes no liability for any legal issues or consequences arising from its use.

---

<div align="center">
  <sub>Built with ❤️ · <a href="https://github.com/anandprtp/antra/issues">Report an Issue</a></sub>
</div>
