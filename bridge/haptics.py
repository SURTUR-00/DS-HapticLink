"""Háptica HD: audio USB del juego (48 kHz, 4 canales int16) → reportes BT 0x39 (3 kHz, 2 canales int8).

Los canales 3 y 4 del audio USB del DualSense son los actuadores (1 y 2 son la bocina/audífonos).
Un hilo propio envía un reporte cada 21.33 ms (64 frames) desde un búfer con reserva adaptativa:
  - si se vacía el búfer, se rellena con silencio en vez de detenerse (el hueco dura solo lo que falta)
    y la reserva objetivo sube;
  - tras un rato estable, la reserva baja de nuevo para recortar latencia;
  - si el juego deja de mandar audio por completo, se deja de transmitir.
"""
import array
import logging
import math
import operator
import threading
import time

from . import rt
from .ds5bt import CHUNK_PERIOD, HAPTIC_CHUNK, TRAFFIC

log = logging.getLogger("haptics")

USB_RATE, USB_CHANNELS = 48000, 4
HAPTIC_RATE = 3000
DECIMATION = USB_RATE // HAPTIC_RATE  # 16
REPORT_BYTES = 2 * HAPTIC_CHUNK  # 128 bytes = 64 frames estéreo int8 por reporte 0x39
REPORT_PERIOD = 2 * CHUNK_PERIOD  # 21.33 ms
FRAME_BYTES = 2  # un frame estéreo int8 (L, R)

PREFILL_STEP = 32  # bytes (16 frames ≈ 5.3 ms) que sube o baja la reserva en cada ajuste
STABLE_SECONDS = 20.0  # sin cortes durante este tiempo → bajar la reserva un paso
FLOOR_RELEASE_SECONDS = 120.0  # sin cortes durante este tiempo → bajar un paso el piso aprendido de los tirones
IDLE_STOP_SECONDS = 0.25  # sin audio nuevo durante este tiempo → el juego dejó de mandar; parar
MAX_JITTER_GAP = 2  # huecos de hasta 2 reportes (≈ 43 ms) seguidos de más audio cuentan como corte
# Ocultamiento de huecos: en vez de silencio, se repite la última vibración enviada desvaneciéndola a cero en
# este número de frames (≈ 43 ms). Un hueco corto en una vibración continua deja de sentirse como un "bache".
CONCEAL_FRAMES = 128
SILENT_TAIL_REPORTS = 5  # reportes de silencio (≈ 107 ms) que se mandan igual antes de dejar de transmitir
TIMING_REPORT_SECONDS = 30.0  # cada cuánto se registra el resumen de tiempos mientras hay háptica


def _dot_python(a, b):
    return sum(map(operator.mul, a, b))


# Producto punto del filtro: math.sumprod (Python 3.12+) lo hace en C, ~2-3 veces más rápido.
_dot = getattr(math, "sumprod", _dot_python)


class Decimator:
    """Filtro FIR paso bajo (sinc con ventana de Blackman) + decimación, para un canal.

    Corta alrededor de 1.3 kHz (Nyquist de la salida: 1.5 kHz), así el contenido agudo del audio no se
    "dobla" en vibraciones graves falsas. Ganancia unitaria en continua.
    """

    def __init__(self, factor=DECIMATION, taps=64, cutoff=1300.0, rate=USB_RATE):
        self.factor = factor
        m = taps - 1
        fc = cutoff / rate
        h = []
        for n in range(taps):
            x = n - m / 2
            sinc = 2 * fc if x == 0 else math.sin(2 * math.pi * fc * x) / (math.pi * x)
            window = 0.42 - 0.5 * math.cos(2 * math.pi * n / m) + 0.08 * math.cos(4 * math.pi * n / m)
            h.append(sinc * window)
        total = sum(h)
        self.taps = [v / total for v in h]
        self.history = [0] * m  # últimas taps-1 muestras de la llamada anterior
        self.next_index = m  # índice (en history + nuevas) de la próxima muestra que produce una salida

    def process(self, samples):
        """Recibe muestras a 48 kHz (lista de números) y devuelve las de salida a 3 kHz (floats)."""
        buf = self.history + list(samples)
        n_taps = len(self.taps)
        out = []
        i = self.next_index
        taps = self.taps
        while i < len(buf):
            out.append(_dot(taps, buf[i - n_taps + 1:i + 1]))
            i += self.factor
        keep = buf[-(n_taps - 1):]
        self.next_index = i - (len(buf) - len(keep))
        self.history = keep
        return out


def _traffic_snapshot():
    """(paquetes, bytes, estados omitidos, reportes hápticos, en silencio, no enviados) del tráfico Bluetooth."""
    return tuple(TRAFFIC[k] for k in ("packets", "bytes", "state_skipped", "haptic", "haptic_silent",
                                      "haptic_skipped"))


def _to_int8(value, gain):
    v = int(value * gain)
    return (127 if v > 127 else -128 if v < -128 else v) & 0xFF


class HapticBridge:
    def __init__(self, ds, prefill_reports=1, max_prefill_reports=3, gain=1.0, speaker=None):
        """prefill_reports: reserva inicial en reportes de 21.33 ms (la reserva adaptativa parte de aquí).
        max_prefill_reports: tope de la reserva adaptativa (latencia máxima añadida ≈ 21 ms × este valor).
        speaker: audio.SpeakerPath para mandar también la bocina/audífonos en cada reporte (o None)."""
        self.ds = ds
        self.speaker = speaker
        self.headphones = False  # audífonos conectados al jack del control → el audio va al jack
        self.mic_enabled = False  # el host abrió el micrófono → mantenerlo encendido en cada reporte
        self.min_prefill = max(PREFILL_STEP, int(prefill_reports * REPORT_BYTES) // 2)
        self.max_prefill = int(max_prefill_reports * REPORT_BYTES)
        self.prefill = min(self.max_prefill, int(prefill_reports * REPORT_BYTES))
        self.gain = gain / 256  # int16 → int8
        self.buf = bytearray()
        self.cond = threading.Condition()
        self.left, self.right = Decimator(), Decimator()
        self.running = False
        self.streaming = False
        self.last_feed = 0.0
        self.last_underrun = time.monotonic()
        self.floor = self.min_prefill  # piso de la reserva aprendido de los tirones (ver _maybe_shrink)
        self._last_counted_underrun = self._floor_lowered = 0.0
        self._gap_reports = 0  # reportes seguidos rellenados con silencio
        self._gap_bump = 0  # subida provisional de la reserva al empezar el hueco
        self.stats = {"reportes": 0, "underruns": 0, "descartados": 0, "reserva_ms": 0.0}
        self._update_reserve_stat()
        self._history = bytes(REPORT_BYTES)  # último bloque enviado (fuente del ocultamiento)
        self._conceal_pos = 0  # frames ya ocultados en el hueco actual
        self._silent_run = 0  # reportes seguidos sin nada que reproducir (ver _should_send)
        # Diagnóstico de microcortes: máximos de la ventana actual (ms). Lo actualizan también virtual_ds5
        # (pacing de los URB isócronos) y el hilo emisor.
        self.timing = self._new_timing()
        self._timing_since = time.monotonic()
        self._timing_start = (0, 0)  # (reportes, underruns) al empezar la ventana
        self._speaker_start = (0, 0)  # (cortes, descartes) de la bocina/audífonos al empezar la ventana
        self._traffic_start = _traffic_snapshot()  # TRAFFIC al empezar la ventana
        self._last_feed_pc = None
        self.thread = threading.Thread(target=self._sender, name="haptics", daemon=True)

    def _update_reserve_stat(self):
        self.stats["reserva_ms"] = round(self.prefill / FRAME_BYTES / HAPTIC_RATE * 1000, 1)

    def start(self):
        self.running = True
        self.thread.start()

    def stop(self):
        self.running = False
        with self.cond:
            self.cond.notify_all()

    # ------------------------------------------------------------------ entrada (hilo de asyncio)
    def feed_usb_audio(self, pcm: bytes):
        """Recibe PCM 48 kHz de 4 canales int16 LE (el payload de los URB isócronos OUT)."""
        if not pcm:
            return
        now = time.perf_counter()
        if self._last_feed_pc is not None:
            gap = (now - self._last_feed_pc) * 1000
            if gap < 1000:  # más de 1 s es una pausa del juego, no un retraso
                self.timing["feed_gap"] = max(self.timing["feed_gap"], gap)
        self._last_feed_pc = now
        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) - len(pcm) % (2 * USB_CHANNELS)])
        self.timing["fed_frames"] += len(samples) // USB_CHANNELS
        if self.speaker is not None:
            try:
                self.speaker.feed(samples[0::USB_CHANNELS], samples[1::USB_CHANNELS], mono=not self.headphones)
            except OSError as e:  # un fallo de Opus no debe cortar la háptica
                log.warning("error encoding speaker audio: %s", e)
        left = self.left.process(samples[2::USB_CHANNELS])
        right = self.right.process(samples[3::USB_CHANNELS])
        out = bytearray()
        for l_val, r_val in zip(left, right):
            out.append(_to_int8(l_val, self.gain))
            out.append(_to_int8(r_val, self.gain))
        with self.cond:
            self.buf += out
            self.last_feed = time.monotonic()
            # Acotar la latencia: no dejar que el búfer crezca más allá de la reserva + 2 reportes.
            limit = self.prefill + 2 * REPORT_BYTES
            if len(self.buf) > limit:
                drop = len(self.buf) - limit
                drop -= drop % FRAME_BYTES
                del self.buf[:drop]
                self.stats["descartados"] += drop
            self.cond.notify()

    def reset(self):
        """El host cerró el stream de audio (SET_INTERFACE alt 0) o se perdió el control: vaciar todo."""
        with self.cond:
            self.buf.clear()
            self.left, self.right = Decimator(), Decimator()
            self.streaming = False
            self._gap_ended(resumed=False)
            self._history = bytes(REPORT_BYTES)
        self._last_feed_pc = None
        if self.speaker is not None:
            self.speaker.reset()

    # ------------------------------------------------------------------ reserva adaptativa
    # Un hueco corto (≤ MAX_JITTER_GAP reportes) seguido de más audio es un corte por jitter: cuenta y sube
    # la reserva. Un hueco más largo es una pausa del juego o de Windows (p. ej. fin de un sonido): no
    # cuenta y se deshace la subida provisional que se hizo al empezar el hueco.
    def _gap_started(self):
        self._gap_reports += 1
        if self._gap_reports == 1:
            self._gap_bump = min(PREFILL_STEP, self.max_prefill - self.prefill)
            self.prefill += self._gap_bump
            self._update_reserve_stat()

    def _gap_ended(self, resumed):
        if not self._gap_reports:
            return
        if resumed and self._gap_reports <= MAX_JITTER_GAP:
            self.stats["underruns"] += 1
            self.last_underrun = self._last_counted_underrun = time.monotonic()
            self.floor = max(self.floor, self.prefill)  # recordar la reserva que hizo falta
        else:
            self.prefill -= self._gap_bump
            self._update_reserve_stat()
        self._gap_reports = self._gap_bump = 0
        self._conceal_pos = 0

    def _conceal(self, n_bytes):
        """n_bytes de relleno para un hueco: la última vibración repetida y desvaneciéndose (no silencio)."""
        frames = n_bytes // FRAME_BYTES
        src = self._history
        src_frames = len(src) // FRAME_BYTES
        out = bytearray(n_bytes)
        for i in range(frames):
            gain = 1.0 - (self._conceal_pos + i) / CONCEAL_FRAMES
            if gain <= 0:
                break
            j = ((self._conceal_pos + i) % src_frames) * FRAME_BYTES
            for c in range(FRAME_BYTES):
                v = src[j + c]
                v = v - 256 if v > 127 else v  # int8 con signo
                out[i * FRAME_BYTES + c] = int(v * gain) & 0xFF
        self._conceal_pos += frames
        return bytes(out)

    @staticmethod
    def _new_timing():
        # fed_frames: frames USB recibidos (ritmo real del audio que entra); min_level: lo mínimo que tuvo el búfer
        # al enviar (bytes); iso_idle: tiempo en que no había ningún URB de audio de Windows esperando (ms).
        return {"feed_gap": 0.0, "iso_late": 0.0, "bt_write": 0.0, "sender_late": 0.0, "fed_frames": 0,
                "min_level": None, "iso_idle": 0.0}

    def _maybe_report_timing(self):
        """Resumen periódico para diagnosticar microcortes (solo mientras se transmite háptica)."""
        now = time.monotonic()
        if now - self._timing_since < TIMING_REPORT_SECONDS:
            return
        reports = self.stats["reportes"] - self._timing_start[0]
        underruns = self.stats["underruns"] - self._timing_start[1]
        t = self.timing
        speaker = ""
        if self.speaker is not None:
            gaps, dropped = self.speaker.gaps, self.speaker.dropped
            speaker = (f" | {'headphones' if self.headphones else 'speaker'}: {gaps - self._speaker_start[0]} gaps,"
                       f" {dropped - self._speaker_start[1]} dropped")
            self._speaker_start = (gaps, dropped)
        elapsed = now - self._timing_since
        level_ms = (t["min_level"] or 0) / FRAME_BYTES / HAPTIC_RATE * 1000
        traffic = _traffic_snapshot()
        packets, sent_bytes, skipped, haptic, silent, not_sent = (
            a - b for a, b in zip(traffic, self._traffic_start))
        self._traffic_start = traffic
        log.info("last %.0f s: %d reports, %d underruns (concealed) | max: audio arrival gap %.1f ms, iso pacing"
                 " late %.1f ms, BT write %.1f ms, sender late %.1f ms | reserve %.1f ms%s | audio in %.4f s/s,"
                 " buffer min %.1f ms, no URB queued %.0f ms | BT out %.0f packets/s, %.0f kbit/s,"
                 " %.0f repeated states/s skipped, %.0f%% of haptic reports silent (%.0f%% not sent)",
                 elapsed, reports, underruns, t["feed_gap"], t["iso_late"], t["bt_write"],
                 t["sender_late"], self.stats["reserva_ms"], speaker, t["fed_frames"] / USB_RATE / elapsed,
                 level_ms, t["iso_idle"], packets / elapsed, sent_bytes * 8 / 1000 / elapsed, skipped / elapsed,
                 100 * silent / haptic if haptic else 0, 100 * not_sent / haptic if haptic else 0)
        self.timing = self._new_timing()
        self._timing_since = now
        self._timing_start = (self.stats["reportes"], self.stats["underruns"])

    def _maybe_shrink(self, now=None):
        """Sube rápido, baja despacio. La reserva baja un paso cada STABLE_SECONDS sin cortes, pero no por debajo
        del "piso": la reserva a la que se llegó en el último tirón, que a su vez baja un paso cada
        FLOOR_RELEASE_SECONDS sin cortes. Visto el 2026-11-21: tirones de todo el sistema de ~35-45 ms cada ~10
        min; la reserva subía a 64 ms y en 5 min volvía al mínimo (10,7 ms), justo a tiempo para el siguiente."""
        now = time.monotonic() if now is None else now
        if self.floor > self.min_prefill and now - max(self._last_counted_underrun, self._floor_lowered) \
                > FLOOR_RELEASE_SECONDS:
            self.floor = max(self.min_prefill, self.floor - PREFILL_STEP)
            self._floor_lowered = now
        lowest = max(self.min_prefill, self.floor)
        if now - self.last_underrun > STABLE_SECONDS and self.prefill > lowest:
            self.prefill = max(lowest, self.prefill - PREFILL_STEP)
            self.last_underrun = now  # esperar otro periodo estable antes del siguiente paso
            self._update_reserve_stat()

    # ------------------------------------------------------------------ salida (hilo propio)
    def _next_chunk(self):
        """Devuelve el siguiente bloque de 128 bytes, o None si hay que dejar de transmitir."""
        with self.cond:
            if not self.streaming:
                while self.running and len(self.buf) < self.prefill + REPORT_BYTES:
                    self.cond.wait(0.1)
                if not self.running:
                    return None
                self.streaming = True
                return "start"
            level, low = len(self.buf), self.timing.get("min_level")
            self.timing["min_level"] = level if low is None else min(low, level)
            if len(self.buf) >= REPORT_BYTES:
                chunk = bytes(self.buf[:REPORT_BYTES])
                del self.buf[:REPORT_BYTES]
                self._gap_ended(resumed=True)
                self._history = chunk  # solo vibración real (no relleno) sirve de fuente al ocultamiento
                return chunk
            if time.monotonic() - self.last_feed > IDLE_STOP_SECONDS:
                # El juego dejó de mandar audio: no tiene sentido seguir enviando silencio por BT.
                self.buf.clear()
                self.streaming = False
                self._gap_ended(resumed=False)
                return "stop"
            # Hueco: mandar lo que haya y completar con la última vibración desvaneciéndose (ocultamiento),
            # sin detener la transmisión.
            have = bytes(self.buf)
            self.buf.clear()
            self._gap_started()
            return have + self._conceal(REPORT_BYTES - len(have))

    def _sender(self):
        mmcss = rt.raise_thread_priority("haptics")
        try:
            self._send_loop()
        finally:
            rt.revert_thread_priority(mmcss)

    def _should_send(self, chunk, speaker):
        """Si hay que mandar este reporte. Con la vibración en cero, sin audio para la bocina/audífonos y sin el
        micrófono abierto, el reporte no lleva nada: tras unos pocos de cola (para que la vibración termine
        limpia) se dejan de mandar hasta que vuelva algo. Medido el 2026-11-23: ~56 % de los reportes eran
        silencio (27-100 % según la escena), y cada uno ocupa el Bluetooth con 548 bytes."""
        if speaker is not None or self.mic_enabled or any(chunk):
            self._silent_run = 0
            return True
        TRAFFIC["haptic_silent"] += 1
        self._silent_run += 1
        if self._silent_run <= SILENT_TAIL_REPORTS:
            return True
        TRAFFIC["haptic_skipped"] += 1
        return False

    def _send_loop(self):
        next_t = time.perf_counter()
        while self.running:
            chunk = self._next_chunk()
            if chunk is None:
                return
            if chunk in ("start", "stop"):
                next_t = time.perf_counter()
                if chunk == "start":
                    self._timing_since = time.monotonic()
                    self._timing_start = (self.stats["reportes"], self.stats["underruns"])
                    if self.speaker is not None:
                        self._speaker_start = (self.speaker.gaps, self.speaker.dropped)
                    self._traffic_start = _traffic_snapshot()
                    self.timing = self._new_timing()
                continue
            late = (time.perf_counter() - next_t) * 1000
            self.timing["sender_late"] = max(self.timing["sender_late"], late)
            speaker = self.speaker.pop_pair() if self.speaker is not None else None
            TRAFFIC["haptic"] += 1
            if self._should_send(chunk, speaker):
                # BtLink descarta en silencio si no hay control conectado (devuelve None)
                result = self.ds.send_haptics_39(chunk, speaker=speaker, headphones=self.headphones,
                                                 mic=self.mic_enabled)
                if isinstance(result, tuple) and len(result) == 2:
                    self.timing["bt_write"] = max(self.timing["bt_write"], result[1])
            self.stats["reportes"] += 1
            self._maybe_shrink()
            self._maybe_report_timing()
            next_t += REPORT_PERIOD
            delay = next_t - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.1:
                next_t = time.perf_counter()  # nos atrasamos demasiado (p. ej. el PC se trabó): re-sincronizar
