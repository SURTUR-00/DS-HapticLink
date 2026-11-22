"""Utilidades de Windows para la app: instancia única, inicio con Windows, portapapeles, elevación."""
import ctypes
import os
import subprocess
import sys
import threading
import winreg
from ctypes import wintypes
from pathlib import Path

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Local\\DSHapticLink.App"
SHOW_EVENT = "Local\\DSHapticLink.Show"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "DSHapticLink"

k32.CreateMutexW.restype = wintypes.HANDLE
k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
k32.CreateEventW.restype = wintypes.HANDLE
k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
k32.OpenEventW.restype = wintypes.HANDLE
k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
k32.SetEvent.argtypes = [wintypes.HANDLE]
k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k32.WaitForSingleObject.restype = wintypes.DWORD


def frozen():
    import __main__
    return getattr(sys, "frozen", False) or hasattr(__main__, "__compiled__")


def app_command(*args):
    """Comando para volver a lanzar la app (inicio con Windows): el .exe, o pythonw + main.py en desarrollo."""
    if frozen():
        exe = Path(sys.argv[0]).resolve()
        return subprocess.list2cmdline([str(exe), *args])
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    main = Path(__file__).resolve().parent / "main.py"
    return subprocess.list2cmdline([str(pythonw if pythonw.exists() else sys.executable), str(main), *args])


# ---------------------------------------------------------------------------------- instancia única
class SingleInstance:
    """La primera instancia se queda con el mutex y espera un evento para mostrarse.
    Las siguientes solo activan ese evento (la primera trae su ventana al frente) y terminan."""

    def __init__(self, on_show):
        self.mutex = k32.CreateMutexW(None, False, MUTEX_NAME)
        self.primary = ctypes.get_last_error() != ERROR_ALREADY_EXISTS
        if self.primary:
            self.event = k32.CreateEventW(None, False, False, SHOW_EVENT)
            threading.Thread(target=self._wait, args=(on_show,), daemon=True, name="instancia").start()

    def _wait(self, on_show):
        while True:
            if k32.WaitForSingleObject(self.event, 0xFFFFFFFF) == 0:
                on_show()

    @staticmethod
    def signal_primary():
        EVENT_MODIFY_STATE = 0x0002
        handle = k32.OpenEventW(EVENT_MODIFY_STATE, False, SHOW_EVENT)
        if handle:
            k32.SetEvent(handle)
            k32.CloseHandle(handle)


# ---------------------------------------------------------------------------------- inicio con Windows
def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
            return True
    except OSError:
        return False


def set_autostart(enabled):
    """Sin administrador: la clave Run del usuario."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, app_command("--minimized"))
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------------- portapapeles
def copy_to_clipboard(text):
    CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    data = text.encode("utf-16-le") + b"\0\0"
    if not user32.OpenClipboard(None):
        return False
    try:
        user32.EmptyClipboard()
        handle = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        ptr = k32.GlobalLock(handle)
        ctypes.memmove(ptr, data, len(data))
        k32.GlobalUnlock(handle)
        return bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
    finally:
        user32.CloseClipboard()


# ---------------------------------------------------------------------------------- elevación
class SHELLEXECUTEINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]


def helper_command(*args):
    """helper/dsh-helper.exe junto a la app; en desarrollo, python helper/dsh_helper.py."""
    if frozen():
        return str(Path(sys.argv[0]).resolve().parent / "helper" / "dsh-helper.exe"), list(args)
    script = Path(__file__).resolve().parent.parent / "helper" / "dsh_helper.py"
    return sys.executable, [str(script), *args]


def run_elevated(exe, args):
    """Lanza exe como administrador (muestra el UAC) y espera a que termine. Devuelve el código de salida,
    o None si el usuario rechazó el UAC."""
    SEE_MASK_NOCLOSEPROCESS, SW_HIDE = 0x40, 0
    info = SHELLEXECUTEINFO(cbSize=ctypes.sizeof(SHELLEXECUTEINFO), fMask=SEE_MASK_NOCLOSEPROCESS,
                            lpVerb="runas", lpFile=exe, lpParameters=subprocess.list2cmdline(args),
                            lpDirectory=str(Path(exe).parent), nShow=SW_HIDE)
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        return None
    k32.WaitForSingleObject(info.hProcess, 0xFFFFFFFF)
    code = wintypes.DWORD()
    k32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    k32.CloseHandle(info.hProcess)
    return code.value


def open_bluetooth_settings():
    os.startfile("ms-settings:bluetooth")


def open_power_settings(reason=None):
    """Donde se arregla cada caso: los planes están en el Panel de control; el modo de energía y el ahorro de
    energía, en Configuración → Sistema → Energía."""
    os.startfile("powercfg.cpl" if reason == "plan" else "ms-settings:powersleep")


# ---------------------------------------------------------------------- plan de energía
# El plan "Economizador" (o una copia suya), el modo "Mejor eficiencia energética" de Windows 11 y el "Ahorro de
# energía" frenan el procesador y el USB/Bluetooth: el audio del puente llega tarde y se entrecorta. Visto en un
# registro real (2026-11-21): PC de escritorio en Economizador sin que el usuario lo recordara.
POWER_SAVER = "a1841308-3541-4fab-bc81-f71556f20b4a"  # SCHEME_MAX
BEST_EFFICIENCY_OVERLAY = "961cc777-2547-4f9d-8174-7d86181b8a7a"  # modo de energía "Mejor eficiencia energética"
PERSONALITY = "245d8541-3943-4422-b025-13a784f679b7"  # GUID_POWERSCHEME_PERSONALITY: plan base de cada combinación


class _GUID(ctypes.Structure):
    _fields_ = [("data", ctypes.c_ubyte * 16)]

    @classmethod
    def parse(cls, text):
        import uuid
        g = cls()
        ctypes.memmove(g.data, uuid.UUID(text).bytes_le, 16)
        return g

    def __str__(self):
        import uuid
        return str(uuid.UUID(bytes_le=bytes(self.data)))


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte), ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte), ("BatteryLifeTime", wintypes.DWORD),
                ("BatteryFullLifeTime", wintypes.DWORD)]


def power_plan():
    """{"name", "guid", "power_saver": bool, "reason"} del plan de energía activo (None si no se pudo leer).
    reason: "plan" (Economizador o una copia), "mode" (modo Mejor eficiencia) o "saver" (Ahorro de energía)."""
    try:
        pp = ctypes.WinDLL("powrprof")
    except OSError:
        return None
    active = ctypes.POINTER(_GUID)()
    if pp.PowerGetActiveScheme(None, ctypes.byref(active)) != 0:
        return None
    scheme = _GUID()
    ctypes.memmove(scheme.data, active.contents.data, 16)
    k32.LocalFree(active)
    base, kind, size = _GUID(), wintypes.DWORD(), wintypes.DWORD(16)
    personality = _GUID.parse(PERSONALITY)
    if pp.PowerReadACValue(None, ctypes.byref(scheme), None, ctypes.byref(personality), ctypes.byref(kind),
                           base.data, ctypes.byref(size)) != 0 or size.value != 16:
        base = scheme
    name_buf, name_size = (ctypes.c_ubyte * 512)(), wintypes.DWORD(512)
    name = ""
    if pp.PowerReadFriendlyName(None, ctypes.byref(scheme), None, None, name_buf, ctypes.byref(name_size)) == 0:
        name = bytes(name_buf[:name_size.value]).decode("utf-16-le", "replace").rstrip("\0")
    reason = None
    if str(base) == POWER_SAVER or str(scheme) == POWER_SAVER:
        reason = "plan"
    else:
        overlay = _GUID()
        getter = getattr(pp, "PowerGetEffectiveOverlayScheme", None)  # Windows 10 1709+
        if getter is not None and getter(ctypes.byref(overlay)) == 0 and str(overlay) == BEST_EFFICIENCY_OVERLAY:
            reason = "mode"
        status = _SystemPowerStatus()
        if reason is None and k32.GetSystemPowerStatus(ctypes.byref(status)) and status.SystemStatusFlag == 1:
            reason = "saver"
    return {"name": name, "guid": str(scheme), "power_saver": reason is not None, "reason": reason}
