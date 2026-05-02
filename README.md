<p align="center">
  <img src="assets/antra-header.svg" width="100%" alt="Antra"/>
</p>

<p align="center">
  <strong>Connect your Tidal · Qobuz · Amazon Music or Apple Music account and download hi-res lossless audio. Spotify podcasts too. Free, forever.</strong>
</p>

<p align="center">
  <a href="https://github.com/anandprtp/Antra/releases"><img src="https://img.shields.io/github/v/release/anandprtp/Antra?color=0ea5e9&label=latest&style=flat-square&labelColor=0d1117"/></a>
  <img src="https://img.shields.io/badge/Windows%20·%20macOS%20·%20Linux-supported-0ea5e9?style=flat-square&labelColor=0d1117"/>
  <img src="https://img.shields.io/badge/License-MIT-0ea5e9?style=flat-square&labelColor=0d1117"/>
  <img src="https://img.shields.io/badge/Free-Forever-0ea5e9?style=flat-square&labelColor=0d1117"/>
  <a href="https://t.me/antraaverse"><img src="https://img.shields.io/badge/Community-Telegram-26A5E4?style=flat-square&labelColor=0d1117&logo=telegram&logoColor=white"/></a>
</p>

<p align="center">
  <a href="https://github.com/anandprtp/Antra/releases">
    <img src="https://img.shields.io/badge/⬇_Download_Latest_Release-0ea5e9?style=for-the-badge&labelColor=0d1117"/>
  </a>
  &nbsp;
  <a href="FEATURES.md">
    <img src="https://img.shields.io/badge/✦_Full_Feature_Guide-7DD3FC?style=for-the-badge&labelColor=0d1117"/>
  </a>
</p>

<br/>

<p align="center">
  <img src="assets/screenshots/Screenshot%202026-04-29%20021124.png" width="780"/>
</p>

---

## What it does

Connect your own Tidal, Qobuz, Amazon Music, or Apple Music account (free trials work), paste any Spotify, Apple Music, or Amazon Music link — playlist, album, artist, or podcast — and Antra picks the highest-quality source, downloads it, tags it with full metadata (title, artist, artwork, genre, lyrics), and organises it into a clean `Artist / Album` folder structure ready for Navidrome, Jellyfin, or Plex.

Use your own account. No middleman. No Python. One binary.

```
Sources:  Your Tidal · Qobuz · Amazon Music accounts (FLAC, up to 24-bit/192kHz)
          Apple Music account (AAC 256 kbps)
          Soulseek P2P (rare / out-of-print releases)
Matching: ISRC-based, exact pressing every time — best 24-bit source wins
Output:   FLAC · ALAC · AAC · auto-tagged · Navidrome · Jellyfin · Plex ready
```

> **Amazon Music FLAC** requires an L3/L1-certified `.wvd` file.

→ **[Full feature guide](FEATURES.md)**

---

## Install

Download the build for your platform from [Releases](https://github.com/anandprtp/Antra/releases) and run it. No installation required.

| Platform | File |
|---|---|
| Windows 10+ | `Antra.exe` |
| macOS 12+ (Apple Silicon) | `Antra-macOS.dmg` |
| macOS 12+ (Intel) | `Antra-macOS-Intel.dmg` |
| Linux | `Antra-Linux.AppImage` |

> **Windows Defender flag?** False positive. PyInstaller bundles sometimes trigger AV heuristics. All code is here and auditable; build from source if you prefer.

---

## Quick start

1. Launch Antra and pick your Music Library folder on first run
2. Go to **Settings** and connect at least one account (Tidal, Qobuz, or Amazon Music — free trials work)
3. Paste any Spotify, Apple Music, Amazon Music, or Spotify podcast URL
4. Press **Add to Library**

Tracks download, get tagged, and land in the right folder automatically.

---

## Build from source

Requirements: Python 3.11+, Go 1.23+, Node.js 18+, Wails v2

```bash
git clone https://github.com/anandprtp/Antra
cd Antra
pip install -r requirements-desktop.txt
python build_desktop.py
# output: antra-wails/build/bin/Antra.exe
```

---

## Keep Antra alive

Antra is free and always will be. It takes real time to maintain: tracking API changes, fixing broken sources, and shipping new features.

If Antra saves you money on streaming subscriptions, consider giving back:

<p align="center">
  <a href="https://ko-fi.com/antraverse">
    <img src="https://img.shields.io/badge/Support_on_Ko--fi-FF5E5B?style=for-the-badge&logo=ko-fi&logoColor=white"/>
  </a>
  &nbsp;
  <a href="https://github.com/anandprtp/Antra">
    <img src="https://img.shields.io/badge/⭐_Star_the_Repo-FFD700?style=for-the-badge&logo=github&logoColor=black"/>
  </a>
</p>

<p align="center">
  PayPal: <a href="https://paypal.me/hoshiyaar1501">https://paypal.me/hoshiyaar1501</a><br/>
  USDT (TRC20): <code>TCzJhbLfeSphfwRAmXTQrPKW3RrniG2H8q</code>
</p>

---

## Soulseek / P2P

Optional integration with the Soulseek P2P network for rare albums, limited pressings, and out-of-print releases. Antra auto-downloads and manages the backend. Just add your credentials on first run.

> The Soulseek network runs on sharing. If you download from it, share back.

---

> [!TIP]
> Star the repo to get notified about all new releases directly from GitHub.

---

## Disclaimer

This repository and its contents are provided strictly for educational and research purposes. The software is provided "as-is" without warranty of any kind, express or implied, as stated in the MIT License.

- No copyrighted content is hosted, stored, mirrored, or distributed by this repository.
- Users must ensure that their use of this software is properly authorized and complies with all applicable laws, regulations, and third-party terms of service.
- This software is provided free of charge by the maintainer. If you paid a third party for access to this software in its original form from this repository, you may have been misled or scammed. Any redistribution or commercial use by third parties must comply with the terms of the repository license. No affiliation, endorsement, or support by the maintainer is implied unless explicitly stated in writing.
- Antra is an independent project. It is not affiliated with, endorsed by, or connected to any other project or version on other platforms that may share a similar name. The maintainer of this repository has no control over or responsibility for third-party projects.
- The author(s) disclaim all liability for any direct, indirect, incidental, or consequential damages arising from the use or misuse of this software. Users assume all risk associated with its use.
- If you are a copyright holder or authorized representative and believe this repository infringes upon your rights, please contact the maintainer with sufficient detail (including relevant URLs and proof of ownership). The matter will be promptly investigated and appropriate action will be taken, which may include removal of the referenced material.

---

<p align="center">
  <sub>Built with ❤️ by <a href="https://github.com/anandprtp">Hoshiyaar Singh</a> · <a href="https://github.com/anandprtp/Antra/issues">Report an Issue</a> · <a href="https://t.me/antraaverse">Join the Community</a></sub>
</p>
