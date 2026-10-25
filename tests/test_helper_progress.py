import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from helper import dsh_helper


class ProgressTest(unittest.TestCase):
    """El helper no debe caerse si la app tiene abierto setup_status.json justo al reemplazarlo."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.status = self.dir / "setup_status.json"
        patches = [mock.patch.object(dsh_helper, "STATUS_FILE", self.status),
                   mock.patch.object(dsh_helper, "LOG_FILE", self.dir / "setup.log")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_retries_while_file_is_open(self):
        progress = dsh_helper.Progress("en")
        progress("start", 1)
        handle = open(self.status, encoding="utf-8")  # como la app al leerlo
        threading.Timer(0.3, handle.close).start()
        progress("net", 0, ok=False)  # no debe lanzar excepción
        time.sleep(0.4)
        self.assertEqual(json.loads(self.status.read_text(encoding="utf-8"))["ok"], False)

    def test_never_raises_if_file_stays_open(self):
        progress = dsh_helper.Progress("en")
        progress("start", 1)
        with open(self.status, encoding="utf-8"):
            progress("download", 10)  # se omite sin fallar
        self.assertIn("could not write", (self.dir / "setup.log").read_text(encoding="utf-8"))

    def test_protected_dir_is_not_the_app_folder(self):
        self.assertEqual(dsh_helper.PROTECTED_DIR.name, "DSHapticLink Helper")

    def test_uninstall_reports_what_could_not_be_removed(self):
        from bridge import devrestart
        protected = self.dir / "DSHapticLink Helper"
        protected.mkdir()
        with mock.patch.object(dsh_helper, "PROTECTED_DIR", protected), \
                mock.patch.object(dsh_helper, "SETUP_DIR", self.dir / "setup"), \
                mock.patch.object(dsh_helper, "USBIP_EXE", self.dir / "no-usbip.exe"), \
                mock.patch.object(dsh_helper, "USBIP_UNINSTALLER", self.dir / "no-unins000.exe"), \
                mock.patch.object(devrestart, "uninstall_task", return_value=(0, "ok")):
            with mock.patch.object(devrestart, "task_installed", return_value=False):
                self.assertEqual(dsh_helper.uninstall(drivers=True, quiet=True), 0)
            self.assertFalse(protected.exists())
            with mock.patch.object(devrestart, "task_installed", return_value=True):  # la tarea no se pudo borrar
                self.assertEqual(dsh_helper.uninstall(drivers=False, quiet=True), 6)
        self.assertIn("errors: scheduled task", (self.dir / "setup.log").read_text(encoding="utf-8"))

    def test_signature_check_without_powershell(self):
        import sys
        signed = Path(getattr(sys, "_base_executable", sys.executable))  # python.exe de python.org está firmado
        self.assertTrue(dsh_helper.authenticode_ok(signed))
        unsigned = self.dir / "unsigned.exe"
        unsigned.write_bytes(signed.read_bytes()[:4096])  # copia truncada: firma inválida
        self.assertFalse(dsh_helper.authenticode_ok(unsigned))

    def test_task_definition_runs_on_battery_and_on_demand(self):
        from bridge import devrestart
        xml = devrestart.task_xml(r"C:\Program Files\DSHapticLink Helper\dsh-helper.exe", "restart", "PC\\user & co")
        self.assertIn("<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>", xml)
        self.assertIn("<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>", xml)
        self.assertIn("<RunLevel>HighestAvailable</RunLevel>", xml)
        self.assertNotIn("<Triggers>", xml)  # sin horario: solo a pedido
        self.assertIn("PC\\user &amp; co", xml)  # escapado

    def test_leftover_usbip_exe_is_not_an_installation(self):
        from bridge import devrestart
        folder = self.dir / "USBip"
        folder.mkdir()
        (folder / "usbip.exe").write_bytes(b"")  # lo que queda al desinstalar sin reiniciar
        with mock.patch.object(devrestart, "USBIP_DIR", folder):
            self.assertFalse(devrestart.usbip_installed())
            for name in devrestart.USBIP_REQUIRED:
                (folder / name).write_bytes(b"")
            self.assertTrue(devrestart.usbip_installed())

    def _bundled(self, content):
        path = self.dir / dsh_helper.USBIP_INSTALLER_NAME
        path.write_bytes(content)
        return mock.patch.object(dsh_helper, "BUNDLED_USBIP", path)

    def test_uses_bundled_installer_when_it_matches(self):
        good = b"usbip installer"
        with self._bundled(good), \
                mock.patch.object(dsh_helper, "USBIP_SHA256", __import__("hashlib").sha256(good).hexdigest().upper()), \
                mock.patch.object(dsh_helper, "download_usbip", side_effect=AssertionError("should not download")):
            self.assertEqual(dsh_helper.usbip_installer_bytes(dsh_helper.Progress("en")), good)

    def test_downloads_when_bundled_installer_was_tampered(self):
        good = b"official installer"
        with self._bundled(b"tampered"), \
                mock.patch.object(dsh_helper, "USBIP_SHA256", __import__("hashlib").sha256(good).hexdigest().upper()), \
                mock.patch.object(dsh_helper, "download_usbip", return_value=good):
            self.assertEqual(dsh_helper.usbip_installer_bytes(dsh_helper.Progress("en")), good)
        with self._bundled(b"tampered"), \
                mock.patch.object(dsh_helper, "download_usbip", return_value=b"also wrong"):
            self.assertIsNone(dsh_helper.usbip_installer_bytes(dsh_helper.Progress("en")))


if __name__ == "__main__":
    unittest.main()
