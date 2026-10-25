"""Conexión con el DualSense Bluetooth que sobrevive a desconexiones.

El control puede desaparecer en cualquier momento (se apaga, se queda sin batería, sale de rango, o se cambia
entre cable y Bluetooth). Windows lo vuelve a registrar con otro identificador, así que hay que buscarlo
y abrirlo de nuevo. Mientras no hay control, las escrituras se descartan en silencio en vez de fallar.
"""
import logging
import threading
import time

from . import devrestart
from .ds5bt import DualSenseBT, find_bt_dualsense
from .winhid import ExclusiveAccessError

log = logging.getLogger("bt")

RESTART_COOLDOWN = 10  # segundos mínimos entre reinicios del control (si algo falla, no reiniciar en bucle)
MAX_COOLDOWN = 60  # tras fallos seguidos la espera se duplica hasta este tope (cada intento hace parpadear a Steam)
# Cuánto tiempo intentar abrirlo tras pedir el reinicio. Debe cubrir el arranque de la tarea Y toda la consulta de
# retiro: Steam (aunque tenga desactivado el soporte de PlayStation) tiene el control abierto, tarda ~4-5 s en
# soltarlo y lo vuelve a abrir enseguida; el hueco en que nadie lo tiene puede llegar casi al final. Probado en
# 0.4.1: con 5 s el puente se rendía antes de que Steam lo soltara.
GRAB_SECONDS = 15


class BtLink:
    def __init__(self, on_connect=None, exclusive=True):
        """on_connect(info) se llama cada vez que se abre el control.
        exclusive: abrir el control en exclusiva para que los juegos solo puedan usar el DualSense virtual."""
        self.ds = None
        self.info = None
        self.on_connect = on_connect
        self.exclusive = exclusive
        self.lock = threading.Lock()
        self.dropped_writes = 0
        self.blocked = False  # falta la tarea de reinicio: no hay forma de liberarlo (instalación incompleta)
        self.held = False  # se reinició pero otro programa (Steam) lo volvió a abrir antes que el puente
        self.stopping = False  # el puente se está cerrando: no seguir intentando tomar el control
        self._last_restart = -MAX_COOLDOWN
        self._cooldown = RESTART_COOLDOWN

    @property
    def connected(self):
        return self.ds is not None

    def connect(self):
        """Busca y abre el control. Devuelve True si quedó conectado."""
        with self.lock:
            if self.ds is not None:
                return True
            info = find_bt_dualsense()
            if not info:
                return False
            try:
                ds = DualSenseBT(info, exclusive=self.exclusive)
            except ExclusiveAccessError:
                ds = self._restart_and_grab(info)
                if ds is None:
                    return False
            except OSError as e:
                log.debug("couldn't open the controller: %s", e)
                return False
            if self.blocked or self.held:
                log.info("the controller became free and was taken exclusively")
            self.blocked = self.held = False
            try:
                ds.get_feature(0x05, 41)  # activa el modo de reporte extendido 0x31
            except OSError:
                pass
            self.ds, self.info = ds, info
        if self.on_connect:
            try:
                self.on_connect(info)
            except Exception:
                log.exception("error preparing the newly connected controller")
        return True

    def _restart_and_grab(self, info):
        """Windows (dwm, GameInput, explorer, Game Bar) o Steam tienen abierto el control: reiniciarlo y abrirlo
        en exclusiva en cuanto todos lo suelten (durante la consulta de retiro o al reaparecer), antes de que lo
        vuelvan a abrir. Devuelve el DualSenseBT o None."""
        now = time.monotonic()
        if self.stopping or now - self._last_restart < self._cooldown:
            return None
        self._last_restart = now
        instance = devrestart.instance_id_from_hid_path(info["path"].decode())
        how = devrestart.request_restart([instance])
        if how is None:
            if not self.blocked:
                log.warning("Windows has the DualSense open and it can't be taken exclusively. Run the initial setup"
                            " once (it asks for administrator permission a single time) so the bridge can free it"
                            " by itself; or turn the controller off and on while the bridge is running.")
                self.blocked = True
            return None
        start = time.monotonic()
        deadline = start + GRAB_SECONDS
        while time.monotonic() < deadline and not self.stopping:
            try:
                ds = DualSenseBT(info, exclusive=True)
                log.info("controller restarted (%s) and taken exclusively after %.1f s", how,
                         time.monotonic() - start)
                self.blocked = self.held = False
                self._cooldown = RESTART_COOLDOWN
                return ds
            except ExclusiveAccessError:
                pass  # alguien de Windows ganó esta vez: se reintentará tras el enfriamiento
            except OSError:
                pass  # aún no reaparece tras el reinicio
            time.sleep(0.001)
        if self.stopping:
            return None
        if self.held:
            self._cooldown = min(self._cooldown * 2, MAX_COOLDOWN)
        else:
            log.warning("couldn't take the controller after restarting it: another program keeps reopening it"
                        " (Steam does). Retrying with increasing pauses; closing Steam frees it right away.")
        log.info("next attempt in %d s", self._cooldown)
        self.held = True
        return None

    def release_to_windows(self):
        """Al salir: soltar el control. Los juegos que lo abren por su cuenta lo ven de inmediato; GameInput y la
        Game Bar solo lo abren cuando el control se conecta (reiniciar el dispositivo no basta: probado), así
        que para ellos hay que apagarlo y encenderlo."""
        was_exclusive = self.exclusive and self.ds is not None
        self.disconnect()
        if was_exclusive:
            log.info("controller released. If Game Bar or another Windows app doesn't see it, turn it off and on.")

    def disconnect(self):
        with self.lock:
            ds, self.ds = self.ds, None
        if ds is not None:
            try:
                ds.close()
            except Exception:
                pass

    def read(self, timeout_ms=100):
        """Lee un reporte. Lanza OSError si el control se desconectó (lo usa el hilo lector para detectarlo)."""
        ds = self.ds
        if ds is None:
            raise OSError("controller not connected")
        try:
            return ds.read(timeout_ms)
        except (ValueError, OSError) as e:
            raise OSError(str(e)) from e

    def __getattr__(self, name):
        """Delegar el resto de métodos (send_usb_output, send_haptics_39, set_lights, get_feature...) al control
        actual. Sin control, o si falla la escritura, se devuelve None y se cuenta como descartada."""
        method = getattr(DualSenseBT, name, None)
        if method is None or not callable(method):
            raise AttributeError(name)

        def call(*args, **kwargs):
            ds = self.ds
            if ds is None:
                self.dropped_writes += 1
                return None
            try:
                return getattr(ds, name)(*args, **kwargs)
            except Exception:
                self.dropped_writes += 1
                return None

        return call
