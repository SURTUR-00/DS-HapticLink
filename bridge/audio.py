"""Bocina, jack de audífonos y micrófono del DualSense por Bluetooth (todo en Opus).

Salida (bocina / audífonos): canales 1-2 del audio USB del juego (48 kHz) → se re-muestrea 512 → 480 frames
(como DS5Dongle: así cada par de tramas Opus de 10 ms dura lo mismo que un reporte 0x39 de 21.33 ms) →
Opus CBR 200 bytes por trama → 2 tramas por reporte 0x39, junto con la háptica.

Entrada (micrófono): el control manda tramas Opus mono de 71 bytes dentro de sus reportes 0x31 → se
decodifican a PCM 48 kHz y se entregan al endpoint isócrono del micrófono USB (estéreo, como el real).

libopus viene con el paquete pyogg (opus.dll); aquí solo se usa la DLL vía ctypes.
"""
import array
import collections
import ctypes
import importlib.util
import logging
import threading
from pathlib import Path

log = logging.getLogger("audio")

RATE = 48000
FRAME = 480  # 10 ms a 48 kHz
OPUS_SPEAKER_BYTES = 200  # CBR: 200 bytes × 100 tramas/s = 160 kbit/s
MIC_OPUS_BYTES = 71
INPUT_FRAMES_PER_OPUS = 512  # 10.67 ms de audio USB → una trama Opus "de 480"

OPUS_APPLICATION_AUDIO = 2049
OPUS_SET_BITRATE = 4002
OPUS_SET_VBR = 4006
OPUS_SET_COMPLEXITY = 4010
OPUS_SET_EXPERT_FRAME_DURATION = 4040
OPUS_FRAMESIZE_10_MS = 5003

_lib = None


def opus_lib():
    """Carga opus.dll (del paquete pyogg). Devuelve None si no está disponible."""
    global _lib
    if _lib is not None:
        return _lib or None
    try:
        from .config import app_dir
        bundled = app_dir() / "opus.dll"  # la app compilada la trae junto al .exe
        if bundled.exists():
            dll = bundled
        else:  # desarrollo: la del paquete pyogg
            spec = importlib.util.find_spec("pyogg")
            if spec is None:
                raise OSError("opus.dll not found (neither bundled nor from pyogg)")
            dll = Path(spec.origin).parent / "opus.dll"
        lib = ctypes.CDLL(str(dll))
        lib.opus_encoder_create.restype = ctypes.c_void_p
        lib.opus_encoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.opus_encode.restype = ctypes.c_int32
        lib.opus_encode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
                                    ctypes.c_char_p, ctypes.c_int32]
        lib.opus_encoder_ctl.restype = ctypes.c_int
        lib.opus_decoder_create.restype = ctypes.c_void_p
        lib.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.opus_decode.restype = ctypes.c_int
        lib.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32,
                                    ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int]
        lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
        lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        _lib = lib
    except OSError as e:
        log.warning("libopus unavailable (%s): no speaker, headphones or microphone over Bluetooth", e)
        _lib = False
        return None
    return _lib


class OpusEncoder:
    def __init__(self, channels=2, frame_bytes=OPUS_SPEAKER_BYTES):
        """frame_bytes: tamaño fijo (CBR) de cada trama de 10 ms."""
        lib = opus_lib()
        err = ctypes.c_int()
        self.lib, self.channels, self.frame_bytes = lib, channels, frame_bytes
        self.st = lib.opus_encoder_create(RATE, channels, OPUS_APPLICATION_AUDIO, ctypes.byref(err))
        if err.value != 0 or not self.st:
            raise OSError(f"opus_encoder_create failed ({err.value})")
        # Mismos ajustes que DS5Dongle: tramas de 10 ms, CBR de 200 bytes, complejidad mínima.
        lib.opus_encoder_ctl(ctypes.c_void_p(self.st), OPUS_SET_EXPERT_FRAME_DURATION, ctypes.c_int(OPUS_FRAMESIZE_10_MS))
        lib.opus_encoder_ctl(ctypes.c_void_p(self.st), OPUS_SET_BITRATE, ctypes.c_int(frame_bytes * 8 * 100))
        lib.opus_encoder_ctl(ctypes.c_void_p(self.st), OPUS_SET_VBR, ctypes.c_int(0))
        lib.opus_encoder_ctl(ctypes.c_void_p(self.st), OPUS_SET_COMPLEXITY, ctypes.c_int(0))
        self.out = ctypes.create_string_buffer(frame_bytes)

    def encode(self, interleaved):
        """interleaved: FRAME × canales muestras int16 (array('h') sin copiar, o lista).
        Devuelve exactamente frame_bytes (relleno con ceros)."""
        if isinstance(interleaved, array.array) and interleaved.typecode == "h":
            pcm = (ctypes.c_int16 * len(interleaved)).from_buffer(interleaved)
        else:
            pcm = (ctypes.c_int16 * len(interleaved))(*interleaved)
        n = self.lib.opus_encode(self.st, pcm, FRAME, self.out, self.frame_bytes)
        if n <= 0:
            raise OSError(f"opus_encode failed ({n})")
        return self.out.raw[:n].ljust(self.frame_bytes, b"\0")

    def __del__(self):
        if getattr(self, "st", None):
            self.lib.opus_encoder_destroy(self.st)


class OpusDecoder:
    def __init__(self, channels=1):
        lib = opus_lib()
        err = ctypes.c_int()
        self.lib, self.channels = lib, channels
        self.st = lib.opus_decoder_create(RATE, channels, ctypes.byref(err))
        if err.value != 0 or not self.st:
            raise OSError(f"opus_decoder_create failed ({err.value})")
        self.pcm = (ctypes.c_int16 * (FRAME * channels))()

    def decode(self, data):
        n = self.lib.opus_decode(self.st, bytes(data), len(data), self.pcm, FRAME, 0)
        if n <= 0:
            raise OSError(f"opus_decode failed ({n})")
        return list(self.pcm[:n * self.channels])

    def __del__(self):
        if getattr(self, "st", None):
            self.lib.opus_decoder_destroy(self.st)


class StereoResampler:
    """Re-muestreo lineal en streaming de 512 → 480 frames (paso exacto 16/15), los dos canales a la vez.

    Aritmética entera: la posición se lleva en quinceavos de muestra, así que no hay errores de redondeo que se
    acumulen ni conversiones de float. Las posiciones se calculan una vez para ambos canales y la salida sale ya
    intercalada (L, R) en un array int16 listo para Opus. Es ~3 veces más rápido que el bucle muestra a muestra
    anterior: con audífonos era la mayor parte del trabajo del puente (medido: ~5 % de un núcleo)."""

    IN, OUT = INPUT_FRAMES_PER_OPUS // 32, FRAME // 32  # 16 muestras de entrada por cada 15 de salida

    def __init__(self):
        # Próxima salida, en 1/OUT de muestra, contando desde la última muestra del bloque anterior (índice 0).
        # Al empezar no hay bloque anterior: la primera salida es la primera muestra nueva (índice 1).
        self.pos = self.OUT
        self.prev_l = self.prev_r = 0

    def process(self, left, right=None):
        """left/right: muestras int16 (right=None: mono, la misma señal en ambos canales).
        Devuelve array('h') intercalado L, R."""
        n = len(left)
        if n == 0:
            return array.array("h")
        step, den = self.IN, self.OUT
        start, end = self.pos, n * den  # índice 0 = muestra previa; el último índice usable es n - 1 (+1 = n)
        count = (end - start + step - 1) // step if start < end else 0
        positions = range(start, start + step * count, step)
        idx = [p // den for p in positions]
        frac = [p % den for p in positions]
        xl = [self.prev_l]
        xl += left
        l_out = [xl[i] + (xl[i + 1] - xl[i]) * f // den for i, f in zip(idx, frac)]
        out = array.array("h", bytes(4 * count))  # 2 × count muestras int16 en cero
        out[0::2] = array.array("h", l_out)
        if right is None:
            out[1::2] = out[0::2]
            self.prev_l = self.prev_r = xl[-1]
        else:
            xr = [self.prev_r]
            xr += right
            out[1::2] = array.array("h", [xr[i] + (xr[i + 1] - xr[i]) * f // den for i, f in zip(idx, frac)])
            self.prev_l, self.prev_r = xl[-1], xr[-1]
        self.pos = start + step * count - end
        return out


class SpeakerPath:
    """Audio de bocina/audífonos: PCM USB → tramas Opus listas para el reporte 0x39."""

    # Tope del búfer en tramas de 10 ms. Las tramas se consumen al ritmo de la háptica, así que el búfer sigue a
    # la reserva de háptica (hasta 64 ms + 2 reportes ≈ 107 ms): con un tope menor se descartaba audio (saltos)
    # justo cuando la reserva estaba alta para aguantar un tirón.
    def __init__(self, max_frames=12):
        self.encoder = OpusEncoder(2)
        self.resampler = StereoResampler()
        self.pending = array.array("h")  # muestras re-muestreadas aún sin completar una trama (intercaladas L/R)
        self.frames = collections.deque(maxlen=max_frames)  # (bytes opus, es_silencio)
        self.encoded = 0
        # Diagnóstico (el log de háptica los muestra cada 30 s): reportes que salieron sin audio porque no había
        # tramas (corte audible) y tramas descartadas porque el búfer estaba lleno (salto/chasquido audible).
        self.gaps = 0
        self.dropped = 0
        self._flowing = False

    def feed(self, left, right, mono=False):
        """left/right: muestras int16 a 48 kHz de los canales 1 y 2 del audio USB.
        mono=True para la bocina: el DualSense tiene una sola bocina que solo reproduce el canal derecho,
        así que se mezclan ambos canales y se manda la mezcla a los dos (si no, lo del canal izquierdo
        apenas se oiría). Con audífonos en el jack se conserva el estéreo."""
        if mono:
            self.pending += self.resampler.process([(a + b) >> 1 for a, b in zip(left, right)])
        else:
            self.pending += self.resampler.process(left, right)
        while len(self.pending) >= FRAME * 2:
            block = self.pending[:FRAME * 2]
            del self.pending[:FRAME * 2]
            silent = not any(block)
            if len(self.frames) == self.frames.maxlen:
                self.dropped += 1  # append() va a descartar la trama más vieja
            self.frames.append((None if silent else self.encoder.encode(block), silent))
            self.encoded += 1
            self._flowing = True

    def pop_pair(self):
        """Dos tramas (400 bytes) para el siguiente reporte, o None si no hay o ambas son silencio."""
        if len(self.frames) < 2:
            if self._flowing:  # ya había audio: este reporte sale sin sonido (antes del primero no cuenta)
                self.gaps += 1
            return None
        (a, sa), (b, sb) = self.frames.popleft(), self.frames.popleft()
        if sa and sb:
            return None
        silence = self._silence()
        return (a or silence) + (b or silence)

    def _silence(self):
        if not hasattr(self, "_silence_frame"):
            self._silence_frame = self.encoder.encode([0] * (FRAME * 2))
        return self._silence_frame

    def reset(self):
        self.frames.clear()
        self.pending = array.array("h")
        self.resampler = StereoResampler()
        self._flowing = False  # el stream se cerró: el próximo arranque no cuenta como corte


class MicPath:
    """Micrófono: tramas Opus del control → PCM estéreo int16 48 kHz para el endpoint USB."""

    def __init__(self, max_ms=80):
        self.decoder = OpusDecoder(1)
        self.buf = bytearray()
        self.max_bytes = max_ms * 48 * 4
        self.lock = threading.Lock()
        self.frames = 0
        self.errors = 0

    def on_bt_frame(self, data):
        try:
            mono = self.decoder.decode(data[:MIC_OPUS_BYTES])
        except OSError:
            self.errors += 1
            return
        m = array.array("h", mono)
        stereo = array.array("h", bytes(len(m) * 4))
        stereo[0::2] = m
        stereo[1::2] = m
        with self.lock:
            self.buf += stereo.tobytes()
            if len(self.buf) > self.max_bytes:
                del self.buf[:len(self.buf) - self.max_bytes]
            self.frames += 1

    def read(self, nbytes):
        """Devuelve nbytes de PCM; si no hay suficiente, completa con silencio."""
        with self.lock:
            chunk = bytes(self.buf[:nbytes])
            del self.buf[:nbytes]
        return chunk.ljust(nbytes, b"\0")

    def reset(self):
        with self.lock:
            self.buf.clear()
