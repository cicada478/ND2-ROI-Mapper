# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import os
import site
import sys


project_root = Path(SPECPATH)
app_version = os.environ.get("ND2_ROI_MAPPER_VERSION", "1.1.0")
is_windows = sys.platform == "win32"

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
    version=(
        str(project_root / ".release-staging" / "windows_version_info.txt")
        if is_windows
        else None
    ),
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

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="ND2 ROI Mapper.app",
        icon=None,
        bundle_identifier="io.github.cicada478.nd2-roi-mapper",
        version=app_version,
        info_plist={
            "CFBundleDisplayName": "ND2 ROI Mapper",
            "CFBundleName": "ND2 ROI Mapper",
            "CFBundleShortVersionString": app_version,
            "CFBundleVersion": app_version,
            "NSHighResolutionCapable": True,
            "NSPrincipalClass": "NSApplication",
        },
    )
