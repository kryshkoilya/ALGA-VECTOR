# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

project_root = Path(SPEC).resolve().parents[1]
rtlsdr_datas, rtlsdr_binaries, rtlsdr_hiddenimports = collect_all("rtlsdr")
rtllib_datas, rtllib_binaries, rtllib_hiddenimports = collect_all("pyrtlsdrlib")
usb_datas, usb_binaries, usb_hiddenimports = collect_all("usb")
serial_hiddenimports = collect_submodules("serial")

a = Analysis(
    [str(project_root / "src" / "alga_vector" / "__main__.py")],
    pathex=[str(project_root / "src")],
    binaries=[*rtlsdr_binaries, *rtllib_binaries, *usb_binaries],
    datas=[
        (
            str(project_root / "src" / "alga_vector" / "assets"),
            "alga_vector/assets",
        ),
        *rtlsdr_datas,
        *rtllib_datas,
        *usb_datas,
    ],
    hiddenimports=[
        *serial_hiddenimports,
        *rtlsdr_hiddenimports,
        *rtllib_hiddenimports,
        *usb_hiddenimports,
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Keep development-only packages out of the portable runtime.  Pydantic's
    # optional integration hooks can otherwise make PyInstaller discover mypy
    # merely because the build environment also contains the dev extras.
    excludes=[
        "PyInstaller",
        "matplotlib",
        "mypy",
        "notebook",
        "pytest",
        "ruff",
        "tkinter",
    ],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

gui_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ALGA VECTOR",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(project_root / "packaging" / "version_gui.txt"),
)

cli_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ALGA VECTOR CLI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=str(project_root / "packaging" / "version_cli.txt"),
)

coll = COLLECT(
    gui_exe,
    cli_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ALGA VECTOR",
)
