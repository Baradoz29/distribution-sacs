# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


ROOT_DIR = Path(globals().get("SPECPATH", ".")).resolve()
DATA_FILES = [
    (str(ROOT_DIR / "static"), "static"),
    (str(ROOT_DIR / "data" / "douarnenez_buildings_geopf_snapshot.geojson"), "data"),
    (str(ROOT_DIR / "outils" / "data" / "insee_first_names_weighted.json"), "outils/data"),
]

a = Analysis(
    ["launcher.py"],
    pathex=[str(ROOT_DIR)],
    binaries=[],
    datas=DATA_FILES,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="SuiviDistributionSacs",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
