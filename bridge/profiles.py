"""Perfiles de control: con qué descriptores USB se presenta el DualSense virtual.

- Perfil incluido: el DualSense estándar (PID 0CE6), capturado una vez del control de referencia. Sirve para
  cualquier DualSense normal; sus datos propios (calibración, MAC, firmware) se leen en vivo por Bluetooth.
- Perfil capturado por cable: para modelos o formatos que el incluido no cubre (p. ej. DualSense Edge 0DF2).
  Cuando el control se conecta por USB, la app lee sus descriptores (IOCTL del hub, como USBView) y sus feature
  reports, y lo guarda en el registro de controles.

Windows no permite leer el descriptor HID crudo sin un driver: para un perfil capturado se RECONSTRUYE a
partir del incluido (entradas y salida del gamepad, que son iguales en toda la familia) más la lista de
feature reports y sus tamaños que informa Windows (HidP_GetValueCaps).
"""
import ctypes
import logging
import struct
from ctypes import wintypes
from dataclasses import dataclass, field

from . import descriptors as D

log = logging.getLogger("profiles")

BUILTIN_PID = 0x0CE6
MODEL_NAMES = {0x0CE6: "DualSense", 0x0DF2: "DualSense Edge"}


@dataclass
class Profile:
    source: str  # "incluido" | "cable"
    device: bytes
    config: bytes
    hid_report: bytes
    strings: dict
    features: dict = field(default_factory=dict)

    @property
    def pid(self):
        return struct.unpack_from("<H", self.device, 10)[0]

    @property
    def bcd_device(self):
        return struct.unpack_from("<H", self.device, 12)[0]

    def to_json(self):
        return {"source": self.source, "device": self.device.hex(), "config": self.config.hex(),
                "hid_report": self.hid_report.hex(), "strings": {str(k): v for k, v in self.strings.items()},
                "features": {f"{k:02X}": v.hex() for k, v in self.features.items()}}

    @classmethod
    def from_json(cls, d):
        return cls(d["source"], bytes.fromhex(d["device"]), bytes.fromhex(d["config"]),
                   bytes.fromhex(d["hid_report"]), {int(k): v for k, v in d["strings"].items()},
                   {int(k, 16): bytes.fromhex(v) for k, v in d["features"].items()})


def builtin_profile():
    return Profile("incluido", D.DEVICE, D.CONFIG, D.HID_REPORT, dict(D.STRINGS), dict(D.FEATURE_FALLBACK))


# ---------------------------------------------------------------------------------- identidad del control
def mac_from_pairing_report(report):
    """Feature 0x09: bytes 1-6 = MAC del control, al revés. Devuelve 'AA:BB:CC:DD:EE:FF' o None."""
    if not report or len(report) < 7 or report[0] != 0x09:
        return None
    mac = bytes(report[1:7])[::-1]
    if not any(mac):
        return None
    return ":".join(f"{b:02X}" for b in mac)


def firmware_from_info_report(report):
    """Feature 0x20 (como el driver de Linux): fecha y hora de compilación en texto, versión de hardware
    (le32 en 24) y de firmware (le32 en 28)."""
    if not report or len(report) < 32 or report[0] != 0x20:
        return None
    built = bytes(report[1:20]).decode("ascii", errors="replace").strip("\0 ")
    hw, fw = struct.unpack_from("<II", bytes(report), 24)
    return {"build": built, "hardware": f"0x{hw:08X}", "firmware": f"0x{fw:08X}"}


# ---------------------------------------------------------------------------------- descriptor HID
def _builtin_prefix():
    """Del descriptor incluido: todo hasta el reporte de salida 0x02 inclusive (entradas + salida)."""
    data = D.HID_REPORT
    i = data.find(bytes([0x85, 0x02]))  # Report ID (2)
    j = data.find(bytes([0x91, 0x02]), i)  # Output (Data,Var,Abs)
    if i < 0 or j < 0:
        raise ValueError("unexpected built-in descriptor")
    return data[:j + 2]


def builtin_feature_ids():
    """IDs de feature report que declara el descriptor incluido (entradas 85 <id> ... B1 02)."""
    data, ids, i = D.HID_REPORT, set(), len(_builtin_prefix())
    current = None
    while i < len(data) - 1:
        if data[i] == 0x85:
            current = data[i + 1]
            i += 2
            continue
        if data[i] == 0xB1 and current is not None:
            ids.add(current)
            current = None
        i += 1
    return ids


def build_hid_report(feature_sizes, output_len=48):
    """Descriptor HID: el gamepad del perfil incluido + un feature report vendor por cada ID capturado.
    feature_sizes: {id: tamaño total en bytes, con el ID}. output_len: tamaño del reporte de salida 0x02."""
    prefix = bytearray(_builtin_prefix())
    if output_len != 48:  # ajustar el Report Count (0x95) justo antes del Output 0x02
        k = prefix.rfind(bytes([0x95]))
        prefix[k + 1] = output_len - 1
    out = bytes(prefix)
    for n, (rid, size) in enumerate(sorted(feature_sizes.items())):
        count = size - 1
        usage = 0x30 + n
        out += bytes([0x85, rid, 0x09, usage & 0xFF])
        out += bytes([0x95, count]) if count < 256 else bytes([0x96, count & 0xFF, count >> 8])
        out += bytes([0xB1, 0x02])
    return out + bytes([0xC0])


def patch_config_hid_length(config, hid_len):
    """El descriptor HID de la configuración declara cuánto mide el descriptor de reportes: actualizarlo."""
    config = bytearray(config)
    i = config.find(bytes([9, 0x21]))
    if i >= 0:
        struct.pack_into("<H", config, i + 7, hid_len)
    return bytes(config)


# ---------------------------------------------------------------------------------- captura por cable
cfg = ctypes.WinDLL("cfgmgr32")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
CM_DRP_ADDRESS = 0x1D
IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION = 0x220410


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


GUID_DEVINTERFACE_USB_HUB = GUID(0xF18A0E88, 0xC30C, 0x11D0,
                                 (ctypes.c_ubyte * 8)(0x88, 0x15, 0x00, 0xA0, 0xC9, 0x06, 0xBE, 0xD8))


def _devnode(instance):
    dn = wintypes.DWORD()
    if cfg.CM_Locate_DevNodeW(ctypes.byref(dn), instance, 0) != 0:
        raise OSError(f"device not found: {instance}")
    return dn


def _instance_id(dn):
    buf = ctypes.create_unicode_buffer(512)
    cfg.CM_Get_Device_IDW(dn, buf, 512, 0)
    return buf.value


def _parent(dn):
    parent = wintypes.DWORD()
    if cfg.CM_Get_Parent(ctypes.byref(parent), dn, 0) != 0:
        raise OSError("device has no parent")
    return parent


def usb_device_and_hub(hid_path):
    """De la ruta HID (\\\\?\\HID#VID_054C&PID_0CE6&MI_03#...) al dispositivo USB compuesto y su hub.
    Devuelve (ruta de interfaz del hub, número de puerto)."""
    from .devrestart import instance_id_from_hid_path
    dn = _devnode(instance_id_from_hid_path(hid_path))
    for _ in range(3):  # HID → interfaz USB (MI_03) → dispositivo compuesto
        dn = _parent(dn)
        inst = _instance_id(dn).upper()
        if inst.startswith("USB\\") and "&MI_" not in inst:
            break
    else:
        raise OSError("the controller's USB device was not found")
    address, length = wintypes.DWORD(), wintypes.ULONG(4)
    cfg.CM_Get_DevNode_Registry_PropertyW(dn, CM_DRP_ADDRESS, None, ctypes.byref(address), ctypes.byref(length), 0)
    hub_id = _instance_id(_parent(dn))
    size = wintypes.ULONG()
    cfg.CM_Get_Device_Interface_List_SizeW(ctypes.byref(size), ctypes.byref(GUID_DEVINTERFACE_USB_HUB), hub_id, 0)
    paths = ctypes.create_unicode_buffer(max(size.value, 1))
    cfg.CM_Get_Device_Interface_ListW(ctypes.byref(GUID_DEVINTERFACE_USB_HUB), hub_id, paths, size, 0)
    hub_path = paths[:].split("\0")[0]
    if not hub_path:
        raise OSError("the controller's USB hub was not found")
    return hub_path, address.value


def _get_descriptor(hub, port, dtype, index=0, lang=0, length=4096):
    req = struct.pack("<IBBHHH", port, 0x80, 6, (dtype << 8) | index, lang, length) + bytes(length)
    buf = ctypes.create_string_buffer(req, len(req))
    returned = wintypes.DWORD()
    if not k32.DeviceIoControl(hub, IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION, buf, len(req), buf, len(req),
                               ctypes.byref(returned), None):
        raise OSError(ctypes.get_last_error(), f"GET_DESCRIPTOR {dtype} failed")
    return buf.raw[12:returned.value]


def read_usb_descriptors(hid_path):
    """Descriptor de dispositivo, de configuración y textos, leídos del control real por cable."""
    hub_path, port = usb_device_and_hub(hid_path)
    hub = k32.CreateFileW(hub_path, 0x40000000, 0x2, None, 3, 0, None)  # GENERIC_WRITE, FILE_SHARE_WRITE
    if hub in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), "couldn't open the USB hub")
    try:
        device = _get_descriptor(hub, port, 1, length=18)
        head = _get_descriptor(hub, port, 2, length=9)
        config = _get_descriptor(hub, port, 2, length=struct.unpack_from("<H", head, 2)[0])
        langs = _get_descriptor(hub, port, 3, 0, 0, 255)
        lang = struct.unpack_from("<H", langs, 2)[0] if len(langs) >= 4 else 0x0409
        strings = {}
        for idx in sorted({device[14], device[15], device[16]} - {0}):
            s = _get_descriptor(hub, port, 3, idx, lang, 255)
            strings[idx] = s[2:].decode("utf-16-le", errors="replace")
        return device, config, strings
    finally:
        k32.CloseHandle(hub)


def capture_profile(hid_path, hid_device):
    """Perfil completo de un control conectado por cable. hid_device: un WinHidDevice abierto en esa ruta
    (el mismo con el que el puente lo bloquea para los juegos mientras carga)."""
    from .winhid import feature_report_sizes
    device, config, strings = read_usb_descriptors(hid_path)
    sizes, output_len = feature_report_sizes(hid_device)
    features = {}
    for rid, size in sizes.items():
        try:
            features[rid] = bytes(hid_device.get_feature_report(rid, size))
        except OSError:
            pass  # varios feature reports son de solo escritura: es normal que fallen
    pid = struct.unpack_from("<H", device, 10)[0]
    builtin = builtin_profile()
    if pid == BUILTIN_PID and set(sizes) == builtin_feature_ids():
        hid_report = builtin.hid_report  # el mismo formato que el incluido: usar el descriptor verificado
    else:
        hid_report = build_hid_report(sizes, output_len)
    config = patch_config_hid_length(config, len(hid_report))
    profile = Profile("cable", device, config, hid_report, strings, features)
    identity = {"mac": mac_from_pairing_report(features.get(0x09)),
                "firmware": firmware_from_info_report(features.get(0x20)),
                "model": MODEL_NAMES.get(pid, f"PID {pid:04X}"), "pid": pid}
    return profile, identity
