# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-file packaging for PortHelper Fluent.

Build with::

    python -m PyInstaller --noconfirm --clean --distpath dist --workpath build_work build/app.spec

The spec intentionally contains no COLLECT step: passing ``a.binaries`` and
``a.datas`` directly to EXE produces the single-file executable.  Only the
application package, main.py and the icon are added as data; analysis/, docs/,
tests/ and .git/ are never included.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

# SPECPATH points at the directory containing this spec (build/).
BUILD_DIR = Path(SPECPATH)
PROJECT_ROOT = BUILD_DIR.parent
ICON_PATH = BUILD_DIR / "icon.ico"
VERSION_PATH = BUILD_DIR / "version_info.txt"

# qfluentwidgets keeps its fonts/icons/QSS inside the compiled _rc.resource
# module, so this currently yields no data files; keep the call so future
# releases that ship sidecar resources are picked up automatically.
datas = list(collect_data_files("qfluentwidgets"))
if ICON_PATH.is_file():
    # find_app_icon() probes sys._MEIPASS/build/icon.ico at runtime.
    datas.append((str(ICON_PATH), "build"))

hiddenimports = [
    # hid is imported lazily inside app/io/hid_worker.py, so modulegraph cannot
    # discover it without help.  hidapi ships as a single ABI-tagged .pyd.
    "hid",
    # qrcode selects its Pillow backend at runtime.
    "qrcode",
    "qrcode.image.pil",
    "qrcode.image.styledpil",
    "qrcode.image.styles.moduledrawers.pil",
    # pyserial chooses the Windows backend at runtime.
    "serial",
    "serial.tools.list_ports",
    "serial.tools.list_ports_windows",
    # Explicit top-level imports keep the packaging report stable even if a
    # module later becomes optional/imported dynamically.
    "psutil",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageSequence",
    "numpy",
]

excludes = [
    "PyQt5",
    "PyQt6.QtWebEngineCore",
    "PyQt6.QtWebEngineWidgets",
    "PyQt6.QtWebEngineQuick",
    "PyQt6.QtQuick",
    "PyQt6.QtQuickWidgets",
    "PyQt6.QtQuick3D",
    "PyQt6.QtQml",
    "PyQt6.Qt3DCore",
    "PyQt6.Qt3DRender",
    "PyQt6.Qt3DInput",
    "PyQt6.Qt3DLogic",
    "PyQt6.Qt3DAnimation",
    "PyQt6.Qt3DExtras",
    "PyQt6.QtMultimedia",
    "PyQt6.QtMultimediaWidgets",
    "tkinter",
    "matplotlib",
    "scipy",
    "pandas",
]

a = Analysis(
    [str(PROJECT_ROOT / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PortHelperFluent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(ICON_PATH),
    version=str(VERSION_PATH),
)
