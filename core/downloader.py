"""Video downloading via yt-dlp. Supports YouTube, TikTok, Facebook, Instagram, etc."""
import os
import re

import yt_dlp

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def get_youtube_id(url: str) -> str | None:
    patterns = [
        r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/|youtube\.com/shorts/|youtube\.com/live/)([^&\n?#/]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def is_youtube(url: str) -> bool:
    return get_youtube_id(url) is not None


def net_opts(proxy: str | None = None, cookies_browser: str | None = None,
             cookies_file: str | None = None) -> dict:
    """yt-dlp options for reaching the site: proxy + cookies.

    Cookies make requests look like a logged-in browser, which is what gets past
    YouTube's "HTTP Error 403 / bot" rejection (common on shared VPN IPs).
    An explicit cookies.txt wins over the browser picker, since it's the fallback
    people reach for when browser extraction fails (e.g. Chrome v127+ encryption).
    """
    opts: dict = {}
    if proxy:
        opts["proxy"] = proxy
    if cookies_file:
        opts["cookiefile"] = cookies_file
    elif cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    return opts


# YouTube does not gate every video the same way: from a datacenter IP some play
# fine while others answer "Sign in to confirm you're not a bot". Which of
# YouTube's player clients yt-dlp pretends to be changes that answer, so rather
# than give up on the first refusal we work down this list. Order matters — the
# default client is tried first and is fastest when it works.
PLAYER_CLIENTS = [c.strip() for c in os.environ.get(
    "YTDLP_PLAYER_CLIENTS", "default,tv_simply,ios,android_vr,mweb,web_embedded"
).split(",") if c.strip()]

_BOT_BLOCK_SIGNS = ("not a bot", "sign in to confirm", "http error 403", "forbidden",
                    "unable to extract", "failed to extract")


def _client_opts(client: str) -> dict:
    """Extractor args pinning yt-dlp to one YouTube player client."""
    if client == "default":
        return {}
    return {"extractor_args": {"youtube": {"player_client": [client]}}}


def _with_client_fallback(url: str, base_opts: dict, run):
    """Run `run(opts)` against each player client until one gets through.

    Only bot-style refusals are retried; a genuinely missing or private video
    fails the same way on every client and should surface at once.
    """
    if not is_youtube(url):
        return run(base_opts)

    last_error: Exception | None = None
    for client in PLAYER_CLIENTS:
        opts = {**base_opts, **_client_opts(client)}
        try:
            return run(opts)
        except Exception as e:
            low = str(e).lower()
            if not any(sign in low for sign in _BOT_BLOCK_SIGNS):
                raise
            last_error = e
    raise last_error if last_error else RuntimeError("Download failed")


def get_available_qualities(url: str, proxy: str | None = None, cookies_browser: str | None = None,
                            cookies_file: str | None = None) -> list[str]:
    """Return available video heights like ['1080p', '720p', ...] (descending).

    Uses separate video+audio streams (merged by ffmpeg at download time),
    so heights above 720p are included too.
    """
    ydl_opts = {"quiet": True, "no_warnings": True}
    ydl_opts.update(net_opts(proxy, cookies_browser, cookies_file))

    def probe(opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    info = _with_client_fallback(url, ydl_opts, probe)
    heights = set()
    for fmt in info.get("formats", []):
        if fmt.get("vcodec") not in (None, "none") and fmt.get("height"):
            heights.add(fmt["height"])
    return [f"{h}p" for h in sorted(heights, reverse=True)]


def download_video(url: str, quality: str | None = None, progress_hook=None, proxy: str | None = None,
                   cookies_browser: str | None = None, cookies_file: str | None = None,
                   out_dir: str | None = None):
    """Download best video+audio merged to mp4. quality is like '1080p' or None for best<=1080.

    proxy: optional http/https/socks proxy URL for users whose ISP blocks the source.
    cookies_browser / cookies_file: see net_opts() — needed to get past YouTube 403.
    out_dir: where to write. The server passes a per-user folder so two people
    downloading at once can't land on each other's files; the desktop app leaves
    it unset and keeps using DOWNLOAD_DIR.
    Returns (path, title, duration_seconds).
    """
    height = int(quality.rstrip("p")) if quality else 1080
    target_dir = out_dir or DOWNLOAD_DIR
    os.makedirs(target_dir, exist_ok=True)

    ydl_opts = {
        "outtmpl": os.path.join(target_dir, "%(title).60s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "format": (
            f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={height}]+bestaudio"
            f"/best[height<={height}]/best"
        ),
        "noplaylist": True,
        # Pull several pieces of the file at once instead of one long straw.
        # A residential proxy throttles any single connection, so on the server
        # this is most of the download slowness people notice; overridable via
        # YTDLP_FRAGMENTS in case a proxy dislikes the parallelism.
        "concurrent_fragment_downloads": int(os.environ.get("YTDLP_FRAGMENTS", "4")),
        "retries": 5,
        "fragment_retries": 5,
    }
    ydl_opts.update(net_opts(proxy, cookies_browser, cookies_file))
    if progress_hook:
        ydl_opts["progress_hooks"] = [progress_hook]

    def fetch(opts):
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise RuntimeError("Could not extract video info")

            filename = ydl.prepare_filename(info)
            base = os.path.splitext(filename)[0]
            path = base + ".mp4"
            if not os.path.exists(path):
                # merge_output_format usually yields .mp4, but fall back to the raw name
                path = filename
            if not os.path.exists(path):
                raise RuntimeError("Downloaded file not found")

            return path, info.get("title", "video"), info.get("duration") or 0

    return _with_client_fallback(url, ydl_opts, fetch)
