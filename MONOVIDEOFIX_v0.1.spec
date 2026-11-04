# -*- mode: python ; coding: utf-8 -*-
import os
import shutil
from pathlib import Path

spec_dir = Path(SPECPATH)


def _find_binary(name: str) -> str:
    # 1. Check local project folder or scratch dist folder
    candidates = [
        spec_dir / name,
        Path(r"C:\Users\aronf\.gemini\antigravity\scratch\fix_video_judder\dist\MONOVIDEOFIX_v0.1") / name,
    ]
    for c in candidates:
        if c.is_file():
            return str(c.resolve())
    # 2. Check system PATH (resolving symlinks if any)
    which_path = shutil.which(name)
    if which_path:
        return str(Path(which_path).resolve())
    raise FileNotFoundError(f"Impossibile trovare {name} per il bundle PyInstaller.")


ffmpeg_path = _find_binary("ffmpeg.exe")
ffprobe_path = _find_binary("ffprobe.exe")

a = Analysis(
    ['video_judder_gui.py'],
    pathex=[],
    binaries=[
        (ffmpeg_path, '.'),
        (ffprobe_path, '.'),
    ],
    datas=[
        ('monovideofix.ico', '.'),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'scipy', 'pandas', 'torch', 'torchvision', 'PIL'],
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
    name='MONOVIDEOFIX_v0.1',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['monovideofix.ico'],
)
