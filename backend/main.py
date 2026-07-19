"""FastAPI backend for Soft Clipper.

Serves the built React frontend (frontend/dist) and exposes a job-based API
over the core/ modules.

The app runs in one of two shapes, decided by whether APP_USERS is set:

  * desktop  — one local user, no login, files next to the .exe (the original)
  * server   — several signed-in users who must never see each other's videos,
               clips, jobs or settings; each gets their own directory tree

Everything that used to be module-level single-user state is now keyed by user
id, and every endpoint resolves its caller through auth.current_user.

Run:  .venv\\Scripts\\python.exe -m uvicorn backend.main:app --port 8501
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from backend import auth
from core import ai, captions, downloader, transcript, utils, video

# ── path setup (works both as source and as a bundled PyInstaller exe) ────────
if getattr(sys, "frozen", False):
    # bundled resources (frontend, ffmpeg) live in the unpacked _MEIPASS folder;
    # user data (downloads, clips, config) lives next to the .exe so it persists
    BUNDLE_DIR = sys._MEIPASS
    DATA_DIR = os.path.dirname(sys.executable)
else:
    BUNDLE_DIR = DATA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

os.chdir(DATA_DIR)

# On Render this points at the mounted disk, so clips survive a deploy. Left
# unset it resolves to the app folder, which is what the desktop build wants.
DATA_ROOT = os.environ.get("DATA_ROOT") or DATA_DIR

# make bundled ffmpeg/ffprobe reachable via the plain "ffmpeg" calls in core/
_bin = os.path.join(BUNDLE_DIR, "bin")
if os.path.isdir(_bin):
    os.environ["PATH"] = _bin + os.pathsep + os.environ.get("PATH", "")

app = FastAPI(title="Soft Clipper")

# signs the login cookie; harmless in desktop mode where nothing reads it
app.add_middleware(SessionMiddleware, secret_key=auth.session_secret(),
                   session_cookie="soft_clipper", same_site="lax", https_only=False)

_origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["http://localhost:5173", "http://127.0.0.1:5173"],  # vite dev
    allow_credentials=True,     # the login cookie has to travel with API calls
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── per-user storage ─────────────────────────────────────────────────────────
def user_root(user: str) -> str:
    """Where this user's files live.

    Desktop mode keeps the original flat layout so an existing install still
    finds its downloads and clips. Server mode gives every user their own
    subtree — the first half of keeping them apart, the other half being the
    ownership check in clip_file().
    """
    if not auth.MULTI_USER:
        return DATA_ROOT
    # the user id reaches the filesystem, so allow nothing that could climb out
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", user) or "user"
    return os.path.join(DATA_ROOT, "users", safe)


def user_downloads(user: str) -> str:
    path = os.path.join(user_root(user), "downloads")
    os.makedirs(path, exist_ok=True)
    return path


def user_clips(user: str) -> str:
    path = os.path.join(user_root(user), "clips")
    os.makedirs(path, exist_ok=True)
    return path


# ── session state (one per user) ─────────────────────────────────────────────
def new_session() -> dict:
    return {
        "video_path": None,
        "video_title": None,
        "video_duration": 0.0,
        "video_url": None,
        "transcript_segments": None,
        "transcript_source": None,
        "proxy_path": None,
        "clips": [],  # [{name, path, meta}]
    }


sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()


def get_session(user: str) -> dict:
    with _sessions_lock:
        return sessions.setdefault(user, new_session())


# Settings per user, cached in memory and written through to that user's own
# folder. Keeping them only in memory meant a key had to be re-entered after
# every restart — which on a spun-down free instance is several times a day.
#
# The file holds an API key in plain text. It lives on the server's disk under
# the user's directory, so it is exactly as private as the disk and the Render
# account are; that is a fair trade for a small team, but it is the reason not
# to put a personal key on a shared box you don't control.
user_configs: dict[str, dict] = {}


def _user_config_path(user: str) -> str:
    return os.path.join(user_root(user), "config.json")


def load_user_config(user: str) -> dict:
    if not auth.MULTI_USER:
        return utils.load_config()
    if user not in user_configs:
        try:
            with open(_user_config_path(user), "r", encoding="utf-8") as f:
                user_configs[user] = json.load(f)
        except (OSError, json.JSONDecodeError):
            user_configs[user] = {}
    return dict(user_configs[user])


def save_user_config(user: str, cfg: dict) -> None:
    if not auth.MULTI_USER:
        utils.save_config(cfg)      # desktop: persist next to the .exe
        return
    user_configs[user] = cfg        # server: one user's settings stay their own
    path = _user_config_path(user)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass                        # memory copy still serves this process


# ── job system ────────────────────────────────────────────────────────────────
jobs: dict[str, dict] = {}

# Encoding is the expensive part: each render can eat a core and hundreds of MB,
# so a handful of users hitting "cut" together would OOM the box. Jobs past the
# limit wait their turn instead of all starting at once.
_render_slots = threading.BoundedSemaphore(int(os.environ.get("RENDER_SLOTS", "2")))


def _prune_jobs() -> None:
    """Drop finished jobs after a while so the dict can't grow forever."""
    cutoff = time.time() - 3600
    for jid, j in list(jobs.items()):
        if j["status"] in ("done", "error") and j.get("finished_at", 0) < cutoff:
            jobs.pop(jid, None)


def start_job(user: str, target, *args) -> str:
    job_id = uuid.uuid4().hex[:12]
    jobs[job_id] = {"status": "running", "progress": 0.0, "message": "Starting...",
                    "result": None, "error": None, "user": user}
    _prune_jobs()

    def runner():
        job = jobs[job_id]
        if not _render_slots.acquire(blocking=False):
            job["message"] = "Waiting for a free slot..."
            _render_slots.acquire()
        try:
            result = target(job, *args)
            job.update(status="done", progress=1.0, result=result, message="Done")
        except Exception as e:
            job.update(status="error", error=str(e), message=str(e))
        finally:
            job["finished_at"] = time.time()
            _render_slots.release()

    threading.Thread(target=runner, daemon=True).start()
    return job_id


def owned_job(job_id: str, user: str) -> dict:
    """A job the caller owns. Others are 404, not 403 — a stranger's job id
    should not even confirm it exists."""
    job = jobs.get(job_id)
    if not job or job.get("user") != user:
        raise HTTPException(404, "Job not found")
    return job


# ── helpers ───────────────────────────────────────────────────────────────────
def get_api_key(user: str) -> str:
    """The user's own Gemini key, else the shared one from the environment."""
    key = load_user_config(user).get("gemini_api_key", "") or os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("Gemini API key is not set — add it in Settings")
    return key


def get_proxy(user: str) -> str | None:
    """The proxy downloads go through.

    On the desktop this is the user's own setting — their ISP is what blocks
    YouTube, so the fix belongs to them. On a server the download happens from
    one machine on one IP, so the proxy is the operator's setting and a
    per-user override would only let someone quietly break their own downloads.
    """
    if auth.MULTI_USER:
        return os.environ.get("DOWNLOAD_PROXY", "").strip() or None
    return (load_user_config(user).get("proxy", "").strip()
            or os.environ.get("DOWNLOAD_PROXY", "").strip() or None)


_cookie_copy: dict[str, str] = {}


def writable_cookies(path: str | None) -> str | None:
    """A cookie file yt-dlp is allowed to write back to.

    YouTube rotates cookies, and yt-dlp saves the refreshed jar to the file when
    it finishes. Render mounts secret files read-only, so pointing straight at
    /etc/secrets/cookies.txt turns a working download into a permission error.
    Copy once to somewhere writable and let rotation happen there.
    """
    if not path or not os.path.isfile(path):
        return None
    if os.access(path, os.W_OK):
        return path
    cached = _cookie_copy.get(path)
    if cached and os.path.isfile(cached):
        return cached
    dest = os.path.join(DATA_ROOT, "_cookies.txt")
    try:
        os.makedirs(DATA_ROOT, exist_ok=True)
        shutil.copyfile(path, dest)
    except OSError:
        return path      # can't copy — better to try the original than nothing
    _cookie_copy[path] = dest
    return dest


def get_cookies(user: str) -> dict:
    """Optional cookie source, as kwargs for the downloader.

    Sending a logged-in browser's cookies is what gets past YouTube's 403 /
    bot rejection, which anonymous requests hit on shared VPN and ISP IPs.
    """
    cfg = load_user_config(user)
    if auth.MULTI_USER:
        # server-side concern, same as the proxy: there is no browser to read
        # cookies from and no way for a user to put a file on the box
        return {"cookies_browser": None,
                "cookies_file": writable_cookies(os.environ.get("COOKIES_FILE", "").strip() or None)}
    return {
        "cookies_browser": cfg.get("cookies_browser", "").strip() or None,
        "cookies_file": writable_cookies(cfg.get("cookies_file", "").strip() or None),
    }


# The site answered but refused us — usually anti-bot rejection of an anonymous
# request from a shared VPN/ISP IP. Cookies from a logged-in browser fix this.
_BOT_BLOCK_SIGNS = (
    "http error 403", " 403", "forbidden", "sign in to confirm", "not a bot",
    "confirm your age", "login required", "private video", "unable to extract",
)

# We couldn't reach the site at all — ISP block, DNS, or a dead proxy.
_UNREACHABLE_SIGNS = (
    "getaddrinfo", "failed to resolve", "name or service", "connection", "timed out",
    "timeout", "network", "unable to connect", "tunnel connection", "ssl", "urlopen",
    "not available in your",
)


def download_error_message(e: Exception, user: str) -> str:
    """Turn a raw yt-dlp error into a user-friendly, actionable message.

    The two failure modes need opposite fixes, so keep them apart: a VPN gets you
    *to* YouTube, but cookies are what stop YouTube refusing you once you're there.
    """
    raw = str(e)
    low = raw.lower()
    cookies = get_cookies(user)
    has_cookies = bool(cookies["cookies_browser"] or cookies["cookies_file"])

    if any(sign in low for sign in _BOT_BLOCK_SIGNS):
        if has_cookies:
            return (
                "YouTube refused this download (403). Your cookies may be stale or from a "
                "browser that isn't signed in — sign in to YouTube in that browser, or export "
                "a fresh cookies.txt, then try again.\n\nDetails: " + raw
            )
        if auth.MULTI_USER:
            return (
                "YouTube refused this download (403) — it treated the server as a bot. "
                "Fix: set a download proxy (DOWNLOAD_PROXY) or supply a cookies.txt "
                "(COOKIES_FILE) on the server.\n\nDetails: " + raw
            )
        return (
            "YouTube refused this download (403). This usually means it treated the request "
            "as a bot — common on shared VPN IPs. Fix: in Settings, set 'Cookies from browser' "
            "to a browser where you're signed in to YouTube (or point to a cookies.txt file). "
            "That makes the download look like your normal browser.\n\nDetails: " + raw
        )

    if any(sign in low for sign in _UNREACHABLE_SIGNS):
        if get_proxy(user):
            return (
                "Couldn't reach the video. Your proxy may not be working — check or change "
                "it in Settings.\n\nDetails: " + raw
            )
        return (
            "Couldn't reach the video. Your ISP may be blocking the site. Try a VPN, or set "
            "a proxy in Settings.\n\nDetails: " + raw
        )

    return raw


def ensure_transcript(job: dict, user: str) -> list[dict]:
    sess = get_session(user)
    if sess["transcript_segments"]:
        return sess["transcript_segments"]
    if not sess["video_path"]:
        raise RuntimeError("Download a video first")

    # 1) YouTube captions
    if sess["video_url"] and downloader.is_youtube(sess["video_url"]):
        job["message"] = "Fetching YouTube captions..."
        vid = downloader.get_youtube_id(sess["video_url"])
        segs = transcript.fetch_youtube_transcript(vid)
        if segs:
            sess.update(transcript_segments=segs, transcript_source="youtube")
            return segs

    # 2) Gemini audio transcription
    key = get_api_key(user)
    job["message"] = "Extracting audio..."
    audio_path = os.path.join(user_downloads(user), "_audio_temp.mp3")
    if not transcript.extract_audio(sess["video_path"], audio_path):
        raise RuntimeError("Audio extraction failed")
    try:
        job["message"] = "Transcribing with Gemini AI..."
        segs = ai.transcribe_audio(audio_path, key)
        sess.update(transcript_segments=segs, transcript_source="gemini")
        return segs
    finally:
        if os.path.exists(audio_path):
            os.remove(audio_path)


def ensure_proxy(job: dict, user: str) -> str:
    sess = get_session(user)
    if sess["proxy_path"] and os.path.exists(sess["proxy_path"]):
        return sess["proxy_path"]
    job["message"] = "Creating low-res proxy for AI..."
    proxy = os.path.join(user_downloads(user), "_proxy_temp.mp4")
    ok, err = video.make_proxy(sess["video_path"], proxy)
    if not ok:
        raise RuntimeError(f"Proxy error: {err}")
    sess["proxy_path"] = proxy
    return proxy


def clip_output_dir(user: str) -> str:
    sess = get_session(user)
    out = os.path.join(user_clips(user),
                       f"{utils.sanitize(sess['video_title'] or 'video')[:30]}_{int(time.time())}")
    os.makedirs(out, exist_ok=True)
    return out


def clip_to_api(user: str, c: dict) -> dict:
    rel = os.path.relpath(c["path"], user_clips(user)).replace("\\", "/")
    size = os.path.getsize(c["path"]) if os.path.exists(c["path"]) else 0
    return {
        "name": c["name"], "url": f"/api/clips/file/{rel}", "size_mb": round(size / 1e6, 1),
        "meta": c.get("meta", {}), "render": c.get("render"),
    }


def render_record(job: dict, user: str, rec: dict, out_dir: str) -> None:
    """Render a clip record (single or stitched) according to rec['render']."""
    sess = get_session(user)
    r = rec["render"]
    segs = r["segments"]
    cap = r.get("captions", {})
    ass_file = None
    if cap.get("enabled") and sess["transcript_segments"]:
        if len(segs) == 1:
            cs = transcript.segments_between(
                sess["transcript_segments"], segs[0]["start_sec"], segs[0]["end_sec"])
        else:
            cs = transcript.segments_for_stitched(sess["transcript_segments"], segs)
        if cs:
            ass_file = captions.build_ass(
                cs, os.path.join(out_dir, "_cap_tmp.ass"),
                style=cap.get("style", "TikTok Bold"),
                words_per_line=cap.get("words_per_line", 4),
            )
    try:
        if len(segs) == 1:
            ok, err = video.render_clip(
                sess["video_path"], rec["path"], segs[0]["start_sec"], segs[0]["end_sec"],
                ratio=r.get("ratio"), ass_file=ass_file, reframe=r.get("reframe", "smart"),
            )
        else:
            ok, err = video.render_stitched_clip(
                sess["video_path"], rec["path"], segs,
                ratio=r.get("ratio"), ass_file=ass_file, work_dir=out_dir,
                reframe=r.get("reframe", "smart"),
            )
    finally:
        if ass_file and os.path.exists(ass_file):
            os.remove(ass_file)
    if not ok:
        raise RuntimeError(f"{rec['name']}: {err}")


def rerender_clip(job: dict, user: str, rec: dict) -> None:
    """Re-render an edited clip into a new versioned file, remove the old one."""
    out_dir = os.path.dirname(rec["path"])
    old_path = rec["path"]
    rec["version"] = rec.get("version", 1) + 1
    base = utils.sanitize(rec["name"])[:60] or "clip"
    rec["path"] = os.path.join(out_dir, f"{base}_v{rec['version']}.mp4")
    job["message"] = f"Re-rendering: {rec['name']}"
    render_record(job, user, rec, out_dir)
    if old_path != rec["path"] and os.path.exists(old_path):
        try:
            os.remove(old_path)
        except OSError:
            pass


# ── request models ────────────────────────────────────────────────────────────
class LoginBody(BaseModel):
    username: str
    password: str


class ConfigBody(BaseModel):
    # all optional so each setting can be updated independently
    api_key: str | None = None
    proxy: str | None = None
    cookies_browser: str | None = None
    cookies_file: str | None = None


class DownloadBody(BaseModel):
    url: str
    quality: str | None = None


class DetectBody(BaseModel):
    mode: str = "transcript"       # transcript | visual
    query: str = ""
    num_clips: int = 6
    min_len: int = 15
    max_len: int = 60


class CaptionOpts(BaseModel):
    enabled: bool = True
    style: str = "TikTok Bold"
    words_per_line: int = 4


class CutBody(BaseModel):
    clips: list[dict]              # [{name, start_sec, end_sec, meta?}]
    ratio: str | None = "9:16"
    reframe: str = "smart"         # smart | fit | split | center
    captions: CaptionOpts = CaptionOpts()


class ReelBody(BaseModel):
    mode: str = "teaser"           # teaser | highlight
    analysis: str = "transcript"   # transcript | visual
    theme: str = ""
    target_duration: int = 45
    ratio: str | None = "9:16"
    reframe: str = "smart"         # smart | fit | split | center
    captions: CaptionOpts = CaptionOpts()


# ── auth endpoints ────────────────────────────────────────────────────────────
@app.get("/api/me")
def me(request: Request):
    """Who am I — the frontend asks this first to decide whether to show login."""
    if not auth.MULTI_USER:
        return {"multi_user": False, "user": auth.LOCAL_USER}
    user = request.session.get("user")
    return {"multi_user": True, "user": user if user in auth.USERS else None}


@app.post("/api/login")
def login(body: LoginBody, request: Request):
    if not auth.MULTI_USER:
        return {"ok": True, "user": auth.LOCAL_USER}
    if not auth.check_password(body.username.strip(), body.password):
        raise HTTPException(401, "Wrong username or password")
    request.session["user"] = body.username.strip()
    return {"ok": True, "user": body.username.strip()}


@app.post("/api/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


# ── config endpoints ──────────────────────────────────────────────────────────
@app.get("/api/config")
def get_config(user: str = Depends(auth.current_user)):
    cfg = load_user_config(user)
    key = cfg.get("gemini_api_key", "")
    shared_key = bool(os.environ.get("GEMINI_API_KEY"))
    return {
        "has_key": bool(key) or shared_key,
        "key_preview": f"...{key[-4:]}" if key else ("shared" if shared_key else None),
        # not secret — sent back so the user can see/edit the values in Settings
        "proxy": cfg.get("proxy", ""),
        "cookies_browser": cfg.get("cookies_browser", ""),
        "cookies_file": cfg.get("cookies_file", ""),
        # true only in the packaged .exe — the "Add to Desktop" button needs a real exe
        "packaged": bool(getattr(sys, "frozen", False)),
        # so Settings can tell the truth about where a saved key ends up
        "multi_user": auth.MULTI_USER,
    }


@app.post("/api/config")
def set_config(body: ConfigBody, user: str = Depends(auth.current_user)):
    cfg = load_user_config(user)
    # update only the fields the client actually sent
    if body.api_key is not None:
        cfg["gemini_api_key"] = body.api_key.strip()
    if body.proxy is not None:
        cfg["proxy"] = body.proxy.strip()
    if body.cookies_browser is not None:
        cfg["cookies_browser"] = body.cookies_browser.strip()
    if body.cookies_file is not None:
        cfg["cookies_file"] = body.cookies_file.strip()
    save_user_config(user, cfg)
    return {"ok": True}


@app.post("/api/create-shortcut")
def create_shortcut(user: str = Depends(auth.current_user)):
    """Create a 'Soft Clipper' shortcut on the user's Desktop (packaged app only)."""
    if not getattr(sys, "frozen", False):
        raise HTTPException(400, "Desktop shortcuts can only be created from the installed app")
    exe = sys.executable                 # the Soft Clipper.exe bootloader
    exe_dir = os.path.dirname(exe)
    ps = (
        '$ws = New-Object -ComObject WScript.Shell; '
        '$desktop = $ws.SpecialFolders("Desktop"); '
        '$lnk = Join-Path $desktop "Soft Clipper.lnk"; '
        '$s = $ws.CreateShortcut($lnk); '
        f'$s.TargetPath = "{exe}"; '
        f'$s.WorkingDirectory = "{exe_dir}"; '
        f'$s.IconLocation = "{exe},0"; '
        '$s.Description = "Soft Clipper - AI Video Clipper"; '
        '$s.Save(); '
        'Write-Output $lnk'
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        raise HTTPException(400, f"Couldn't create the shortcut: {e}")
    if result.returncode != 0:
        raise HTTPException(400, f"Couldn't create the shortcut: {(result.stderr or '')[-200:]}")
    return {"ok": True, "path": result.stdout.strip()}


# ── video endpoints ───────────────────────────────────────────────────────────
@app.get("/api/qualities")
def qualities(url: str, user: str = Depends(auth.current_user)):
    try:
        return {"qualities": downloader.get_available_qualities(
            url, proxy=get_proxy(user), **get_cookies(user))}
    except Exception as e:
        raise HTTPException(400, download_error_message(e, user))


@app.post("/api/jobs/download")
def job_download(body: DownloadBody, user: str = Depends(auth.current_user)):
    def work(job):
        def hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes", 0)
                if total:
                    job["progress"] = min(0.95, done / total)
                job["message"] = f"Downloading... {int(job['progress']*100)}%"
            elif d.get("status") == "finished":
                job["progress"] = 0.97
                job["message"] = "Merging / processing..."

        job["message"] = "Fetching video info..."
        try:
            path, title, duration = downloader.download_video(
                body.url.strip(), body.quality, progress_hook=hook,
                proxy=get_proxy(user), out_dir=user_downloads(user), **get_cookies(user))
        except Exception as e:
            raise RuntimeError(download_error_message(e, user))
        if not duration:
            duration = video.get_video_info(path)["duration"]
        get_session(user).update(
            video_path=path, video_title=title, video_duration=duration,
            video_url=body.url.strip(), transcript_segments=None,
            transcript_source=None, proxy_path=None, clips=[],
        )
        return {"title": title, "duration": duration, "video_url": "/api/video/stream"}

    return {"job_id": start_job(user, work)}


def load_local_video(user: str, path: str) -> dict:
    """Point the user's session at a video file already on disk."""
    try:
        info = video.get_video_info(path)
        duration = info.get("duration") or 0
    except Exception:
        duration = 0
    if not duration:
        raise HTTPException(400, "Couldn't read that file — is it a video?")
    sess = get_session(user)
    sess.update(
        video_path=path,
        video_title=os.path.splitext(os.path.basename(path))[0],
        video_duration=duration,
        video_url=None,          # local file: no captions to fetch, AI transcribes the audio
        transcript_segments=None,
        transcript_source=None,
        proxy_path=None,
        clips=[],
    )
    return {"title": sess["video_title"], "duration": duration, "video_url": "/api/video/stream"}


@app.post("/api/video/upload")
async def upload_video(file: UploadFile = File(...), user: str = Depends(auth.current_user)):
    """Take a video the user picked or dropped in the browser.

    A native file dialog would avoid this copy, but Windows won't let a
    background process raise a window over the browser, so the dialog opened
    behind it and looked like a hang. The browser's own picker always works.
    """
    name = os.path.basename(file.filename or "video")
    ext = os.path.splitext(name)[1].lower() or ".mp4"
    dest = os.path.join(user_downloads(user),
                        f"{utils.sanitize(os.path.splitext(name)[0])[:60]}{ext}")
    try:
        # stream to disk in chunks — a multi-GB video must never be held in memory
        with open(dest, "wb") as out:
            while chunk := await file.read(4 * 1024 * 1024):
                out.write(chunk)
    except Exception as e:
        if os.path.exists(dest):
            os.remove(dest)
        raise HTTPException(400, f"Couldn't save the upload: {e}")
    finally:
        await file.close()

    try:
        return load_local_video(user, dest)
    except HTTPException:
        if os.path.exists(dest):     # not a video — don't leave the junk behind
            os.remove(dest)
        raise


@app.get("/api/video")
def video_state(user: str = Depends(auth.current_user)):
    sess = get_session(user)
    if not sess["video_path"] or not os.path.exists(sess["video_path"]):
        return {"loaded": False}
    return {
        "loaded": True,
        "title": sess["video_title"],
        "duration": sess["video_duration"],
        "url": sess["video_url"],
        "stream_url": "/api/video/stream",
        "transcript_source": sess["transcript_source"],
    }


@app.get("/api/video/stream")
def video_stream(user: str = Depends(auth.current_user)):
    sess = get_session(user)
    if not sess["video_path"] or not os.path.exists(sess["video_path"]):
        raise HTTPException(404, "No video loaded")
    return FileResponse(sess["video_path"], media_type="video/mp4")


# ── AI endpoints ──────────────────────────────────────────────────────────────
@app.post("/api/jobs/detect")
def job_detect(body: DetectBody, user: str = Depends(auth.current_user)):
    sess = get_session(user)
    if not sess["video_path"]:
        raise HTTPException(400, "Download a video first")

    def work(job):
        key = get_api_key(user)
        if body.mode == "visual":
            proxy = ensure_proxy(job, user)
            job["message"] = "AI is watching the video (this can take a while)..."
            job["progress"] = 0.4
            moments = ai.detect_viral_moments_visual(
                proxy, sess["video_duration"], key,
                num_clips=body.num_clips, min_len=body.min_len, max_len=body.max_len,
                user_query=body.query,
            )
        else:
            ensure_transcript(job, user)
            job["message"] = "AI is finding viral moments..."
            job["progress"] = 0.5
            moments = ai.detect_viral_moments(
                transcript.transcript_to_prompt_text(sess["transcript_segments"]),
                sess["video_duration"], key,
                num_clips=body.num_clips, min_len=body.min_len, max_len=body.max_len,
                user_query=body.query,
            )
        return {"moments": moments, "transcript_source": sess["transcript_source"]}

    return {"job_id": start_job(user, work)}


@app.post("/api/jobs/cut")
def job_cut(body: CutBody, user: str = Depends(auth.current_user)):
    sess = get_session(user)
    if not sess["video_path"]:
        raise HTTPException(400, "Download a video first")

    def work(job):
        out_dir = clip_output_dir(user)
        produced = []
        n = len(body.clips)
        for i, c in enumerate(body.clips):
            name = utils.sanitize(c.get("name") or f"clip_{i+1}")[:60]
            job["message"] = f"Rendering clip: {name} ({i+1}/{n})"
            job["progress"] = i / max(n, 1)
            rec = {
                "name": name,
                "path": os.path.join(out_dir, f"{name}.mp4"),
                "meta": c.get("meta", {}),
                "version": 1,
                "render": {
                    "segments": [{"start_sec": float(c["start_sec"]), "end_sec": float(c["end_sec"])}],
                    "ratio": body.ratio,
                    "reframe": body.reframe,
                    "captions": body.captions.model_dump(),
                },
            }
            render_record(job, user, rec, out_dir)
            produced.append(rec)

        sess["clips"] = produced
        return {"clips": [clip_to_api(user, c) for c in produced]}

    return {"job_id": start_job(user, work)}


@app.post("/api/jobs/reel")
def job_reel(body: ReelBody, user: str = Depends(auth.current_user)):
    sess = get_session(user)
    if not sess["video_path"]:
        raise HTTPException(400, "Download a video first")

    def work(job):
        key = get_api_key(user)
        if body.analysis == "visual":
            proxy = ensure_proxy(job, user)
            job["message"] = "AI is watching the video and planning..."
            job["progress"] = 0.3
            plan = ai.plan_reel(
                key, sess["video_duration"], mode=body.mode, theme=body.theme,
                target_duration=body.target_duration, proxy_video_path=proxy,
            )
        else:
            ensure_transcript(job, user)
            job["message"] = "AI is planning the reel..."
            job["progress"] = 0.3
            plan = ai.plan_reel(
                key, sess["video_duration"], mode=body.mode, theme=body.theme,
                target_duration=body.target_duration,
                timestamped_transcript=transcript.transcript_to_prompt_text(sess["transcript_segments"]),
            )

        out_dir = clip_output_dir(user)
        name = utils.sanitize(plan["hook_title"])[:50] or body.mode
        job["message"] = f"Cutting & stitching {len(plan['segments'])} segments..."
        job["progress"] = 0.6
        rec = {
            "name": name,
            "path": os.path.join(out_dir, f"{name}.mp4"),
            "meta": plan,
            "version": 1,
            "render": {
                "segments": plan["segments"],
                "ratio": body.ratio,
                "reframe": body.reframe,
                "captions": body.captions.model_dump(),
            },
        }
        render_record(job, user, rec, out_dir)

        sess["clips"] = [rec]
        return {"clips": [clip_to_api(user, rec)], "plan": plan}

    return {"job_id": start_job(user, work)}


# ── clip editing ──────────────────────────────────────────────────────────────
class EditBody(BaseModel):
    index: int
    name: str
    segments: list[dict]          # [{start_sec, end_sec}]
    ratio: str | None = "9:16"
    reframe: str = "smart"
    captions: CaptionOpts = CaptionOpts()


class AiEditBody(BaseModel):
    index: int
    instruction: str


def _get_clip(user: str, index: int) -> dict:
    clips = get_session(user)["clips"]
    if index < 0 or index >= len(clips):
        raise HTTPException(400, "Clip not found")
    return clips[index]


@app.post("/api/jobs/edit")
def job_edit(body: EditBody, user: str = Depends(auth.current_user)):
    sess = get_session(user)
    rec = _get_clip(user, body.index)

    def work(job):
        segs = []
        for s in body.segments:
            st, en = float(s["start_sec"]), float(s["end_sec"])
            if sess["video_duration"]:
                en = min(en, sess["video_duration"])
                st = max(0.0, min(st, en))
            if en - st >= 1.0:
                segs.append({"start_sec": st, "end_sec": en})
        if not segs:
            raise RuntimeError("No valid segments — check start/end times")

        rec["name"] = utils.sanitize(body.name)[:60] or rec["name"]
        rec["render"] = {
            "segments": segs,
            "ratio": body.ratio,
            "reframe": body.reframe,
            "captions": body.captions.model_dump(),
        }
        rerender_clip(job, user, rec)
        return {"clips": [clip_to_api(user, c) for c in sess["clips"]]}

    return {"job_id": start_job(user, work)}


@app.post("/api/jobs/ai_edit")
def job_ai_edit(body: AiEditBody, user: str = Depends(auth.current_user)):
    sess = get_session(user)
    rec = _get_clip(user, body.index)
    if not body.instruction.strip():
        raise HTTPException(400, "Instruction is empty")

    def work(job):
        key = get_api_key(user)
        job["message"] = "AI is reading your instruction..."
        job["progress"] = 0.2

        context = None
        if sess["transcript_segments"]:
            segs = rec["render"]["segments"]
            lo = max(0.0, min(s["start_sec"] for s in segs) - 90)
            hi = max(s["end_sec"] for s in segs) + 90
            window = [s for s in sess["transcript_segments"] if lo <= s["start"] <= hi]
            if window:
                context = transcript.transcript_to_prompt_text(window)

        plan = ai.edit_clip_plan(
            body.instruction, rec["render"], sess["video_duration"], key,
            transcript_context=context,
        )

        r = rec["render"]
        if plan.get("segments"):
            r["segments"] = plan["segments"]
        if plan.get("reframe") and plan["reframe"] != "keep":
            r["reframe"] = plan["reframe"]
        if plan.get("ratio") and plan["ratio"] != "keep":
            r["ratio"] = None if plan["ratio"] == "original" else plan["ratio"]
        if plan.get("captions") in ("on", "off"):
            r["captions"]["enabled"] = plan["captions"] == "on"

        job["progress"] = 0.5
        rerender_clip(job, user, rec)
        return {
            "clips": [clip_to_api(user, c) for c in sess["clips"]],
            "explanation": plan.get("explanation", ""),
        }

    return {"job_id": start_job(user, work)}


# ── jobs & clips ──────────────────────────────────────────────────────────────
@app.get("/api/jobs/{job_id}")
def job_status(job_id: str, user: str = Depends(auth.current_user)):
    return owned_job(job_id, user)


@app.get("/api/clips")
def list_clips(user: str = Depends(auth.current_user)):
    return {"clips": [clip_to_api(user, c) for c in get_session(user)["clips"]
                      if os.path.exists(c["path"])]}


@app.get("/api/clips/zip")
def clips_zip(user: str = Depends(auth.current_user)):
    existing = [c for c in get_session(user)["clips"] if os.path.exists(c["path"])]
    if not existing:
        raise HTTPException(404, "No clips generated yet")
    zip_path = os.path.join(user_clips(user), "_all_clips.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for c in existing:
            zf.write(c["path"], arcname=os.path.basename(c["path"]))
    return FileResponse(zip_path, media_type="application/zip", filename="clips.zip")


@app.get("/api/clips/file/{rel_path:path}")
def clip_file(rel_path: str, user: str = Depends(auth.current_user)):
    """Serve a clip, but only to the user who owns it.

    This replaced a plain StaticFiles mount on the clips folder. That mount
    served any path to anyone, so one user could read another's clips by
    guessing the URL — which is exactly what separate dashboards must prevent.
    """
    root = os.path.realpath(user_clips(user))
    full = os.path.realpath(os.path.join(root, rel_path))
    # realpath first, then check containment: this also stops ../ and symlinks
    if full != root and not full.startswith(root + os.sep):
        raise HTTPException(404, "Not found")
    if not os.path.isfile(full):
        raise HTTPException(404, "Not found")
    return FileResponse(full)


# ── static serving ────────────────────────────────────────────────────────────
DIST = os.path.join(BUNDLE_DIR, "frontend", "dist")
if os.path.isdir(DIST):
    app.mount("/", StaticFiles(directory=DIST, html=True), name="frontend")
