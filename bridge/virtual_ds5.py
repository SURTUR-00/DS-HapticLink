"""DualSense USB virtual: responde como el control real por cable y lo puentea al control por Bluetooth.

Endpoints (iguales al control real):
  ep0          control: descriptores, SET_INTERFACE, peticiones HID y de audio (UAC1)
  ep1 OUT iso  audio 48 kHz 4 canales: 1-2 bocina/audífonos, 3-4 háptica  → HapticBridge
  ep2 IN  iso  micrófono 48 kHz 2 canales → por ahora silencio
  ep3 OUT int  reporte de salida 0x02 (LED, gatillos, rumble) → BT 0x31
  ep4 IN  int  reporte de entrada 0x01 ← BT 0x31
"""
import asyncio
import collections
import logging
import struct
import threading
import time

from . import descriptors as D
from .profiles import builtin_profile, firmware_from_info_report, mac_from_pairing_report
from .usbip import DIR_IN, EPIPE, DeviceInfo, UrbResult

log = logging.getLogger("ds5")

# Unidades de la topología de audio del control (ver CONFIG): 2 = feature unit de la bocina, 5 = del micrófono
FU_SPEAKER, FU_MIC = 0x02, 0x05
UAC_SET_CUR, UAC_GET_CUR, UAC_GET_MIN, UAC_GET_MAX, UAC_GET_RES = 0x01, 0x81, 0x82, 0x83, 0x84
FU_MUTE, FU_VOLUME = 0x01, 0x02
ISO_PACKET_SECONDS = 0.001  # HS con bInterval 4 → un paquete cada 1 ms
DEFAULT_LIGHTBAR = (0, 0, 128)  # azul: el color por defecto que pone el driver de Linux
PLAYER_1 = 0x04  # LED de jugador central bajo el touchpad

# Feature reports propios de cada control que se leen en vivo (mismo tamaño por BT y por USB):
# 0x05 calibración del giroscopio/acelerómetro, 0x09 y 0x0B MAC del control y del host, 0x20 firmware,
# 0x22 hardware/firmware.
LIVE_FEATURES = {0x05: 41, 0x09: 20, 0x0B: 42, 0x20: 64, 0x22: 64}

# Reporte de entrada en reposo: sticks centrados, gatillos sueltos, cruceta neutra (8), sin botones
NEUTRAL_INPUT = bytes([0x01, 0x80, 0x80, 0x80, 0x80, 0, 0, 0, 0x08]) + bytes(55)


def _hid_descriptor(config):
    i = config.find(bytes([9, 0x21]))
    return config[i:i + 9]


def _interfaces(config):
    """(clase, subclase, protocolo) de cada interfaz (alt 0) del descriptor de configuración, para USB/IP."""
    out, i = [], 0
    while i + 9 <= len(config) and config[i] > 0:
        if config[i + 1] == 4 and config[i + 3] == 0:
            out.append((config[i + 5], config[i + 6], config[i + 7]))
        i += config[i]
    return out


def _device_info(profile):
    return DeviceInfo(busid="1-1", vid=0x054C, pid=profile.pid, bcd_device=profile.bcd_device, dev_class=0,
                      dev_subclass=0, dev_protocol=0, configuration_value=1, num_configurations=1,
                      interfaces=_interfaces(profile.config))


# Feature reports que un juego puede mandarle al DualSense "por cable" pero que no deben llegar al control real
# por Bluetooth.
BLOCKED_FEATURES = {
    # Probado (God of War Ragnarök, 2026-10-13): el juego lo manda al abrirse (08 02 00…) y por BT el control se
    # apaga en el acto (había que presionar PS de nuevo). Por cable no hace nada visible.
    0x08: "Bluetooth/power control (it turns the controller off over Bluetooth)",
    # Precaución: es el reporte de emparejamiento; escribirlo emparejaría el control con otro equipo.
    0x0A: "pairing info (it would unpair the controller from this PC)",
}
# Offsets en el reporte de salida USB 0x02 (id incluido), como en el driver de Linux (hid-playstation)
OUT_VALID_FLAG1 = 2
OUT_LIGHTBAR_CONTROL = 0x04  # valid_flag1 bit 2: el reporte trae color de la barra de luz
OUT_LIGHTBAR_RGB = slice(45, 48)


class VirtualDualSense:
    def __init__(self, ds, haptics, loop, mic=None):
        """mic: audio.MicPath para el micrófono del control (o None: el micrófono USB entrega silencio)."""
        self.ds = ds
        self.haptics = haptics
        self.loop = loop
        self.mic = mic
        self.status_byte = None  # byte de estado del control: bit 0 = audífonos conectados
        # Por BT el firmware no alterna el silencio al presionar el botón de mute (por cable sí, y en PS5 lo
        # hace la consola): el puente hace ese papel.
        self.mic_muted = False
        self._mute_button = False
        self.bt_idle = False  # no llegan datos del control (apagado o fuera de rango)
        self.battery = None  # (porcentaje, "descargando" | "cargando" | "cargado" | "error")
        self.lightbar = DEFAULT_LIGHTBAR  # color cuando ningún juego controla la barra (ajustable)
        self.speaker_boost = 0  # refuerzo de la bocina 0-7 (ajustable)
        self.mic_allowed = True  # el usuario permite usar el micrófono del control (ajustable)
        self.last_game_output = 0.0  # cuándo mandó un juego/app su último reporte de salida
        self._game_was_active = False
        self._game_lightbar = None  # último color de barra que mandó el juego
        self._last_rx = time.monotonic()
        self._extended = False  # el control ya está mandando reportes extendidos 0x31
        self._last_mode_request = 0.0
        self.profile = builtin_profile()  # con qué descriptores se presenta el DualSense virtual
        self.info = _device_info(self.profile)
        self.reattach_needed = False  # cambió el perfil: hay que reconectar el virtual para que Windows lo relea
        self.on_controller = None  # callback(mac, pid, firmware) al conectarse un control
        self.controller_mac = None
        self.controller_firmware = None
        self._live = {}  # feature reports leídos en vivo del control conectado
        self.alt = {1: 0, 2: 0}
        self.volume = {FU_SPEAKER: 0, FU_MIC: 48 * 256}  # en 1/256 dB
        self.mute = {FU_SPEAKER: 0, FU_MIC: 0}
        self.features = dict(self.profile.features)
        # Entrada: último reporte y URBs esperando uno nuevo
        self.latest_input = NEUTRAL_INPUT
        self.input_fresh = False
        self.input_waiters = collections.deque()
        self.iso_clock = {}
        self.counters = collections.Counter()
        self.reader = threading.Thread(target=self._bt_reader, name="bt-reader", daemon=True)
        self.running = False

    # ---------------------------------------------------------------- arranque / Bluetooth
    def start(self):
        # Los feature reports (calibración, MAC, firmware) son los capturados por USB del control real:
        # son los bytes exactos que mandaría por cable. El hilo lector se encarga de conectar el control BT.
        self.running = True
        self.reader.start()

    def _prepare_controller(self):
        """Se llama en cada conexión del control BT.
        Por BT el firmware retiene la barra de luz (indicador de conexión) e ignora los colores que mande
        el juego hasta que se libera. La liberación la apaga, así que luego se pone un color por defecto.
        Mismos pasos que hace el driver de Linux al conectar."""
        self._load_live_features()
        self.ds.release_lightbar()
        time.sleep(0.2)  # dejar que termine el desvanecimiento antes de fijar el color
        self.ds.set_lights(tuple(self.lightbar), player=PLAYER_1)
        # Audio: micrófono automático con cancelación de ruido, los volúmenes que tenga Windows, y volver a
        # encender el micrófono si el host ya lo tenía abierto (si no, tras reconectar quedaría mudo).
        self.ds.set_audio_control()
        self.apply_speaker_boost(self.speaker_boost)
        self._apply_volume()
        self.ds.set_mute_light(self.mic_muted)  # conservar el estado del mute tras reconectar
        self.status_byte = None
        if self.alt.get(2) and self.mic_allowed:
            self.ds.send_mic_status(True)

    def _load_live_features(self):
        """Los datos propios de cada control (calibración de sensores, MAC, firmware, hardware) se leen del
        control conectado, para que funcione con cualquier DualSense y no con los del control de las capturas.
        Por BT el reporte trae el mismo contenido que por USB, salvo los últimos 4 bytes (CRC por BT, ceros
        por USB: verificado con las capturas), que se ponen en cero."""
        self._live = {}
        for rid, size in LIVE_FEATURES.items():
            data = self.ds.get_feature(rid, size)  # None si falla (BtLink no lanza excepciones)
            if data and len(data) >= size and data[0] == rid:
                data = bytearray(data[:size])
                data[-4:] = bytes(4)
                self._live[rid] = bytes(data)
        self.features.update(self._live)
        if self._live:
            log.info("controller-specific data read live: %s", " ".join(f"0x{r:02X}" for r in self._live))
        else:
            log.warning("couldn't read the controller-specific data; using fallback data")
        # Identidad: para "Mis controles" (y elegir el perfil de ese control)
        self.controller_mac = mac_from_pairing_report(self._live.get(0x09))
        self.controller_firmware = firmware_from_info_report(self._live.get(0x20))
        if self.on_controller is not None and self.controller_mac:
            pid = (self.ds.info or {}).get("product_id")
            try:
                self.on_controller(self.controller_mac, pid, self.controller_firmware)
            except Exception:
                log.exception("error registering the controller")

    def set_profile(self, profile):
        """Cambia los descriptores con los que se presenta el DualSense virtual (p. ej. el perfil capturado por
        cable de un DualSense Edge). Windows solo los lee al conectarse, así que hay que reconectar el virtual."""
        if profile.to_json() == self.profile.to_json():
            return
        log.info("virtual controller profile: %s (PID %04X)", profile.source, profile.pid)
        self.profile = profile
        self.info = _device_info(profile)
        self.features = dict(profile.features)
        self.features.update(self._live)  # lo leído en vivo del control conectado manda
        self.reattach_needed = True

    def _apply_volume(self):
        """Volumen y silencio que Windows fija en el audio USB → el control (como DS5Dongle)."""
        speaker_db = self.volume[FU_SPEAKER] / 256  # -100..0 dB
        mic_db = self.volume[FU_MIC] / 256  # 0..48 dB
        level = max(0, min(100, int(round(100 + speaker_db))))
        settings = (level, max(0, int(mic_db)), bool(self.mute[FU_SPEAKER]),
                    bool(self.mute[FU_MIC]) or self.mic_muted)
        if settings != getattr(self, "_last_volume", None):
            self._last_volume = settings
            log.info("Windows volume → speaker/jack %d%%%s | microphone %d dB%s", level,
                     " (muted)" if settings[2] else "", settings[1], " (muted)" if settings[3] else "")
        self.ds.set_volumes(headphones=level, speaker=level, mic=settings[1],
                            speaker_mute=settings[2], mic_mute=settings[3])

    def restore_controller(self):
        """Deja el control como sin el puente: gatillos libres y barra de luz en el color por defecto.
        Si no, se quedaría con el último estado del juego (o con la barra apagada) hasta reconectarlo.
        Sin control conectado, BtLink descarta las escrituras en silencio."""
        self.ds.set_triggers()
        self.ds.set_lights(tuple(self.lightbar), player=PLAYER_1)

    # ---------------------------------------------------------------- ajustes en vivo (desde la interfaz)
    @property
    def game_active(self):
        """Un juego o app está controlando el DualSense (mandó un reporte de salida hace poco)."""
        return time.monotonic() - self.last_game_output < 3.0

    def _check_game_ended(self):
        """Al cerrarse, los juegos "limpian" el control dejando la barra y los LED de jugador apagados: parece
        desconectado aunque siga funcionando (probado con God of War). Cuando el juego deja de mandar reportes
        y la última barra que pidió era negra, se vuelve al color del usuario. Si dejó un color, se respeta
        (hay juegos que solo mandan reportes cuando algo cambia)."""
        if self._game_was_active and not self.game_active:
            self._game_was_active = False
            if self._game_lightbar in (None, (0, 0, 0)):
                log.info("the game stopped and left the lights off: restoring the light bar color")
                self.ds.set_lights(tuple(self.lightbar), player=PLAYER_1)

    def set_lightbar(self, rgb):
        """Color de la barra cuando ningún juego la controla. Si un juego la está usando, no se pisa."""
        self.lightbar = tuple(rgb)
        if not self.game_active:
            self.ds.set_lights(self.lightbar, player=PLAYER_1)

    def set_mic_allowed(self, allowed):
        """Ajuste del usuario: si está apagado, el micrófono del control no se enciende aunque Windows lo abra."""
        self.mic_allowed = bool(allowed)
        self._set_mic_stream(bool(self.alt.get(2)))

    def _set_mic_stream(self, host_open):
        on = host_open and self.mic_allowed
        self.haptics.mic_enabled = on
        if self.mic is not None:
            self.mic.reset()
        self.ds.send_mic_status(on)

    def apply_speaker_boost(self, boost):
        """Refuerzo de volumen propio de la bocina del control (SpeakerCompPreGain, 0-7)."""
        self.speaker_boost = max(0, min(7, int(boost)))
        self.ds.set_state(flags1=0x80, extra={37: self.speaker_boost})  # AllowAudioControl2

    def stop(self):
        self.running = False
        if hasattr(self.ds, "stopping"):
            self.ds.stopping = True  # cortar un intento de tomar el control en curso (dura hasta 15 s)

    def _bt_reader(self):
        waiting_logged = False
        while self.running:
            if not self.ds.connected:
                if not self.ds.connect():
                    if not waiting_logged:
                        log.warning("waiting for the DualSense over Bluetooth... (turn it on with the PS button)")
                        waiting_logged = True
                    time.sleep(1.0)
                    continue
                waiting_logged = False
                self.counters["bt_conexiones"] += 1
                log.info("DualSense connected over Bluetooth")
                self._prepare_controller()
                self._last_rx = time.monotonic()
                self._extended = False
            try:
                r = self.ds.read(100)
            except OSError as e:
                log.warning("lost the connection with the controller (%s); the game keeps the virtual controller"
                            " and it will reconnect by itself", e)
                self.ds.disconnect()
                self._went_silent()
                continue
            if r:
                self._handle_report(r)
            else:
                self._check_idle(time.monotonic())

    # Con el acceso exclusivo, apagar el control NO cierra el dispositivo: Windows lo mantiene registrado y
    # nuestro handle sigue siendo válido; simplemente dejan de llegar datos, y al encenderlo vuelven a llegar
    # (en modo básico). Por eso el apagado y la reconexión se detectan por los datos, no por errores.
    IDLE_SECONDS = 2.0

    def _went_silent(self):
        """El control se apagó o salió de rango: soltar todo y vaciar el audio."""
        self.haptics.reset()
        # Soltar todo: si no, el juego vería presionado lo que estuviera presionado al desconectarse.
        self.loop.call_soon_threadsafe(self._new_input, NEUTRAL_INPUT)

    def _check_idle(self, now):
        if not self.bt_idle and now - self._last_rx > self.IDLE_SECONDS:
            self.bt_idle = True
            log.warning("no data from the controller (turned off or out of range?); waiting for it to come back")
            self._went_silent()

    def _handle_report(self, r):
        now = time.monotonic()
        self._last_rx = now
        self.counters["bt_raw"] += 1
        self._check_game_ended()
        if self.bt_idle:
            self.bt_idle = False
            log.info("the controller is back")
        if r[0] == 0x01:
            # Modo básico (reporte corto sin sensores). Si ya estaba en modo extendido, el control se reinició:
            # se reconectó tras apagarlo, y volvió a su estado de fábrica (LED, audio, micrófono...).
            self.counters["bt_basico"] += 1
            if self._extended:
                self._extended = False
                self.counters["bt_conexiones"] += 1
                log.info("the controller reconnected: preparing it again")
                self._prepare_controller()
            # Leer el feature 0x05 (calibración) es lo que lo hace pasar al modo extendido 0x31 (como en Linux).
            if now - self._last_mode_request > 1.0:
                self._last_mode_request = now
                self.ds.get_feature(0x05, 41)
                log.info("the controller was in basic mode; requested extended mode")
            return
        if r[0] != 0x31 or len(r) < 65:
            return
        self._extended = True
        if r[1] & 0x02:  # el control indica que este reporte trae audio del micrófono, no entradas
            self.counters["mic_bt"] += 1
            if self.mic is not None:
                self.mic.on_bt_frame(bytes(r[3:]))
            return
        self._check_battery(r[54])
        self._check_status(r[55])
        self._check_mute_button(bool(r[11] & 0x04))
        report = bytes([0x01]) + bytes(r[2:65])
        self.loop.call_soon_threadsafe(self._new_input, report)

    BATTERY_STATES = {0x0: "descargando", 0x1: "cargando", 0x2: "cargado"}

    def _check_battery(self, value):
        """Nivel (0-10, bits 0-3) y estado de carga (bits 4-7). Igual que el driver de Linux: nivel × 10 + 5."""
        level = min(100, (value & 0x0F) * 10 + 5)
        state = self.BATTERY_STATES.get(value >> 4, "error")
        if state == "cargado":
            level = 100
        self.battery = (level, state)

    def _check_status(self, status):
        """Audífonos conectados al jack del control (bit 0 del byte de estado)."""
        previous, self.status_byte = self.status_byte, status
        if previous is not None and previous == status:
            return
        headphones = bool(status & 0x01)
        if previous is None or bool(previous & 0x01) != headphones:
            self.haptics.headphones = headphones
            log.info("controller audio → %s", "headphones (jack)" if headphones else "speaker")

    def _check_mute_button(self, pressed):
        """Cada pulsación del botón de mute alterna el silencio del micrófono y su LED (al presionar, no al soltar)."""
        if pressed and not self._mute_button:
            self.mic_muted = not self.mic_muted
            log.info("microphone %s (mute button)", "muted" if self.mic_muted else "unmuted")
            self.ds.set_mute_light(self.mic_muted)
            self._apply_volume()
        self._mute_button = pressed

    def _new_input(self, report):
        self.latest_input = report
        self.counters["bt_in"] += 1
        while self.input_waiters:
            fut = self.input_waiters.popleft()
            if not fut.done():
                fut.set_result(report)
                return
        self.input_fresh = True

    def on_detach(self):
        self.alt = {1: 0, 2: 0}
        self.haptics.reset()

    # ---------------------------------------------------------------- despacho de URBs
    async def handle_urb(self, urb):
        if urb.ep == 0:
            return await self._control(urb)
        if urb.ep == 4 and urb.direction == DIR_IN:
            return await self._interrupt_in(urb)
        if urb.ep == 3:
            return await self._interrupt_out(urb)
        if urb.ep == 1:
            return await self._iso_out(urb)
        if urb.ep == 2:
            return await self._iso_in(urb)
        log.warning("URB for unknown endpoint %d", urb.ep)
        return UrbResult(status=EPIPE)

    async def _interrupt_in(self, urb):
        self.counters["int_in"] += 1
        if self.input_fresh:
            self.input_fresh = False
            report = self.latest_input
        else:
            fut = self.loop.create_future()
            self.input_waiters.append(fut)
            try:
                report = await fut
            finally:
                if fut in self.input_waiters:
                    self.input_waiters.remove(fut)
        return UrbResult(data=report[:urb.buffer_length])

    async def _interrupt_out(self, urb):
        self._output_report(urb.data)
        return UrbResult()

    def _output_report(self, data):
        self.counters["out_report"] += 1
        if self.counters["out_report"] == 1:
            log.info("first output report from an application: %s", bytes(data[:12]).hex(" "))
        if data and data[0] == 0x02 and len(data) >= 48:
            if any(data[1:48]):  # Windows manda reportes vacíos al enumerar; eso no es un juego
                self.last_game_output = time.monotonic()
                self._game_was_active = True
            if data[OUT_VALID_FLAG1] & OUT_LIGHTBAR_CONTROL:
                self._game_lightbar = tuple(data[OUT_LIGHTBAR_RGB])
            try:
                self.ds.send_usb_output(data)
            except OSError as e:
                log.warning("couldn't forward the output report: %s", e)
        else:
            log.debug("output report ignored: %s", bytes(data[:8]).hex())

    async def _pace_iso(self, ep, packets):
        """Completa los URB isócronos al ritmo real (1 ms por paquete), como lo haría el hardware."""
        now = time.perf_counter()
        start = max(now, self.iso_clock.get(ep, now))
        end = start + packets * ISO_PACKET_SECONDS
        self.iso_clock[ep] = end
        await asyncio.sleep(max(0.0, end - now))
        if ep == 1:  # diagnóstico de microcortes: cuánto se pasó la pausa del momento que tocaba
            timing = getattr(self.haptics, "timing", None)
            if timing is not None:
                timing["iso_late"] = max(timing["iso_late"], (time.perf_counter() - end) * 1000)
        return int(start * 1000) & 0x7FF  # número de frame USB (11 bits)

    async def _iso_out(self, urb):
        self.counters["iso_out"] += 1
        packets = urb.iso or []
        if self.alt.get(1):
            pcm = b"".join(urb.data[p.offset:p.offset + p.length] for p in packets)
            self.haptics.feed_usb_audio(pcm)
        for p in packets:
            p.actual_length, p.status = p.length, 0
        frame = await self._pace_iso(1, len(packets))
        return UrbResult(actual_length=sum(p.length for p in packets), iso=packets, start_frame=frame)

    async def _iso_in(self, urb):
        """Micrófono: 48 frames estéreo int16 por ms = 192 bytes por paquete (silencio si no hay audio)."""
        self.counters["iso_in"] += 1
        packets = urb.iso or []
        data = bytearray(urb.buffer_length)
        for p in packets:
            n = min(p.length, 192)
            if self.mic is not None and self.alt.get(2) and self.mic_allowed:
                chunk = self.mic.read(n)  # se lee siempre, para no acumular audio viejo mientras está silenciado
                if not self.mic_muted:
                    data[p.offset:p.offset + n] = chunk
            p.actual_length, p.status = n, 0
        frame = await self._pace_iso(2, len(packets))
        return UrbResult(data=bytes(data), iso=packets, start_frame=frame)

    # ---------------------------------------------------------------- endpoint 0
    async def _control(self, urb):
        bm, req, value, index, length = struct.unpack("<BBHHH", urb.setup)
        kind, recipient = (bm >> 5) & 3, bm & 0x1F
        try:
            if kind == 0:
                data = self._standard(bm, req, value, index, urb)
            elif kind == 1 and recipient == 1 and (index & 0xFF) == 3:
                data = self._hid_class(bm, req, value, urb)
            elif kind == 1:
                data = self._audio_class(bm, req, value, index, urb)
            else:
                data = None
        except Exception:
            log.exception("error in control request %s", urb.setup.hex())
            data = None
        if data is None:
            log.debug("STALL control bm=%02X req=%02X val=%04X idx=%04X len=%d", bm, req, value, index, length)
            return UrbResult(status=EPIPE)
        if bm & 0x80:
            return UrbResult(data=bytes(data[:length]))
        return UrbResult(actual_length=len(urb.data))

    def _standard(self, bm, req, value, index, urb):
        if req == 0x06:  # GET_DESCRIPTOR
            dtype, didx = value >> 8, value & 0xFF
            if bm == 0x80:
                if dtype == 1:
                    return self.profile.device
                if dtype == 2:
                    return self.profile.config
                if dtype == 3:
                    if didx == 0:
                        return struct.pack("<BBH", 4, 3, D.LANGID)
                    s = self.profile.strings.get(didx)
                    if s is None:
                        return None
                    enc = s.encode("utf-16-le")
                    return bytes([2 + len(enc), 3]) + enc
                if dtype == 6:
                    return D.DEVICE_QUALIFIER
                return None
            if bm == 0x81 and (index & 0xFF) == 3:  # descriptores de clase de la interfaz HID
                if dtype == 0x22:
                    return self.profile.hid_report
                if dtype == 0x21:
                    return _hid_descriptor(self.profile.config)
            return None
        if req == 0x00:  # GET_STATUS
            return b"\x00\x00"
        if req == 0x08:  # GET_CONFIGURATION
            return b"\x01"
        if req == 0x0A:  # GET_INTERFACE
            return bytes([self.alt.get(index & 0xFF, 0)])
        if req == 0x09:  # SET_CONFIGURATION
            log.info("SET_CONFIGURATION %d", value)
            return b""
        if req == 0x0B:  # SET_INTERFACE
            itf, alt = index & 0xFF, value
            self.alt[itf] = alt
            log.info("audio interface %d → alt %d (%s)", itf, alt, "open" if alt else "closed")
            if itf == 1 and alt == 0:
                self.haptics.reset()
            if itf == 2:
                self._set_mic_stream(bool(alt))
            return b""
        if req in (0x01, 0x03):  # CLEAR_FEATURE / SET_FEATURE
            return b""
        return None

    def _hid_class(self, bm, req, value, urb):
        rtype, rid = value >> 8, value & 0xFF
        if req == 0x0A:  # SET_IDLE
            return b""
        if req == 0x01 and rtype == 3:  # GET_REPORT feature
            self.counters[f"get_feature_{rid:02X}"] += 1
            log.debug("GET_REPORT feature 0x%02X", rid)
            data = self.features.get(rid)
            if data is None:
                log.info("GET_REPORT feature 0x%02X has no data → STALL", rid)
            return data
        if req == 0x01 and rtype == 1:  # GET_REPORT input
            return self.latest_input
        if req == 0x09 and rtype == 2:  # SET_REPORT output (por control en vez de interrupt)
            self._output_report(urb.data)
            return b""
        if req == 0x09 and rtype == 3:  # SET_REPORT feature → reenviar al control BT
            log.info("SET_REPORT feature 0x%02X (%d bytes): %s", rid, len(urb.data), bytes(urb.data[:16]).hex())
            if rid in BLOCKED_FEATURES:
                # Se responde OK al juego (por cable el comando no tendría efecto visible), pero no se manda al
                # control: por Bluetooth lo apagaría o lo desemparejaría del PC.
                log.info("  not forwarded to the controller: %s", BLOCKED_FEATURES[rid])
                return b""
            # El reporte BT mide lo mismo que el USB (id incluido); el CRC ocupa sus últimos 4 bytes.
            payload = bytes(urb.data[1:]) if urb.data[:1] == bytes([rid]) else bytes(urb.data)
            self.ds.set_feature(rid, payload, len(payload) + 1)
            return b""
        return None

    def _audio_class(self, bm, req, value, index, urb):
        selector, entity = value >> 8, index >> 8
        recipient = bm & 0x1F
        if recipient == 2:  # endpoint: frecuencia de muestreo (solo 48 kHz)
            if req == UAC_SET_CUR:
                return b""
            if req == UAC_GET_CUR:
                return (48000).to_bytes(3, "little")
            return None
        if entity not in (FU_SPEAKER, FU_MIC):
            return None
        if selector == FU_MUTE:
            if req == UAC_SET_CUR:
                self.mute[entity] = urb.data[0] if urb.data else 0
                self._apply_volume()
                return b""
            if req == UAC_GET_CUR:
                return bytes([self.mute[entity]])
            return None
        if selector == FU_VOLUME:
            # Rangos copiados de DS5Dongle: bocina -100..0 dB en pasos de 1 dB; mic 0..48 dB
            ranges = {FU_SPEAKER: (-100 * 256, 0, 256), FU_MIC: (0, 48 * 256, 0x7A)}[entity]
            if req == UAC_SET_CUR:
                if len(urb.data) >= 2:
                    self.volume[entity] = struct.unpack("<h", urb.data[:2])[0]
                    self._apply_volume()
                return b""
            values = {UAC_GET_CUR: self.volume[entity], UAC_GET_MIN: ranges[0], UAC_GET_MAX: ranges[1],
                      UAC_GET_RES: ranges[2]}
            if req in values:
                return struct.pack("<h", values[req])
        return None
