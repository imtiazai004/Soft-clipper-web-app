# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec building the PyArmor-OBFUSCATED app (from build_obf/)."""
import os
import shutil
import sys

from PyInstaller.utils.hooks import collect_all

# obfuscated sources take priority so collect_all/imports resolve to them
OBF = os.path.abspath("build_obf")
sys.path.insert(0, OBF)

datas = []
binaries = []
hiddenimports = []

for pkg in [
    "uvicorn", "fastapi", "starlette", "pydantic", "pydantic_core",
    "google.genai", "yt_dlp", "youtube_transcript_api",
    "anyio", "h11", "click", "websockets", "httptools", "watchfiles",
    "multipart",   # python-multipart: FastAPI imports it lazily for uploads
]:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

hiddenimports += [
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "uvicorn.loops.asyncio",
]

# obfuscated packages + the pyarmor runtime (from build_obf, first on sys.path)
for pkg in ["core", "backend", "pyarmor_runtime_000000"]:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

import cv2
cv2_data = os.path.join(os.path.dirname(cv2.__file__), "data")
if os.path.isdir(cv2_data):
    datas += [(cv2_data, os.path.join("cv2", "data"))]

datas += [("frontend/dist", "frontend/dist")]

import glob


def _find_tool(name):
    """Locate the REAL ffmpeg/ffprobe binary (not a wrapper/shim)."""
    exe = name + ".exe"
    d = os.environ.get("FFMPEG_DIR")
    if d and os.path.isfile(os.path.join(d, exe)):
        return os.path.join(d, exe)
    p = shutil.which(name)
    if p:
        rp = os.path.realpath(p)
        if os.path.getsize(rp) < 2_000_000:
            for cand in glob.glob(rf"C:\ProgramData\chocolatey\lib\**\{exe}", recursive=True):
                if os.path.getsize(cand) > 2_000_000:
                    return cand
        return rp
    return None


for tool in ("ffmpeg", "ffprobe"):
    p = _find_tool(tool)
    if p:
        datas += [(p, "bin")]
    else:
        raise SystemExit(f"Could not find a real {tool} binary to bundle")

a = Analysis(
    [os.path.join(OBF, "launcher.py")],
    pathex=[OBF],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "PyQt5", "PySide2"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Soft Clipper",
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
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Soft Clipper",
)
