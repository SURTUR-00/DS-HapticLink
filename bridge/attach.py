"""Mantiene conectado el DualSense virtual (usbip attach) y decide qué hacer cuando aparece el cable.

- Si la conexión USB/IP se pierde (p. ej. al suspender el PC), lo vuelve a conectar.
- Cable conectado y el control SIGUE por Bluetooth (en PC el DualSense suele quedarse en BT y el cable
  solo lo carga): se mantiene el puente y se bloquea el control USB (apertura exclusiva) para que el juego no vea dos.
- Cable conectado y el Bluetooth se cortó (el control pasó de verdad a USB): se retira el virtual y se deja
  visible el control por cable, para que el juego lo use directamente.
"""
import asyncio
import logging
import subprocess
from pathlib import Path

import hid

from .ds5bt import DUALSENSE_PIDS, SONY_VID

log = logging.getLogger("attach")

USBIP_EXE = Path(r"C:\Program Files\USBip\usbip.exe")
BUSID = "1-1"

MODE_NORMAL, MODE_CHARGING, MODE_CABLE = "normal", "carga", "cable"
# La app compilada no tiene consola: sin esto, Windows abre una ventana de consola por un instante cada vez que
# se ejecuta usbip.exe (se veía al abrir la app, antes de la interfaz).
CREATE_NO_WINDOW = 0x08000000


def run_usbip(*args):
    """Ejecuta usbip.exe (sin ventana) y devuelve (código, salida)."""
    p = subprocess.run([str(USBIP_EXE), *args], capture_output=True, encoding="oem", errors="replace", timeout=30,
                       creationflags=CREATE_NO_WINDOW)
    return p.returncode, (p.stdout + p.stderr).strip()


def usb_dualsense_paths():
    """Rutas HID de los DualSense por USB (el virtual también: para Windows es USB).
    Solo la colección de gamepad (usage page 1, usage 5): una por dispositivo."""
    return {d["path"].decode() for d in hid.enumerate(SONY_VID)
            if d["product_id"] in DUALSENSE_PIDS and d.get("bus_type") == 1
            and d.get("usage_page") == 0x01 and d.get("usage") == 0x05}


def parse_attached_port(output):
    """'successfully attached to port 1' → 1"""
    for word in output.split():
        if word.isdigit():
            return int(word)
    return None


class AttachManager:
    def __init__(self, server, usbip=run_usbip, usb_paths=usb_dualsense_paths, bt_connected=lambda: True,
                 hide=None, unhide=None, interval=2.0, settle=3.0, needs_reattach=None, on_real_usb=None):
        """hide/unhide(ruta_hid): bloquear o liberar un dispositivo para los juegos (None para no hacerlo).
        needs_reattach(): True si cambió el perfil del virtual y hay que reconectarlo (lo devuelve una vez).
        on_real_usb(rutas): se llama (en un hilo) cuando hay un control real conectado por cable."""
        self.needs_reattach = needs_reattach
        self.on_real_usb = on_real_usb
        self.server = server
        self.usbip = usbip
        self.usb_paths = usb_paths
        self.bt_connected = bt_connected
        self.hide, self.unhide = hide, unhide
        self.interval = interval
        self.settle = settle  # tiempo máximo esperando a que Windows enumere el virtual tras conectarlo
        self.port = None  # puerto del controlador virtual de usbip-win2 donde quedó conectado
        self.virtual_paths = set()  # ruta(s) HID del DualSense virtual
        self.hidden = set()  # rutas del control real por cable que ocultamos nosotros
        self.mode = MODE_NORMAL
        self._failures = 0

    @property
    def attached(self):
        return self.server.attached.is_set()

    @property
    def cable_mode(self):
        return self.mode == MODE_CABLE

    async def _paths(self):
        return await asyncio.to_thread(self.usb_paths)

    async def attach(self):
        before = await self._paths()
        code, out = await asyncio.to_thread(self.usbip, "attach", "-r", "127.0.0.1", "-b", BUSID)
        port = parse_attached_port(out)
        if port is None:
            self._failures += 1
            if self._failures in (1, 5) or self._failures % 30 == 0:
                log.error("couldn't attach the virtual controller (attempt %d): %s", self._failures, out or code)
            return False
        self._failures = 0
        self.port = port
        log.info("virtual controller attached (usbip port %d)", port)
        try:
            await asyncio.wait_for(self.server.attached.wait(), timeout=5)
        except asyncio.TimeoutError:
            log.warning("usbip reported success, but the server didn't receive the import")
        # Aprender la ruta HID del virtual: la que aparece nueva tras conectarlo.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settle
        while True:
            new = (await self._paths()) - before
            if new or loop.time() >= deadline:
                break
            await asyncio.sleep(0.25)
        if len(new) == 1:
            self.virtual_paths = new
        return True

    async def detach(self):
        if self.port is None:
            return
        code, out = await asyncio.to_thread(self.usbip, "detach", "-p", str(self.port))
        log.info("virtual controller detached: %s", out or code)
        self.port = None

    async def _hide_real(self, real):
        if self.hide is None:
            return
        for path in real - self.hidden:
            await asyncio.to_thread(self.hide, path)
            self.hidden.add(path)

    async def _unhide_real(self):
        if self.unhide is None:
            self.hidden.clear()
            return
        for path in list(self.hidden):
            await asyncio.to_thread(self.unhide, path)
            self.hidden.discard(path)

    async def check(self):
        """Un paso del vigilante. Separado de run() para poder probarlo."""
        if self.attached and self.needs_reattach is not None and self.needs_reattach():
            log.info("controller profile changed: reattaching the virtual controller so Windows rereads it")
            await self.detach()
            self.virtual_paths = set()
        paths = await self._paths()
        if self.attached and not self.virtual_paths:
            # No se sabe cuál es el virtual: solo contar. No se oculta nada, para no ocultar el virtual por error.
            real = set(sorted(paths)[1:])
        else:
            real = paths - self.virtual_paths  # (tras retirar el virtual, su ruta puede tardar en desaparecer)
        real_visible = real - self.hidden
        if real and self.on_real_usb is not None and (self.virtual_paths or not self.attached):
            # Solo con certeza de cuál es el real (si no, se podría "capturar" el virtual).
            try:
                await asyncio.to_thread(self.on_real_usb, set(real))
            except Exception:
                log.exception("error handling the cable-connected controller")

        if real and not self.bt_connected():
            if self.mode != MODE_CABLE:
                log.warning("controller switched to USB: detaching the virtual controller so the game uses the cable")
                self.mode = MODE_CABLE
            if self.port is not None or self.attached:
                await self.detach()
            await self._unhide_real()
            return

        if real:
            if self.mode != MODE_CHARGING:
                log.info("cable connected but the controller stays on Bluetooth (charging only): keeping the bridge")
                self.mode = MODE_CHARGING
            if self.virtual_paths:
                await self._hide_real(real_visible)
        else:
            if self.mode == MODE_CABLE:
                log.info("cable disconnected: back to the Bluetooth bridge")
            elif self.mode == MODE_CHARGING:
                log.info("cable disconnected")
            self.mode = MODE_NORMAL
            self.hidden.clear()  # el dispositivo ya no existe

        if not self.attached:
            if self.port is not None:
                # usbip-win2 cree que sigue conectado pero el servidor perdió la conexión (p. ej. suspensión).
                log.warning("lost the virtual controller connection; reattaching")
                await self.detach()
            await self.attach()

    async def run(self):
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("error in the connection watcher")
            await asyncio.sleep(self.interval)
