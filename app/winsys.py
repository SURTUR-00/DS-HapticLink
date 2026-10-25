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
