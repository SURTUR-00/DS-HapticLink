"""Acceso HID directo con la API de Windows, con opción de apertura EXCLUSIVA (reemplazo de HidHide).

hidapi siempre abre los dispositivos compartidos. Abriendo el control con dwShareMode = 0, Windows rechaza
cualquier otra apertura de lectura/escritura (ERROR_SHARING_VIOLATION): los juegos pueden ver que existe,
pero no usarlo, así que usan el DualSense virtual. No requiere drivers ni permisos de administrador.

Limitación: solo funciona si nadie más lo tenía abierto antes (Steam, DS4Windows, un juego...).

La interfaz imita a hid.device (read/write/get_feature_report/send_feature_report/close/error) para que
DualSenseBT pueda usar cualquiera de los dos.
"""
import ctypes
import logging
from ctypes import wintypes

log = logging.getLogger("exclusive")

GENERIC_READ, GENERIC_WRITE = 0x80000000, 0x40000000
FILE_SHARE_READ, FILE_SHARE_WRITE = 0x1, 0x2
OPEN_EXISTING = 3
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_SHARING_VIOLATION = 32
ERROR_IO_PENDING = 997
WAIT_OBJECT_0, WAIT_TIMEOUT = 0, 0x102
IOCTL_HID_SET_FEATURE = 0xB0191
IOCTL_HID_GET_FEATURE = 0xB0192
INVALID_HANDLE = wintypes.HANDLE(-1).value


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [("Usage", wintypes.USHORT), ("UsagePage", wintypes.USHORT),
                ("InputReportByteLength", wintypes.USHORT), ("OutputReportByteLength", wintypes.USHORT),
                ("FeatureReportByteLength", wintypes.USHORT), ("Reserved", wintypes.USHORT * 17),
                ("Fields", wintypes.USHORT * 10)]


k32 = ctypes.WinDLL("kernel32", use_last_error=True)
hiddll = ctypes.WinDLL("hid", use_last_error=True)

k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
k32.CreateEventW.restype = wintypes.HANDLE
k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
k32.WriteFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p]
k32.GetOverlappedResult.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k32.WaitForSingleObject.restype = wintypes.DWORD
k32.ResetEvent.argtypes = [wintypes.HANDLE]
k32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
k32.CloseHandle.argtypes = [wintypes.HANDLE]
hiddll.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
hiddll.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hiddll.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]


class HIDP_VALUE_CAPS(ctypes.Structure):
    # Solo interesan ReportID, BitSize y ReportCount; el resto es relleno con el tamaño correcto (72 bytes).
    _fields_ = [("UsagePage", wintypes.USHORT), ("ReportID", ctypes.c_ubyte), ("IsAlias", ctypes.c_ubyte),
                ("BitField", wintypes.USHORT), ("LinkCollection", wintypes.USHORT), ("LinkUsage", wintypes.USHORT),
                ("LinkUsagePage", wintypes.USHORT), ("IsRange", ctypes.c_ubyte), ("IsStringRange", ctypes.c_ubyte),
                ("IsDesignatorRange", ctypes.c_ubyte), ("IsAbsolute", ctypes.c_ubyte), ("HasNull", ctypes.c_ubyte),
                ("Reserved", ctypes.c_ubyte), ("BitSize", wintypes.USHORT), ("ReportCount", wintypes.USHORT),
                ("Reserved2", wintypes.USHORT * 5), ("UnitsExp", wintypes.ULONG), ("Units", wintypes.ULONG),
                ("LogicalMin", wintypes.LONG), ("LogicalMax", wintypes.LONG), ("PhysicalMin", wintypes.LONG),
                ("PhysicalMax", wintypes.LONG), ("Range", wintypes.USHORT * 8)]


hiddll.HidP_GetValueCaps.argtypes = [ctypes.c_int, ctypes.POINTER(HIDP_VALUE_CAPS), ctypes.POINTER(wintypes.USHORT),
                                     ctypes.c_void_p]
HIDP_OUTPUT, HIDP_FEATURE = 1, 2


def feature_report_sizes(device):
    """Feature reports que declara un dispositivo HID abierto: ({id: tamaño con el ID}, tamaño de salida)."""
    preparsed = ctypes.c_void_p()
    if not hiddll.HidD_GetPreparsedData(device.handle, ctypes.byref(preparsed)):
        raise _last_error("HidD_GetPreparsedData")
    try:
        caps = HIDP_CAPS()
        hiddll.HidP_GetCaps(preparsed, ctypes.byref(caps))
        # Fields = NumberLinkCollectionNodes, Input(Button,Value,DataIndices)Caps, Output(...), Feature(...):
        # el índice 8 es NumberFeatureValueCaps.
        count = caps.Fields[8]
        sizes = {}
        if count:
            arr = (HIDP_VALUE_CAPS * count)()
            n = wintypes.USHORT(count)
            hiddll.HidP_GetValueCaps(HIDP_FEATURE, arr, ctypes.byref(n), preparsed)
            for vc in arr[:n.value]:
                sizes[vc.ReportID] = sizes.get(vc.ReportID, 0) + vc.BitSize * vc.ReportCount // 8
        return {rid: size + 1 for rid, size in sorted(sizes.items())}, caps.OutputReportByteLength
    finally:
        hiddll.HidD_FreePreparsedData(preparsed)


class ExclusiveAccessError(OSError):
    """Otro programa ya tiene abierto el dispositivo, así que no se puede abrir en exclusiva."""


def _last_error(msg):
    err = ctypes.get_last_error()
    return OSError(err, f"{msg}: {ctypes.FormatError(err).strip()} ({err})")


class WinHidDevice:
    def __init__(self, path: str, exclusive: bool = True):
        share = 0 if exclusive else FILE_SHARE_READ | FILE_SHARE_WRITE
        h = k32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, share, None, OPEN_EXISTING, FILE_FLAG_OVERLAPPED, None)
        if h in (None, INVALID_HANDLE):
            if ctypes.get_last_error() == ERROR_SHARING_VIOLATION:
                raise ExclusiveAccessError(ERROR_SHARING_VIOLATION, "another program has the device open")
            raise _last_error("couldn't open the device")
        self.handle = h
        self.path = path
        self.exclusive = exclusive
        preparsed = ctypes.c_void_p()
        caps = HIDP_CAPS()
        if hiddll.HidD_GetPreparsedData(h, ctypes.byref(preparsed)):
            hiddll.HidP_GetCaps(preparsed, ctypes.byref(caps))
            hiddll.HidD_FreePreparsedData(preparsed)
        self.input_len = caps.InputReportByteLength or 64
        self.output_len = caps.OutputReportByteLength or 64
        self.feature_len = caps.FeatureReportByteLength or 64
        # La lectura queda pendiente entre llamadas (como hidapi): así un read con timeout no pierde reportes.
        self._read_ol = OVERLAPPED(hEvent=k32.CreateEventW(None, True, False, None))
        self._read_buf = ctypes.create_string_buffer(self.input_len)
        self._read_pending = False
        self._write_ol = OVERLAPPED(hEvent=k32.CreateEventW(None, True, False, None))
        self._last_error = ""

    # ------------------------------------------------------------------ interfaz tipo hid.device
    def read(self, size=None, timeout_ms=100):
        """Devuelve el siguiente reporte de entrada (lista de bytes, con el ID), o [] si no llegó a tiempo.
        Lanza OSError si el dispositivo se desconectó."""
        if not self._read_pending:
            k32.ResetEvent(self._read_ol.hEvent)
            ok = k32.ReadFile(self.handle, self._read_buf, self.input_len, None, ctypes.byref(self._read_ol))
            if not ok and ctypes.get_last_error() != ERROR_IO_PENDING:
                raise _last_error("read error")
            self._read_pending = True
        if k32.WaitForSingleObject(self._read_ol.hEvent, int(timeout_ms)) == WAIT_TIMEOUT:
            return []
        n = wintypes.DWORD()
        ok = k32.GetOverlappedResult(self.handle, ctypes.byref(self._read_ol), ctypes.byref(n), False)
        self._read_pending = False
        if not ok:
            raise _last_error("read error")
        return list(self._read_buf.raw[:n.value])

    def write(self, data):
        """Escribe un reporte de salida. Windows exige el tamaño máximo de salida, así que se rellena."""
        buf = ctypes.create_string_buffer(bytes(data).ljust(self.output_len, b"\0")[:self.output_len])
        k32.ResetEvent(self._write_ol.hEvent)
        ok = k32.WriteFile(self.handle, buf, self.output_len, None, ctypes.byref(self._write_ol))
        if not ok and ctypes.get_last_error() != ERROR_IO_PENDING:
            self._last_error = ctypes.FormatError(ctypes.get_last_error())
            return -1
        if k32.WaitForSingleObject(self._write_ol.hEvent, 1000) != WAIT_OBJECT_0:
            k32.CancelIoEx(self.handle, ctypes.byref(self._write_ol))
            self._last_error = "timeout de escritura"
            return -1
        n = wintypes.DWORD()
        if not k32.GetOverlappedResult(self.handle, ctypes.byref(self._write_ol), ctypes.byref(n), False):
            self._last_error = ctypes.FormatError(ctypes.get_last_error())
            return -1
        return n.value

    def _feature_ioctl(self, code, buf, out_len):
        ol = OVERLAPPED(hEvent=k32.CreateEventW(None, True, False, None))
        try:
            n = wintypes.DWORD()
            ok = k32.DeviceIoControl(self.handle, code, buf, self.feature_len, buf if out_len else None, out_len,
                                     ctypes.byref(n), ctypes.byref(ol))
            if not ok and ctypes.get_last_error() != ERROR_IO_PENDING:
                raise _last_error("feature report")
            if not k32.GetOverlappedResult(self.handle, ctypes.byref(ol), ctypes.byref(n), True):
                raise _last_error("feature report")
            return n.value
        finally:
            k32.CloseHandle(ol.hEvent)

    def get_feature_report(self, report_id, size):
        buf = ctypes.create_string_buffer(self.feature_len)
        buf[0] = report_id
        self._feature_ioctl(IOCTL_HID_GET_FEATURE, buf, self.feature_len)
        return list(buf.raw[:size])

    def send_feature_report(self, data):
        buf = ctypes.create_string_buffer(bytes(data).ljust(self.feature_len, b"\0")[:self.feature_len])
        self._feature_ioctl(IOCTL_HID_SET_FEATURE, buf, 0)
        return len(data)

    def error(self):
        return self._last_error

    def close(self):
        if self.handle:
            k32.CancelIoEx(self.handle, None)
            k32.CloseHandle(self.handle)
            k32.CloseHandle(self._read_ol.hEvent)
            k32.CloseHandle(self._write_ol.hEvent)
            self.handle = None


class ExclusiveHolder:
    """Mantiene abiertos en exclusiva dispositivos que los juegos no deben usar (p. ej. el control por cable
    mientras solo carga y el puente sigue por Bluetooth). Es lo que en V1 hacía HidHide."""

    def __init__(self):
        self.devices = {}

    def hold(self, path):
        if path in self.devices:
            return True
        try:
            self.devices[path] = WinHidDevice(path, exclusive=True)
            log.info("cable-connected controller blocked for games while charging")
            return True
        except ExclusiveAccessError:
            pass
        except OSError as e:
            log.debug("couldn't open %s: %s", path, e)
            return False
        # Windows ya lo abrió: reiniciarlo y tomarlo en cuanto reaparezca (como con el control Bluetooth).
        from . import devrestart
        import time
        if devrestart.request_restart([devrestart.instance_id_from_hid_path(path)]) is None:
            log.warning("couldn't block the cable-connected controller: Windows has it open")
            return False
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                self.devices[path] = WinHidDevice(path, exclusive=True)
                log.info("cable-connected controller restarted and blocked for games while charging")
                return True
            except OSError:
                time.sleep(0.001)
        log.warning("couldn't block the cable-connected controller after restarting it")
        return False

    def release(self, path):
        dev = self.devices.pop(path, None)
        if dev is not None:
            dev.close()
            log.info("cable-connected controller released")

    def release_all(self):
        for path in list(self.devices):
            self.release(path)
