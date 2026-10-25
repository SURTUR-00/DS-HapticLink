"""Textos de la bandeja y las notificaciones (la interfaz web tiene los suyos en ui/i18n.js)."""

TEXTS = {
    "es": {
        "open": "Abrir DS HapticLink",
        "pause": "Pausar",
        "resume": "Reanudar",
        "quit": "Salir",
        "tooltip": {
            "listo": "DS HapticLink: listo",
            "en_juego": "DS HapticLink: en juego",
            "esperando_control": "DS HapticLink: enciende tu DualSense",
            "cable": "DS HapticLink: usando el cable",
            "iniciando": "DS HapticLink: iniciando…",
            "detenido": "DS HapticLink: en pausa",
            "falta_configurar": "DS HapticLink: falta configurar",
            "problema": "DS HapticLink: hay un problema",
        },
        "connected": "DualSense conectado",
        "disconnected": "DualSense desconectado",
        "battery_low": "Batería baja: {pct}%",
        "already_running": "DS HapticLink ya está abierto",
        "repair": "Falta un componente de DS HapticLink. Abre la app y presiona \"Instalar componentes\" para repararlo.",
    },
    "en": {
        "open": "Open DS HapticLink",
        "pause": "Pause",
        "resume": "Resume",
        "quit": "Quit",
        "tooltip": {
            "listo": "DS HapticLink: ready",
            "en_juego": "DS HapticLink: in game",
            "esperando_control": "DS HapticLink: turn on your DualSense",
            "cable": "DS HapticLink: using the cable",
            "iniciando": "DS HapticLink: starting…",
            "detenido": "DS HapticLink: paused",
            "falta_configurar": "DS HapticLink: setup needed",
            "problema": "DS HapticLink: there's a problem",
        },
        "connected": "DualSense connected",
        "disconnected": "DualSense disconnected",
        "battery_low": "Low battery: {pct}%",
        "already_running": "DS HapticLink is already open",
        "repair": "A DS HapticLink component is missing. Open the app and click \"Install components\" to repair it.",
    },
}


def t(lang, key, **fmt):
    value = TEXTS.get(lang, TEXTS["en"])[key]
    return value.format(**fmt) if isinstance(value, str) else value
