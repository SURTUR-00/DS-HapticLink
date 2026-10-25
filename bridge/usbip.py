"""Servidor USB/IP mínimo (protocolo 1.1.1) que expone un único dispositivo virtual.

Especificación: https://docs.kernel.org/usb/usbip_protocol.html
El dispositivo implementa `handle_urb(urb)` como corrutina y devuelve un `UrbResult`.
"""
import asyncio
import logging
import struct
from dataclasses import dataclass, field

log = logging.getLogger("usbip")

USBIP_VERSION = 0x0111
OP_REQ_DEVLIST, OP_REP_DEVLIST = 0x8005, 0x0005
OP_REQ_IMPORT, OP_REP_IMPORT = 0x8003, 0x0003
CMD_SUBMIT, CMD_UNLINK, RET_SUBMIT, RET_UNLINK = 1, 2, 3, 4
DIR_OUT, DIR_IN = 0, 1
ECONNRESET = -104
EPIPE = -32  # STALL
ISO_NO_PACKETS = 0xFFFFFFFF
SPEED_HIGH = 3


@dataclass
class IsoPacket:
    offset: int
    length: int
    actual_length: int = 0
    status: int = 0


@dataclass
class Urb:
    seqnum: int
    devid: int
    direction: int
    ep: int
    transfer_flags: int
    buffer_length: int
    start_frame: int
    interval: int
    setup: bytes
    data: bytes = b""
    iso: list = None  # lista de IsoPacket, o None si no es isócrono


@dataclass
class UrbResult:
    status: int = 0
    data: bytes = b""  # para IN: datos devueltos (en isócrono, el búfer completo con huecos)
    actual_length: int = None  # para OUT: bytes aceptados; None = len(data) (IN) o buffer_length (OUT)
    iso: list = None
    start_frame: int = 0
    error_count: int = 0


@dataclass
class DeviceInfo:
    busid: str
    vid: int
    pid: int
    bcd_device: int
    dev_class: int
    dev_subclass: int
    dev_protocol: int
    configuration_value: int
    num_configurations: int
    interfaces: list  # [(clase, subclase, protocolo), ...]
    busnum: int = 1
    devnum: int = 1
    speed: int = SPEED_HIGH
    path: str = "/sys/devices/dshapticlink/virtual-dualsense"

    def pack(self, with_interfaces: bool):
        out = struct.pack(">256s32sIIIHHHBBBBBB", self.path.encode(), self.busid.encode(), self.busnum,
                          self.devnum, self.speed, self.vid, self.pid, self.bcd_device, self.dev_class,
                          self.dev_subclass, self.dev_protocol, self.configuration_value,
                          self.num_configurations, len(self.interfaces))
        if with_interfaces:
            out += b"".join(struct.pack(">BBBx", *itf) for itf in self.interfaces)
        return out


async def _read_exact(reader, n):
    return await reader.readexactly(n)


class UsbIpServer:
    def __init__(self, device, host="127.0.0.1", port=3240):
        self.device = device
        self.host, self.port = host, port
        self.server = None
        self.attached = asyncio.Event()

    async def start(self):
        loop = asyncio.get_running_loop()
        default = loop.get_exception_handler()

        def quiet_resets(loop, context):
            # En Windows, cuando usbip-win2 cierra la conexión (detach), el proactor de asyncio registra
            # un ConnectionResetError (WinError 10054) al cerrar el socket. Es inofensivo: no ensuciar el log.
            if isinstance(context.get("exception"), (ConnectionResetError, ConnectionAbortedError)):
                return
            if default:
                default(loop, context)
            else:
                loop.default_exception_handler(context)

        loop.set_exception_handler(quiet_resets)
        self.server = await asyncio.start_server(self._client, self.host, self.port)
        log.info("USB/IP server listening on %s:%d", self.host, self.port)

    async def _client(self, reader, writer):
        peer = writer.get_extra_info("peername")
        try:
            version, code, _status = struct.unpack(">HHI", await _read_exact(reader, 8))
            info = self.device.info
            if code == OP_REQ_DEVLIST:
                writer.write(struct.pack(">HHII", USBIP_VERSION, OP_REP_DEVLIST, 0, 1) + info.pack(True))
                await writer.drain()
            elif code == OP_REQ_IMPORT:
                busid = (await _read_exact(reader, 32)).rstrip(b"\0").decode()
                if busid != info.busid:
                    writer.write(struct.pack(">HHI", USBIP_VERSION, OP_REP_IMPORT, 1))
                    await writer.drain()
                    return
                writer.write(struct.pack(">HHI", USBIP_VERSION, OP_REP_IMPORT, 0) + info.pack(False))
                await writer.drain()
                log.info("device imported by %s", peer)
                self.attached.set()
                await self._urb_loop(reader, writer)
            else:
                log.warning("unknown operation 0x%04X from %s", code, peer)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            if self.attached.is_set():
                log.info("client %s disconnected", peer)
                self.attached.clear()
                self.device.on_detach()
            writer.close()

    async def _urb_loop(self, reader, writer):
        pending = {}  # seqnum -> Task
        write_lock = asyncio.Lock()

        async def send(data):
            async with write_lock:
                writer.write(data)
                await writer.drain()

        async def run_urb(urb):
            try:
                result = await self.device.handle_urb(urb)
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("error handling URB on ep%d", urb.ep)
                result = UrbResult(status=EPIPE)
            finally:
                pending.pop(urb.seqnum, None)
            try:
                await send(self._pack_ret_submit(urb, result))
            except (ConnectionError, OSError):
                pass  # el cliente se fue (p. ej. usbip detach): no hay a quién responder

        while True:
            header = await _read_exact(reader, 48)
            command, seqnum, devid, direction, ep = struct.unpack_from(">IIIII", header, 0)
            if command == CMD_SUBMIT:
                flags, buf_len, start_frame, num_packets, interval = struct.unpack_from(">IiiiI", header, 20)
                setup = header[40:48]
                data = await _read_exact(reader, buf_len) if direction == DIR_OUT and buf_len > 0 else b""
                iso = None
                if num_packets not in (0, -1) and num_packets != ISO_NO_PACKETS:
                    raw = await _read_exact(reader, 16 * num_packets)
                    iso = [IsoPacket(*struct.unpack_from(">IIIi", raw, 16 * k)) for k in range(num_packets)]
                urb = Urb(seqnum, devid, direction, ep, flags, buf_len, start_frame, interval, setup, data, iso)
                pending[seqnum] = asyncio.create_task(run_urb(urb))
            elif command == CMD_UNLINK:
                unlink_seq = struct.unpack_from(">I", header, 20)[0]
                task = pending.pop(unlink_seq, None)
                status = 0
                if task and not task.done():
                    task.cancel()
                    status = ECONNRESET
                await send(struct.pack(">IIIIIi24x", RET_UNLINK, seqnum, 0, 0, 0, status))
            else:
                raise ConnectionError(f"unknown USB/IP command {command}")

    @staticmethod
    def _pack_ret_submit(urb, r: UrbResult):
        if urb.direction == DIR_IN:
            payload = r.data
            actual = len(r.data) if r.actual_length is None else r.actual_length
        else:
            payload = b""
            actual = urb.buffer_length if r.actual_length is None else r.actual_length
        iso = r.iso if urb.iso is not None else None
        num_packets = len(iso) if iso is not None else ISO_NO_PACKETS
        if iso is not None and urb.direction == DIR_IN:
            # En isócrono IN se envían solo los bytes reales de cada paquete, uno tras otro.
            payload = b"".join(r.data[p.offset:p.offset + p.actual_length] for p in iso)
            actual = sum(p.actual_length for p in iso)
        out = struct.pack(">IIIIIiiiii8x", RET_SUBMIT, urb.seqnum, 0, 0, 0, r.status, actual, r.start_frame,
                          num_packets if iso is not None else -1, r.error_count)
        out += payload
        if iso is not None:
            out += b"".join(struct.pack(">IIIi", p.offset, p.length, p.actual_length, p.status) for p in iso)
        return out
