"""Reinicio del dispositivo HID del DualSense para poder tomarlo en exclusiva (reemplazo de HidHide).

Por qué: varios componentes de Windows (dwm.exe, GameInputSvc, explorer.exe, Game Bar) abren el control en
cuanto aparece, y así nadie puede abrirlo en exclusiva. Al deshabilitarlo y habilitarlo, todos pierden su
handle; el puente lo abre en el primer milisegundo y, desde ahí, Windows ya no lo vuelve a tomar.

Deshabilitar un dispositivo requiere administrador. Para no mostrar el UAC cada vez, se instala UNA vez una
tarea programada ("DSHapticLink\\RestartDualSense") que ejecuta este archivo con privilegios elevados; el puente la
lanza sin UAC con `schtasks /run` después de escribir en un archivo qué dispositivos reiniciar.

Este archivo se ejecuta también solo (por la tarea), así que no importa nada del paquete `bridge`.
Seguridad: solo reinicia dispositivos HID de DualSense (se valida cada identificador).
"""
import ctypes
import os
import re
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

TASK_NAME = r"DSHapticLink\RestartDualSense"
USBIP_DIR = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "USBip"
# Archivos que deja una instalación completa de usbip-win2. Solo usbip.exe no basta: pasó en una prueba real que,
# tras desinstalarlo sin reiniciar, quedó usbip.exe suelto (Windows lo borra al reiniciar) sin libusbip.dll ni el
# driver; la instalación siguiente lo dio por instalado y se saltó el driver ("no se encontró libusbip.dll").
USBIP_REQUIRED = ("usbip.exe", "libusbip.dll", "unins000.exe")


def usbip_installed():
    return all((USBIP_DIR / name).exists() for name in USBIP_REQUIRED)
# En ProgramData y no en AppData: Windows redirige en silencio las escrituras en AppData de las apps
# empaquetadas (MSIX), y entonces la tarea (que corre fuera de esa app) no vería la petición.
REQUEST_FILE = Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "DSHapticLink" / "restart_request.txt"
# HID del DualSense / DualSense Edge, por Bluetooth (VID&0002054C) o por cable (VID_054C)
ALLOWED = re.compile(r"^HID\\(\{00001124-0000-1000-8000-00805F9B34FB\}_VID&0002054C_PID&0(CE6|DF2)"
                     r"|VID_054C&PID_0(CE6|DF2)(&MI_\d\d)?)\\[0-9A-F&]+$", re.IGNORECASE)

CR_SUCCESS = 0
CM_REMOVE_UI_NOT_OK = 0x1
CM_REMOVE_DISABLE = 0x4  # Windows 8+: deja el nodo deshabilitado, como CM_Disable_DevNode
VETO_TYPES = ["Unknown", "LegacyDevice", "PendingClose", "WindowsApp", "WindowsService", "OutstandingOpen", "Device",
              "Driver", "IllegalDeviceRequest", "InsufficientPower", "NonDisableable", "LegacyDriver",
              "InsufficientRights", "AlreadyRemoved"]
CREATE_NO_WINDOW = 0x08000000


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def instance_id_from_hid_path(path):
    r"""\\?\HID#{...}_VID&..._PID&...#c&2&0&0000#{guid} → HID\{...}_VID&..._PID&...\C&2&0&0000"""
    p = path[4:] if path.startswith("\\\\?\\") else path
    return "\\".join(p.split("#")[:3]).upper()


def restart_instances(instance_ids, log=print):
    """Deshabilita y vuelve a habilitar cada dispositivo (requiere administrador). Devuelve cuántos reinició."""
    cfg = ctypes.WinDLL("cfgmgr32")
    done = 0
    for inst in instance_ids:
        inst = inst.strip()
        if not ALLOWED.match(inst):
            log(f"ignored (not a DualSense): {inst}")
            continue
        dn = wintypes.DWORD()
        if cfg.CM_Locate_DevNodeW(ctypes.byref(dn), inst, 0) != CR_SUCCESS:
            log(f"not found: {inst}")
            continue
        # Igual que CM_Disable_DevNode, pero si algún programa se niega a soltarlo dice cuál (PNP_VETO_TYPE + nombre).
        # Un veto no siempre es malo: si el puente lo abrió en exclusiva durante la consulta (cuando Steam y Windows
        # lo soltaron), el veto es del propio puente y el control ya quedó tomado.
        veto_type, veto_name = ctypes.c_int(0), ctypes.create_unicode_buffer(512)
        t0 = time.monotonic()
        r = cfg.CM_Query_And_Remove_SubTreeW(dn, ctypes.byref(veto_type), veto_name, 512,
                                             CM_REMOVE_UI_NOT_OK | CM_REMOVE_DISABLE)
        if r != CR_SUCCESS:
            kind = VETO_TYPES[veto_type.value] if 0 <= veto_type.value < len(VETO_TYPES) else veto_type.value
            log(f"couldn't disable {inst} (CR={r:#x}, veto={kind} {veto_name.value!r}, "
                f"{time.monotonic() - t0:.1f} s)")
            continue
        log(f"disabled in {time.monotonic() - t0:.1f} s")
        time.sleep(0.3)
        r = cfg.CM_Enable_DevNode(dn, 0)
        if r != CR_SUCCESS:
            # Reintentar: dejar el control deshabilitado sería peor que cualquier otra cosa.
            time.sleep(0.5)
            r = cfg.CM_Enable_DevNode(dn, 0)
        log(f"restarted {inst}" if r == CR_SUCCESS else f"COULDN'T RE-ENABLE {inst}! (CR={r:#x})")
        done += r == CR_SUCCESS
    return done


# ---------------------------------------------------------------------------------------------- tarea
def _schtasks(*args):
    # schtasks responde en la página de códigos de consola (OEM), no en UTF-8 ni en la ANSI.
    p = subprocess.run(["schtasks", *args], capture_output=True, encoding="oem", errors="replace",
                       creationflags=CREATE_NO_WINDOW)
    return p.returncode, (p.stdout + p.stderr).strip()


def task_installed():
    return _schtasks("/query", "/tn", TASK_NAME)[0] == 0


def task_command():
    """Ejecutable al que apunta la tarea (o None si no hay tarea)."""
    code, out = _schtasks("/query", "/tn", TASK_NAME, "/xml")
    if code != 0:
        return None
    m = re.search(r"<Command>(.*?)</Command>", out, re.DOTALL)
    return m.group(1).strip().strip('"') if m else None


def task_ready():
    """La tarea existe Y su ejecutable también. Pasó en una prueba real: se borró a mano la carpeta del helper
    (C:\\Program Files\\DSHapticLink Helper); la tarea seguía ahí, pero cada ejecución fallaba con 0x80070002
    (archivo no encontrado) y el control nunca se reiniciaba. Así la app vuelve a ofrecer "Install components"."""
    command = task_command()
    return bool(command) and Path(command).exists()


TASK_DESCRIPTION = (
    "DS HapticLink: restarts your DualSense (Bluetooth) when the DS HapticLink app asks for it, so the app can "
    "use it exclusively and games see only its virtual wired DualSense. It only touches DualSense HID devices and "
    "runs on demand (no schedule). Source code: https://github.com/SURTUR-00/DS-HapticLink")


def task_xml(command, arguments, user):
    """Definición completa de la tarea. Con `schtasks /create /tr` no se puede poner descripción, y Windows le
    deja "no iniciar con batería" (en una laptop sin cargador el control nunca se reiniciaba)."""
    from xml.sax.saxutils import escape
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>DS HapticLink</Author>
    <Description>{escape(TASK_DESCRIPTION)}</Description>
    <URI>\\{escape(TASK_NAME)}</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT2M</ExecutionTimeLimit>
    <Priority>4</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
    </Exec>
  </Actions>
</Task>
"""


def install_task(command=None, arguments="", xml_dir=None):
    """Crea la tarea (requiere administrador): con privilegios elevados, para el usuario actual y solo con la
    sesión iniciada (no guarda contraseña), sin horario (solo a pedido) y también con batería.
    command: en el producto, C:\\Program Files\\DSHapticLink Helper\\dsh-helper.exe (con arguments="restart"): una
    ruta que solo un administrador puede modificar. Por defecto, en desarrollo, este archivo con pythonw.
    xml_dir: carpeta donde escribir la definición; en el producto, una solo de administradores (si fuera
    escribible por el usuario, podría cambiarla antes de que schtasks la lea)."""
    if command is None:
        python_exe = Path(sys.executable)
        # pythonw.exe: sin ventana de consola (python.exe abriría una que le roba el foco al puente).
        windowless = python_exe.with_name("pythonw.exe")
        if windowless.exists():
            python_exe = windowless
        command, arguments = str(python_exe), f'-I "{Path(__file__).resolve()}"'
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".lstrip("\\")
    import tempfile
    folder = Path(xml_dir) if xml_dir else Path(tempfile.mkdtemp(prefix="dsh_task_"))
    folder.mkdir(parents=True, exist_ok=True)
    xml_file = folder / "RestartDualSense.xml"
    xml_file.write_text(task_xml(command, arguments, user), encoding="utf-16")
    try:
        return _schtasks("/create", "/tn", TASK_NAME, "/xml", str(xml_file), "/f")
    finally:
        xml_file.unlink(missing_ok=True)


def uninstall_task():
    return _schtasks("/delete", "/tn", TASK_NAME, "/f")


def request_restart(instance_ids):
    """Pide reiniciar estos dispositivos: directamente si somos administrador; si no, por la tarea.
    Devuelve 'directo', 'tarea' o None si no hay forma de hacerlo."""
    ids = [i for i in instance_ids if ALLOWED.match(i)]
    if not ids:
        return None
    if is_admin():
        restart_instances(ids, log=lambda m: None)
        return "directo"
    if not task_ready():
        return None
    REQUEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    REQUEST_FILE.write_text("\n".join(ids), encoding="utf-8")
    code, _ = _schtasks("/run", "/tn", TASK_NAME)
    return "tarea" if code == 0 else None


LOG_FILE = REQUEST_FILE.with_name("restart.log")


def _task_main():
    """Punto de entrada de la tarea programada: reinicia lo que pidió el puente y borra la petición."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        def log(msg):
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")

        log(f"task started (admin={is_admin()}, request={REQUEST_FILE})")
        try:
            ids = REQUEST_FILE.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            log(f"no request: {e}")
            return
        try:
            REQUEST_FILE.unlink()
        except OSError:
            pass
        n = restart_instances(ids, log=log)
        log(f"done: {n} restarted")

    if LOG_FILE.stat().st_size > 200_000:  # que el registro no crezca sin límite
        LOG_FILE.write_text(LOG_FILE.read_text(encoding="utf-8")[-50_000:], encoding="utf-8")


if __name__ == "__main__":
    _task_main()
