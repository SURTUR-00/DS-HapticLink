"""Pruebas sin hardware del puente. Ejecutar desde la carpeta del proyecto:

    .venv\\Scripts\\python.exe -m unittest discover -s tests -t . -v
"""
import asyncio
import math
import struct
import sys
import time
import unittest
import zlib

from bridge import descriptors as D
from bridge.attach import AttachManager, parse_attached_port
from bridge.btlink import BtLink
from bridge.ds5bt import DualSenseBT, bt_crc
from bridge.haptics import REPORT_BYTES, Decimator, HapticBridge
from bridge.usbip import UsbIpServer
from bridge.virtual_ds5 import NEUTRAL_INPUT, VirtualDualSense


class FakeHid:
    """Sustituye a hid.device: guarda lo que se escribe."""

    def __init__(self):
        self.writes = []

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def get_feature_report(self, rid, size):
        return [rid] + [0] * (size - 1)

    def send_feature_report(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def read(self, size, timeout):
        return []

    def close(self):
        pass


def fake_dualsense():
    ds = DualSenseBT.__new__(DualSenseBT)
    ds.dev = FakeHid()
    ds.seq = 0
    ds.haptic_counter = 0
    import threading
    ds.lock = threading.Lock()
    return ds


def crc_ok(report, header=0xA2):
    return zlib.crc32(bytes([header]) + report[:-4]).to_bytes(4, "little") == report[-4:]


class ProtocolTests(unittest.TestCase):
    def test_crc_matches_seeded_reference(self):
        # Implementación de referencia de SAxense: CRC32 con semilla precalculada para el byte 0xA2.
        def ref(data):
            crc = ~0xEADA2D49 & 0xFFFFFFFF
            for b in data:
                crc ^= b
                for _ in range(8):
                    crc = (crc >> 1) ^ (0xEDB88320 & -(crc & 1) & 0xFFFFFFFF)
            return (~crc & 0xFFFFFFFF).to_bytes(4, "little")
        data = bytes(range(70))
        self.assertEqual(bt_crc(data), ref(data))

    def test_usb_output_becomes_bt_0x31(self):
        ds = fake_dualsense()
        usb = bytearray(48)
        usb[0], usb[1], usb[2], usb[45:48] = 0x02, 0x0C, 0x04, b"\x11\x22\x33"
        ds.send_usb_output(bytes(usb))
        out = ds.dev.writes[-1]
        self.assertEqual(len(out), 78)
        self.assertEqual(out[0], 0x31)
        self.assertEqual(out[2], 0x10)
        self.assertEqual(out[3:50], bytes(usb[1:48]))
        self.assertTrue(crc_ok(out))

    def test_haptics_report_layout(self):
        ds = fake_dualsense()
        ds.send_haptics_39(bytes(range(128)))
        out = ds.dev.writes[-1]
        self.assertEqual(len(out), 547)
        self.assertEqual(out[0], 0x39)
        self.assertEqual(out[2:5], bytes([0x91, 6, 0x7E]))
        self.assertEqual(out[10:12], bytes([0xD2, 64]))
        self.assertEqual(out[12:140], bytes(range(128)))
        self.assertTrue(crc_ok(out))

    def test_feature_set_uses_0x53_seed(self):
        ds = fake_dualsense()
        ds.set_feature(0x08, b"\x02" + bytes(46), 48)
        out = ds.dev.writes[-1]
        self.assertEqual(len(out), 48)
        self.assertTrue(crc_ok(out, header=0x53))

    def test_descriptor_sizes(self):
        self.assertEqual(len(D.DEVICE), 18)
        self.assertEqual(int.from_bytes(D.CONFIG[2:4], "little"), len(D.CONFIG))
        i = D.CONFIG.find(bytes([9, 0x21]))
        self.assertEqual(int.from_bytes(D.CONFIG[i + 7:i + 9], "little"), len(D.HID_REPORT))


class HapticsTests(unittest.TestCase):
    def test_decimator_passes_dc_and_low_tones(self):
        d = Decimator()
        self.assertAlmostEqual(d.process([1000] * 4800)[-1], 1000, delta=1)
        d = Decimator()
        out = d.process([10000 * math.sin(2 * math.pi * 100 * n / 48000) for n in range(48000)])
        self.assertGreater(max(out[1500:]), 9500)

    def test_decimator_removes_high_tones(self):
        d = Decimator()
        out = d.process([10000 * math.sin(2 * math.pi * 4000 * n / 48000) for n in range(48000)])
        self.assertLess(max(abs(v) for v in out[1500:]), 100)

    def test_decimator_chunked_equals_whole(self):
        xs = [int(8000 * math.sin(n / 7)) for n in range(4801)]
        whole = Decimator().process(xs)
        d = Decimator()
        parts = []
        for k in range(0, len(xs), 389):  # tamaños de bloque irregulares
            parts += d.process(xs[k:k + 389])
        self.assertEqual(len(whole), len(parts))
        for a, b in zip(whole, parts):
            self.assertAlmostEqual(a, b, places=6)

    def test_feed_extracts_channels_3_and_4(self):
        hb = HapticBridge(ds=None)
        frames = 4800  # 100 ms
        pcm = struct.pack(f"<{frames * 4}h", *([0, 0, 12800, -12800] * frames))
        hb.feed_usb_audio(pcm)
        tail = hb.buf[-20:]
        self.assertEqual(tail[0::2], bytes([50]) * 10)  # 12800/256 = 50 en el lado izquierdo
        self.assertEqual(tail[1::2], bytes([256 - 50]) * 10)  # -50 en el derecho

    def test_buffer_is_bounded(self):
        hb = HapticBridge(ds=None, prefill_reports=1)
        pcm = struct.pack(f"<{48000 * 4}h", *([0, 0, 1000, 1000] * 48000))  # 1 s de golpe
        hb.feed_usb_audio(pcm)
        self.assertLessEqual(len(hb.buf), hb.prefill + 2 * REPORT_BYTES)

    def _gap(self, hb, reports):
        hb.streaming = True
        hb.buf = bytearray(10)
        hb.last_feed = time.monotonic()
        for _ in range(reports):
            chunk = hb._next_chunk()
            self.assertEqual(len(chunk), REPORT_BYTES)  # se completa con silencio, no se detiene

    def test_short_gap_counts_and_raises_reserve(self):
        hb = HapticBridge(ds=None, prefill_reports=1)
        before = hb.prefill
        self._gap(hb, 1)
        hb.buf = bytearray(REPORT_BYTES)  # vuelve el audio
        hb._next_chunk()
        self.assertEqual(hb.stats["underruns"], 1)
        self.assertGreater(hb.prefill, before)

    def test_long_pause_does_not_raise_reserve(self):
        hb = HapticBridge(ds=None, prefill_reports=1)
        before = hb.prefill
        self._gap(hb, 5)  # p. ej. Windows pausó el audio un momento
        hb.buf = bytearray(REPORT_BYTES)
        hb._next_chunk()
        self.assertEqual((hb.stats["underruns"], hb.prefill), (0, before))

    def test_end_of_stream_does_not_raise_reserve(self):
        hb = HapticBridge(ds=None, prefill_reports=1)
        before = hb.prefill
        self._gap(hb, 1)
        hb.last_feed = time.monotonic() - 5
        self.assertEqual(hb._next_chunk(), "stop")
        self.assertEqual((hb.stats["underruns"], hb.prefill), (0, before))

    def test_stops_when_game_stops_sending(self):
        hb = HapticBridge(ds=None)
        hb.streaming = True
        hb.last_feed = time.monotonic() - 5
        self.assertEqual(hb._next_chunk(), "stop")
        self.assertFalse(hb.streaming)

    @staticmethod
    def _signed(chunk):
        return [v - 256 if v > 127 else v for v in chunk]

    def test_gap_is_concealed_with_fading_vibration(self):
        hb = HapticBridge(ds=None, prefill_reports=1)
        hb.streaming = True
        hb.last_feed = time.monotonic()
        real = bytes([100, 156] * (REPORT_BYTES // 2))  # vibración continua: +100 / -100
        hb.buf = bytearray(real)
        self.assertEqual(hb._next_chunk(), real)
        first = self._signed(hb._next_chunk())  # hueco: no es silencio
        self.assertGreater(abs(first[0]), 90)  # empieza casi con la misma fuerza
        self.assertLess(abs(first[-2]), abs(first[0]))  # y se va desvaneciendo
        second = self._signed(hb._next_chunk())
        self.assertLess(max(map(abs, second)), abs(first[-2]) + 1)
        self.assertEqual(second[-2:], [0, 0])  # llega a cero dentro del ocultamiento (≈ 43 ms)
        third = hb._next_chunk()
        self.assertEqual(third, bytes(REPORT_BYTES))  # después, silencio

    def test_concealment_restarts_after_real_audio(self):
        hb = HapticBridge(ds=None, prefill_reports=1)
        hb.streaming = True
        hb.last_feed = time.monotonic()
        hb.buf = bytearray([50, 50] * (REPORT_BYTES // 2))
        hb._next_chunk()
        hb._next_chunk()  # hueco ocultado
        hb.buf = bytearray([80, 80] * (REPORT_BYTES // 2))  # vuelve el audio
        hb._next_chunk()
        concealed = self._signed(hb._next_chunk())  # nuevo hueco: parte de la última vibración real (80)
        self.assertGreater(concealed[0], 75)

    def test_partial_chunk_keeps_real_samples(self):
        hb = HapticBridge(ds=None, prefill_reports=1)
        hb.streaming = True
        hb.last_feed = time.monotonic()
        hb.buf = bytearray([7, 7] * 10)  # llegó solo una parte
        chunk = hb._next_chunk()
        self.assertEqual(chunk[:20], bytes([7, 7] * 10))
        self.assertEqual(len(chunk), REPORT_BYTES)

    def test_high_resolution_timer(self):
        from bridge import rt
        self.assertTrue(rt.enable_high_resolution_timer())
        try:
            async def measure():
                t = time.perf_counter()
                for _ in range(20):
                    await asyncio.sleep(0.002)
                return (time.perf_counter() - t) / 20 * 1000
            self.assertLess(asyncio.run(measure()), 8)  # sin él, ~16 ms por pausa
        finally:
            rt.disable_high_resolution_timer()


class BtLinkTests(unittest.TestCase):
    def test_writes_without_controller_are_dropped(self):
        link = BtLink()
        self.assertIsNone(link.send_usb_output(bytes(48)))
        self.assertEqual(link.dropped_writes, 1)
        with self.assertRaises(OSError):
            link.read()

    def test_delegates_to_controller(self):
        link = BtLink()
        link.ds = fake_dualsense()
        link.set_lights((1, 2, 3))
        self.assertEqual(len(link.ds.dev.writes), 1)


class ConfigTests(unittest.TestCase):
    def test_validation_persistence_and_listeners(self):
        import os
        import tempfile
        from bridge.config import Config
        path = os.path.join(tempfile.mkdtemp(), "config.json")
        cfg = Config(path)
        seen = []
        cfg.listeners.append(lambda k, v: seen.append((k, v)))
        self.assertEqual(cfg.set("haptics_gain", 9), 2.0)  # se limita a 0.5-2.0
        self.assertEqual(cfg.set("lightbar_color", [300, -5, 10]), [255, 0, 10])
        self.assertEqual(cfg.set("speaker_boost", 12), 7)
        self.assertIn(cfg.get("language"), ("es", "en"))  # None = idioma de Windows
        self.assertEqual(seen[0], ("haptics_gain", 2.0))
        again = Config(path)  # se guardó en disco
        self.assertEqual(again.get("lightbar_color"), [255, 0, 10])
        with self.assertRaises(KeyError):
            cfg.set("no_existe", 1)

    def test_service_status_before_start(self):
        from bridge.service import BridgeService
        s = BridgeService()
        self.assertEqual(s.status()["state"], "detenido")
        self.assertFalse(s.test_rumble())


class ControllersTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from bridge.controllers import ControllerRegistry
        self.path = Path(tempfile.mkdtemp()) / "controllers.json"
        self.reg = ControllerRegistry(self.path)

    def test_register_rename_settings_forget(self):
        from bridge.controllers import ControllerRegistry
        self.reg.seen("AA:BB:CC:DD:EE:01", pid=0x0CE6, firmware={"firmware": "0x0110002A"})
        self.reg.rename("AA:BB:CC:DD:EE:01", "  Control de Fran  ")
        self.reg.set_setting("AA:BB:CC:DD:EE:01", "lightbar_color", [255, 0, 0])
        again = ControllerRegistry(self.path)  # persiste
        item = again.listing()[0]
        self.assertEqual((item["name"], item["model"], item["settings"]),
                         ("Control de Fran", "DualSense", {"lightbar_color": [255, 0, 0]}))
        self.assertFalse(item["needs_cable"])
        again.set_setting("AA:BB:CC:DD:EE:01", "lightbar_color", None)  # volver al ajuste general
        self.assertEqual(again.listing()[0]["settings"], {})
        with self.assertRaises(KeyError):
            again.set_setting("AA:BB:CC:DD:EE:01", "language", "es")
        again.forget("AA:BB:CC:DD:EE:01")
        self.assertEqual(again.listing(), [])

    def test_edge_needs_cable_until_profile_captured(self):
        from bridge.profiles import builtin_profile
        self.reg.seen("AA:BB:CC:DD:EE:02", pid=0x0DF2)
        self.assertTrue(self.reg.needs_cable("AA:BB:CC:DD:EE:02"))
        self.reg.save_profile("AA:BB:CC:DD:EE:02", builtin_profile(), {"pid": 0x0DF2, "model": "DualSense Edge"})
        self.assertFalse(self.reg.needs_cable("AA:BB:CC:DD:EE:02"))
        restored = self.reg.profile_for("AA:BB:CC:DD:EE:02")
        self.assertEqual(restored.to_json(), builtin_profile().to_json())

    def test_rebuilt_hid_descriptor_matches_real_control_layout(self):
        from bridge import profiles as P
        caps = {0x05: 41, 0x08: 48, 0x09: 20, 0x0A: 27, 0x0B: 42, 0x0C: 42, 0x20: 64, 0x21: 5, 0x22: 64,
                0x80: 64, 0x81: 64, 0x82: 10, 0x83: 64, 0x84: 64, 0x85: 3, 0xA0: 2, 0xE0: 64, 0xF0: 64,
                0xF1: 64, 0xF2: 16, 0xF4: 64, 0xF5: 4}  # lo que Windows informó del control real por USB
        self.assertEqual(P.builtin_feature_ids(), set(caps))
        rebuilt = P.build_hid_report(caps)
        self.assertEqual(len(rebuilt), len(D.HID_REPORT))
        config = P.patch_config_hid_length(D.CONFIG, 300)
        i = config.find(bytes([9, 0x21]))
        self.assertEqual(struct.unpack_from("<H", config, i + 7)[0], 300)

    def test_identity_parsing(self):
        from bridge import profiles as P
        self.assertEqual(P.mac_from_pairing_report(bytes([9, 1, 2, 3, 4, 5, 6]) + bytes(13)), "06:05:04:03:02:01")
        self.assertIsNone(P.mac_from_pairing_report(bytes(20)))
        fw = P.firmware_from_info_report(D.FEATURE_FALLBACK[0x20])
        self.assertTrue(fw["build"].startswith("Jul"))


class DevRestartTests(unittest.TestCase):
    def test_only_dualsense_devices_are_allowed(self):
        from bridge.devrestart import ALLOWED, instance_id_from_hid_path
        bt = instance_id_from_hid_path("\\\\?\\HID#{00001124-0000-1000-8000-00805f9b34fb}_VID&0002054c_PID&0ce6"
                                       "#c&1a2b3c4d&1&0000#{4d1e55b2-f16f-11cf-88cb-001111000030}")
        self.assertEqual(bt, "HID\\{00001124-0000-1000-8000-00805F9B34FB}_VID&0002054C_PID&0CE6\\C&1A2B3C4D&1&0000")
        self.assertTrue(ALLOWED.match(bt))
        self.assertTrue(ALLOWED.match("HID\\VID_054C&PID_0CE6&MI_03\\A&3263D428&0&0000"))
        for bad in ("HID\\VID_046D&PID_C52B&MI_01\\8&1&0&0000",  # un mouse
                    "USB\\VID_054C&PID_0CE6\\8&29C54EA8&0&4",  # el dispositivo USB padre, no el HID
                    "HID\\VID_054C&PID_0CE6&MI_03\\A&1&0&0000\\..\\otro",
                    "ROOT\\SYSTEM\\0000"):
            self.assertFalse(ALLOWED.match(bad), bad)

    def test_without_admin_or_task_no_restart_is_attempted(self):
        from unittest import mock
        from bridge import devrestart
        with mock.patch.object(devrestart, "is_admin", return_value=False), \
                mock.patch.object(devrestart, "task_ready", return_value=False):
            self.assertIsNone(devrestart.request_restart(["HID\\VID_054C&PID_0CE6&MI_03\\A&1&0&0000"]))
        self.assertIsNone(devrestart.request_restart(["ROOT\\SYSTEM\\0000"]))

    def test_task_whose_helper_was_deleted_is_not_ready(self):
        from unittest import mock
        from bridge import devrestart
        xml = ('<?xml version="1.0" encoding="UTF-16"?><Task><Actions><Exec>'
               '<Command>"C:\\Program Files\\DSHapticLink Helper\\no-such-helper.exe"</Command>'
               '<Arguments>restart</Arguments></Exec></Actions></Task>')
        with mock.patch.object(devrestart, "_schtasks", return_value=(0, xml)):
            self.assertEqual(devrestart.task_command(), "C:\\Program Files\\DSHapticLink Helper\\no-such-helper.exe")
            self.assertFalse(devrestart.task_ready())
        with mock.patch.object(devrestart, "_schtasks", return_value=(0, xml.replace(
                "C:\\Program Files\\DSHapticLink Helper\\no-such-helper.exe", sys.executable))):
            self.assertTrue(devrestart.task_ready())
        with mock.patch.object(devrestart, "_schtasks", return_value=(1, "")):  # no hay tarea
            self.assertFalse(devrestart.task_ready())


class ExclusiveAccessTests(unittest.TestCase):
    """El bloqueo de apertura exclusiva de Windows funciona igual con archivos que con dispositivos HID."""

    def test_second_open_is_blocked_and_released(self):
        import os
        import tempfile
        from bridge.winhid import ExclusiveAccessError, ExclusiveHolder, WinHidDevice
        fd, path = tempfile.mkstemp()
        os.close(fd)
        try:
            first = WinHidDevice(path, exclusive=True)
            with self.assertRaises(ExclusiveAccessError):
                WinHidDevice(path, exclusive=False)  # como lo intentaría un juego
            first.close()
            WinHidDevice(path, exclusive=False).close()  # liberado: ya se puede abrir
            holder = ExclusiveHolder()
            self.assertTrue(holder.hold(path))
            with self.assertRaises(ExclusiveAccessError):
                WinHidDevice(path, exclusive=False)
            holder.release_all()
            WinHidDevice(path, exclusive=False).close()
        finally:
            os.remove(path)


class FakeEvent:
    def __init__(self):
        self.value = False

    def is_set(self):
        return self.value

    async def wait(self):
        return True


class FakeServer:
    def __init__(self):
        self.attached = FakeEvent()


class AttachTests(unittest.IsolatedAsyncioTestCase):
    VIRTUAL, REAL = "virtual-path", "cable-path"

    def make(self):
        server = FakeServer()
        calls, hidden = [], []
        state = {"real": False, "bt": True}

        def usbip(*args):
            calls.append(args[0])
            server.attached.value = args[0] == "attach"
            return 0, "successfully attached to port 3" if args[0] == "attach" else "port 3 is successfully detached"

        def paths():
            found = {self.VIRTUAL} if server.attached.value else set()
            return found | ({self.REAL} if state["real"] else set())

        mgr = AttachManager(server, usbip=usbip, usb_paths=paths, bt_connected=lambda: state["bt"],
                            hide=lambda p: hidden.append(("hide", p)), unhide=lambda p: hidden.append(("unhide", p)),
                            settle=0.5)
        return mgr, server, calls, hidden, state

    def test_parse_port(self):
        self.assertEqual(parse_attached_port("successfully attached to port 12"), 12)
        self.assertIsNone(parse_attached_port("usbip: error: cannot connect"))

    async def test_learns_virtual_path(self):
        mgr, server, calls, hidden, state = self.make()
        await mgr.check()
        self.assertEqual((calls, mgr.port, mgr.virtual_paths), (["attach"], 3, {self.VIRTUAL}))
        await mgr.check()
        self.assertEqual(calls, ["attach"])  # nada que hacer

    async def test_charging_cable_keeps_bridge_and_hides_usb(self):
        mgr, server, calls, hidden, state = self.make()
        await mgr.check()
        state["real"] = True  # se conectó el cable, pero el control sigue por Bluetooth
        await mgr.check()
        self.assertEqual(mgr.mode, "carga")
        self.assertEqual(calls, ["attach"])  # el virtual NO se retira
        self.assertEqual(hidden, [("hide", self.REAL)])  # el juego no ve el control por cable
        await mgr.check()
        self.assertEqual(len(hidden), 1)  # no se repite
        state["real"] = False
        await mgr.check()
        self.assertEqual(mgr.mode, "normal")

    async def test_real_usb_switch_yields_to_cable_and_returns(self):
        mgr, server, calls, hidden, state = self.make()
        await mgr.check()
        state["real"] = True
        await mgr.check()  # primero, cable de carga (BT activo): se oculta
        state["bt"] = False  # el control pasó a USB de verdad
        await mgr.check()
        self.assertTrue(mgr.cable_mode)
        self.assertEqual(calls[-1], "detach")
        self.assertEqual(hidden[-1], ("unhide", self.REAL))  # el juego usa el cable
        await mgr.check()
        self.assertEqual(calls, ["attach", "detach"])
        state["real"], state["bt"] = False, True  # se quitó el cable y volvió el Bluetooth
        await mgr.check()
        self.assertFalse(mgr.cable_mode)
        self.assertEqual(calls[-1], "attach")

    async def test_profile_change_reattaches_virtual(self):
        mgr, server, calls, hidden, state = self.make()
        flag = {"v": False}

        def take():
            value, flag["v"] = flag["v"], False
            return value

        mgr.needs_reattach = take
        await mgr.check()
        flag["v"] = True  # cambió el perfil
        await mgr.check()
        self.assertEqual(calls, ["attach", "detach", "attach"])

    async def test_real_usb_is_reported_for_capture(self):
        mgr, server, calls, hidden, state = self.make()
        seen = []
        mgr.on_real_usb = lambda paths: seen.append(paths)
        await mgr.check()
        state["real"] = True
        await mgr.check()
        self.assertEqual(seen, [{self.REAL}])  # solo el real, nunca el virtual

    async def test_reattaches_after_lost_connection(self):
        mgr, server, calls, hidden, state = self.make()
        await mgr.check()
        server.attached.value = False  # p. ej. el PC se suspendió y se cortó la conexión USB/IP
        await mgr.check()
        self.assertEqual(calls, ["attach", "detach", "attach"])


class FakeHaptics:
    def __init__(self):
        self.fed = b""
        self.resets = 0

    def feed_usb_audio(self, pcm):
        self.fed += pcm

    def reset(self):
        self.resets += 1


class UsbIpHarness(unittest.IsolatedAsyncioTestCase):
    """Habla con el DualSense virtual por el protocolo USB/IP real, como lo haría usbip-win2."""

    async def asyncSetUp(self):
        self.link = BtLink()
        self.link.ds = fake_dualsense()
        self.haptics = FakeHaptics()
        self.device = VirtualDualSense(self.link, self.haptics, asyncio.get_running_loop())
        self.server = UsbIpServer(self.device, port=0)
        await self.server.start()
        port = self.server.server.sockets[0].getsockname()[1]
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port)
        self.writer.write(struct.pack(">HHI", 0x0111, 0x8003, 0) + b"1-1".ljust(32, b"\0"))
        reply = await self.reader.readexactly(8 + 312)
        self.assertEqual(struct.unpack(">HHI", reply[:8]), (0x0111, 0x0003, 0))
        self.seq = 0

    async def asyncTearDown(self):
        self.writer.close()
        self.server.server.close()

    async def submit(self, ep, direction, setup=bytes(8), data=b"", length=None, iso=None):
        self.seq += 1
        length = len(data) if length is None else length
        n = len(iso) if iso is not None else -1
        hdr = struct.pack(">IIIIIIiiiI", 1, self.seq, 0x10001, direction, ep, 0, length, 0, n, 0) + setup
        body = data
        if iso is not None:
            body += b"".join(struct.pack(">IIIi", off, ln, 0, 0) for off, ln in iso)
        self.writer.write(hdr + body)
        ret = await asyncio.wait_for(self.reader.readexactly(48), timeout=5)
        cmd, seq, _, _, _, status, actual, start, npk, errs = struct.unpack(">IIIIIiiiii", ret[:40])
        self.assertEqual((cmd, seq), (3, self.seq))
        payload = await self.reader.readexactly(actual) if direction == 1 and actual > 0 else b""
        if iso is not None:
            await self.reader.readexactly(16 * npk)
        return status, payload

    async def control_in(self, bm, req, value, index, length):
        return await self.submit(0, 1, struct.pack("<BBHHH", bm, req, value, index, length), length=length)


class VirtualDeviceOverUsbIpTests(UsbIpHarness):
    async def test_descriptors(self):
        status, data = await self.control_in(0x80, 6, 0x0100, 0, 18)
        self.assertEqual((status, data), (0, D.DEVICE))
        status, data = await self.control_in(0x80, 6, 0x0200, 0, 9)
        self.assertEqual(data, D.CONFIG[:9])
        status, data = await self.control_in(0x80, 6, 0x0200, 0, len(D.CONFIG))
        self.assertEqual(data, D.CONFIG)
        status, data = await self.control_in(0x81, 6, 0x2200, 3, 1024)
        self.assertEqual(data, D.HID_REPORT)
        status, data = await self.control_in(0x80, 6, 0x0302, 0x0409, 255)
        self.assertEqual(data[2:].decode("utf-16-le"), "DualSense Wireless Controller")

    async def test_unknown_request_stalls(self):
        status, _ = await self.control_in(0x80, 6, 0x0F00, 0, 5)  # BOS: el control USB 2.0 no lo tiene
        self.assertEqual(status, -32)

    async def test_feature_report_from_capture(self):
        status, data = await self.control_in(0xA1, 0x01, 0x0320, 3, 64)
        self.assertEqual(data, D.FEATURE_FALLBACK[0x20])

    async def test_live_features_replace_captured_ones(self):
        mac = bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66])
        bt_09 = bytes([0x09]) + mac + bytes(9) + b"\xde\xad\xbe\xef"  # 20 B por BT, con CRC al final
        self.link.ds.dev.get_feature_report = lambda rid, size: list(bt_09) if rid == 0x09 else [rid] + [7] * (size - 1)
        self.device._load_live_features()
        status, data = await self.control_in(0xA1, 0x01, 0x0309, 3, 20)
        self.assertEqual(data[1:7], mac)  # la MAC del control conectado, no la de la captura
        self.assertEqual(data[-4:], bytes(4))  # el CRC de BT no llega al juego

    def test_no_mac_in_fallback(self):
        # Los datos de respaldo vienen de un control real: sus MAC (la del control en 1-6 y la del PC emparejado
        # en 10-15) deben estar borradas; se leen en vivo del control conectado.
        for rid in (0x09, 0x0B):
            report = D.FEATURE_FALLBACK[rid]
            self.assertEqual(report[1:7], bytes(6), f"MAC del control en el feature 0x{rid:02X}")
            self.assertEqual(report[10:16], bytes(6), f"MAC del PC en el feature 0x{rid:02X}")

    async def test_profile_change_updates_descriptors(self):
        from bridge.profiles import Profile
        edge_device = bytearray(D.DEVICE)
        edge_device[10:12] = (0x0DF2).to_bytes(2, "little")
        edge = Profile("cable", bytes(edge_device), D.CONFIG, D.HID_REPORT, dict(D.STRINGS), {0x20: b"\x20" + bytes(63)})
        self.device.set_profile(edge)
        self.assertTrue(self.device.reattach_needed)
        self.assertEqual(self.device.info.pid, 0x0DF2)
        self.assertEqual(self.device.info.interfaces, [(1, 1, 0), (1, 2, 0), (1, 2, 0), (3, 0, 0)])
        status, data = await self.control_in(0x80, 6, 0x0100, 0, 18)
        self.assertEqual(struct.unpack_from("<H", data, 10)[0], 0x0DF2)  # Windows ve un Edge

    async def test_output_report_reaches_bluetooth(self):
        report = bytes([0x02, 0, 0x04]) + bytes(45)
        status, _ = await self.submit(3, 0, data=report)
        self.assertEqual(status, 0)
        self.assertEqual(self.link.ds.dev.writes[-1][0], 0x31)

    async def test_feature_set_reaches_bluetooth_with_crc(self):
        report = bytes([0x80, 0x02]) + bytes(46)
        setup = struct.pack("<BBHHH", 0x21, 0x09, 0x0380, 3, len(report))
        status, _ = await self.submit(0, 0, setup, data=report)
        self.assertEqual(status, 0)
        out = self.link.ds.dev.writes[-1]
        self.assertEqual(len(out), 48)
        self.assertEqual(out[:2], bytes([0x80, 0x02]))
        self.assertTrue(crc_ok(out, header=0x53))

    async def test_feature_0x08_is_not_forwarded(self):
        # God of War manda esto al iniciar (SET_REPORT feature 0x08 de 48 bytes): por BT apagaba el control.
        report = bytes([0x08, 0x02]) + bytes(46)
        setup = struct.pack("<BBHHH", 0x21, 0x09, 0x0308, 3, len(report))
        before = len(self.link.ds.dev.writes)
        status, _ = await self.submit(0, 0, setup, data=report)
        self.assertEqual(status, 0)  # el juego recibe OK
        self.assertEqual(len(self.link.ds.dev.writes), before)  # pero no llega al control

    async def test_lights_restored_when_game_leaves_them_off(self):
        report = bytearray(48)
        report[0], report[2] = 0x02, 0x04  # barra de luz en negro, como al cerrar el juego
        report[3] = 1  # algo distinto de cero: un juego, no Windows enumerando
        await self.submit(3, 0, data=bytes(report))
        self.device.last_game_output -= 4  # pasaron más de 3 s sin reportes
        before = len(self.link.ds.dev.writes)
        self.device._check_game_ended()
        self.assertGreater(len(self.link.ds.dev.writes), before)
        before = len(self.link.ds.dev.writes)
        self.device._check_game_ended()  # solo una vez
        self.assertEqual(len(self.link.ds.dev.writes), before)

    async def test_game_color_is_kept_when_game_goes_quiet(self):
        report = bytearray(48)
        report[0], report[2], report[45] = 0x02, 0x04, 200  # el juego dejó la barra en rojo
        await self.submit(3, 0, data=bytes(report))
        self.device.last_game_output -= 4
        before = len(self.link.ds.dev.writes)
        self.device._check_game_ended()
        self.assertEqual(len(self.link.ds.dev.writes), before)

    async def test_input_report_waits_for_bluetooth(self):
        task = asyncio.create_task(self.submit(4, 1, length=64))
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())
        report = bytes([0x01, 1, 2, 3, 4]) + bytes(59)
        self.device._new_input(report)
        status, data = await task
        self.assertEqual(data, report)

    async def test_audio_iso_feeds_haptics_only_when_open(self):
        pcm = bytes(range(256)) * 3  # 768 bytes = 2 paquetes de 384
        await self.submit(1, 0, data=pcm, iso=[(0, 384), (384, 384)])
        self.assertEqual(self.haptics.fed, b"")
        await self.submit(0, 0, struct.pack("<BBHHH", 0x01, 0x0B, 1, 1, 0))  # SET_INTERFACE 1 → alt 1
        t0 = time.perf_counter()
        status, _ = await self.submit(1, 0, data=pcm, iso=[(0, 384), (384, 384)])
        self.assertEqual(status, 0)
        self.assertEqual(self.haptics.fed, pcm)
        self.assertGreaterEqual(time.perf_counter() - t0, 0.0015)  # se completó al ritmo real (2 ms)

    async def test_neutral_input_shape(self):
        self.assertEqual(len(NEUTRAL_INPUT), 64)
        self.assertEqual(NEUTRAL_INPUT[8] & 0x0F, 8)


class AudioTests(unittest.TestCase):
    def setUp(self):
        from bridge import audio
        if not audio.opus_lib():
            self.skipTest("libopus (pyogg) no instalado")
        self.audio = audio

    def test_speaker_and_headphone_packets(self):
        ds = fake_dualsense()
        speaker = bytes([7]) * 400
        ds.send_haptics_39(bytes(128), speaker=speaker)
        out = ds.dev.writes[-1]
        self.assertEqual(out[140:142], bytes([0xD3, 200]))
        self.assertEqual(out[142:542], speaker)
        self.assertTrue(crc_ok(out))
        ds.send_haptics_39(bytes(128), speaker=speaker, headphones=True, mic=True)
        out = ds.dev.writes[-1]
        self.assertEqual(out[140], 0xD6)
        self.assertEqual(out[4], 0x7F)

    def test_speaker_path_rate_matches_haptics(self):
        sp = self.audio.SpeakerPath()
        pairs = 0
        for _ in range(10):  # 10 reportes × 1024 frames USB (21.33 ms c/u), intercalando entrada y salida
            sp.feed([3000] * 1024, [-3000] * 1024)
            p = sp.pop_pair()
            if p:
                self.assertEqual(len(p), 400)
                pairs += 1
        self.assertEqual(sp.encoded, 20)  # 2 tramas Opus por reporte: mismo ritmo que la háptica
        self.assertGreaterEqual(pairs, 9)  # el primero puede quedar corto por el arranque del re-muestreo

    def test_left_channel_reaches_mono_speaker(self):
        dec = self.audio.OpusDecoder(2)
        tone = [int(8000 * math.sin(2 * math.pi * 440 * n / 48000)) for n in range(1024 * 8)]
        silence = [0] * len(tone)

        def right_peak(mono):
            sp = self.audio.SpeakerPath()
            sp.feed(tone, silence, mono=mono)  # sonido solo en el canal izquierdo
            pcm = []
            while sp.frames:
                data, silent = sp.frames.popleft()
                if data:
                    pcm += dec.decode(data)
            return max(abs(v) for v in pcm[1::2][2000:])

        self.assertGreater(right_peak(mono=True), 3000)  # bocina: la mezcla llega al canal que suena
        self.assertLess(right_peak(mono=False), 800)  # audífonos: estéreo intacto

    def test_speaker_buffer_is_bounded(self):
        sp = self.audio.SpeakerPath()
        for _ in range(10):  # 213 ms de golpe: se conservan solo las tramas más recientes
            sp.feed([3000] * 1024, [-3000] * 1024)
        self.assertLessEqual(len(sp.frames), 6)

    def test_mic_roundtrip(self):
        enc = self.audio.OpusEncoder(1, frame_bytes=self.audio.MIC_OPUS_BYTES)  # como el control
        mic = self.audio.MicPath()
        tone = [int(6000 * math.sin(2 * math.pi * 300 * n / 48000)) for n in range(480)]
        mic.on_bt_frame(enc.encode(tone))
        self.assertEqual((mic.frames, mic.errors), (1, 0))
        pcm = mic.read(192 * 10)
        samples = struct.unpack("<960h", pcm)
        self.assertEqual(samples[0::2], samples[1::2])  # mono duplicado a estéreo
        self.assertEqual(mic.read(4), bytes(4))  # sin datos → silencio


class ReconnectTests(UsbIpHarness):
    """Con acceso exclusivo, apagar el control no da error: se detecta por los datos."""

    def ext(self, buttons=0x08):
        r = [0x31, 0x00, 0x80, 0x80, 0x80, 0x80, 0, 0, 0, buttons] + [0] * 68
        return r

    async def test_silence_releases_buttons(self):
        cross = self.ext(buttons=0x28)  # ✕ presionado
        self.device._handle_report(cross)
        await asyncio.sleep(0)
        self.assertEqual(self.device.latest_input[8], 0x28)
        self.device._check_idle(time.monotonic() + 5)  # 5 s sin datos: el control se apagó
        await asyncio.sleep(0)
        self.assertTrue(self.device.bt_idle)
        self.assertEqual(self.device.latest_input, NEUTRAL_INPUT)  # el juego ya no ve ✕ presionado
        self.device._handle_report(self.ext())
        self.assertFalse(self.device.bt_idle)

    async def test_basic_mode_after_extended_means_reconnected(self):
        self.device._handle_report(self.ext())
        writes_before = len(self.link.ds.dev.writes)
        self.device._handle_report([0x01] + [0x80] * 9)  # el control volvió en modo básico
        self.assertEqual(self.device.counters["bt_conexiones"], 1)
        new = self.link.ds.dev.writes[writes_before:]
        self.assertTrue(any(w[0] == 0x31 and w[4] & 0x08 for w in new))  # se liberó la barra de luz otra vez
        self.device._handle_report([0x01] + [0x80] * 9)  # más reportes básicos: no cuenta doble
        self.assertEqual(self.device.counters["bt_conexiones"], 1)


class VirtualAudioTests(UsbIpHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.haptics.headphones = False
        self.haptics.mic_enabled = False

    async def set_interface(self, itf, alt):
        await self.submit(0, 0, struct.pack("<BBHHH", 0x01, 0x0B, alt, itf, 0))

    async def test_mic_interface_enables_controller_mic(self):
        await self.set_interface(2, 1)
        self.assertTrue(self.haptics.mic_enabled)
        out = self.link.ds.dev.writes[-1]
        self.assertEqual(out[:5], bytes([0x32, out[1], 0x91, 1, 0x03]))
        await self.set_interface(2, 0)
        self.assertEqual(self.link.ds.dev.writes[-1][4], 0x02)

    async def test_mic_audio_reaches_usb(self):
        class FakeMic:
            def read(self, n):
                return bytes([9]) * n

            def reset(self):
                pass

        self.device.mic = FakeMic()
        await self.set_interface(2, 1)
        status, data = await self.submit(2, 1, length=392, iso=[(0, 196), (196, 196)])
        self.assertEqual(data, bytes([9]) * 384)  # 192 bytes reales por paquete de 1 ms

    async def test_mute_button_toggles_on_each_press(self):
        self.device._check_mute_button(True)  # presionar
        self.assertTrue(self.device.mic_muted)
        out = self.link.ds.dev.writes
        light = [w for w in out if w[0] == 0x31 and w[4] & 0x01]  # AllowMuteLight
        self.assertEqual(light[-1][3 + 8], 1)  # LED encendido
        self.device._check_mute_button(True)  # sigue presionado: no alterna otra vez
        self.assertTrue(self.device.mic_muted)
        self.device._check_mute_button(False)  # soltar
        self.device._check_mute_button(True)  # segunda pulsación
        self.assertFalse(self.device.mic_muted)

    async def test_muted_mic_sends_silence(self):
        class FakeMic:
            def read(self, n):
                return bytes([9]) * n

            def reset(self):
                pass

        self.device.mic = FakeMic()
        await self.set_interface(2, 1)
        self.device._check_mute_button(True)
        status, data = await self.submit(2, 1, length=196, iso=[(0, 196)])
        self.assertEqual(data, bytes(192))

    async def test_headphone_jack_switches_output(self):
        self.device._check_status(0x00)
        self.assertFalse(self.haptics.headphones)
        self.device._check_status(0x01)
        self.assertTrue(self.haptics.headphones)

    async def test_windows_volume_reaches_controller(self):
        setup = struct.pack("<BBHHH", 0x21, 0x01, 0x0200, 0x0200, 2)  # SET_CUR volumen, unidad 2 (bocina)
        await self.submit(0, 0, setup, data=struct.pack("<h", -30 * 256))
        out = self.link.ds.dev.writes[-1]
        self.assertEqual(out[0], 0x31)
        self.assertEqual(out[3] & 0x30, 0x30)  # permitir volumen de bocina y audífonos
        self.assertEqual(out[3 + 5], 70)  # 100 + (-30 dB)


if __name__ == "__main__":
    unittest.main()
