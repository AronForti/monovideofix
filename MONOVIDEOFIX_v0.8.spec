# -*- mode: python ; coding: utf-8 -*-
import os
import shutil
import zipfile
from pathlib import Path

spec_dir = Path(SPECPATH)


def _find_binary(name: str) -> str:
    candidates = [
        spec_dir / name,
        Path(r"C:\Users\aronf\.gemini\antigravity\scratch\fix_video_judder\dist\MONOVIDEOFIX_v0.1") / name,
    ]
    for c in candidates:
        if c.is_file():
            return str(c.resolve())
    which_path = shutil.which(name)
    if which_path:
        return str(Path(which_path).resolve())
    raise FileNotFoundError(f"Impossibile trovare {name} per il bundle PyInstaller.")


ffmpeg_path = _find_binary("ffmpeg.exe")
ffprobe_path = _find_binary("ffprobe.exe")

a = Analysis(
    ['video_judder_gui.py'],
    pathex=[],
    binaries=[],
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
    name='MONOVIDEOFIX_v0.8',
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

# Post-build: appende lo ZIP con ffmpeg.exe e ffprobe.exe in coda a MONOVIDEOFIX_v0.8.exe
built_exe = Path(DISTPATH) / "MONOVIDEOFIX_v0.8.exe"
if built_exe.is_file():
    print(f"[MONOVIDEOFIX] Incorporamento on-demand di ffmpeg.exe e ffprobe.exe in {built_exe.name}...")
    with zipfile.ZipFile(built_exe, mode="a", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.write(ffmpeg_path, arcname="ffmpeg.exe")
        zf.write(ffprobe_path, arcname="ffprobe.exe")
    print(f"[MONOVIDEOFIX] Completato! Dimensione finale: {built_exe.stat().st_size / (1024*1024):.2f} MB")
