"""Tiempo real en Windows: precisión del reloj y prioridad de los hilos de audio.

Con el reloj normal de Windows (15,6 ms) cada pausa corta dura ~16 ms: medido, asyncio.sleep(0.002) tardaba
15,8 ms de media. El puente marca con esas pausas el ritmo de los URB isócronos (el audio del juego, del que
salen la háptica y el sonido del jack), así que el audio avanzaba a saltos de 16 ms y la reserva de háptica se
vaciaba de vez en cuando (microcortes, más notorios en juegos de háptica continua como Stellar Blade).
Con timeBeginPeriod(1), lo que usan juegos y reproductores, la misma pausa tarda ~3 ms.
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


def enable_high_resolution_timer():
    """Reloj del sistema a 1 ms mientras el puente corre (Windows lo restaura al terminar el proceso)."""
    global _timer_active
    if sys.platform != "win32" or _timer_active:
        return _timer_active
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


def raise_thread_priority(name):
    """Prioridad alta para el hilo que llama (los del audio: el de asyncio/USB y el que envía la háptica)."""
    if sys.platform == "win32" and not _k32.SetThreadPriority(_k32.GetCurrentThread(), THREAD_PRIORITY_HIGHEST):
        log.debug("couldn't raise the priority of the %s thread (error %d)", name, ctypes.get_last_error())
