# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for PRC Factsheet Automation.
#
# Build with (from this folder, inside the venv created by build_exe.bat):
#     pyinstaller --noconfirm PRC_App.spec
#
# Produces a folder build (onedir) at dist\PRC_Factsheet_Automation\ containing
# PRC_Factsheet_Automation.exe plus its dependencies. Onedir is used instead of
# onefile because easyocr/torch are large: onefile would re-extract ~1-2 GB to a
# temp folder on every single launch, making startup very slow. To distribute
# the app, zip the whole dist\PRC_Factsheet_Automation\ folder and share that.

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

# Packages that need their non-Python data files (model configs, character
# lists, etc.) and compiled binaries explicitly collected - PyInstaller's
# automatic import scanner misses these for ML-heavy libraries.
PACKAGES_TO_COLLECT = [
    "easyocr",
    "torch",
    "torchvision",
    "cv2",
    "skimage",
    "scipy",
    "PIL",
    "shapely",
    "pyclipper",
    "bidi",
    "fitz",       # pymupdf
    "pandas",
    "openpyxl",
]

for pkg in PACKAGES_TO_COLLECT:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:
        # Not every package above is guaranteed to be installed/needed in
        # every environment (e.g. shapely/pyclipper/bidi are easyocr's
        # transitive deps and their exact package names can shift between
        # releases). Skip silently rather than aborting the whole build -
        # a genuinely missing hard dependency will surface immediately when
        # you run the sanity-check import in build_exe.bat, or as a clear
        # ModuleNotFoundError the first time you launch the built .exe.
        print(f"[spec] Skipping collect_all for '{pkg}': {e}")

block_cipher = None

a = Analysis(
    ["PRC_UI.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PRC_Factsheet_Automation",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,   # windowed app, no console box.
    # If the app appears to silently vanish on launch (a startup error
    # before the in-app error dialog can even be created), temporarily set
    # console=True, rebuild, and run the .exe from a Command Prompt window
    # to see the real traceback.
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PRC_Factsheet_Automation",
)
