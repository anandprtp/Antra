# Flatpak packaging

Builds an installable Flatpak bundle for Linux:

```bash
flatpak install --user ./Antra-Linux.flatpak
flatpak run io.github.anandprtp.Antra
```

**App ID:** `io.github.anandprtp.Antra` · **Runtime:** `org.gnome.Platform//50`

## Why this exists

Linux has never been unsupported — the AppImage has shipped since v1.1.1. The
friction the request was actually about is that the AppImage needs a
distro-specific `webkit2gtk-4.1` package installed by hand first, and on Fedora
that is the most common first-run failure.

`org.gnome.Platform` contains WebKitGTK, so the Flatpak has nothing to install
first. That is the entire benefit; it is not a rewrite of how the app is built.

## Files

| File | Purpose |
|---|---|
| `io.github.anandprtp.Antra.yml` | flatpak-builder manifest |
| `io.github.anandprtp.Antra.desktop` | Desktop entry (must be named after the app-id) |
| `io.github.anandprtp.Antra.metainfo.xml` | AppStream metadata |
| `stage.py` | Copies the built binary + metadata into a staging dir, resizes the icon, stamps the release version |
| `verify_stage.py` | Cross-checks the manifest, desktop file, metainfo and staged files against each other |

## Building locally

You need a Linux machine with `flatpak` and `flatpak-builder`, plus an
already-built Linux `Antra` binary (`cd antra-wails && wails build -tags webkit2_41`).

```bash
flatpak remote-add --user --if-not-exists flathub \
  https://flathub.org/repo/flathub.flatpakrepo
flatpak install --user -y flathub org.gnome.Platform//50 org.gnome.Sdk//50

python3 packaging/flatpak/stage.py \
  --bin antra-wails/build/bin/Antra \
  --out flatpak-stage --version 2.0.0 --date "$(date -u +%F)"
python3 packaging/flatpak/verify_stage.py flatpak-stage

cd flatpak-stage
flatpak-builder --user --disable-rofiles-fuse --force-clean \
  --repo=repo build-dir io.github.anandprtp.Antra.yml
flatpak build-bundle \
  --runtime-repo=https://flathub.org/repo/flathub.flatpakrepo \
  repo ../Antra-Linux.flatpak io.github.anandprtp.Antra
```

CI does exactly this in the `build-linux` job, reusing the binary it has already
smoke-tested. Those steps are `continue-on-error` on purpose: a packaging failure
must not cost the release its AppImage.

## Permissions, and why each one is there

| Permission | Reason |
|---|---|
| `--socket=wayland`, `--socket=fallback-x11`, `--share=ipc`, `--device=dri` | GTK3 + WebKitGTK window and compositing |
| `--share=network` | Every download and metadata lookup |
| `--socket=pulseaudio` | The Downloaded Music player and the save-notification sound |
| `--filesystem=home` | See below — the one that needs an argument |

`--filesystem=home` is not laziness. Antra is a library manager: it scans the
user's whole music tree on every launch to deduplicate against what is already on
disk and to populate the Downloaded Music browser. That is a persistent,
arbitrary path, which is the wrong shape for a portal grant. Two further reasons,
both measured rather than assumed:

1. Wails opens its folder picker with GTK's own `GtkFileChooserDialog`
   (`antra-wails/app_backend.go:392`), **not** the XDG portal. Narrowing to
   `--filesystem=xdg-music` would leave the picker showing only the sandbox.
2. The default library path is `$HOME/Music` (`app_backend.go:177`). Flatpak
   only leaves `$HOME` pointing at the real home when home access is granted;
   with `xdg-music` alone `$HOME` is remapped to `~/.var/app/<id>`, so the default
   would silently resolve to a sandboxed directory the user's other music players
   cannot see. It would look like it worked.

A useful side effect: because `$HOME` stays real, `~/.local/share/Antra/config.json`
is the same file the AppImage uses, so moving from the AppImage to the Flatpak
keeps existing settings, history and library path.

## Troubleshooting

**Blank or black window.** WebKitGTK under Wayland sometimes needs its DMA-BUF
renderer or accelerated compositing turned off. Neither is set by default,
because both cost rendering performance and neither problem has been observed
here — try them in this order:

```bash
flatpak override --user --env=WEBKIT_DISABLE_DMABUF_RENDERER=1 io.github.anandprtp.Antra
flatpak override --user --env=WEBKIT_DISABLE_COMPOSITING_MODE=1 io.github.anandprtp.Antra
```

If one of them turns out to be needed on ordinary hardware, move it into
`finish-args` in the manifest rather than leaving every user to discover it.

**`runtime not found` when building.** The pinned GNOME runtime has gone EOL.
Bump `runtime-version` in the manifest — it is one number. GNOME 48 went EOL in
March 2026; 50 was current when this was written (2026-09-09).

**Folder picker shows nothing but the sandbox.** `--filesystem=home` was dropped
or overridden. Check `flatpak override --user --show io.github.anandprtp.Antra`.

## What this is NOT: a Flathub listing

This produces a bundle you can install from a file. It is **not** ready for
Flathub, and the gap is bigger than a build step. Three things stand in the way,
in descending order of difficulty:

1. **Licensing.** Antra is under the **Elastic License 2.0** (`Elastic-2.0` is a
   valid SPDX id, but OSI status is *rejected* — it is source-available, not open
   source). Flathub does host proprietary apps, but they normally ship as
   `extra-data` that downloads the vendor's binary at install time rather than
   being redistributed by Flathub, and the review is stricter. This is a
   licensing/policy decision, not an engineering one, and it should be settled
   before any effort goes into (2) or (3).
2. **Source build.** Flathub builds from source with no network access during
   the build. That means vendoring the Go module tree *and* the npm tree, and
   rebuilding the ~70 MB PyInstaller backend inside the sandbox. This manifest
   deliberately consumes the prebuilt binary instead, which is why it is not a
   Flathub manifest.
3. **Screenshots.** The two referenced in the metainfo are real and reachable,
   but they are from v1.1.7 and predate the v2.0.0 theme engine. They should be
   retaken before anyone reviews the listing.

The `.deb` / `.rpm` route is worth weighing against a Flathub push: it is
substantially less work and covers a large share of the same complaint, though
it does not solve the WebKit dependency the way a Flatpak runtime does.

## Unverified

This manifest has **not been built or run on Linux**. It was authored and checked
on Windows, where everything except `flatpak-builder` itself can be exercised:
the manifest, desktop entry and metainfo parse and satisfy their cross-file
invariants; `stage.py` runs against the real icon and a real binary; and
`verify_stage.py` catches 7/7 deliberate sabotages. The first real CI run is what
will confirm the runtime version, the build, and that the app launches inside the
sandbox. See the FR-1 entry in the internal v2.0.0 notes for the full list of what is
proven and what is not.
