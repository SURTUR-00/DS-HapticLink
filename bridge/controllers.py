"""Registro de controles conocidos ("Mis controles"), guardado en controllers.json.

Cada control se identifica por su MAC (feature 0x09, se lee por Bluetooth: no hace falta cable). Guarda nombre,
modelo, firmware, último uso, ajustes propios (que reemplazan a los generales) y, si se conectó por cable
alguna vez, su perfil USB capturado.
"""
import json
import logging
import threading
import time

from .config import data_dir
from .profiles import BUILTIN_PID, MODEL_NAMES, Profile

log = logging.getLogger("controllers")

PER_CONTROLLER_SETTINGS = ("lightbar_color", "haptics_gain")


class ControllerRegistry:
    def __init__(self, path=None):
        self.path = path or data_dir() / "controllers.json"
        self.lock = threading.Lock()
        self.items = {}
        self.listeners = []
        try:
            self.items = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("controllers.json unreadable (%s); starting fresh", e)

    def _save(self):
        with self.lock:
            data = json.dumps(self.items, ensure_ascii=False, indent=2)
        try:
            self.path.write_text(data, encoding="utf-8")
        except OSError as e:
            log.warning("couldn't save controllers.json: %s", e)
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:
                log.exception("error notifying controller changes")

    def get(self, mac):
        return self.items.get(mac)

    def seen(self, mac, pid=None, firmware=None):
        """Un control se conectó: crear su entrada o actualizar su último uso."""
        if not mac:
            return None
        with self.lock:
            entry = self.items.get(mac)
            if entry is None:
                number = len(self.items) + 1
                entry = self.items[mac] = {"name": f"DualSense {number}", "first_seen": time.time(), "settings": {}}
                log.info("new controller registered: %s", mac)
            entry["last_seen"] = time.time()
            if pid is not None:
                entry["pid"] = pid
                entry["model"] = MODEL_NAMES.get(pid, f"PID {pid:04X}")
            if firmware:
                entry["firmware"] = firmware
        self._save()
        return entry

    def rename(self, mac, name):
        with self.lock:
            if mac in self.items:
                self.items[mac]["name"] = str(name).strip()[:40] or self.items[mac]["name"]
        self._save()

    def set_setting(self, mac, key, value):
        """Ajuste propio del control; None lo quita y vuelve a usar el general."""
        if key not in PER_CONTROLLER_SETTINGS:
            raise KeyError(key)
        with self.lock:
            if mac not in self.items:
                return
            settings = self.items[mac].setdefault("settings", {})
            if value is None:
                settings.pop(key, None)
            else:
                settings[key] = value
        self._save()

    def forget(self, mac):
        with self.lock:
            self.items.pop(mac, None)
        self._save()

    def save_profile(self, mac, profile, identity):
        with self.lock:
            entry = self.items.setdefault(mac, {"name": f"DualSense {len(self.items) + 1}",
                                                "first_seen": time.time(), "settings": {}})
            entry["profile"] = profile.to_json()
            entry["profile_captured"] = time.time()
            entry["pid"] = identity.get("pid")
            entry["model"] = identity.get("model")
            if identity.get("firmware"):
                entry["firmware"] = identity["firmware"]
        self._save()

    def profile_for(self, mac):
        entry = self.items.get(mac) if mac else None
        if entry and entry.get("profile"):
            try:
                return Profile.from_json(entry["profile"])
            except (KeyError, ValueError) as e:
                log.warning("invalid saved profile for %s: %s", mac, e)
        return None

    def needs_cable(self, mac):
        """El modelo no es el que cubre el perfil incluido y todavía no se capturó su perfil por cable."""
        entry = self.items.get(mac) if mac else None
        return bool(entry and entry.get("pid") not in (None, BUILTIN_PID) and not entry.get("profile"))

    def listing(self):
        with self.lock:
            out = []
            for mac, e in self.items.items():
                out.append({"mac": mac, "name": e.get("name"), "model": e.get("model", "DualSense"),
                            "firmware": e.get("firmware"), "last_seen": e.get("last_seen"),
                            "settings": dict(e.get("settings", {})), "has_profile": bool(e.get("profile")),
                            "needs_cable": e.get("pid") not in (None, BUILTIN_PID) and not e.get("profile")})
        return sorted(out, key=lambda c: -(c["last_seen"] or 0))
