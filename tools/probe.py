"""Lista los DualSense conectados y muestra sus capacidades HID (tamaños de reporte)."""
import ctypes
from ctypes import wintypes

import hid

SONY_VID = 0x054C
DUALSENSE_PIDS = {0x0CE6: "DualSense", 0x0DF2: "DualSense Edge"}
BUS_NAMES = {1: "USB", 2: "Bluetooth"}


class HIDP_CAPS(ctypes.Structure):
    _fields_ = [
        ("Usage", wintypes.USHORT),
        ("UsagePage", wintypes.USHORT),
        ("InputReportByteLength", wintypes.USHORT),
        ("OutputReportByteLength", wintypes.USHORT),
        ("FeatureReportByteLength", wintypes.USHORT),
        ("Reserved", wintypes.USHORT * 17),
        ("NumberLinkCollectionNodes", wintypes.USHORT),
        ("NumberInputButtonCaps", wintypes.USHORT),
        ("NumberInputValueCaps", wintypes.USHORT),
        ("NumberInputDataIndices", wintypes.USHORT),
        ("NumberOutputButtonCaps", wintypes.USHORT),
        ("NumberOutputValueCaps", wintypes.USHORT),
        ("NumberOutputDataIndices", wintypes.USHORT),
        ("NumberFeatureButtonCaps", wintypes.USHORT),
        ("NumberFeatureValueCaps", wintypes.USHORT),
        ("NumberFeatureDataIndices", wintypes.USHORT),
    ]


class HIDP_VALUE_CAPS(ctypes.Structure):
    # Solo necesitamos ReportID, ReportCount y BitSize; el resto se deja como relleno.
    _fields_ = [
        ("UsagePage", wintypes.USHORT),
        ("ReportID", ctypes.c_ubyte),
        ("IsAlias", ctypes.c_ubyte),
        ("BitField", wintypes.USHORT),
        ("LinkCollection", wintypes.USHORT),
        ("LinkUsage", wintypes.USHORT),
        ("LinkUsagePage", wintypes.USHORT),
        ("IsRange", ctypes.c_ubyte),
        ("IsStringRange", ctypes.c_ubyte),
        ("IsDesignatorRange", ctypes.c_ubyte),
        ("IsAbsolute", ctypes.c_ubyte),
        ("HasNull", ctypes.c_ubyte),
        ("Reserved", ctypes.c_ubyte),
        ("BitSize", wintypes.USHORT),
        ("ReportCount", wintypes.USHORT),
        ("Reserved2", wintypes.USHORT * 5),
        ("UnitsExp", wintypes.ULONG),
        ("Units", wintypes.ULONG),
        ("LogicalMin", wintypes.LONG),
        ("LogicalMax", wintypes.LONG),
        ("PhysicalMin", wintypes.LONG),
        ("PhysicalMax", wintypes.LONG),
        ("Range", wintypes.USHORT * 8),
    ]


HidP_Input, HidP_Output, HidP_Feature = 0, 1, 2

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
hiddll = ctypes.WinDLL("hid")
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
hiddll.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
hiddll.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hiddll.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(HIDP_CAPS)]
hiddll.HidP_GetValueCaps.argtypes = [ctypes.c_int, ctypes.POINTER(HIDP_VALUE_CAPS),
                                     ctypes.POINTER(wintypes.USHORT), ctypes.c_void_p]


def report_sizes(preparsed, report_type, count):
    """Devuelve {report_id: bytes} sumando los value caps de cada reporte."""
    if not count:
        return {}
    arr = (HIDP_VALUE_CAPS * count)()
    n = wintypes.USHORT(count)
    hiddll.HidP_GetValueCaps(report_type, arr, ctypes.byref(n), preparsed)
    sizes = {}
    for vc in arr[: n.value]:
        sizes[vc.ReportID] = sizes.get(vc.ReportID, 0) + vc.BitSize * vc.ReportCount // 8
    return {rid: size + 1 for rid, size in sorted(sizes.items())}  # +1 por el byte de ID


def win_caps(path):
    handle = k32.CreateFileW(path, 0, 3, None, 3, 0, None)  # sin acceso de lectura/escritura: solo consulta
    if handle in (None, wintypes.HANDLE(-1).value):
        return None
    preparsed = ctypes.c_void_p()
    try:
        if not hiddll.HidD_GetPreparsedData(handle, ctypes.byref(preparsed)):
            return None
        caps = HIDP_CAPS()
        hiddll.HidP_GetCaps(preparsed, ctypes.byref(caps))
        return caps, {
            "input": report_sizes(preparsed, HidP_Input, caps.NumberInputValueCaps),
            "output": report_sizes(preparsed, HidP_Output, caps.NumberOutputValueCaps),
            "feature": report_sizes(preparsed, HidP_Feature, caps.NumberFeatureValueCaps),
        }
    finally:
        if preparsed:
            hiddll.HidD_FreePreparsedData(preparsed)
        k32.CloseHandle(handle)


def main():
    found = [d for d in hid.enumerate(SONY_VID) if d["product_id"] in DUALSENSE_PIDS]
    if not found:
        print("No se encontró ningún DualSense. ¿Está encendido y conectado?")
        return
    for d in found:
        print(f"== {DUALSENSE_PIDS[d['product_id']]}  bus={BUS_NAMES.get(d.get('bus_type'), d.get('bus_type'))}"
              f"  usage_page=0x{d['usage_page']:04X} usage=0x{d['usage']:04X}")
        print(f"   path: {d['path'].decode(errors='replace')}")
        result = win_caps(d["path"].decode())
        if not result:
            print("   (no se pudieron leer las capacidades)")
            continue
        caps, sizes = result
        print(f"   longitudes máx.: input={caps.InputReportByteLength} output={caps.OutputReportByteLength}"
              f" feature={caps.FeatureReportByteLength}")
        for kind, table in sizes.items():
            listing = ", ".join(f"0x{rid:02X}:{size}" for rid, size in table.items())
            print(f"   {kind:7}: {listing}")


if __name__ == "__main__":
    main()
