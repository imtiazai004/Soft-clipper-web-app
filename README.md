# Soft Clipper

Turn long videos into viral vertical clips for TikTok, Instagram Reels, YouTube
Shorts and Facebook. Soft Clipper finds the best moments in a long video with
AI, reframes them to 9:16 (keeping the speaker in frame), burns in captions, and
exports ready-to-post clips.

Built with a **React (Vite)** frontend and a **FastAPI** backend, packaged as a
single-folder Windows app (no install needed for end users).

## Features

- **AI moment detection** — ranks viral-worthy moments with a virality score
  (transcript-based, or visual analysis for non-talking videos)
- **Prompt search** — "find the funny moments", "every goal", etc.
- **Smart reframing** — dynamic face-tracking crop, fit-on-blur, split-screen, center
- **Auto captions** — TikTok-style burned-in captions in the original language
- **Teasers & highlight reels** — stitch moments from across the video into one clip
- **Edit & fix** — adjust any clip with a prompt or manually, then re-render
- **Captions & hashtags** — generated for every clip

## For end users

Download the latest release, unzip, and run `Soft Clipper.exe`. Add your own
free [Google Gemini API key](https://aistudio.google.com/app/apikey) the first
time. Needs Windows 10/11 — ffmpeg is bundled.

## Development

Requirements: Python 3.12, Node 20+, ffmpeg on PATH.

```bat
install.bat      :: one-time: python env + deps + frontend build
run.bat          :: run from source (opens the browser)
```

Or manually:

```bash
uv venv .venv --python 3.12
uv pip install -r requirements.txt --python .venv/Scripts/python.exe
cd frontend && npm install && npm run build && cd ..
.venv/Scripts/python.exe launcher.py
```

## Build the Windows app

```bash
.venv/Scripts/python.exe build_release.py
```

Produces `release/Soft-Clipper.zip`. Obfuscation (PyArmor) is off by default;
set `OBFUSCATE=1` and register a PyArmor license to enable it.

## Project layout

```
launcher.py          desktop entry point (starts server + opens browser)
backend/main.py      FastAPI app + job API
core/                downloader, transcript, ai, video, captions, reframe, utils
frontend/            React (Vite) UI
build.spec           PyInstaller spec (free build)
build_obf.spec       PyInstaller spec (PyArmor-obfuscated build)
build_release.py     one-command build -> release/Soft-Clipper.zip
.github/workflows/   auto-build & publish on version tag
```
