"""Timestamped transcript fetching.

Primary: YouTube captions via youtube-transcript-api (v1.x instance API).
Fallback: audio extraction + Gemini audio transcription (works for any
language / any yt-dlp-supported platform).

A transcript is always a list of segments:
    [{"start": float_seconds, "duration": float_seconds, "text": str}, ...]
"""
import os

from youtube_transcript_api import YouTubeTranscriptApi

from . import proc
from .utils import seconds_to_mmss

PREFERRED_LANGS = ["en", "ur", "hi", "ps", "ar"]


def fetch_youtube_transcript(video_id: str, proxy: str | None = None) -> list[dict] | None:
    """Fetch captions from YouTube. Returns segments or None if unavailable.

    proxy: route the caption request the same way downloads go. YouTube blocks
    these requests from datacenter IPs too (RequestBlocked), so from a server
    the direct fetch fails for many videos that actually *do* have captions —
    including auto-generated ones — and the caller then falls back to the slow
    Gemini transcription for no reason. Going through the proxy fixes that.
    """
    try:
        if proxy:
            from youtube_transcript_api.proxies import GenericProxyConfig
            ytt = YouTubeTranscriptApi(
                proxy_config=GenericProxyConfig(http_url=proxy, https_url=proxy))
        else:
            ytt = YouTubeTranscriptApi()
        transcript_list = ytt.list(video_id)
        transcript = None
        # manually created captions first, then auto-generated, any language
        try:
            transcript = transcript_list.find_manually_created_transcript(
                PREFERRED_LANGS + [t.language_code for t in transcript_list]
            )
        except Exception:
            try:
                transcript = transcript_list.find_generated_transcript(
                    PREFERRED_LANGS + [t.language_code for t in transcript_list]
                )
            except Exception:
                return None
        fetched = transcript.fetch()
        return [
            {"start": s.start, "duration": s.duration, "text": s.text.replace("\n", " ")}
            for s in fetched
        ]
    except Exception:
        return None


def extract_audio(video_path: str, out_path: str) -> bool:
    """Extract compressed mono audio for cheap upload to Gemini."""
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k",
        out_path,
    ]
    result = proc.run(cmd)
    return result.returncode == 0 and os.path.exists(out_path)


def transcript_to_prompt_text(segments: list[dict]) -> str:
    """Render segments as timestamped lines for the AI prompt."""
    lines = []
    for seg in segments:
        lines.append(f"[{seconds_to_mmss(seg['start'])}] {seg['text']}")
    return "\n".join(lines)


def segments_for_stitched(segments: list[dict], windows: list[dict]) -> list[dict]:
    """Caption segments for a stitched clip: each window [start_sec, end_sec] is
    rebased onto the cumulative output timeline in order."""
    out = []
    offset = 0.0
    for w in windows:
        for seg in segments_between(segments, w["start_sec"], w["end_sec"]):
            out.append({
                "start": seg["start"] + offset,
                "duration": seg["duration"],
                "text": seg["text"],
            })
        offset += w["end_sec"] - w["start_sec"]
    return out


def _srt_time(seconds: float) -> str:
    """SRT wants HH:MM:SS,mmm — comma, not a full stop, and always three digits."""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:            # rounding up a value like 4.9996
        s, ms = s + 1, 0
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(segments: list[dict]) -> str:
    """A subtitle file for the transcript.

    Worth having for a reason beyond completeness: this is the file people upload
    alongside the video. YouTube and TikTok both take an .srt for their own
    captions, which are indexed for search and readable with the sound off —
    neither of which burnt-in captions can do.
    """
    out = []
    for i, seg in enumerate(segments or [], start=1):
        start = float(seg.get("start", 0.0))
        end = start + max(0.2, float(seg.get("duration", 2.0)))
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        out.append(f"{i}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n")
    return "\n".join(out)


def to_txt(segments: list[dict], timestamps: bool = True) -> str:
    """The transcript as prose, optionally with [MM:SS] in front of each line.

    Without timestamps this is what someone pastes into a blog post, a newsletter
    or a description; with them it is how they find the moment they remember
    hearing.
    """
    lines = []
    for seg in segments or []:
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        lines.append(
            f"[{seconds_to_mmss(float(seg.get('start', 0.0)))}] {text}" if timestamps else text
        )
    return "\n".join(lines) + ("\n" if lines else "")


def segments_between(segments: list[dict], start: float, end: float) -> list[dict]:
    """Segments overlapping the [start, end] window, times re-based to clip start."""
    out = []
    for seg in segments:
        seg_start = seg["start"]
        seg_end = seg_start + seg.get("duration", 2.0)
        if seg_end > start and seg_start < end:
            out.append({
                "start": max(0.0, seg_start - start),
                "duration": min(seg_end, end) - max(seg_start, start),
                "text": seg["text"],
            })
    return out
