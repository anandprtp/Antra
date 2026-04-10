<div align="center">

# 🎧 Antra

**Get Spotify and Apple Music tracks in true Lossless (FLAC) from multiple hi-res streaming sources — no premium account required.**

![Windows](https://img.shields.io/badge/Windows-10%2B-0078D6?style=for-the-badge&logo=windows&logoColor=white)
![macOS](https://img.shields.io/badge/macOS-12%2B-000000?style=for-the-badge&logo=apple&logoColor=white)
![Linux](https://img.shields.io/badge/Linux-Ubuntu%2024.04-FCC624?style=for-the-badge&logo=linux&logoColor=black)
![Version](https://img.shields.io/badge/Version-1.0.0-00ffcc?style=for-the-badge)
![License](https://img.shields.io/badge/License-MIT-green?style=for-the-badge)
![Free](https://img.shields.io/badge/Free-Forever-ff69b4?style=for-the-badge)

[![Reddit](https://img.shields.io/badge/Community-Reddit-FF4500?style=for-the-badge&logo=reddit&logoColor=white)](https://www.reddit.com/r/antraverse/)
[![Ko-fi](https://img.shields.io/badge/Support-Ko--fi-FF5E5B?style=for-the-badge&logo=ko-fi&logoColor=white)](https://ko-fi.com/antraverse)

### [⬇ Download Latest Release](https://github.com/anandprtp/antra/releases)

<img src="assets/screenshots/Screenshot%202026-04-04%20034601.png" width="800" />
<img src="assets/screenshots/Screenshot%202026-04-04%20034621.png" width="800" />
<img src="assets/screenshots/Screenshot%202026-04-04%20034709.png" width="800" />

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
| 🔬 **Quality Analyzer** | Built-in spectrogram analyzer to verify the actual quality of any audio file |
| 🔍 **Smart Matching** | ISRC-based matching ensures you always get the exact right track |
| 🌐 **No Account Needed** | Works out of the box — no premium streaming service login required |
| ⚡ **P2P Alternative** | Optional peer-to-peer integration for rare, hi-res, and out-of-print releases |
| 🎤 **Artist Search** | Search for any artist and download their full discography or individual albums |
| 🚀 **Fast Downloads** | Parallel download engine for significantly faster batch and playlist downloads |

---

## Audio Quality Priority

Antra works through a waterfall of supported platforms, always trying the highest quality lossless formats first, before falling back to lossy formats.

---

## Soulseek/slskd Integration

Antra features optional Soulseek integration powered by [`slskd`](https://github.com/slskd/slskd), a third-party client for the Soulseek network.

Antra does not bundle `slskd` in its desktop release. If Soulseek integration is enabled, Antra downloads and manages a compatible `slskd` release on the user's machine.

- **Zero-Setup Daemon**: Antra automatically downloads, configures, and manages a native `slskd` instance in the background. No external setup required.
- **API-Driven**: Communicates directly with the `slskd` API for seamless background searches and queuing—no separate UI needed.
- **Painless Onboarding**: Simply provide your network credentials on the first run, and Antra permanently integrates P2P as a lossless fallback source.

*(Note: You need a free network account to use this feature.)*

Important notes:

- `slskd` is a separate third-party project and is not affiliated with or endorsed by Antra.
- Users are responsible for complying with Soulseek network rules, the `slskd` license, and applicable law in their jurisdiction.
- This integration is optional and can be left disabled if you do not want to use Soulseek-based sourcing.

> **Soulseek community etiquette:** The Soulseek network runs on sharing. If you're downloading from it, please share music back. Don't be a leecher — keep your share folder populated and leave slskd running when you can. The community depends on everyone contributing.

---

## Requirements

- **Windows 10+**, **macOS 12+**, or **Linux** (Ubuntu 24.04+)
- **No installation required** — Antra ships as a self-contained binary (`.exe` / `.dmg` / `.AppImage`)
- Optional: [ffmpeg](https://ffmpeg.org/download.html) in PATH for format conversion

---

## Installation

1. Download the build for your platform from [Releases](https://github.com/anandprtp/antra/releases):
   - **Windows**: `Antra.exe`
   - **macOS**: `Antra-macOS.dmg`
   - **Linux**: `Antra-Linux.AppImage`
2. Run it — no installation, no Python, no dependencies
3. On first launch, select your Music Library folder
4. Paste a playlist or artist URL and click **Add to Library**

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
<summary>What are the music sources?</summary>

Community run Hi-Fi and Amazon endpoints which get you the highest possible FLAC available on the internet, for free.
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

## 🙌 Acknowledgements

A massive shoutout to the **Community run Hifi-APIs** that made these lossless downloads possible! 

While those APIs originally laid the foundation, Antra builds heavily on top of them to make the engine significantly faster and solves critical reliability problems. Previously, you might occasionally get truncated music or something other than the exact song requested. With Antra's strict filtering engine, you are guaranteed to get exactly the right song, fully intact, and in the highest quality.

---

## Support Development

If Antra saves you time, consider supporting ongoing development via the link at the top of the page.

---

## Disclaimer

Antra is an independent open-source tool and is **not affiliated with, endorsed by, or connected to** any streaming service.

This project is intended for **personal, private use only**. You are responsible for ensuring your use complies with the Terms of Service of the respective platforms and applicable laws in your jurisdiction.

The software is provided "as is" without warranty of any kind. The author assumes no liability for any legal issues or consequences arising from its use.

---

<div align="center">
  <sub>Built with ❤️ · <a href="https://github.com/anandprtp/antra/issues">Report an Issue</a></sub>
</div>
