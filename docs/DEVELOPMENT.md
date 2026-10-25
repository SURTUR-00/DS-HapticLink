# Development

## Setup

Windows 10/11 x64 with **Python 3.14 (64-bit)**.

```
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

To run the bridge you also need the components the app installs (usbip-win2 and the restart task). The easiest
way is to run the app once and click **Install components**; in development the task runs
`bridge\devrestart.py` with your venv's `pythonw.exe` instead of the compiled helper.

## Run

```
.venv\Scripts\python.exe app\main.py            # the app (tray + window)
.venv\Scripts\python.exe run_bridge.py -v       # only the bridge, in a console, verbose
.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

Preview the interface in a browser without the bridge: serve `ui\` and open
`index.html?mock&state=listo&lang=en` (other states: `en_juego`, `esperando_control`, `falta_configurar&setup`,
`falta_configurar&setup&installing`; `&edge` simulates a DualSense Edge that needs its cable profile).

```
.venv\Scripts\python.exe -m http.server 8765 --directory ui
```

Environment variables for testing: `DSH_AUTOQUIT=<seconds>` quits the app automatically; `DSH_NO_WEBVIEW=1`
simulates a PC without WebView2 (tray-only mode).

## Build

```
.venv\Scripts\python.exe build.py               # dist\DSHapticLink\ (portable) + dist\DSHapticLink-<version>-portable.zip (+ .sha256)
.venv\Scripts\python.exe build.py --console     # same, but DSHapticLink.exe with a console window, for debugging
```

The first build downloads Nuitka's C compiler (Zig) and takes several minutes. `build.py` also downloads the
pinned usbip-win2 installer (checked against its SHA-256) and its license into `build\usbip\`, and bundles them.

The version number lives in `app\version.py`.

## Layout

| Folder | Contents |
|---|---|
| `app/` | `main.py` (tray, window, API for the UI), DSH icon, texts, Windows utilities, WebView2 check |
| `ui/` | Interface (HTML/CSS/JS), English and Spanish (`i18n.js`) |
| `bridge/` | The bridge: Bluetooth protocol, USB/IP server, virtual DualSense, haptics, Opus audio, exclusive access, service, settings |
| `helper/` | `dsh-helper`: the only part that needs administrator rights (install components, restart the controller) |
| `tests/` | Tests without hardware |
| `tools/` | Developer tools: `probe.py` (list DualSense HID capabilities), `usb_dump.py` (read a wired DualSense's USB descriptors; writes `results/`, which is git-ignored because it contains your controller's MAC addresses) |

## How it works

1. **Virtual DualSense.** `bridge/usbip.py` is a USB/IP 1.1.1 server on `127.0.0.1:3240`; `bridge/attach.py`
   runs `usbip attach` so usbip-win2 creates a USB device on the PC. `bridge/virtual_ds5.py` answers as a wired
   DualSense: descriptors (`bridge/descriptors.py`, `bridge/profiles.py`), HID reports, UAC1 audio (4 channels,
   48 kHz: channels 1-2 speaker/headphones, 3-4 haptic actuators) and microphone.
2. **Bluetooth link.** `bridge/ds5bt.py` speaks the DualSense Bluetooth protocol: input report `0x31`, output
   `0x31` with CRC32, haptics/audio report `0x39` (DS5Dongle format: 64 frames of 3 kHz int8 stereo every
   21.33 ms, optionally plus two 200-byte Opus frames for the speaker or headphone jack), microphone Opus frames.
   Calibration, MAC and firmware feature reports are read live from each controller.
3. **Haptics.** `bridge/haptics.py` decimates the game's 48 kHz actuator channels to 3 kHz (FIR low-pass), keeps
   an adaptive reserve, and conceals short gaps by fading out the last vibration instead of sending silence.
4. **Exclusive access without HidHide.** Windows components (dwm, GameInput, explorer, Game Bar) and Steam keep
   the Bluetooth controller open, so an exclusive open fails. `bridge/btlink.py` asks the scheduled task
   (`bridge/devrestart.py`, run by the elevated helper) to disable and re-enable the device, and opens it
   exclusively in the gap (measured: under 1 ms). The exclusive handle survives the controller being turned off
   and on.
5. **Real-time.** `bridge/rt.py` sets Windows' timer to 1 ms (`timeBeginPeriod`) and raises thread priorities;
   without it, asyncio sleeps last ~16 ms and the isochronous audio arrives in bursts (haptic micro-cuts).

## Security notes

- The scheduled task runs elevated, so it only ever executes the helper copy in
  `C:\Program Files\DSHapticLink Helper` (admin-only), never the portable folder.
- The helper only restarts devices whose instance ID matches a DualSense (`devrestart.ALLOWED`).
- The usbip-win2 installer is verified (SHA-256 in memory, then Authenticode) inside an admin-only folder and run
  from there.

## Diagnostics

The app log is `logs\dshapticlink.log` next to the app (or `%APPDATA%\DSHapticLink\logs` if the folder isn't
writable). The helper writes `C:\ProgramData\DSHapticLink\setup.log` and `restart.log`. While haptics are active
the bridge logs a timing summary every 30 s (`haptics  last 30 s: …`).
