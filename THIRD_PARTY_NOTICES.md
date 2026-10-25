# Third-party notices

DS HapticLink is released under the [MIT License](LICENSE). The release package also contains, or is built
with, the following third-party components. Each one remains under its own license.

## Redistributed in the release package

| Component | License | Notes |
|---|---|---|
| [usbip-win2](https://github.com/vadimgrn/usbip-win2) 0.9.8.1 installer | BSD-2-Clause | Redistributed unmodified (`helper\USBip-0.9.8.1-x64.exe`); its license is in `licenses\usbip-win2.txt`. The installer is digitally signed by its authors; DS HapticLink checks its SHA-256 and signature before running it. |
| [libopus](https://opus-codec.org/) (`opus.dll`, via [PyOgg](https://github.com/TeamPyOgg/PyOgg)) | BSD-3-Clause (libopus), Unlicense (PyOgg) | Audio codec for the controller's speaker, headphone jack and microphone over Bluetooth. |
| [Python](https://www.python.org/) 3.14 runtime | PSF License | Compiled into the executables by Nuitka. |
| [pywebview](https://github.com/r0x0r/pywebview) | BSD-3-Clause | User interface window (uses the Microsoft Edge WebView2 runtime, which is part of Windows and not redistributed). Includes the Microsoft WebView2 SDK DLLs, under Microsoft's WebView2 SDK license. |
| [pythonnet](https://github.com/pythonnet/pythonnet) and [clr_loader](https://github.com/pythonnet/clr-loader) | MIT | Used by pywebview to talk to WebView2. |
| [pystray](https://github.com/moses-palmer/pystray) | LGPL-3.0 | Tray icon. Its source code is available at the link; you can rebuild DS HapticLink with a modified pystray from this repository's source (see `docs/DEVELOPMENT.md`). |
| [Pillow](https://python-pillow.org/) | MIT-CMU (HPND) | Draws the tray icon. |
| [cython-hidapi](https://github.com/trezor/cython-hidapi) and [HIDAPI](https://github.com/libusb/hidapi) | BSD-3-Clause (chosen among their available licenses) | Finding the controller. |
| [bottle](https://bottlepy.org/), [proxy_tools](https://pypi.org/project/proxy_tools/), [cffi](https://cffi.readthedocs.io/), [pycparser](https://github.com/eliben/pycparser), [six](https://github.com/benjaminp/six), [typing_extensions](https://github.com/python/typing_extensions) | MIT / BSD-3-Clause / PSF | Dependencies of the packages above. |

The executables are compiled with [Nuitka](https://nuitka.net/) (Apache-2.0; the compiled output is not
subject to Nuitka's license).

## Protocol knowledge (no code copied)

DS HapticLink's Bluetooth implementation was written from scratch, using these projects as protocol references.
Many thanks to their authors:

- [DS5Dongle](https://github.com/awalol/DS5Dongle) (MIT): the Bluetooth `0x39` haptics/audio report format and
  the idea of presenting a wired DualSense to the PC.
- [SAxense](https://github.com/egormanga/SAxense) (MPL-2.0): DualSense HD haptics over Bluetooth.
- Linux's [hid-playstation](https://github.com/torvalds/linux/blob/master/drivers/hid/hid-playstation.c) driver
  (GPL-2.0): report layouts, CRC and battery/status fields.

## Trademarks

DualSense, DualSense Edge, PlayStation and PS5 are trademarks of Sony Interactive Entertainment Inc. DS HapticLink
is an independent project and is not affiliated with, endorsed or sponsored by Sony, nor by Paliverse (DSX).
