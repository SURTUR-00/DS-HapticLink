# Changelog

## 0.4.11

- Fixed constant small audio and haptic cuts when Windows delivers the game audio in bursts (typical of the
  *Power saver* plan): when an audio packet arrived late, the virtual USB clock restarted from that moment and the
  delay was lost, so the virtual controller took in slightly less audio than real time (measured: 0.9985 s per
  second, ~45 ms short every 30 s) and the haptic buffer slowly ran dry no matter how big the reserve was. The
  clock now keeps running like real USB hardware and catches up. The 30-second log line now shows the incoming
  audio rate, the lowest buffer level and how long no audio packet was queued.

- Fixed stuttering audio (headphones/speaker) and more haptic micro-gaps after opening and closing the window during
  a game: Windows 11 stops honoring the 1 ms timer for processes whose window is hidden, minimized or covered.
  DS HapticLink now opts out of that power throttling (and of efficiency mode).

- No more false "Low battery" notifications: controller input reports are checked with their CRC (corrupt ones
  are discarded, as the Linux driver does), a new battery reading must hold for 5 s before it is shown, and the
  notification only appears after the battery has stayed low for a minute. Battery changes are now logged.

- Warns when Windows is saving power (the *Power saver* plan or a copy of it, the *Best power efficiency* power
  mode, or energy saver): a card in the window with a button to the power options, a one-time notification, and
  a line in the log and in *Copy diagnostics*. Power saving can slow down the CPU, USB and Bluetooth. If you use
  power saving on purpose, *Don't warn me again* hides the warning (*General → Warn me if Windows is saving power*
  brings it back); the log still records it.

- Less CPU for the controller audio: about 45% less with headphones/speaker (exact integer resampling of both
  channels at once, Opus encoding without copies) and about half for haptics (the filter uses `math.sumprod`).

- The audio threads are registered with MMCSS ("Pro Audio"), the Windows service for real-time audio, so they keep
  their turn on the CPU whichever window is in the foreground.

- Fewer cuts from recurring system hiccups (~35-45 ms stalls, e.g. when switching windows during a game): the
  haptic reserve still rises at once, but now falls back slowly (it remembers the reserve the last hiccup needed
  and lowers it one step every 2 minutes without cuts) instead of returning to the minimum in about 5 minutes. The
  headphone/speaker buffer was raised to match (it could drop audio when the reserve was high).

- The 30-second haptics line in the log now also counts headphone/speaker cuts (gaps) and dropped audio.

- Steam no longer warns "battery low" while the controller is off: the virtual controller's idle report carried a
  battery of 0%. It now keeps the last battery level the controller reported ("full" before the first one).

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
