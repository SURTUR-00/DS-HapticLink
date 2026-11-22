"""Avisos de la bandeja: el de batería baja solo sale si la lectura se mantiene."""
import unittest
from types import SimpleNamespace
from unittest import mock

from app import main


class FakeTray:
    def __init__(self):
        self.sent = []

    def notify(self, text, title):
        self.sent.append(text)


def fake_app(controller):
    status = {"controller": controller}
    return SimpleNamespace(
        service=SimpleNamespace(status=lambda: status), config=SimpleNamespace(get=lambda key: True),
        tray=FakeTray(), lang="es", notified={"battery_low": False, "battery_low_since": None, "connected": True},
        _check_components=lambda c: None, _check_power=lambda: False, controller=controller)


class BatteryNotificationTests(unittest.TestCase):
    def tick(self, app, at):
        with mock.patch.object(main.time, "monotonic", return_value=at):
            main.DsxApp._notifications(app)

    def test_brief_low_reading_does_not_notify(self):
        c = {"connected": True, "battery": 5, "battery_state": "descargando"}
        app = fake_app(c)
        self.tick(app, 0)
        self.tick(app, 10)
        c.update(battery=55)  # se recuperó: era una lectura suelta
        self.tick(app, 20)
        c.update(battery=5)
        self.tick(app, 70)  # el contador empieza de nuevo
        self.assertEqual(app.tray.sent, [])

    def test_sustained_low_battery_notifies_once(self):
        app = fake_app({"connected": True, "battery": 15, "battery_state": "descargando"})
        for at in (0, 30, 61, 90, 200):
            self.tick(app, at)
        self.assertEqual(len([s for s in app.tray.sent if "15" in s]), 1)

    def test_no_battery_warning_while_disconnected(self):
        app = fake_app({"connected": False, "battery": 5, "battery_state": "descargando"})
        app.notified["connected"] = False
        for at in (0, 100):
            self.tick(app, at)
        self.assertEqual(app.tray.sent, [])


class PowerPlanTests(unittest.TestCase):
    def check(self, app, plan, at):
        with mock.patch.object(main.winsys, "power_plan", return_value=plan), \
                mock.patch.object(main.time, "monotonic", return_value=at):
            return main.DsxApp._check_power(app)

    def test_warns_once_when_power_saver_starts(self):
        app = SimpleNamespace(power=None, _power_checked=-main.POWER_CHECK_SECONDS)
        saver = {"name": "Economizador", "guid": "a1841308-3541-4fab-bc81-f71556f20b4a", "power_saver": True,
                 "reason": "plan"}
        balanced = {"name": "Equilibrado", "guid": "381b4222-f694-41f0-9685-ff5bb260df2e", "power_saver": False,
                    "reason": None}
        self.assertTrue(self.check(app, saver, 0))  # al arrancar ya estaba en ahorro: se avisa
        self.assertFalse(self.check(app, saver, 10))  # antes de 30 s no se vuelve a mirar
        self.assertFalse(self.check(app, saver, 40))  # sigue igual: no se repite
        self.assertFalse(self.check(app, balanced, 80))  # lo corrigió
        self.assertTrue(self.check(app, saver, 120))  # volvió a ahorro: se avisa otra vez
        self.assertEqual(app.power, saver)

    def test_tray_warning_respects_the_setting(self):
        for enabled, expected in ((True, 1), (False, 0)):
            app = fake_app({"connected": True, "battery": 80, "battery_state": "descargando"})
            app._check_power = lambda: True  # acaba de pasar a ahorro de energía
            app.config = SimpleNamespace(get=lambda key, on=enabled: on if key == "power_warning" else True)
            with mock.patch.object(main.time, "monotonic", return_value=0):
                main.DsxApp._notifications(app)
            self.assertEqual(len(app.tray.sent), expected)

    @unittest.skipUnless(main.sys.platform == "win32", "Windows")
    def test_reads_the_real_power_plan(self):
        plan = main.winsys.power_plan()
        self.assertIsNotNone(plan)
        self.assertEqual(set(plan), {"name", "guid", "power_saver", "reason"})
        self.assertTrue(plan["name"])


if __name__ == "__main__":
    unittest.main()
