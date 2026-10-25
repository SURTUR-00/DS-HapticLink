"""dsh-helper: las únicas acciones de DS HapticLink que necesitan administrador.

    dsh-helper install [--lang es|en]   instala usbip-win2 (descargado y verificado) y la tarea de reinicio
    dsh-helper uninstall [--drivers]    quita la tarea y la copia protegida (y usbip-win2 con --drivers)
    dsh-helper restart                  lo ejecuta la tarea programada: reinicia el DualSense que pidió el puente

La app (DSHapticLink.exe) lo lanza elevado UNA vez (un solo UAC) para instalar. El progreso se escribe en
C:\\ProgramData\\DSHapticLink\\setup_status.json para que la interfaz muestre una barra de avance.

Seguridad: la tarea elevada apunta a la copia de este ejecutable en C:\\Program Files\\DSHapticLink Helper (solo un
administrador puede modificarla), nunca a la carpeta portable, que el usuario sí puede modificar.
"""
import argparse
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# El helper se compila junto con bridge/devrestart.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from bridge import devrestart  # noqa: E402

USBIP_VERSION = "0.9.8.1"
USBIP_URL = f"https://github.com/vadimgrn/usbip-win2/releases/download/v.{USBIP_VERSION}/USBip-{USBIP_VERSION}-x64.exe"
USBIP_SHA256 = "38CAD6D4432B52D5BB9409D9AD03B72FDFFC4ADA4CD3A48FBECA1A2752A8518A"
USBIP_INSTALLER_NAME = f"USBip-{USBIP_VERSION}-x64.exe"
# Desde 0.4.6 el instalador viene dentro del paquete, junto a este ejecutable (usbip-win2 es BSD-2-Clause: se puede
# redistribuir con su licencia). Si falta o no coincide la huella, se descarga de su sitio oficial como respaldo.
BUNDLED_USBIP = Path(sys.argv[0]).resolve().parent / USBIP_INSTALLER_NAME
USBIP_EXE = Path(r"C:\Program Files\USBip\usbip.exe")
USBIP_UNINSTALLER = Path(r"C:\Program Files\USBip\unins000.exe")
# Carpeta propia del helper, distinta de cualquier carpeta donde el usuario pueda poner la app (por ejemplo
# C:\Program Files\DSHapticLink): la instalación la borra y la vuelve a copiar.
PROTECTED_DIR = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "DSHapticLink Helper"
# Carpeta temporal para el instalador, solo escribible por administradores (hereda los permisos de Program Files):
# se verifica y se ejecuta desde aquí, así nadie puede cambiarlo entre la verificación y la ejecución.
SETUP_DIR = PROTECTED_DIR.with_name("DSHapticLink Helper Setup")
STATUS_FILE = devrestart.REQUEST_FILE.with_name("setup_status.json")
LOG_FILE = devrestart.REQUEST_FILE.with_name("setup.log")
CREATE_NO_WINDOW = 0x08000000

TEXT = {
    "es": {
        "start": "Preparando la instalación…",
        "download": "Descargando usbip-win2 (driver del control virtual)…",
        "prepare": "Preparando usbip-win2 (driver del control virtual)…",
        "verify": "Verificando usbip-win2…",
        "install_usbip": "Instalando usbip-win2 (los dispositivos USB pueden desconectarse un momento)…",
        "usbip_ok": "usbip-win2 ya está instalado",
        "copy": "Copiando el asistente protegido…",
        "task": "Creando la tarea que libera el control sin pedir permisos…",
        "done": "¡Listo!",
        "bad_hash": "usbip-win2 no coincide con la versión esperada (huella SHA-256 distinta). Se canceló.",
        "bad_sig": "La firma digital de usbip-win2 no es válida. Se canceló.",
        "net": "No se pudo descargar usbip-win2. Revisa tu conexión a internet.",
        "install_fail": "El instalador de usbip-win2 falló (código {code}).",
        "task_fail": "No se pudo crear la tarea: {msg}",
        "unexpected": "Error inesperado: {msg}",
        "u_start": "Preparando la desinstalación…",
        "u_task": "Quitando la tarea programada y el asistente…",
        "u_driver": "Quitando usbip-win2 (los dispositivos USB pueden desconectarse un momento)…",
        "u_done": "¡Listo!",
        "u_fail": "No se pudo quitar: {errors}",
    },
    "en": {
        "start": "Preparing the installation…",
        "download": "Downloading usbip-win2 (virtual controller driver)…",
        "prepare": "Preparing usbip-win2 (virtual controller driver)…",
        "verify": "Verifying usbip-win2…",
        "install_usbip": "Installing usbip-win2 (USB devices may disconnect for a moment)…",
        "usbip_ok": "usbip-win2 is already installed",
        "copy": "Copying the protected helper…",
        "task": "Creating the task that frees the controller without asking for permission…",
        "done": "Done!",
        "bad_hash": "usbip-win2 doesn't match the expected version (different SHA-256). Cancelled.",
        "bad_sig": "usbip-win2's digital signature is not valid. Cancelled.",
        "net": "Couldn't download usbip-win2. Check your internet connection.",
        "install_fail": "The usbip-win2 installer failed (code {code}).",
        "task_fail": "Couldn't create the task: {msg}",
        "unexpected": "Unexpected error: {msg}",
        "u_start": "Preparing to uninstall…",
        "u_task": "Removing the scheduled task and the helper…",
        "u_driver": "Removing usbip-win2 (USB devices may disconnect for a moment)…",
        "u_done": "Done!",
        "u_fail": "Couldn't remove: {errors}",
    },
}


def log(text):
    """setup.log en inglés, para diagnosticar instalaciones fallidas. Nunca falla."""
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass


class Progress:
    def __init__(self, lang):
        self.t = TEXT.get(lang, TEXT["en"])
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, key, percent, ok=None, **fmt):
        """Nunca falla: la app lee el archivo cada 0.3 s y, mientras lo tiene abierto, Windows no deja
        reemplazarlo (acceso denegado). Se reintenta un momento; si no se puede, se omite esta actualización."""
        state = {"message": self.t[key].format(**fmt), "percent": percent, "ok": ok, "time": time.time()}
        if ok is not None or key not in ("download",):
            log(f"{key} {percent}% ok={ok} {fmt or ''}".rstrip())
        tmp = STATUS_FILE.with_suffix(".tmp")
        for attempt in range(40 if ok is not None else 10):  # el estado final se reintenta más (hasta 2 s)
            try:
                tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, STATUS_FILE)
                return
            except OSError:
                time.sleep(0.05)
        log(f"could not write {STATUS_FILE.name} ({key})")


def frozen():
    return "__compiled__" in globals() or getattr(sys, "frozen", False)


def authenticode_ok(path):
    """Firma Authenticode válida, con WinVerifyTrust (la misma comprobación que Get-AuthenticodeSignature).
    Antes se lanzaba PowerShell para esto: un programa sin firma que abre PowerShell como administrador es de lo
    que más alerta a los antivirus (Defender marcó el helper como Behavior:Win32/Execution.A!ml)."""
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    class WINTRUST_FILE_INFO(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD), ("pcwszFilePath", wintypes.LPCWSTR), ("hFile", wintypes.HANDLE),
                    ("pgKnownSubject", ctypes.c_void_p)]

    class WINTRUST_DATA(ctypes.Structure):
        _fields_ = [("cbStruct", wintypes.DWORD), ("pPolicyCallbackData", ctypes.c_void_p),
                    ("pSIPClientData", ctypes.c_void_p), ("dwUIChoice", wintypes.DWORD),
                    ("fdwRevocationChecks", wintypes.DWORD), ("dwUnionChoice", wintypes.DWORD),
                    ("pFile", ctypes.POINTER(WINTRUST_FILE_INFO)), ("dwStateAction", wintypes.DWORD),
                    ("hWVTStateData", wintypes.HANDLE), ("pwszURLReference", wintypes.LPCWSTR),
                    ("dwProvFlags", wintypes.DWORD), ("dwUIContext", wintypes.DWORD),
                    ("pSignatureSettings", ctypes.c_void_p)]

    WTD_UI_NONE, WTD_REVOKE_NONE, WTD_CHOICE_FILE = 2, 0, 1
    WTD_STATEACTION_VERIFY, WTD_STATEACTION_CLOSE = 1, 2
    # WINTRUST_ACTION_GENERIC_VERIFY_V2 = {00AAC56B-CD44-11d0-8CC2-00C04FC295EE}
    action = GUID(0x00AAC56B, 0xCD44, 0x11D0, (ctypes.c_ubyte * 8)(0x8C, 0xC2, 0x00, 0xC0, 0x4F, 0xC2, 0x95, 0xEE))
    file_info = WINTRUST_FILE_INFO(ctypes.sizeof(WINTRUST_FILE_INFO), str(path), None, None)
    data = WINTRUST_DATA(cbStruct=ctypes.sizeof(WINTRUST_DATA), dwUIChoice=WTD_UI_NONE,
                         fdwRevocationChecks=WTD_REVOKE_NONE, dwUnionChoice=WTD_CHOICE_FILE,
                         pFile=ctypes.pointer(file_info), dwStateAction=WTD_STATEACTION_VERIFY)
    wintrust = ctypes.WinDLL("wintrust")
    wintrust.WinVerifyTrust.restype = ctypes.c_long
    wintrust.WinVerifyTrust.argtypes = [wintypes.HWND, ctypes.POINTER(GUID), ctypes.c_void_p]
    result = wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))
    data.dwStateAction = WTD_STATEACTION_CLOSE
    wintrust.WinVerifyTrust(None, ctypes.byref(action), ctypes.byref(data))
    if result != 0:
        log(f"signature check failed for {path}: WinVerifyTrust 0x{result & 0xFFFFFFFF:08X}")
    return result == 0


def download_usbip(progress):
    """Descarga el instalador oficial (respaldo si no viene en el paquete). Devuelve los bytes."""
    request = urllib.request.Request(USBIP_URL, headers={"User-Agent": "DSHapticLink-helper"})
    data = bytearray()
    with urllib.request.urlopen(request, timeout=60) as r:
        total = int(r.headers.get("Content-Length") or 0)
        while chunk := r.read(256 * 1024):
            data += chunk
            if total:
                progress("download", 5 + int(45 * len(data) / total))
    return bytes(data)


def sha256_ok(data):
    return hashlib.sha256(data).hexdigest().upper() == USBIP_SHA256


def usbip_installer_bytes(progress):
    """El instalador incluido en el paquete si está y es el esperado; si no, el descargado. Lanza OSError si no
    se puede descargar; devuelve None si lo descargado tampoco coincide."""
    if BUNDLED_USBIP.exists():
        progress("prepare", 20)
        data = BUNDLED_USBIP.read_bytes()
        if sha256_ok(data):
            log("using the bundled usbip-win2 installer")
            return data
        log("the bundled usbip-win2 installer doesn't match the expected SHA-256: downloading it instead")
    progress("download", 5)
    data = download_usbip(progress)
    return data if sha256_ok(data) else None


def install(lang):
    progress = Progress(lang)
    log(f"install started (helper at {Path(sys.argv[0]).resolve().parent})")
    try:
        return _install(progress)
    except Exception as e:  # cualquier fallo debe llegar a la interfaz, no dejarla esperando
        import traceback
        log("unexpected error:\n" + traceback.format_exc())
        progress("unexpected", 0, ok=False, msg=f"{type(e).__name__}: {e}")
        return 9


def _install(progress):
    progress("start", 1)
    devrestart.enable_disabled_dualsense(log=log)  # reparar un control que haya quedado deshabilitado
    # 1. usbip-win2 (si falta o quedó incompleto)
    if devrestart.usbip_installed():
        progress("usbip_ok", 50)
    else:
        try:
            data = usbip_installer_bytes(progress)
        except OSError as e:  # URLError, timeout, conexión cortada…
            log(f"download failed: {type(e).__name__}: {e}")
            progress("net", 0, ok=False)
            return 2
        if data is None:
            progress("bad_hash", 0, ok=False)
            return 3
        # Verificado en memoria; se escribe en una carpeta solo de administradores y se ejecuta desde ahí.
        progress("verify", 52)
        shutil.rmtree(SETUP_DIR, ignore_errors=True)
        SETUP_DIR.mkdir(parents=True)
        installer = SETUP_DIR / USBIP_INSTALLER_NAME
        installer.write_bytes(data)
        try:
            if not authenticode_ok(installer):
                progress("bad_sig", 0, ok=False)
                return 3
            progress("install_usbip", 60)
            code = subprocess.run([str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"]).returncode
        finally:
            shutil.rmtree(SETUP_DIR, ignore_errors=True)
        log(f"usbip-win2 installer exited with code {code}")
        if code != 0 or not devrestart.usbip_installed():
            progress("install_fail", 0, ok=False, code=code)
            return 4
    # 2. Copia protegida del helper + tarea programada
    if frozen():
        progress("copy", 85)
        # Se copia la carpeta completa del helper (el .exe y su runtime): la tarea elevada solo debe ejecutar
        # archivos que el usuario no pueda modificar.
        source = Path(sys.argv[0]).resolve().parent
        if source != PROTECTED_DIR:  # (si ya se está ejecutando desde la copia protegida, no hay nada que copiar)
            if PROTECTED_DIR.exists():
                shutil.rmtree(PROTECTED_DIR, ignore_errors=True)
            # (sin el instalador de usbip-win2: no hace falta ahí y pesa 26 MB)
            shutil.copytree(source, PROTECTED_DIR, ignore=shutil.ignore_patterns("USBip-*.exe"))
        task = {"command": str(PROTECTED_DIR / "dsh-helper.exe"), "arguments": "restart", "xml_dir": PROTECTED_DIR}
    else:
        task = {}  # desarrollo: python + bridge/devrestart.py
    progress("task", 92)
    code, out = devrestart.install_task(**task)
    if code != 0:
        progress("task_fail", 0, ok=False, msg=out)
        return 5
    progress("done", 100, ok=True)
    return 0


UNINSTALL_TEXT = {
    "es": {"ok": "Se quitaron los componentes de DS HapticLink.{extra}",
           "drivers": "\n\nReinicia Windows para terminar de quitar el driver de usbip-win2.",
           "fail": "No se pudo quitar todo:\n{errors}\n\nCierra DS HapticLink (bandeja → Salir) e inténtalo de nuevo. "
                   "Detalles en C:\\ProgramData\\DSHapticLink\\setup.log."},
    "en": {"ok": "DS HapticLink's components were removed.{extra}",
           "drivers": "\n\nRestart Windows to finish removing the usbip-win2 driver.",
           "fail": "Not everything could be removed:\n{errors}\n\nQuit DS HapticLink (tray → Quit) and try again. "
                   "Details in C:\\ProgramData\\DSHapticLink\\setup.log."},
}


def message_box(text):
    """El helper no tiene consola: el resultado se muestra en una ventana."""
    MB_ICONINFORMATION, MB_SETFOREGROUND = 0x40, 0x10000
    ctypes.windll.user32.MessageBoxW(None, text, "DS HapticLink", MB_ICONINFORMATION | MB_SETFOREGROUND)


def uninstall(drivers, lang="en", quiet=False):
    """Quita la tarea, la copia protegida del helper y (con drivers) usbip-win2. Registra cada paso en setup.log.
    Probado: con el control virtual conectado o sin esperar al desinstalador, usbip-win2 quedaba instalado."""
    progress = Progress(lang)
    progress("u_start", 5)
    log(f"uninstall started (drivers={drivers})")
    errors = []
    if USBIP_EXE.exists():  # soltar el control virtual (si la app sigue abierta, el driver no se puede quitar)
        p = subprocess.run([str(USBIP_EXE), "detach", "--all"], capture_output=True, encoding="oem", errors="replace",
                           creationflags=CREATE_NO_WINDOW)
        log(f"usbip detach --all: {p.returncode} {(p.stdout + p.stderr).strip()}")
    progress("u_task", 20)
    code, out = devrestart.uninstall_task()
    log(f"delete task: {code} {out}")
    if devrestart.task_installed():
        errors.append("scheduled task")
    for folder in (PROTECTED_DIR, SETUP_DIR):
        shutil.rmtree(folder, ignore_errors=True)
        if folder.exists():
            errors.append(str(folder))
    if drivers and USBIP_UNINSTALLER.exists():
        progress("u_driver", 40)
        inno_log = LOG_FILE.with_name("usbip_uninstall.log")
        p = subprocess.run([str(USBIP_UNINSTALLER), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                            f"/LOG={inno_log}"])
        log(f"usbip-win2 uninstaller started: {p.returncode} (log: {inno_log})")
        # El desinstalador de Inno Setup se copia a TEMP y sigue por su cuenta: esperar a que termine de verdad.
        deadline = time.monotonic() + 180
        # usbip.exe puede quedar hasta reiniciar (si estaba en uso): lo que indica que terminó es que se va su
        # desinstalador.
        while USBIP_UNINSTALLER.exists() and time.monotonic() < deadline:
            time.sleep(0.5)
        if USBIP_UNINSTALLER.exists():
            errors.append("usbip-win2")
    log(f"uninstall finished: {'OK' if not errors else 'errors: ' + ', '.join(errors)}")
    if errors:
        progress("u_fail", 0, ok=False, errors=", ".join(errors))
    else:
        progress("u_done", 100, ok=True)
    if not quiet:
        txt = UNINSTALL_TEXT.get(lang, UNINSTALL_TEXT["en"])
        if errors:
            message_box(txt["fail"].format(errors="\n".join(f"• {e}" for e in errors)))
        else:
            message_box(txt["ok"].format(extra=txt["drivers"] if drivers else ""))
    return 0 if not errors else 6


def relaunch_elevated():
    """Lanzado sin permisos (p. ej. desde una terminal normal): pedir el UAC y volver a ejecutarse igual."""
    params = subprocess.list2cmdline(sys.argv[1:] if frozen() else [sys.argv[0], *sys.argv[1:]])
    exe = sys.executable if not frozen() else sys.argv[0]
    r = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(Path(exe).resolve()), params, None, 1)
    return 0 if r > 32 else 1


def main():
    ap = argparse.ArgumentParser(prog="dsh-helper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_install = sub.add_parser("install")
    p_install.add_argument("--lang", default="es")
    p_uninstall = sub.add_parser("uninstall")
    p_uninstall.add_argument("--drivers", action="store_true")
    p_uninstall.add_argument("--lang", default="en")
    p_uninstall.add_argument("--quiet", action="store_true", help="sin ventana de resultado")
    sub.add_parser("restart")
    args = ap.parse_args()
    if args.cmd == "restart":
        devrestart._task_main()
        return 0
    if not devrestart.is_admin():
        if args.cmd == "uninstall":
            return relaunch_elevated()
        print("dsh-helper needs administrator permission.")
        return 1
    if args.cmd == "install":
        return install(args.lang)
    return uninstall(args.drivers, args.lang, args.quiet)


if __name__ == "__main__":
    sys.exit(main())
