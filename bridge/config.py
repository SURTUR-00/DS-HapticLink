"""Ajustes del usuario, guardados en config.json.

Modo portable: config.json junto al ejecutable. Si esa carpeta no es escribible (p. ej. instalado en
Program Files), se usa %APPDATA%\\DSHapticLink.
"""
import json
import logging
import os
import sys
import threading
from pathlib import Path

log = logging.getLogger("config")

DEFAULTS = {
    "language": "en",  # inglés por defecto; el usuario elige en la configuración
    "lightbar_color": [0, 0, 128],  # color de la barra cuando ningún juego la controla
    "haptics_gain": 1.0,  # 0.5 – 2.0
    "speaker_boost": 0,  # 0 – 7: refuerzo de volumen propio de la bocina del control
    "audio_enabled": True,  # bocina, audífonos y micrófono por Bluetooth
    "mic_enabled": True,
    "start_minimized": True,  # al iniciar, ir directo a la bandeja
    "notifications": True,
}


def app_dir():
    """Carpeta de la aplicación: la del .exe compilado, o la raíz del proyecto en desarrollo."""
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        return Path(sys.argv[0]).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir():
    """Carpeta para config.json y logs: junto a la app si se puede escribir ahí (portable)."""
    base = app_dir()
    try:
        probe = base / ".dsh_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return base
    except OSError:
        fallback = Path(os.environ.get("APPDATA", str(Path.home()))) / "DSHapticLink"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


class Config:
    def __init__(self, path=None):
        self.path = Path(path) if path else data_dir() / "config.json"
        self.lock = threading.Lock()
        self.values = dict(DEFAULTS)
        self.listeners = []
        self.load()

    def load(self):
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            for key, value in stored.items():
                if key in DEFAULTS:
                    self.values[key] = value
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            log.warning("config.json unreadable (%s); using defaults", e)

    def save(self):
        with self.lock:
            data = json.dumps(self.values, ensure_ascii=False, indent=2)
        try:
            self.path.write_text(data, encoding="utf-8")
        except OSError as e:
            log.warning("couldn't save config.json: %s", e)

    def get(self, key):
        value = self.values.get(key, DEFAULTS.get(key))
        if key == "language" and value not in ("es", "en"):
            return DEFAULTS["language"]
        return value

    def set(self, key, value):
        if key not in DEFAULTS:
            raise KeyError(key)
        value = self.validate(key, value)
        with self.lock:
            self.values[key] = value
        self.save()
        for listener in list(self.listeners):
            try:
                listener(key, value)
            except Exception:
                log.exception("error applying setting %s", key)
        return value

    @staticmethod
    def validate(key, value):
        if key == "language":
            return value if value in ("es", "en") else DEFAULTS["language"]
        if key == "lightbar_color":
            return [max(0, min(255, int(c))) for c in list(value)[:3]]
        if key == "haptics_gain":
            return max(0.5, min(2.0, float(value)))
        if key == "speaker_boost":
            return max(0, min(7, int(value)))
        return bool(value)

    def snapshot(self):
        with self.lock:
            out = dict(self.values)
        out["language"] = self.get("language")
        return out
