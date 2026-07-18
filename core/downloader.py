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


def get_available_qualities(url: str, proxy: str | None = None, cookies_browser: str | None = None,
                            cookies_file: str | None = None) -> list[str]:
    """Return available video heights like ['1080p', '720p', ...] (descending).

    Uses separate video+audio streams (merged by ffmpeg at download time),
    so heights above 720p are included too.
    """
    ydl_opts = {"quiet": True, "no_warnings": True}
    ydl_opts.update(net_opts(proxy, cookies_browser, cookies_file))
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
    heights = set()
    for fmt in info.get("formats", []):
        if fmt.get("vcodec") not in (None, "none") and fmt.get("height"):
            heights.add(fmt["height"])
    return [f"{h}p" for h in sorted(heights, reverse=True)]


def download_video(url: str, quality: str | None = None, progress_hook=None, proxy: str | None = None,
                   cookies_browser: str | None = None, cookies_file: str | None = None):
    """Download best video+audio merged to mp4. quality is like '1080p' or None for best<=1080.

    proxy: optional http/https/socks proxy URL for users whose ISP blocks the source.
    cookies_browser / cookies_file: see net_opts() — needed to get past YouTube 403.
    Returns (path, title, duration_seconds).
    """
    height = int(quality.rstrip("p")) if quality else 1080

    ydl_opts = {
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title).60s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "merge_output_format": "mp4",
        "format": (
            f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={height}]+bestaudio"
            f"/best[height<={height}]/best"
        ),
        "noplaylist": True,
    }
    ydl_opts.update(net_opts(proxy, cookies_browser, cookies_file))
    if progress_hook:
        ydl_opts["progress_hooks"] = [progress_hook]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
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
