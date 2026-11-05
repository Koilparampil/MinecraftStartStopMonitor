# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for a single-file minecraft-monitor executable.
# Build with `uv run poe build-exe` (runs build_exe.py).

import os

ROOT = os.path.abspath(SPECPATH)  # noqa: F821 - provided by PyInstaller


def _data(source, target):
    """(source, target) when the source exists and is not an empty folder."""
    path = os.path.join(ROOT, source)
    if os.path.isdir(path) and not any(files for _, _, files in os.walk(path)):
        return []
    return [(path, target)] if os.path.exists(path) else []


a = Analysis(  # noqa: F821
    [os.path.join(ROOT, "src", "main.py")],
    pathex=[ROOT],  # so `import src.*` resolves
    binaries=[],
    datas=[
        *_data("src/templates", "templates"),
        *_data("src/static", "static"),
        *_data("src/settings.yaml.example", "."),
        *_data("LICENSE", "."),
    ],
    hiddenimports=[
        "flask_limiter",
        "flask_login",
        "flask_wtf",
        "psutil",
        "yaml",
        "src.web_server",  # imported lazily by src.main
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "playwright", "mcstatus"],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="minecraft-monitor",
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
