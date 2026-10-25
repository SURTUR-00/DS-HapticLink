"""DS HapticLink — app de escritorio: ícono "DSH" en la bandeja + ventana (pywebview) + el puente en segundo plano.

    DSHapticLink.exe [--minimized] [--uninstall]
"""
import collections
import json
import logging
import logging.handlers
import os
import platform
import sys
import threading
import time
from pathlib import Path

if __package__ in (None, ""):  # ejecutado como script (desarrollo): permitir "import app" y "import bridge"
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pystray  # noqa: E402
import webview  # noqa: E402

from app import tray_icon, webview2, winsys  # noqa: E402
from app.i18n import t  # noqa: E402
from app.version import APP_ID, APP_NAME, VERSION  # noqa: E402
from bridge import devrestart  # noqa: E402
from bridge.config import Config, app_dir, data_dir  # noqa: E402
from bridge.service import BridgeService, prerequisites  # noqa: E402

SETUP_STATUS_FILE = devrestart.REQUEST_FILE.with_name("setup_status.json")  # lo escribe dsh-helper install

log = logging.getLogger("app")
UI_DIR = app_dir() / "ui"  # junto al .exe compilado, o en la raíz del proyecto en desarrollo
LOG_LINES = collections.deque(maxlen=400)


class MemoryLog(logging.Handler):
    def emit(self, record):
        LOG_LINES.append(self.format(record))


def setup_logging():
    fmt = logging.Formatter("%(asctime)s %(name)-9s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    logs = data_dir() / "logs"
    logs.mkdir(exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(logs / "dshapticlink.log", maxBytes=1_000_000, backupCount=2,
                                                        encoding="utf-8")
    memory = MemoryLog()
    for handler in (file_handler, memory):
        handler.setFormatter(fmt)
        root.addHandler(handler)


class Api:
    """Métodos que la interfaz (JavaScript) llama con window.pywebview.api.<método>()."""

    def __init__(self, app):
        self._app = app

    def get_info(self):
        return {"version": VERSION, "name": APP_NAME}

    def get_status(self):
        app = self._app
        status = app.service.status()
        status["prerequisites"] = app.prereqs
        status["setup"] = self._app.setup_state
        status["uninstall"] = self._app.uninstall_state
        return status

    def get_settings(self):
        settings = self._app.config.snapshot()
        settings["autostart"] = winsys.autostart_enabled()
        return settings

    def set_setting(self, key, value):
        if key == "autostart":
            winsys.set_autostart(bool(value))
            return bool(value)
        value = self._app.config.set(key, value)
        if key == "language":
            self._app.refresh_tray()
        return value

    def run_setup(self):
        self._app.run_setup()
        return True

    # Desinstalar
    def show_uninstall(self, visible=True):
        self._app.show_uninstall(bool(visible))
        return True

    def run_uninstall(self, drivers=True):
        self._app.run_uninstall(bool(drivers))
        return True

    def open_app_folder(self):
        os.startfile(self._app.uninstall_state["folder"])

    # Mis controles
    def get_controllers(self):
        connected = self._app.service.device.controller_mac if self._app.service.device else None
        items = self._app.service.registry.listing()
        for item in items:
            item["connected"] = item["mac"] == connected and self._app.service.state() not in ("detenido",)
        return items

    def rename_controller(self, mac, name):
        self._app.service.registry.rename(mac, name)
        return True

    def set_controller_setting(self, mac, key, value):
        self._app.service.set_controller_setting(mac, key, value)
        return True

    def forget_controller(self, mac):
        self._app.service.registry.forget(mac)
        return True

    def open_bluetooth_settings(self):
        winsys.open_bluetooth_settings()

    def test_rumble(self):
        return self._app.service.test_rumble()

    def toggle_pause(self):
        return self._app.toggle_pause()

    def copy_diagnostics(self):
        return winsys.copy_to_clipboard(self._app.diagnostics())

    def get_logs(self):
        return list(LOG_LINES)[-150:]

    def hide(self):
        self._app.hide_window()

    def quit(self):
        threading.Thread(target=self._app.quit, daemon=True).start()


class DsxApp:
    def __init__(self, minimized, uninstall=False):
        self.config = Config()
        self.service = BridgeService(self.config)
        self.prereqs = prerequisites()
        self.prereqs_checked = time.monotonic()
        self.setup_state = {"running": False, "message": "", "percent": 0, "ok": None}
        self.uninstall_state = {"view": uninstall, "running": False, "message": "", "percent": 0, "ok": None,
                                "drivers": True, "folder": str(app_dir())}
        self.paused = False
        self.quitting = False
        self.window = None
        self.tray = None
        self.tray_only = False
        self.last_state = None
        self.notified = {"battery_low": False, "connected": None}
        self.start_hidden = minimized and all(self.prereqs.values()) and not uninstall

    @property
    def lang(self):
        return self.config.get("language")

    # ------------------------------------------------------------------ puente
    def start_bridge(self):
        if self.prereqs.get("usbip") and not self.paused:
            self.service.start()

    def toggle_pause(self):
        self.paused = not self.paused
        if self.paused:
            self.service.stop()
        else:
            self.start_bridge()
        self.refresh_tray()
        return self.paused

    def run_setup(self):
        if self.setup_state["running"]:
            return

        def work():
            STATUS_FILE = SETUP_STATUS_FILE
            self.setup_state.update(running=True, message="", percent=0, ok=None)
            try:
                STATUS_FILE.unlink()
            except OSError:
                pass
            poller = threading.Thread(target=self._poll_setup, args=(STATUS_FILE,), daemon=True)
            poller.start()
            exe, args = winsys.helper_command("install", "--lang", self.lang)
            code = winsys.run_elevated(exe, args)
            final = self._read_setup_status(STATUS_FILE)  # el último estado, sin esperar al poller
            self.prereqs = prerequisites()
            ok = code == 0 and all(self.prereqs.values())
            log.info("setup helper exited with code %s (status: %s)", code, final)
            if code is None:
                self.setup_state.update(message="uac_denied", ok=False)
            elif not ok and (final or {}).get("ok") is not False:
                # el helper terminó mal sin dejar su mensaje de error: no mostrar el último paso como si siguiera
                self.setup_state.update(message="", percent=0, ok=False)
            else:
                if final:
                    self.setup_state.update(message=final["message"], percent=final["percent"])
                self.setup_state.update(ok=ok)
            self.setup_state["running"] = False
            if ok:
                self.service.stop()
                self.start_bridge()
            if self.tray_only and self.tray is not None:  # sin ventana: avisar por notificación
                self.tray.notify(webview2.TEXT.get(self.lang, webview2.TEXT["en"])["setup_ok" if ok else "setup_fail"],
                                 APP_NAME)

        threading.Thread(target=work, daemon=True, name="instalación").start()

    @staticmethod
    def _read_setup_status(status_file):
        try:
            data = json.loads(status_file.read_text(encoding="utf-8"))
            return {"message": data["message"], "percent": data["percent"], "ok": data.get("ok")}
        except (OSError, ValueError, KeyError):
            return None

    def _poll_setup(self, status_file):
        while self.setup_state["running"]:
            data = self._read_setup_status(status_file)
            if data and self.setup_state["running"]:
                self.setup_state.update(message=data["message"], percent=data["percent"])
            time.sleep(0.3)

    # ------------------------------------------------------------------ desinstalar
    def show_uninstall(self, visible=True):
        if not self.uninstall_state["running"]:
            self.uninstall_state.update(view=visible, ok=None, message="", percent=0)

    def run_uninstall(self, drivers):
        """Quita lo que DS HapticLink instaló en Windows (con un UAC): la tarea, el helper protegido y, si se pide,
        usbip-win2. La carpeta de la app y sus ajustes quedan (el usuario la borra)."""
        state = self.uninstall_state
        if state["running"]:
            return

        def work():
            state.update(running=True, ok=None, message="", percent=2, drivers=bool(drivers))
            log.info("uninstall requested (remove usbip-win2: %s)", bool(drivers))
            self.service.stop()  # suelta el control real y desconecta el virtual: si no, el driver no se puede quitar
            try:
                winsys.set_autostart(False)
            except OSError:
                pass
            try:
                SETUP_STATUS_FILE.unlink()
            except OSError:
                pass

            def poller():
                while state["running"]:
                    data = self._read_setup_status(SETUP_STATUS_FILE)
                    if data and state["running"]:
                        state.update(message=data["message"], percent=data["percent"])
                    time.sleep(0.3)

            threading.Thread(target=poller, daemon=True).start()
            exe, args = winsys.helper_command("uninstall", *(["--drivers"] if drivers else []), "--lang", self.lang,
                                              "--quiet")
            code = winsys.run_elevated(exe, args)
            final = self._read_setup_status(SETUP_STATUS_FILE)
            log.info("uninstall helper exited with code %s (status: %s)", code, final)
            self.prereqs = prerequisites()
            if code is None:
                state.update(message="uac_denied", ok=False, percent=0)
            elif code == 0:
                state.update(message="", ok=True, percent=100)
            else:
                state.update(message=(final or {}).get("message", ""), ok=False, percent=0)
            state["running"] = False
            if not state["ok"]:
                self.start_bridge()  # no se desinstaló: seguir funcionando como antes
            self.refresh_tray()

        threading.Thread(target=work, daemon=True, name="desinstalación").start()

    # ------------------------------------------------------------------ bandeja
    def build_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem(lambda item: t(self.lang, "open"), lambda: self.show_window(), default=True),
            pystray.MenuItem(lambda item: t(self.lang, "resume") if self.paused else t(self.lang, "pause"),
                             lambda: self.toggle_pause()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(lambda item: t(self.lang, "quit"), lambda: self.quit()),
        )
        self.tray = pystray.Icon(APP_ID, tray_icon.icon_for_state("iniciando"), APP_NAME, menu)
        self.tray.run_detached()
        threading.Thread(target=self._tray_loop, daemon=True, name="bandeja").start()

    def refresh_tray(self):
        if self.tray is None:
            return
        state = "falta_configurar" if not self.prereqs.get("usbip") else self.service.state()
        if self.paused:
            state = "detenido"
        if state != self.last_state:
            self.last_state = state
            self.tray.icon = tray_icon.icon_for_state(state)
        self.tray.title = t(self.lang, "tooltip")[state]
        self.tray.update_menu()

    def _tray_loop(self):
        while not self.quitting:
            try:
                self.refresh_tray()
                self._notifications()
            except Exception:
                log.exception("error updating the tray")
            time.sleep(1)

    def _check_components(self, controller):
        """El puente no pudo pedir el reinicio del control (p. ej. se borró a mano la carpeta del helper): volver
        a comprobar los componentes; si falta algo, la interfaz muestra "Install components" (lo repara con un
        UAC) y se avisa una vez. Como mucho cada 5 s (consulta schtasks)."""
        if not (controller.get("blocked") and self.prereqs.get("task") and not self.setup_state["running"]
                and not self.uninstall_state["view"]):
            return
        if time.monotonic() - self.prereqs_checked < 5:
            return
        self.prereqs_checked = time.monotonic()
        self.prereqs = prerequisites()
        if not all(self.prereqs.values()):
            log.warning("a DS HapticLink component is missing (%s): the setup screen offers to repair it",
                        ", ".join(k for k, v in self.prereqs.items() if not v))
            if self.tray is not None:
                self.tray.notify(t(self.lang, "repair"), APP_NAME)

    def _notifications(self):
        status = self.service.status()
        controller = status.get("controller") or {}
        self._check_components(controller)
        if not self.config.get("notifications") or self.tray is None:
            return
        connected = bool(controller.get("connected"))
        previous = self.notified["connected"]
        if previous is not None and connected != previous:
            self.tray.notify(t(self.lang, "connected" if connected else "disconnected"), APP_NAME)
        self.notified["connected"] = connected
        battery = controller.get("battery")
        low = battery is not None and battery <= 15 and controller.get("battery_state") == "descargando"
        if low and not self.notified["battery_low"]:
            self.tray.notify(t(self.lang, "battery_low", pct=battery), APP_NAME)
        self.notified["battery_low"] = low

    # ------------------------------------------------------------------ ventana
    def show_window(self):
        if self.window is not None:
            self.window.show()
            self.window.restore()
        elif self.tray_only:
            threading.Thread(target=webview2.message, args=(self.lang, "tray_only"), daemon=True).start()

    def run_tray_only(self):
        """Sin WebView2: la app funciona desde la bandeja (el puente y el ícono no lo necesitan)."""
        log.warning("tray-only mode: no settings window (Microsoft Edge WebView2 is missing)")
        self.tray_only = True
        if self.uninstall_state["view"]:  # sin ventana: el helper desinstala y muestra el resultado él mismo
            exe, args = winsys.helper_command("uninstall", "--drivers", "--lang", self.lang)
            winsys.run_elevated(exe, args)
            self.quitting = True
            return
        self.build_tray()
        if not all(self.prereqs.values()) and webview2.message(self.lang, "setup_ask", yes_no=True):
            self.run_setup()  # la pantalla de configuración inicial no se puede mostrar: preguntar con un aviso
        self.start_bridge()
        while not self.quitting:
            time.sleep(0.5)

    def hide_window(self):
        if self.window is not None:
            self.window.hide()

    def _on_closing(self):
        if self.quitting:
            return True
        self.hide_window()  # cerrar la ventana la manda a la bandeja; para salir: menú "Salir"
        return False

    def quit(self):
        if self.quitting:
            return
        self.quitting = True
        log.info("quitting")
        # Seguro: si algo se traba al cerrar (pasó en 0.4.3: la app quedó abierta tras "bridge stopped"), terminar
        # el proceso igual. El puente ya deshizo sus cambios en _cleanup.
        watchdog = threading.Timer(20, self._force_exit)
        watchdog.daemon = True
        watchdog.start()
        self.service.stop()
        if self.tray is not None:
            self.tray.stop()
        if self.window is not None:
            self.window.destroy()

    @staticmethod
    def _force_exit():
        log.warning("closing took too long: forcing the exit")
        logging.shutdown()
        os._exit(0)

    def diagnostics(self):
        status = self.service.status()
        lines = [f"{APP_NAME} {VERSION} | Windows {platform.version()} | Python {platform.python_version()}",
                 f"prerequisites: {self.prereqs}", "status: " + json.dumps(status, ensure_ascii=False), ""]
        return "\n".join(lines + list(LOG_LINES)[-200:])

    def run(self):
        force_tray_only = os.environ.get("DSH_NO_WEBVIEW") == "1"  # pruebas: simular un equipo sin WebView2
        if force_tray_only or not webview2.ensure(self.lang):
            self.run_tray_only()
            return
        self.window = webview.create_window(
            f"{APP_NAME}", url=str(UI_DIR / "index.html"), js_api=Api(self), width=920, height=640,
            min_size=(760, 560), hidden=self.start_hidden, background_color="#0B1220")
        self.window.events.closing += self._on_closing
        self.build_tray()
        if not self.uninstall_state["view"]:  # abierta con --uninstall: no tomar el control para luego soltarlo
            self.start_bridge()
        icon = app_dir() / "assets" / "dsh.ico"
        # Sin storage_path: en modo privado (el de por defecto) pywebview BORRA esa carpeta al salir, y en el .exe
        # compilado "webview\" es la carpeta del propio pywebview (pasó: borró sus .js). Que use una temporal.
        webview.start(gui="edgechromium", icon=str(icon) if icon.exists() else None)


def main():
    setup_logging()
    minimized = "--minimized" in sys.argv
    uninstall = "--uninstall" in sys.argv  # abre directo la pantalla para desinstalar
    holder = {}
    instance = winsys.SingleInstance(on_show=lambda: holder["app"].show_window() if "app" in holder else None)
    if not instance.primary:
        winsys.SingleInstance.signal_primary()
        return
    app = DsxApp(minimized, uninstall)
    holder["app"] = app
    log.info("%s %s started (data in %s)", APP_NAME, VERSION, data_dir())
    autoquit = os.environ.get("DSH_AUTOQUIT")  # pruebas automáticas: cerrar solo tras N segundos
    if autoquit:
        threading.Timer(float(autoquit), app.quit).start()
    app.run()
    if not app.quitting:  # la ventana se destruyó por otra vía
        app.quit()
    # Terminar ya: no esperar a hilos de fondo (bandeja, lector BT, pywebview) que aún estén cerrándose.
    logging.shutdown()
    os._exit(0)


if __name__ == "__main__":
    main()
