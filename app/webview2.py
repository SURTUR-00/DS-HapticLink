"""Microsoft Edge WebView2: el motor con el que pywebview dibuja la ventana de DS HapticLink.

Viene con Windows 11 y casi siempre está en Windows 10 (lo instalan Windows Update, Edge u Office), pero no
está garantizado (p. ej. Windows 10 LTSC o equipos sin internet). Sin él, la app funciona igual desde la
bandeja; solo falta la ventana de ajustes. Aquí se detecta y, si el usuario quiere, se instala el
"Evergreen Bootstrapper" oficial de Microsoft (unos 2 MB) tras verificar su firma.
"""
import ctypes
import logging
import subprocess
import tempfile
import urllib.request
import winreg
from pathlib import Path

log = logging.getLogger("webview2")

CLIENT_KEY = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
BOOTSTRAPPER_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
CREATE_NO_WINDOW = 0x08000000

TEXT = {
    "es": {
        "title": "DS HapticLink",
        "ask": "DS HapticLink necesita Microsoft Edge WebView2 para mostrar su ventana de ajustes.\n\n"
               "¿Descargarlo e instalarlo ahora? (instalador oficial de Microsoft, unos 2 MB)\n\n"
               "Si eliges No, DS HapticLink funciona igual desde el ícono \"DSH\" de la bandeja.",
        "failed": "No se pudo instalar WebView2. DS HapticLink seguirá funcionando desde la bandeja.\n\n"
                  "Puedes instalarlo después desde: https://developer.microsoft.com/microsoft-edge/webview2/",
        "tray_only": "La ventana de ajustes necesita Microsoft Edge WebView2, que no está instalado.\n\n"
                     "DS HapticLink sigue funcionando: tu control y tus juegos no se ven afectados.",
        "setup_ask": "DS HapticLink necesita instalar dos componentes una sola vez (el driver del control virtual y el "
                     "liberador del control). Windows te pedirá permiso de administrador.\n\n¿Instalarlos ahora?",
        "setup_ok": "Componentes instalados. DS HapticLink está listo.",
        "setup_fail": "No se pudieron instalar los componentes.",
    },
    "en": {
        "title": "DS HapticLink",
        "ask": "DS HapticLink needs Microsoft Edge WebView2 to show its settings window.\n\n"
               "Download and install it now? (official Microsoft installer, about 2 MB)\n\n"
               "If you choose No, DS HapticLink still works from the \"DSH\" icon in the tray.",
        "failed": "WebView2 couldn't be installed. DS HapticLink will keep working from the tray.\n\n"
                  "You can install it later from: https://developer.microsoft.com/microsoft-edge/webview2/",
        "tray_only": "The settings window needs Microsoft Edge WebView2, which isn't installed.\n\n"
                     "DS HapticLink keeps working: your controller and your games aren't affected.",
        "setup_ask": "DS HapticLink needs to install two components once (the virtual controller driver and the "
                     "controller unlocker). Windows will ask for administrator permission.\n\nInstall them now?",
        "setup_ok": "Components installed. DS HapticLink is ready.",
        "setup_fail": "The components couldn't be installed.",
    },
}


def installed():
    """WebView2 instalado para todo el equipo o para el usuario (claves de registro documentadas por Microsoft)."""
    places = [(winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\" + CLIENT_KEY),
              (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\" + CLIENT_KEY),
              (winreg.HKEY_CURRENT_USER, "Software\\" + CLIENT_KEY)]
    for root, key in places:
        try:
            with winreg.OpenKey(root, key) as k:
                version, _ = winreg.QueryValueEx(k, "pv")
                if version and version != "0.0.0.0":
                    return True
        except OSError:
            continue
    return False


def message(lang, key, yes_no=False):
    """Cuadro de diálogo nativo de Windows (no necesita WebView2). Devuelve True si el usuario eligió Sí."""
    t = TEXT.get(lang, TEXT["en"])
    MB_YESNO, MB_OK, MB_ICONQUESTION, MB_ICONINFO, IDYES = 0x4, 0x0, 0x20, 0x40, 6
    flags = (MB_YESNO | MB_ICONQUESTION) if yes_no else (MB_OK | MB_ICONINFO)
    return ctypes.windll.user32.MessageBoxW(None, t[key], t["title"], flags) == IDYES


def _signed_by_microsoft(path):
    cmd = (f"$s = Get-AuthenticodeSignature -LiteralPath '{path}'; "
           "($s.Status -eq 'Valid') -and ($s.SignerCertificate.Subject -like '*O=Microsoft Corporation*')")
    p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True,
                       encoding="oem", errors="replace", creationflags=CREATE_NO_WINDOW, timeout=60)
    return p.stdout.strip() == "True"


def install():
    """Descarga el instalador oficial, verifica que lo firmó Microsoft y lo ejecuta (sin permisos de
    administrador se instala solo para el usuario). Devuelve True si al final WebView2 quedó instalado."""
    target = Path(tempfile.mkdtemp(prefix="dsh_wv2_")) / "MicrosoftEdgeWebview2Setup.exe"
    try:
        request = urllib.request.Request(BOOTSTRAPPER_URL, headers={"User-Agent": "DSHapticLink"})
        with urllib.request.urlopen(request, timeout=60) as r:
            target.write_bytes(r.read())
        if not _signed_by_microsoft(target):
            log.error("the downloaded WebView2 installer has no valid Microsoft signature; not running it")
            return False
        code = subprocess.run([str(target), "/silent", "/install"], timeout=600).returncode
        log.info("WebView2 installer finished with code %s", code)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.error("couldn't install WebView2: %s", e)
        return False
    finally:
        try:
            target.unlink()
            target.parent.rmdir()
        except OSError:
            pass
    return installed()


def ensure(lang):
    """Al arrancar: True si hay WebView2 (o se acaba de instalar); False → la app en modo solo bandeja."""
    if installed():
        return True
    log.warning("WebView2 is not installed")
    if not message(lang, "ask", yes_no=True):
        return False
    if install():
        return True
    message(lang, "failed")
    return False
