"""Tiempo real en Windows: precisión del reloj y prioridad de los hilos de audio.

Con el reloj normal de Windows (15,6 ms) cada pausa corta dura ~16 ms: medido, asyncio.sleep(0.002) tardaba
15,8 ms de media. El puente marca con esas pausas el ritmo de los URB isócronos (el audio del juego, del que
salen la háptica y el sonido del jack), así que el audio avanzaba a saltos de 16 ms y la reserva de háptica se
vaciaba de vez en cuando (microcortes, más notorios en juegos de háptica continua como Stellar Blade).
Con timeBeginPeriod(1), lo que usan juegos y reproductores, la misma pausa tarda ~3 ms.

Windows 11 deja de respetar timeBeginPeriod en cuanto la ventana del proceso queda minimizada, oculta (en la
bandeja) o tapada por el juego en pantalla completa, salvo que el proceso lo pida expresamente con
SetProcessInformation(ProcessPowerThrottling, IGNORE_TIMER_RESOLUTION). Visto en un registro real (2026-11-21):
tras abrir y cerrar la ventana a mitad de partida, "iso pacing late" saltó de ~2,5 ms a ~13-16 ms y se quedó
así el resto de la sesión (más microcortes y el audio de los audífonos entrecortado), mientras el hilo emisor,
que usa time.sleep (temporizador de alta resolución propio), seguía en ~1,5 ms.
"""
import ctypes
import logging
import sys
from ctypes import wintypes

log = logging.getLogger("rt")

ABOVE_NORMAL_PRIORITY_CLASS = 0x8000
THREAD_PRIORITY_HIGHEST = 2

_timer_active = False

if sys.platform == "win32":
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _winmm = ctypes.WinDLL("winmm")
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.GetCurrentThread.restype = wintypes.HANDLE
    _k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
    _SetProcessInformation = getattr(_k32, "SetProcessInformation", None)  # Windows 8+
    if _SetProcessInformation is not None:
        _SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        _SetProcessInformation.restype = wintypes.BOOL

PROCESS_POWER_THROTTLING = 4  # PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1  # EcoQoS (modo eficiencia)
PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x4  # Windows 11: ignorar timeBeginPeriod si no está visible


class _PowerThrottlingState(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]


def disable_power_throttling():
    """Que Windows no frene el puente cuando la ventana está oculta: mantener el reloj de 1 ms pedido con
    timeBeginPeriod y no pasar el proceso a "modo eficiencia". ControlMask = lo que se decide, StateMask = 0
    → "nunca". En Windows 10 la marca del reloj no existe: se reintenta solo con la de eficiencia."""
    if sys.platform != "win32" or _SetProcessInformation is None:
        return False
    for mask in (PROCESS_POWER_THROTTLING_EXECUTION_SPEED | PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION,
                 PROCESS_POWER_THROTTLING_EXECUTION_SPEED):
        state = _PowerThrottlingState(PROCESS_POWER_THROTTLING_CURRENT_VERSION, mask, 0)
        if _SetProcessInformation(_k32.GetCurrentProcess(), PROCESS_POWER_THROTTLING, ctypes.byref(state),
                                  ctypes.sizeof(state)):
            if mask & PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION:
                log.info("power throttling disabled: the 1 ms timer holds while the window is hidden")
            else:
                log.info("power throttling disabled (efficiency mode only)")
            return True
    log.warning("couldn't disable power throttling (error %d): the audio may stutter while the window is"
                " hidden", ctypes.get_last_error())
    return False


def enable_high_resolution_timer():
    """Reloj del sistema a 1 ms mientras el puente corre (Windows lo restaura al terminar el proceso)."""
    global _timer_active
    if sys.platform != "win32" or _timer_active:
        return _timer_active
    disable_power_throttling()  # antes de timeBeginPeriod: si no, Windows 11 lo ignora con la ventana oculta
    _timer_active = _winmm.timeBeginPeriod(1) == 0  # TIMERR_NOERROR
    log.info("high-resolution timer (1 ms) %s", "enabled" if _timer_active else "NOT available")
    return _timer_active


def disable_high_resolution_timer():
    global _timer_active
    if sys.platform == "win32" and _timer_active:
        _winmm.timeEndPeriod(1)
        _timer_active = False


def raise_process_priority():
    """Prioridad "superior a la normal": por encima de las apps comunes, por debajo de lo crítico del sistema."""
    if sys.platform == "win32" and not _k32.SetPriorityClass(_k32.GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS):
        log.debug("couldn't raise the process priority (error %d)", ctypes.get_last_error())


AVRT_PRIORITY_HIGH = 1
MMCSS_TASK = "Pro Audio"  # la tarea de MMCSS de las apps de audio de baja latencia

if sys.platform == "win32":
    try:
        _avrt = ctypes.WinDLL("avrt", use_last_error=True)
        _avrt.AvSetMmThreadCharacteristicsW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        _avrt.AvSetMmThreadCharacteristicsW.restype = wintypes.HANDLE
        _avrt.AvSetMmThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        _avrt.AvSetMmThreadPriority.restype = wintypes.BOOL
        _avrt.AvRevertMmThreadCharacteristics.argtypes = [wintypes.HANDLE]
        _avrt.AvRevertMmThreadCharacteristics.restype = wintypes.BOOL
    except OSError:
        _avrt = None


def raise_thread_priority(name):
    """Prioridad de audio para el hilo que llama (el de asyncio/USB y el que envía la háptica).

    Primero se registra en MMCSS (el servicio de Windows para el audio en tiempo real: lo usan el propio motor de
    audio de Windows y los juegos para sus hilos de sonido). Con MMCSS el hilo sube a prioridad de tiempo real
    durante su turno, sin depender de qué ventana está en primer plano. Visto el 2026-11-21: al cambiar de
    ventana con el juego abierto, todo el audio se atrasaba ~35 ms de golpe. MMCSS reserva el 20 % del
    procesador para el resto del sistema, así que no puede acaparar el equipo (el puente usa ~4 % de un núcleo).
    Si MMCSS no está disponible, queda la prioridad "más alta" normal. Devuelve el handle de MMCSS (o None)."""
    if sys.platform != "win32":
        return None
    if not _k32.SetThreadPriority(_k32.GetCurrentThread(), THREAD_PRIORITY_HIGHEST):
        log.debug("couldn't raise the priority of the %s thread (error %d)", name, ctypes.get_last_error())
    if _avrt is None:
        return None
    index = wintypes.DWORD(0)
    handle = _avrt.AvSetMmThreadCharacteristicsW(MMCSS_TASK, ctypes.byref(index))
    if not handle:
        log.warning("couldn't register the %s thread with MMCSS (error %d)", name, ctypes.get_last_error())
        return None
    _avrt.AvSetMmThreadPriority(handle, AVRT_PRIORITY_HIGH)
    log.info("%s thread registered with MMCSS (%s)", name, MMCSS_TASK)
    return handle


def revert_thread_priority(handle):
    """Saca de MMCSS al hilo que llama (antes de terminar)."""
    if handle and _avrt is not None:
        _avrt.AvRevertMmThreadCharacteristics(handle)
