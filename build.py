"""Compila DS HapticLink con Nuitka y arma la carpeta portable + un .zip.

    .venv\\Scripts\\python.exe build.py            → dist\\DSHapticLink\\ y dist\\DSHapticLink-<versión>-portable.zip
    .venv\\Scripts\\python.exe build.py --console  → igual, pero DSHapticLink.exe con consola (para depurar)

La primera vez Nuitka descarga su compilador de C (Zig) y tarda varios minutos; después usa caché.
"""
import argparse
import hashlib
import importlib.util
import urllib.request
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"
sys.path.insert(0, str(ROOT))
from app.version import APP_ID, APP_NAME, VERSION  # noqa: E402

ICON = ROOT / "assets" / "dsh.ico"
COMMON = [
    "--standalone",
    "--assume-yes-for-downloads",
    f"--windows-icon-from-ico={ICON}",
    "--company-name=DS HapticLink",
    f"--product-name={APP_NAME}",
    f"--file-version={VERSION}.0",
    f"--product-version={VERSION}.0",
    "--copyright=DS HapticLink contributors",
]


def nuitka(entry, output_dir, filename, description, extra):
    cmd = [sys.executable, "-m", "nuitka", *COMMON, f"--output-dir={output_dir}",
           f"--output-filename={filename}", f"--file-description={description}", *extra, str(entry)]
    print(">", " ".join(cmd[3:]))
    subprocess.run(cmd, cwd=ROOT, check=True)
    return output_dir / (entry.stem + ".dist")


def opus_dll():
    spec = importlib.util.find_spec("pyogg")
    return Path(spec.origin).parent / "opus.dll"


def usbip_license_url():
    from helper import dsh_helper as H  # la licencia de la misma versión que se incluye
    return f"https://raw.githubusercontent.com/vadimgrn/usbip-win2/v.{H.USBIP_VERSION}/LICENSE.txt"


def _fetch(url, target):
    if not target.exists():
        print(f"> descargando {url}")
        target.parent.mkdir(parents=True, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": "DSHapticLink-build"})
        with urllib.request.urlopen(req, timeout=120) as r:
            target.write_bytes(r.read())
    return target


def usbip_files():
    """Instalador de usbip-win2 (versión fija, verificada por SHA-256) y su licencia BSD-2-Clause, en caché."""
    from helper import dsh_helper as H
    cache = BUILD / "usbip"
    installer = _fetch(H.USBIP_URL, cache / H.USBIP_INSTALLER_NAME)
    digest = hashlib.sha256(installer.read_bytes()).hexdigest().upper()
    if digest != H.USBIP_SHA256:
        installer.unlink()
        sys.exit(f"El instalador de usbip-win2 no coincide con la huella esperada ({digest})")
    return installer, _fetch(usbip_license_url(), cache / "LICENSE.txt")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--console", action="store_true", help=f"{APP_ID}.exe con ventana de consola (depuración)")
    args = ap.parse_args()

    if not ICON.exists():
        from app.tray_icon import save_ico
        save_ico(ICON)

    # 1. Helper: solo biblioteca estándar + bridge.devrestart. Sin consola (lo ejecuta la tarea programada).
    helper_dist = nuitka(ROOT / "helper" / "dsh_helper.py", BUILD / "helper", "dsh-helper.exe",
                         f"{APP_NAME} helper (acciones de administrador)",
                         ["--windows-console-mode=disable", "--include-module=bridge.devrestart"])

    # 2. App: pywebview (WebView2 vía pythonnet), pystray, hidapi. La interfaz y opus.dll van como datos.
    app_dist = nuitka(ROOT / "app" / "main.py", BUILD / "app", f"{APP_ID}.exe", f"{APP_NAME} - DualSense haptics over Bluetooth",
                      [f"--windows-console-mode={'force' if args.console else 'disable'}",
                       "--include-package=app", "--include-package=bridge",
                       "--include-package-data=webview", "--include-package-data=pythonnet",
                       "--include-package-data=clr_loader",
                       f"--include-data-dir={ROOT / 'ui'}=ui", f"--include-data-files={ICON}=assets/dsh.ico",
                       f"--include-data-files={opus_dll()}=opus.dll",
                       "--nofollow-import-to=pyogg", "--nofollow-import-to=tests", "--nofollow-import-to=tools"])

    # 3. Carpeta portable: la app + helper\ (sin archivos de desarrollo).
    target = DIST / APP_ID
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(app_dist, target)
    shutil.copytree(helper_dist, target / "helper")
    for dev_only in ("ui/dev_mock.js",):
        (target / dev_only).unlink(missing_ok=True)
    # usbip-win2 incluido (el helper lo usa en vez de descargarlo) + licencias
    installer, usbip_license = usbip_files()
    shutil.copy2(installer, target / "helper" / installer.name)
    licenses = target / "licenses"
    licenses.mkdir()
    shutil.copy2(usbip_license, licenses / "usbip-win2.txt")
    shutil.copy2(ROOT / "LICENSE", target / "LICENSE.txt")
    shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.md", target / "THIRD_PARTY_NOTICES.md")
    essential = [f"{APP_ID}.exe", "helper/dsh-helper.exe", "opus.dll", "ui/index.html", "ui/app.js", "assets/dsh.ico",
                 "webview/js/api.js", "webview/lib/Microsoft.Web.WebView2.Core.dll",
                 "pythonnet/runtime/Python.Runtime.dll", f"helper/{installer.name}", "licenses/usbip-win2.txt",
                 "LICENSE.txt", "THIRD_PARTY_NOTICES.md"]
    missing = [f for f in essential if not (target / f).exists()]
    if missing:
        sys.exit(f"Faltan archivos esenciales en {target}: {missing}")

    # 4. Zip
    archive = DIST / f"{APP_ID}-{VERSION}-portable.zip"
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(target.rglob("*")):
            if f.is_file():
                z.write(f, Path(APP_ID) / f.relative_to(target))
    size = sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
    zip_sha = hashlib.sha256(archive.read_bytes()).hexdigest().upper()
    (DIST / f"{archive.name}.sha256").write_text(f"{zip_sha}  {archive.name}\n", encoding="ascii")
    print(f"\nListo: {target} ({size / 1e6:.1f} MB) y {archive.name} ({archive.stat().st_size / 1e6:.1f} MB)")
    print(f"SHA-256: {zip_sha}")


if __name__ == "__main__":
    main()
