"""Protocolo del DualSense por Bluetooth (Windows, vía hidapi).

Referencias: DS5Dongle (awalol), SAxense (egormanga), driver hid-playstation de Linux.
  - 0x31: reporte de estado (LED, gatillos, rumble), 78 bytes con CRC32.
  - 0x32: reporte de audio con háptica: PCM estéreo int8 a 3000 Hz, 64 bytes (32 frames) por reporte.
"""
import math
import struct
import threading
import time
import zlib

import hid

SONY_VID = 0x054C
DUALSENSE_PIDS = (0x0CE6, 0x0DF2)
BUS_BLUETOOTH = 2

HAPTIC_RATE = 3000
HAPTIC_CHUNK = 64  # bytes por reporte = 32 frames estéreo int8
SPEAKER_OPUS_BYTES = 200  # una trama Opus CBR de 10 ms para la bocina/audífonos
CHUNK_PERIOD = HAPTIC_CHUNK / 2 / HAPTIC_RATE  # 10.67 ms

# Banderas de SetStateData (47 bytes que van después de [id, seq, tag] en el 0x31)
FLAGS0_RUMBLE_EMULATION = 0x01
FLAGS0_RUMBLE_NOT_HAPTICS = 0x02
FLAGS0_RIGHT_TRIGGER = 0x04
FLAGS0_LEFT_TRIGGER = 0x08
FLAGS0_HEADPHONE_VOLUME = 0x10
FLAGS0_SPEAKER_VOLUME = 0x20
FLAGS0_MIC_VOLUME = 0x40
FLAGS0_AUDIO_CONTROL = 0x80
FLAGS1_MUTE_LIGHT = 0x01
FLAGS1_AUDIO_MUTE = 0x02
FLAGS1_LED_COLOR = 0x04
FLAGS1_RESET_LIGHTS = 0x08
FLAGS1_PLAYER_LEDS = 0x10
FLAGS2_BRIGHTNESS = 0x01
FLAGS2_LIGHTBAR_SETUP = 0x02
LIGHTBAR_SETUP_LIGHT_OUT = 0x02  # suelta la barra de luz del control del firmware

# Modos simples de gatillo
TRIGGER_OFF = [0x05] + [0] * 10


def trigger_resistance(start, force):
    return [0x01, start, force] + [0] * 8


def trigger_section(start, end, force):
    return [0x02, start, end, force] + [0] * 7


def trigger_vibration(start, amplitude, frequency):
    return [0x06, frequency, amplitude, start] + [0] * 7


def bt_crc(data: bytes, header: int = 0xA2) -> bytes:
    """CRC32 de BT: incluye el byte de cabecera HID que Windows no nos deja ver.
    0xA2 = DATA|Output (reportes de salida), 0x53 = SET_REPORT|Feature."""
    return struct.pack("<I", zlib.crc32(bytes([header]) + data) & 0xFFFFFFFF)


def find_bt_dualsense():
    for d in hid.enumerate(SONY_VID):
        if d["product_id"] in DUALSENSE_PIDS and d.get("bus_type") == BUS_BLUETOOTH:
            return d
    return None


class DualSenseBT:
    def __init__(self, info, exclusive=False):
        """exclusive=True: abrir el control en exclusiva (winhid) para que ningún otro programa pueda usarlo.
        Lanza winhid.ExclusiveAccessError si otro programa ya lo tiene abierto."""
        if exclusive:
            from .winhid import WinHidDevice
            self.dev = WinHidDevice(info["path"].decode(), exclusive=True)
        else:
            self.dev = hid.device()
            self.dev.open_path(info["path"])
        self.seq = 0
        self.haptic_counter = 0
        # hidapi en Windows reutiliza una sola estructura OVERLAPPED por dispositivo: las escrituras
        # (y los feature reports) desde varios hilos deben ir serializadas.
        self.lock = threading.Lock()

    def close(self):
        self.dev.close()

    def _next_seq(self):
        s = self.seq
        self.seq = (self.seq + 1) & 0x0F
        return s << 4

    def write_report(self, report_id: int, body, size: int):
        """Construye un reporte BT de `size` bytes: [id] + body + relleno + CRC32. Devuelve (bytes, ms).
        `body` puede ser bytes o una función seq -> bytes (la secuencia se asigna dentro del candado)."""
        with self.lock:
            if callable(body):
                body = body(self._next_seq())
            buf = bytearray(size)
            buf[0] = report_id
            buf[1:1 + len(body)] = body
            buf[-4:] = bt_crc(bytes(buf[:-4]))
            t0 = time.perf_counter()
            n = self.dev.write(bytes(buf))
            if n < 0:
                raise OSError(f"write failed: {self.dev.error()}")
            return n, (time.perf_counter() - t0) * 1000

    def get_feature(self, report_id: int, size: int) -> bytes:
        with self.lock:
            return bytes(self.dev.get_feature_report(report_id, size))

    def set_feature(self, report_id: int, payload: bytes, size: int):
        """SET_REPORT de feature por BT: [id] + payload + CRC32 (semilla 0x53) en los últimos 4 bytes.
        `size` es el tamaño total del reporte; el CRC pisa los últimos 4 bytes del payload (como DS5Dongle)."""
        buf = bytearray(size)
        buf[0] = report_id
        data = payload[:size - 5]
        buf[1:1 + len(data)] = data
        buf[-4:] = bt_crc(bytes(buf[:-4]), header=0x53)
        with self.lock:
            return self.dev.send_feature_report(bytes(buf))

    def send_usb_output(self, usb_report: bytes):
        """Traduce un reporte de salida USB 0x02 (estado: LED, gatillos, rumble) a BT 0x31."""
        state = bytes(usb_report[1:48])
        return self.write_report(0x31, lambda seq: bytes([seq, 0x10]) + state, 78)

    def set_state(self, flags0=0, flags1=0, flags2=0, rumble=(0, 0), right=None, left=None,
                  lightbar_setup=0, brightness=0, player=0, rgb=(0, 0, 0), extra=None):
        """extra: {índice en SetStateData: valor} para campos sin parámetro propio (volúmenes, mute...)."""
        state = bytearray(47)
        state[0], state[1] = flags0, flags1
        state[2], state[3] = rumble  # derecho (ligero), izquierdo (pesado)
        if right is not None:
            state[10:21] = bytes(right)
        if left is not None:
            state[21:32] = bytes(left)
        state[38] = flags2
        state[41] = lightbar_setup
        state[42] = brightness  # 0 = máximo
        state[43] = player
        state[44:47] = bytes(rgb)
        for index, value in (extra or {}).items():
            state[index] = value
        return self.write_report(0x31, lambda seq: bytes([seq, 0x10]) + state, 78)

    def release_lightbar(self):
        """Secuencia del driver de Linux: liberar los LED y desvanecer la barra controlada por el firmware."""
        self.set_state(flags1=FLAGS1_RESET_LIGHTS, flags2=FLAGS2_LIGHTBAR_SETUP,
                       lightbar_setup=LIGHTBAR_SETUP_LIGHT_OUT)

    def set_lights(self, rgb, player=0x04):
        return self.set_state(flags1=FLAGS1_LED_COLOR | FLAGS1_PLAYER_LEDS, flags2=FLAGS2_BRIGHTNESS,
                              brightness=0, player=player, rgb=rgb)

    def set_triggers(self, right=TRIGGER_OFF, left=TRIGGER_OFF):
        return self.set_state(flags0=FLAGS0_RIGHT_TRIGGER | FLAGS0_LEFT_TRIGGER, right=right, left=left)

    def set_rumble(self, heavy, light):
        return self.set_state(flags0=FLAGS0_RUMBLE_EMULATION | FLAGS0_RUMBLE_NOT_HAPTICS, rumble=(light, heavy))

    def send_haptics(self, samples: bytes, size: int = 142):
        """Envía 64 bytes de háptica (int8 L/R intercalado) en un reporte 0x32."""
        assert len(samples) == HAPTIC_CHUNK
        self.haptic_counter = (self.haptic_counter + 1) & 0xFF
        packet_11 = bytes([0x11 | 0x80, 7, 0b11111110, 0, 0, 0, 0, 0xFF, self.haptic_counter])
        packet_12 = bytes([0x12 | 0x80 | 0x40, HAPTIC_CHUNK]) + samples
        return self.write_report(0x32, lambda seq: bytes([seq]) + packet_11 + packet_12, size)

    def send_haptics_39(self, samples: bytes, speaker: bytes = None, headphones: bool = False, mic: bool = False,
                        buffer_length: int = 48):
        """Formato de DS5Dongle: reporte 0x39 (547 bytes, sin relleno) con 128 bytes de háptica (2 bloques)
        y, opcionalmente, 2 tramas Opus de 200 bytes para la bocina (0x13) o los audífonos (0x16).
        `mic` mantiene encendido el envío del micrófono (bit 0 del paquete 0x11)."""
        assert len(samples) == 2 * HAPTIC_CHUNK
        self.haptic_counter = (self.haptic_counter + 2) & 0xFF
        packet_11 = bytes([0x11 | 0x80, 6, 0b01111111 if mic else 0b01111110] + [buffer_length] * 4
                          + [self.haptic_counter])
        packet_12 = bytes([0x12 | 0x80 | 0x40, HAPTIC_CHUNK]) + samples
        audio = b""
        if speaker:
            assert len(speaker) == 2 * SPEAKER_OPUS_BYTES
            audio = bytes([(0x16 if headphones else 0x13) | 0x80 | 0x40, SPEAKER_OPUS_BYTES]) + speaker
        return self.write_report(0x39, lambda seq: bytes([seq]) + packet_11 + packet_12 + audio, 547)

    def send_mic_status(self, enabled: bool):
        """Enciende o apaga el envío del micrófono (paquete 0x11 de 1 byte en un reporte 0x32)."""
        return self.write_report(0x32, lambda seq: bytes([seq, 0x11 | 0x80, 1, 0b11 if enabled else 0b10]), 142)

    def set_audio_control(self, mic_select=0):
        """Selección de micrófono (0 = automático: el de los audífonos si hay) con cancelación de ruido."""
        return self.set_state(flags0=FLAGS0_AUDIO_CONTROL, extra={7: (mic_select & 3) | 0x08})

    def set_volumes(self, headphones: int, speaker: int, mic: int, speaker_mute=False, mic_mute=False):
        """Volúmenes (headphones ≤ 0x7F, speaker ≈ 0x3D-0x64 en PS5, mic ≤ 0x40) y silencios."""
        mute = (0x10 if mic_mute else 0) | (0x60 if speaker_mute else 0)  # MicMute, SpeakerMute+HeadphoneMute
        return self.set_state(flags0=FLAGS0_HEADPHONE_VOLUME | FLAGS0_SPEAKER_VOLUME | FLAGS0_MIC_VOLUME,
                              flags1=FLAGS1_AUDIO_MUTE,
                              extra={4: min(headphones, 0x7F), 5: speaker, 6: min(mic, 0x40), 9: mute})

    def set_mute_light(self, on: bool):
        return self.set_state(flags1=FLAGS1_MUTE_LIGHT, extra={8: 1 if on else 0})

    def send_haptics_32_saxense(self, samples: bytes):
        """Réplica exacta de SAxense: 0x32, cabecera seq/tag fija en 0 y paquete 0x12 sin el bit 6."""
        assert len(samples) == HAPTIC_CHUNK
        self.haptic_counter = (self.haptic_counter + 1) & 0xFF
        packet_11 = bytes([0x11 | 0x80, 7, 0b11111110, 0, 0, 0, 0, 0xFF, self.haptic_counter])
        packet_12 = bytes([0x12 | 0x80, HAPTIC_CHUNK]) + samples
        return self.write_report(0x32, bytes([0]) + packet_11 + packet_12, 142)

    def read(self, timeout_ms=100):
        # La lectura usa su propia OVERLAPPED en hidapi, así que no necesita el candado.
        return self.dev.read(600, timeout_ms)


def tone_stream(freq_fn, amp=0.9, side="both"):
    """Generador infinito de bloques de 64 bytes con una senoidal. freq_fn y amp reciben t en segundos."""
    phase, i = 0.0, 0
    chunk = bytearray()
    while True:
        t = i / HAPTIC_RATE
        i += 1
        phase += 2 * math.pi * freq_fn(t) / HAPTIC_RATE
        a = amp(t) if callable(amp) else amp
        v = int(max(-128, min(127, math.sin(phase) * 127 * a))) & 0xFF
        chunk += bytes([v if side in ("both", "left") else 0, v if side in ("both", "right") else 0])
        if len(chunk) == HAPTIC_CHUNK:
            yield bytes(chunk)
            chunk.clear()


# Entrada BT 0x31: byte 1 es cabecera; desde el byte 2 coincide con el reporte USB 0x01 desplazado en +1.
DPAD = ["↑", "↗", "→", "↘", "↓", "↙", "←", "↖", ""]
BUTTONS = [(9, 0x10, "□"), (9, 0x20, "✕"), (9, 0x40, "○"), (9, 0x80, "△"),
           (10, 0x01, "L1"), (10, 0x02, "R1"), (10, 0x04, "L2"), (10, 0x08, "R2"),
           (10, 0x10, "Create"), (10, 0x20, "Options"), (10, 0x40, "L3"), (10, 0x80, "R3"),
           (11, 0x01, "PS"), (11, 0x02, "Touchpad"), (11, 0x04, "Mute")]


def describe_input(r):
    if not r or r[0] != 0x31 or len(r) < 12:
        return None
    pressed = [name for idx, mask, name in BUTTONS if r[idx] & mask]
    dpad = DPAD[min(r[9] & 0x0F, 8)]
    if dpad:
        pressed.insert(0, f"Cruceta{dpad}")
    return (f"LX={r[2]:3} LY={r[3]:3} RX={r[4]:3} RY={r[5]:3}  L2={r[6]:3} R2={r[7]:3}  "
            f"{' '.join(pressed) or '-'}")
