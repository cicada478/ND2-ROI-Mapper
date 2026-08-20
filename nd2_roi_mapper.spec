# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import site


project_root = Path(SPECPATH)

# Keep the frozen application reproducible and avoid collecting packages from the
# build machine's per-user Python directory. All dependencies come from .build-venv.
site.getusersitepackages = lambda: ""

a = Analysis(
    [str(project_root / "nd2_roi_locator.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[str(project_root / "packaging" / "hooks")],
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
    [],
    exclude_binaries=True,
    name="ND2 ROI Mapper",
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
    version=str(project_root / ".release-staging" / "windows_version_info.txt"),
    contents_directory="_internal",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ND2 ROI Mapper",
)
