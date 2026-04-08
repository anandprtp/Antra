## Downloads

![Windows](https://img.shields.io/github/downloads/anandprtp/Antra/latest/Antra.exe?style=for-the-badge&logo=windows&logoColor=white&label=Windows&display_name=true)
![macOS](https://img.shields.io/github/downloads/anandprtp/Antra/latest/Antra-macOS.dmg?style=for-the-badge&logo=apple&logoColor=white&label=macOS&display_name=true)
![Linux](https://img.shields.io/github/downloads/anandprtp/Antra/latest/Antra-Linux.AppImage?style=for-the-badge&logo=linux&logoColor=black&label=Linux&display_name=true)

- `Antra.exe` - Windows
- `Antra-macOS.dmg` - macOS
- `Antra-Linux.AppImage` - Linux

<details>
<summary>Linux Installation</summary>

Download `Antra-Linux.AppImage`, then run:

```bash
chmod +x Antra-Linux.AppImage
./Antra-Linux.AppImage
```

If Ubuntu blocks execution from the Downloads folder, move it somewhere simple first:

```bash
mv ~/Downloads/Antra-Linux.AppImage ~/
cd ~
chmod +x Antra-Linux.AppImage
./Antra-Linux.AppImage
```

If it still refuses to start, install FUSE support once:

```bash
sudo apt update
sudo apt install libfuse2
```

</details>

<details>
<summary>macOS Installation</summary>

Download `Antra-macOS.dmg`, open it, and drag **Antra** into your **Applications** folder.

On first launch, macOS may block the app with an "Apple could not verify" warning. To open it:

1. Open **Applications**
2. Right-click **Antra**
3. Click **Open**
4. Click **Open** again in the warning dialog

Useful notes for macOS:

- If the app still does not open, go to **System Settings → Privacy & Security** and click **Open Anyway** if that option appears.
- You may need to move the app out of the DMG and into **Applications** before opening it.
- This build is not notarized yet, so the first-run security prompt is expected.

</details>
