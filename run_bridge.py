"""DS HapticLink — modo consola (para depurar). La app normal es app/main.py (DSHapticLink.exe).

    .venv\\Scripts\\python.exe run_bridge.py [-v]
"""
import argparse
import ctypes
import logging
import time

from bridge.config import Config
from bridge.service import BridgeService, prerequisites


def disable_quick_edit():
    """Con la "edición rápida" de la consola, un clic en la ventana pausa el programa. Se desactiva."""
    k32 = ctypes.windll.kernel32
    handle = k32.GetStdHandle(-10)
    mode = ctypes.c_uint()
    if k32.GetConsoleMode(handle, ctypes.byref(mode)):
        k32.SetConsoleMode(handle, (mode.value & ~0x40) | 0x80)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)-9s %(message)s", datefmt="%H:%M:%S")
    disable_quick_edit()
    log = logging.getLogger("console")
    missing = [k for k, ok in prerequisites().items() if not ok]
    if missing:
        log.warning("Setup missing: %s (use the app or 'python helper/dsh_helper.py install' as administrator)", ", ".join(missing))
    service = BridgeService(Config())

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
    def on_console_event(event):
        if event in (2, 5, 6):  # cerrar la ventana, cerrar sesión, apagar: Windows da ~5 s
            service.stop(timeout=4)
        return False

    ctypes.windll.kernel32.SetConsoleCtrlHandler(on_console_event, True)
    service.start()
    try:
        while service.running:
            time.sleep(5)
            s = service.status()
            c, st = s.get("controller", {}), s.get("stats", {})
            log.info("state=%s | controller %s battery=%s%% (%s) | virtual=%s mode=%s | game=%s | BT in=%s out=%s"
                     " haptics=%s underruns=%s reserve=%sms", s["state"], "connected" if c.get("connected") else "no",
                     c.get("battery"), c.get("battery_state"), s.get("virtual", {}).get("attached"),
                     s.get("virtual", {}).get("mode"), s.get("game_active"), st.get("bt_in"), st.get("out_reports"),
                     st.get("haptic_reports"), st.get("underruns"), st.get("reserve_ms"))
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        service.stop()


if __name__ == "__main__":
    main()
