"""Lee (solo lectura) los descriptores USB y los feature reports de un DualSense conectado por cable.

Usa las mismas IOCTL del hub que USBView. Guarda todo en results/usb_dualsense.json para clonar el dispositivo.
"""
import ctypes
import json
import struct
from ctypes import wintypes
from pathlib import Path

import hid

VID, PID = 0x054C, 0x0CE6
RESULTS = Path(__file__).resolve().parent.parent / "results" / "usb_dualsense.json"

cfg = ctypes.WinDLL("cfgmgr32")
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateFileW.restype = wintypes.HANDLE
k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
k32.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]

CR_SUCCESS = 0
CM_DRP_ADDRESS = 0x1D
IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION = 0x220410


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


GUID_DEVINTERFACE_USB_HUB = GUID(0xF18A0E88, 0xC30C, 0x11D0,
                                 (ctypes.c_ubyte * 8)(0x88, 0x15, 0x00, 0xA0, 0xC9, 0x06, 0xBE, 0xD8))


def find_usb_parent_device():
    """Devuelve el instance id del dispositivo compuesto USB del DualSense."""
    size = wintypes.ULONG()
    cfg.CM_Get_Device_ID_List_SizeW(ctypes.byref(size), f"USB\\VID_{VID:04X}&PID_{PID:04X}", 0)
    buf = ctypes.create_unicode_buffer(size.value)
    flags = 0x1  # CM_GETIDLIST_FILTER_ENUMERATOR
    if cfg.CM_Get_Device_ID_ListW("USB", buf, size, flags) != CR_SUCCESS:
        return None
    for dev_id in buf[:].split("\0"):
        if dev_id.upper().startswith(f"USB\\VID_{VID:04X}&PID_{PID:04X}\\"):
            return dev_id
    return None


def hub_and_port(dev_id):
    devinst = wintypes.DWORD()
    if cfg.CM_Locate_DevNodeW(ctypes.byref(devinst), dev_id, 0) != CR_SUCCESS:
        raise RuntimeError("no se encontró el nodo del dispositivo (¿está presente?)")
    address, length = wintypes.DWORD(), wintypes.ULONG(4)
    cfg.CM_Get_DevNode_Registry_PropertyW(devinst, CM_DRP_ADDRESS, None, ctypes.byref(address), ctypes.byref(length), 0)
    parent = wintypes.DWORD()
    cfg.CM_Get_Parent(ctypes.byref(parent), devinst, 0)
    parent_id = ctypes.create_unicode_buffer(512)
    cfg.CM_Get_Device_IDW(parent, parent_id, 512, 0)
    size = wintypes.ULONG()
    cfg.CM_Get_Device_Interface_List_SizeW(ctypes.byref(size), ctypes.byref(GUID_DEVINTERFACE_USB_HUB), parent_id, 0)
    paths = ctypes.create_unicode_buffer(size.value)
    cfg.CM_Get_Device_Interface_ListW(ctypes.byref(GUID_DEVINTERFACE_USB_HUB), parent_id, paths, size, 0)
    hub_path = paths[:].split("\0")[0]
    return hub_path, address.value, parent_id.value


def get_descriptor(hub, port, dtype, index=0, lang=0, length=4096):
    # USB_DESCRIPTOR_REQUEST: ULONG ConnectionIndex; SetupPacket (8 bytes); UCHAR Data[]
    req = struct.pack("<IBBHHH", port, 0x80, 6, (dtype << 8) | index, lang, length) + bytes(length)
    buf = ctypes.create_string_buffer(req, len(req))
    returned = wintypes.DWORD()
    ok = k32.DeviceIoControl(hub, IOCTL_USB_GET_DESCRIPTOR_FROM_NODE_CONNECTION, buf, len(req), buf, len(req),
                             ctypes.byref(returned), None)
    if not ok:
        raise OSError(ctypes.get_last_error(), f"GET_DESCRIPTOR tipo {dtype} falló")
    return buf.raw[12:returned.value]


DESC_NAMES = {1: "DEVICE", 2: "CONFIGURATION", 4: "INTERFACE", 5: "ENDPOINT", 0x0B: "IAD",
              0x21: "HID", 0x24: "CS_INTERFACE", 0x25: "CS_ENDPOINT"}
CLASS_NAMES = {1: "Audio", 3: "HID"}
AUDIO_SUBCLASS = {1: "Control", 2: "Streaming"}


def parse_config(raw):
    """Divide el descriptor de configuración en sus descriptores y genera un resumen legible."""
    items, lines, i, current_class = [], [], 0, None
    while i + 2 <= len(raw):
        length, dtype = raw[i], raw[i + 1]
        if length == 0:
            break
        d = raw[i:i + length]
        items.append(d.hex())
        name = DESC_NAMES.get(dtype, f"0x{dtype:02X}")
        if dtype == 4:
            current_class = (d[5], d[6])
            sub = AUDIO_SUBCLASS.get(d[6], d[6]) if d[5] == 1 else d[6]
            lines.append(f"  INTERFACE #{d[2]} alt {d[3]}  clase={CLASS_NAMES.get(d[5], d[5])}/{sub}  endpoints={d[4]}")
        elif dtype == 5:
            ep_type = ["control", "isócrono", "bulk", "interrupción"][d[3] & 3]
            direction = "IN" if d[2] & 0x80 else "OUT"
            max_packet = struct.unpack_from("<H", d, 4)[0]
            lines.append(f"    ENDPOINT 0x{d[2]:02X} {direction} {ep_type}  maxPacket={max_packet}  intervalo={d[6]}")
        elif dtype == 0x24 and current_class == (1, 2) and len(d) >= 3:
            # En una interfaz de streaming, el subtipo 0x02 es FORMAT_TYPE (0x01 = PCM tipo I)
            if d[2] == 2 and length >= 11 and d[3] == 1:
                rates = [int.from_bytes(d[8 + 3 * k:11 + 3 * k], "little") for k in range(d[7])]
                lines.append(f"    FORMAT_TYPE I: canales={d[4]} bytes/muestra={d[5]} bits={d[6]} Hz={rates}")
            else:
                lines.append(f"    {name} subtipo=0x{d[2]:02X}: {d.hex(' ')}")
        elif dtype == 0x21:
            rep_len = struct.unpack_from("<H", d, 7)[0]
            lines.append(f"    HID descriptor: versión {d[3]:x}.{d[2]:02x}, report descriptor de {rep_len} bytes")
        elif dtype == 2:
            total = struct.unpack_from("<H", d, 2)[0]
            lines.append(f"CONFIGURATION: {d[4]} interfaces, total {total} bytes, atributos=0x{d[7]:02X},"
                         f" consumo máx={d[8] * 2} mA")
        else:
            lines.append(f"    {name}: {d.hex(' ')}")
        i += length
    return items, lines


def main():
    dev_id = find_usb_parent_device()
    if not dev_id:
        raise SystemExit("No se encontró un DualSense conectado por USB.")
    hub_path, port, hub_id = hub_and_port(dev_id)
    print(f"Dispositivo: {dev_id}\nHub: {hub_id}  puerto {port}")

    hub = k32.CreateFileW(hub_path, 0x40000000, 0x2, None, 3, 0, None)  # GENERIC_WRITE, FILE_SHARE_WRITE
    if hub in (None, wintypes.HANDLE(-1).value):
        raise SystemExit(f"No se pudo abrir el hub (error {ctypes.get_last_error()})")
    out = {"instance_id": dev_id}
    try:
        device = get_descriptor(hub, port, 1, length=18)
        out["device_descriptor"] = device.hex()
        bcd_usb, cls, sub, proto, mps, vid, pid, bcd_dev, i_man, i_prod, i_ser, n_cfg = struct.unpack_from(
            "<HBBBBHHHBBBB", device, 2)
        print(f"\nDEVICE: USB {bcd_usb:x}  clase={cls}/{sub}/{proto}  maxPacket0={mps}  VID={vid:04X} PID={pid:04X}"
              f"  bcdDevice={bcd_dev:04X}  strings(man={i_man}, prod={i_prod}, serie={i_ser})  configs={n_cfg}")

        head = get_descriptor(hub, port, 2, length=9)
        total = struct.unpack_from("<H", head, 2)[0]
        config = get_descriptor(hub, port, 2, length=total)
        out["config_descriptor"] = config.hex()
        items, lines = parse_config(config)
        out["config_items"] = items
        print()
        print("\n".join(lines))

        strings = {}
        langs = get_descriptor(hub, port, 3, 0, 0, 255)
        lang = struct.unpack_from("<H", langs, 2)[0] if len(langs) >= 4 else 0x0409
        out["string_langs"] = langs.hex()
        for idx in sorted({i_man, i_prod, i_ser} - {0}):
            s = get_descriptor(hub, port, 3, idx, lang, 255)
            strings[idx] = s[2:].decode("utf-16-le", errors="replace")
        out["strings"] = strings
        print(f"\nSTRINGS: {strings}")
    finally:
        k32.CloseHandle(hub)

    # Feature reports por la interfaz HID USB (las mismas que lee DS5Dongle al conectar)
    features = {}
    usb_hid = next((d for d in hid.enumerate(VID, PID) if d.get("bus_type") == 1), None)
    if usb_hid:
        dev = hid.device()
        dev.open_path(usb_hid["path"])
        report_desc = None
        try:
            report_desc = bytes(dev.get_report_descriptor())
        except (AttributeError, OSError):
            pass
        if report_desc:
            out["hid_report_descriptor_reconstructed"] = report_desc.hex()
            print(f"\nHID report descriptor (reconstruido por Windows): {len(report_desc)} bytes")
        for rid, size in ((0x05, 41), (0x09, 20), (0x20, 64), (0x22, 64)):
            try:
                data = bytes(dev.get_feature_report(rid, size))
                features[f"0x{rid:02X}"] = data.hex()
                print(f"FEATURE 0x{rid:02X}: {len(data)} bytes  {data[:16].hex(' ')}…")
            except OSError as e:
                print(f"FEATURE 0x{rid:02X}: error {e}")
        dev.close()
    out["feature_reports"] = features

    RESULTS.parent.mkdir(exist_ok=True)
    RESULTS.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nGuardado en {RESULTS}")


if __name__ == "__main__":
    main()
