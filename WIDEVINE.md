# Getting a Widevine Device File for Amazon Music

Amazon Music Ultra HD streams are encrypted with Widevine DRM. To decrypt them, Antra needs a `.wvd` file — a Widevine device identity extracted from a real production Android device or emulator.

This guide uses a rooted Android Studio emulator. No physical device needed.

---

## What you need

- [Android Studio](https://developer.android.com/studio)
- [rootAVD](https://gitlab.com/newbit/rootAVD) — roots the emulator with Magisk
- [KeyDive](https://github.com/hyugogirubato/KeyDive) — extracts the CDM identity via Frida
- [frida-server](https://github.com/frida/frida/releases/latest) for Android x86_64

---

## Step 1 — Install the Google Play system image

In Android Studio: **Tools → SDK Manager → SDK Platforms → Show Package Details**

Expand **Android 13.0 (Tiramisu) — API 33** and check:

```
Google Play Intel x86_64 Atom System Image
```

> Must be **Google Play**, not Google APIs or AOSP. Amazon rejects dev-build certificates.

---

## Step 2 — Create the AVD

**Tools → Device Manager → + → Create Virtual Device**

- Hardware: Pixel 7 (or any phone profile)
- System image: **API 33, Google Play, x86_64**
- Do not start it yet

---

## Step 3 — Root with rootAVD

```bash
git clone https://gitlab.com/newbit/rootAVD.git
cd rootAVD
```

Start the emulator from Device Manager, wait for the home screen, then run:

```bash
# Windows
.\rootAVD.bat system-images\android-33\google_apis_playstore\x86_64\ramdisk.img

# macOS / Linux
./rootAVD.sh system-images/android-33/google_apis_playstore/x86_64/ramdisk.img
```

Once done: **Device Manager → dropdown → Cold Boot Now**

Open **Magisk** on the emulator, let it finish setup, then configure:
- **Zygisk** → ON
- **Superuser** → Apps and ADB / Grant

Cold Boot Now again after enabling Zygisk.

---

## Step 4 — Push frida-server

Download `frida-server-XX.X.X-android-x86_64.xz` from the [Frida releases page](https://github.com/frida/frida/releases/latest), extract it, and rename the binary to `frida-server`.

```bash
adb push frida-server /data/local/tmp/frida-server
adb shell chmod 755 /data/local/tmp/frida-server
adb shell "su -c '/data/local/tmp/frida-server -D &'"
```

> frida-server does not survive reboots — re-run the last line after every cold boot.

---

## Step 5 — Extract with KeyDive

```bash
pip install keydive
keydive -kw --output ./output/
```

When KeyDive says **"Successfully attached hook"**, go to the emulator:

**Open Chrome → visit https://bitmovin.com/demos/drm → click Widevine → press Play**

KeyDive captures the private key and client ID from memory. Your `.wvd` file appears in the output folder.

> After KeyDive finishes, restore the pywidevine-compatible construct version:
> ```bash
> pip install "construct==2.8.8"
> ```

---

## Step 6 — Add the .wvd to Antra

In Antra → **Settings → Amazon Music → WVD File** — point it to the `.wvd` file KeyDive generated.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `DEVICE_NOT_ELIGIBLE` | You used a Google APIs or AOSP image. Must be **Google Play**. Re-extract. |
| `su: Permission denied` | Zygisk needs a Cold Boot to take effect. Device Manager → Cold Boot Now. |
| KeyDive: `ADB is not recognized` | Add platform-tools to PATH: `export PATH=$PATH:~/Android/Sdk/platform-tools` |
| `construct` version error | Run `pip install "construct==2.8.8"` after KeyDive finishes. |
| frida-server not found after reboot | Re-run `adb shell "su -c '/data/local/tmp/frida-server -D &'"` — it doesn't persist. |
