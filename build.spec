# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Soft Clipper (FastAPI + React + ffmpeg + OpenCV)."""
import os
import shutil

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

# collect tricky packages fully (submodules + data)
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

# extra hidden imports uvicorn resolves dynamically
hiddenimports += [
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "uvicorn.loops.asyncio",
]

# our own packages (obfuscated or plain)
for pkg in ["core", "backend"]:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# OpenCV Haar cascade data (face detection)
import cv2
cv2_data = os.path.join(os.path.dirname(cv2.__file__), "data")
if os.path.isdir(cv2_data):
    datas += [(cv2_data, os.path.join("cv2", "data"))]

# built React frontend
datas += [("frontend/dist", "frontend/dist")]

# The face detector's model. Without it the app falls back to the Haar cascade,
# which is the detector that framed clips on a painting — and it would do it
# silently, in the packaged build only, which is the worst way to find out.
datas += [("core/models", "core/models")]

# bundle ffmpeg + ffprobe into BUNDLE/bin (added to PATH at runtime)
import glob


def _find_tool(name):
    """Locate the REAL ffmpeg/ffprobe binary (not a wrapper/shim)."""
    exe = name + ".exe"
    # 1. explicit dir (CI sets FFMPEG_DIR to a real static build)
    d = os.environ.get("FFMPEG_DIR")
    if d and os.path.isfile(os.path.join(d, exe)):
        return os.path.join(d, exe)
    # 2. on PATH, resolved
    p = shutil.which(name)
    if p:
        rp = os.path.realpath(p)
        # a real ffmpeg is tens of MB; a Chocolatey shim is tiny -> hunt for the real one
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
    ["launcher.py"],
    pathex=[],
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
