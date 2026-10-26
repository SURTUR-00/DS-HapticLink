# Changelog

## 0.4.10

- No more console window flashing for a moment when DS HapticLink starts (`usbip.exe` was run without hiding its
  console).

- usbip-win2 is installed without its Start menu shortcuts ("USBip" and "Uninstall USBip"), and *Install
  components* removes them from existing installations: uninstalling usbip-win2 from there left DS HapticLink
  without its driver. Use *General → Uninstall…* instead.

## 0.4.9 — first public release

First public version of DS HapticLink: native DualSense HD haptics, adaptive triggers, light bar, speaker,
headphone jack and microphone over Bluetooth on Windows, through a virtual wired DualSense.

Tested over Bluetooth with **God of War Ragnarök**, **Stellar Blade** and **Zenless Zone Zero**, including 30+ minute
sessions.

**Highlights**
- One-click setup with a single administrator prompt. The usbip-win2 driver is now **included in the zip** (no
  download needed; SHA-256 and digital signature verified before installing).
- No HidHide: the real controller is taken exclusively by briefly restarting it.
- Tray app with status colors, English/Spanish interface, "My controllers" with per-controller settings, optional
  cable profile capture.

**Fixes during testing (0.4.1 – 0.4.9)**
- Fewer antivirus false positives: the helper no longer starts PowerShell (the usbip-win2 signature is checked
  with Windows' own `WinVerifyTrust`), and the scheduled task now has an author and a description.
- The controller could be left **disabled** in Windows (Device Manager code 22) when another program blocked a
  restart: it kept working until the next device reset, then "disappeared". The device is now always re-enabled
  after a blocked restart, and every restart and *Install components* repairs any DualSense left disabled.
- usbip-win2 is only considered installed if all its files are there: after uninstalling it without
  restarting, a leftover `usbip.exe` made the next setup skip the driver ("libusbip.dll was not found").
- The scheduled task now **also runs on battery**: on a laptop without its charger the controller was never
  restarted (Windows' default task settings say "don't start on battery").
- **Uninstall from the app:** General → *Uninstall…* (or `DSHapticLink.exe --uninstall`), with progress and the
  option to keep or remove usbip-win2.
- `dsh-helper uninstall` asks for administrator permission by itself, disconnects the virtual controller first,
  waits for the usbip-win2 uninstaller, logs every step and shows the result (before, it could leave usbip-win2
  and the scheduled task installed without telling you).
- Setup no longer gets stuck on "Downloading usbip-win2" (status file race), and errors are shown in the window.
- The helper has its own folder (`C:\Program Files\DSHapticLink Helper`), so it can never touch the app folder.
- If the helper is deleted, the app notices and offers **Install components** again to repair it.
- Works with **Steam open**: the bridge keeps trying for the whole restart instead of 5 s, with increasing pauses
  and a message in the window.
- The controller no longer **turns off when a game starts** (God of War sends a feature report `0x08` that powers
  the controller off over Bluetooth; it's now blocked).
- The light bar comes back to your color when a game exits and leaves the lights off.
- **Haptic micro-cuts fixed:** 1 ms Windows timer, higher priority for the audio threads, and gap concealment
  (from ~1 cut per second to none perceptible). A timing summary is logged every 30 s while haptics are active.
- Quitting from the tray no longer hangs.

**Known issues:** see [Tips and known issues](README.md#tips-and-known-issues). Windows Defender may flag the
helper as `Behavior:Win32/Execution.A!ml` (false positive): allow it in Protection history and click *Install
components* again.
