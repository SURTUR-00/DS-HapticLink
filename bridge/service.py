"""El puente como servicio de fondo, para la interfaz gráfica (o la línea de comandos).

Corre su propio bucle asyncio en un hilo. La interfaz lo controla con start()/stop(), lee status() y le
pasa los ajustes; nada aquí depende de una consola.
"""
import asyncio
import logging
import threading
import time

from . import audio, devrestart, rt
from .attach import AttachManager
from .btlink import BtLink
from .controllers import ControllerRegistry
from .profiles import BUILTIN_PID, builtin_profile, capture_profile
from .winhid import WinHidDevice
from .haptics import HapticBridge
from .usbip import UsbIpServer
from .virtual_ds5 import VirtualDualSense
from .winhid import ExclusiveHolder

log = logging.getLogger("service")

# Estados para la interfaz y el ícono de la bandeja
STATE_STOPPED = "detenido"
STATE_STARTING = "iniciando"
STATE_NEEDS_SETUP = "falta_configurar"
STATE_WAITING = "esperando_control"
STATE_READY = "listo"
STATE_GAME = "en_juego"
STATE_CABLE = "cable"
STATE_PROBLEM = "problema"


def prerequisites():
    """Qué falta instalar: el driver usbip-win2 y la tarea que libera el control sin UAC."""
    return {
        "usbip": devrestart.usbip_installed(),
        "task": devrestart.task_ready(),
    }


class BridgeService:
    def __init__(self, config=None, port=3240, registry=None):
        self.config = config
        self.port = port
        self.registry = registry if registry is not None else ControllerRegistry()
        self.capture_state = {"running": False, "result": None, "mac": None, "time": 0}
        self._captured_paths = set()
        self.thread = None
        self.loop = None
        self._stop = None
        self.device = self.haptics = self.link = self.attach = self.holder = self.server = None
        self.error = None
        self.started_at = None
        self._cleanup_lock = threading.Lock()
        if config is not None:
            config.listeners.append(self._on_setting)

    # ------------------------------------------------------------------ ciclo de vida
    @property
    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.running:
            return
        self.error = None
        ready = threading.Event()
        self.thread = threading.Thread(target=self._thread_main, args=(ready,), name="puente", daemon=True)
        self.thread.start()
        ready.wait(10)

    def stop(self, timeout=10):
        if not self.running:
            return
        if self.loop and self._stop:
            self.loop.call_soon_threadsafe(self._stop.set)
        self.thread.join(timeout)

    def _thread_main(self, ready):
        # Este hilo atiende los URB de USB/IP, incluido el audio isócrono: necesita reloj de 1 ms y prioridad.
        rt.enable_high_resolution_timer()
        rt.raise_process_priority()
        rt.raise_thread_priority("USB/IP")
        try:
            asyncio.run(self._main(ready))
        except Exception as e:
            self.error = str(e)
            log.exception("the bridge stopped because of an error")
        finally:
            ready.set()
            self._cleanup()
            rt.disable_high_resolution_timer()

    async def _main(self, ready):
        self.loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        cfg = self.config
        self.link = BtLink(exclusive=True)
        speaker = mic = None
        if (cfg is None or cfg.get("audio_enabled")) and audio.opus_lib():
            speaker, mic = audio.SpeakerPath(), audio.MicPath()
        gain = cfg.get("haptics_gain") if cfg else 1.0
        self.haptics = HapticBridge(self.link, prefill_reports=1, gain=gain, speaker=speaker)
        self.device = VirtualDualSense(self.link, self.haptics, self.loop, mic=mic)
        if cfg is not None:
            self.device.lightbar = tuple(cfg.get("lightbar_color"))
            self.device.speaker_boost = cfg.get("speaker_boost")
            self.device.mic_allowed = cfg.get("mic_enabled")
        self.device.on_controller = self._on_controller
        self.device.start()
        self.haptics.start()
        self.server = UsbIpServer(self.device, port=self.port)
        await self.server.start()
        self.holder = ExclusiveHolder()
        self.attach = AttachManager(self.server, bt_connected=lambda: self.link.connected,
                                    hide=self.holder.hold, unhide=self.holder.release,
                                    needs_reattach=self._take_reattach, on_real_usb=self._on_real_usb)
        self.started_at = time.monotonic()
        ready.set()
        await self.attach.check()
        watcher = asyncio.create_task(self.attach.run())
        log.info("bridge started")
        try:
            await self._stop.wait()
        finally:
            watcher.cancel()
            await asyncio.to_thread(self._cleanup)
            self.device.stop()
            self.haptics.stop()
            self.server.server.close()
            log.info("bridge stopped")

    def _cleanup(self):
        """Deshace lo que el puente cambió en el sistema. Idempotente."""
        with self._cleanup_lock:
            if self.device is not None:
                self.device.restore_controller()
            if self.attach is not None and self.attach.port is not None:
                code, out = self.attach.usbip("detach", "-p", str(self.attach.port))
                log.info("virtual controller detached (%s)", out or code)
                self.attach.port = None
            if self.holder is not None:
                self.holder.release_all()
            if self.link is not None and self.link.connected:
                self.link.release_to_windows()

    # ------------------------------------------------------------------ controles conocidos y perfiles
    def _on_controller(self, mac, pid, firmware):
        """Se conectó un control (hilo lector): registrarlo, aplicar sus ajustes propios y su perfil."""
        self.registry.seen(mac, pid=pid, firmware=firmware)
        self.apply_controller_settings()
        profile = self.registry.profile_for(mac)
        model_pid = pid or BUILTIN_PID
        if profile is not None and model_pid != BUILTIN_PID:
            self.device.set_profile(profile)  # modelo distinto del incluido (p. ej. Edge): su perfil por cable
        else:
            self.device.set_profile(builtin_profile())
        if self.registry.needs_cable(mac):
            log.warning("this controller (%s) is a model that needs its cable profile: connect it by USB"
                        " once", self.registry.get(mac).get("model"))

    def _take_reattach(self):
        if self.device is not None and self.device.reattach_needed:
            self.device.reattach_needed = False
            return True
        return False

    def _on_real_usb(self, paths):
        """Hay un control real por cable (hilo del vigilante): capturar su perfil una vez por conexión."""
        for path in paths:
            if path in self._captured_paths:
                continue
            self._captured_paths.add(path)
            self.capture_state.update(running=True, result=None, mac=None)
            held = self.holder.devices.get(path) if self.holder else None
            dev = held
            try:
                if dev is None:
                    dev = WinHidDevice(path, exclusive=False)
                profile, identity = capture_profile(path, dev)
                mac = identity.get("mac")
                if not mac:
                    raise OSError("couldn't read the controller's MAC address")
                self.registry.save_profile(mac, profile, identity)
                log.info("cable profile saved: %s %s (firmware %s)", identity["model"], mac,
                         (identity.get("firmware") or {}).get("firmware"))
                self.capture_state.update(result="ok", mac=mac)
                if mac == self.device.controller_mac and identity.get("pid") != BUILTIN_PID:
                    self.device.set_profile(profile)
            except Exception as e:
                log.warning("couldn't capture the cable profile: %s", e)
                self.capture_state.update(result="error")
            finally:
                if dev is not None and dev is not held:
                    dev.close()
                self.capture_state.update(running=False, time=time.time())

    def apply_controller_settings(self):
        """Ajustes efectivos = los generales, reemplazados por los propios del control conectado."""
        if self.device is None:
            return
        own = {}
        entry = self.registry.get(self.device.controller_mac)
        if entry:
            own = entry.get("settings", {})
        cfg = self.config
        color = own.get("lightbar_color") or (cfg.get("lightbar_color") if cfg else None)
        gain = own.get("haptics_gain") or (cfg.get("haptics_gain") if cfg else None)
        if color:
            self.device.set_lightbar(color)
        if gain:
            self.haptics.gain = gain / 256

    def set_controller_setting(self, mac, key, value):
        self.registry.set_setting(mac, key, value)
        if self.running and self.device is not None and mac == self.device.controller_mac:
            self.apply_controller_settings()

    # ------------------------------------------------------------------ ajustes en vivo
    def _on_setting(self, key, value):
        if not self.running or self.device is None:
            return
        if key in ("haptics_gain", "lightbar_color"):
            self.apply_controller_settings()  # respeta los ajustes propios del control conectado
        elif key == "speaker_boost":
            self.device.apply_speaker_boost(value)
        elif key == "mic_enabled":
            self.device.set_mic_allowed(value)

    # ------------------------------------------------------------------ pruebas desde la interfaz
    def test_rumble(self, seconds=0.6):
        """Vibración corta para probar el control (no interfiere con un juego: dura menos de un segundo)."""
        if self.link is None or not self.link.connected:
            return False

        def run():
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                self.link.set_rumble(heavy=180, light=180)
                time.sleep(0.2)
            self.link.set_rumble(0, 0)

        threading.Thread(target=run, daemon=True).start()
        return True

    # ------------------------------------------------------------------ estado
    def state(self):
        if not self.running:
            return STATE_PROBLEM if self.error else STATE_STOPPED
        if self.device is None or self.attach is None:
            return STATE_STARTING
        if not devrestart.usbip_installed():
            return STATE_NEEDS_SETUP
        if self.attach.cable_mode:
            return STATE_CABLE
        if not self.link.connected or self.device.bt_idle:
            if self.link.blocked:
                return STATE_NEEDS_SETUP
            return STATE_WAITING
        if not self.attach.attached:
            return STATE_PROBLEM
        if self.device.game_active:
            return STATE_GAME
        return STATE_READY

    def status(self):
        d, h, a, link = self.device, self.haptics, self.attach, self.link
        out = {"state": self.state(), "error": self.error, "running": self.running}
        if d is None:
            return out
        battery = d.battery
        mac = d.controller_mac
        entry = self.registry.get(mac) if mac else None
        out["capture"] = dict(self.capture_state)
        out["profile"] = {"source": d.profile.source, "pid": f"{d.profile.pid:04X}"}
        out.update({
            "controller": {
                "mac": mac,
                "name": entry.get("name") if entry else None,
                "model": entry.get("model") if entry else None,
                "firmware": (d.controller_firmware or {}).get("firmware"),
                "needs_cable": self.registry.needs_cable(mac),
                "connected": bool(link and link.connected and not d.bt_idle),
                "exclusive": bool(link and link.connected and link.exclusive),
                "blocked": bool(link and link.blocked),
                "held": bool(link and link.held and not link.connected),
                "battery": battery[0] if battery else None,
                "battery_state": battery[1] if battery else None,
                "headphones": h.headphones if h else False,
                "mic_muted": d.mic_muted,
            },
            "virtual": {
                "attached": bool(a and a.attached),
                "mode": a.mode if a else None,
            },
            "game_active": d.game_active,
            "audio": {
                "haptics_streaming": bool(d.alt.get(1)),
                "mic_open": bool(d.alt.get(2)),
                "speaker_frames": h.speaker.encoded if h and h.speaker else 0,
            },
            "stats": {
                "bt_in": d.counters["bt_in"],
                "out_reports": d.counters["out_report"],
                "haptic_reports": h.stats["reportes"] if h else 0,
                "underruns": h.stats["underruns"] if h else 0,
                "reserve_ms": h.stats["reserva_ms"] if h else 0,
                "dropped_writes": link.dropped_writes if link else 0,
                "connections": d.counters["bt_conexiones"],
            },
        })
        return out
