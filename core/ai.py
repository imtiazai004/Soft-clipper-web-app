"""Gemini AI integration (new google-genai SDK).

- Viral moment detection from a timestamped transcript (real timings, no guessing)
- Optional custom prompt search ("find the part where ...")
- Visual analysis mode for non-talking videos (music, sports, gameplay) via
  Gemini video understanding on a low-res proxy
- Teaser / highlight-reel planning (multiple segments stitched into one clip)
- Per-clip virality score, hook title, TikTok caption, hashtags
- Audio transcription fallback for videos without captions (any language)
"""
import json
import os
import time

from google import genai
from google.genai import types

from . import llm
from .utils import load_config, seconds_to_mmss, time_to_seconds

# Rolling alias that always points to the latest stable Gemini Flash and stays
# available to new API keys — avoids the "model no longer available to new users"
# 404 that pinned versions (e.g. gemini-2.5-flash) eventually hit.
# Override in config.json with "gemini_model" if you ever need a specific model.
DEFAULT_MODEL = "gemini-flash-latest"


def _model() -> str:
    return load_config().get("gemini_model", "").strip() or DEFAULT_MODEL

MOMENTS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "start": {"type": "string", "description": "MM:SS start time"},
            "end": {"type": "string", "description": "MM:SS end time"},
            "virality_score": {"type": "integer", "description": "0-100 predicted viral potential"},
            "hook_title": {"type": "string", "description": "Catchy short title / on-screen hook"},
            "caption": {"type": "string", "description": "Ready-to-post TikTok caption text"},
            "hashtags": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string", "description": "Why this moment can go viral"},
        },
        "required": ["start", "end", "virality_score", "hook_title", "caption", "hashtags", "reason"],
    },
}

REEL_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "MM:SS start"},
                    "end": {"type": "string", "description": "MM:SS end"},
                },
                "required": ["start", "end"],
            },
        },
        "hook_title": {"type": "string"},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["segments", "hook_title", "caption", "hashtags", "reason"],
}

EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "MM:SS"},
                    "end": {"type": "string", "description": "MM:SS"},
                },
                "required": ["start", "end"],
            },
        },
        "reframe": {"type": "string", "enum": ["smart", "fit", "split", "center", "keep"]},
        "ratio": {"type": "string", "enum": ["9:16", "1:1", "16:9", "original", "keep"]},
        "captions": {"type": "string", "enum": ["on", "off", "keep"]},
        "explanation": {"type": "string", "description": "one short sentence: what was changed"},
    },
    "required": ["segments", "reframe", "ratio", "captions", "explanation"],
}

TRANSCRIPT_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "start": {"type": "number", "description": "segment start time in seconds"},
            "end": {"type": "number", "description": "segment end time in seconds"},
            "text": {"type": "string"},
        },
        "required": ["start", "end", "text"],
    },
}


def _client(api_key: str) -> genai.Client:
    return genai.Client(api_key=api_key)


def _generate_json(client, contents, schema, temperature=0.4):
    # A plain string prompt can go to any provider. Anything else means an
    # uploaded video or audio file, which only Gemini can take, so those calls
    # stay on Gemini whatever the setting says — see transcribe_audio and the
    # visual detectors.
    if isinstance(contents, str) and not llm.is_gemini():
        return llm.generate_json(contents, schema, temperature)

    response = client.models.generate_content(
        model=_model(),
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            temperature=temperature,
        ),
    )
    return json.loads(response.text)


def _upload_and_wait(client, path: str, timeout_s: int = 300, on_status=None):
    """Upload a file to Gemini Files API and wait until it is ACTIVE.

    `on_status` is called with a line to show the person waiting. This stretch is
    the longest silent gap in the whole app — a 40-minute video means a large
    upload and then however long Google takes to process it, during which nothing
    else has anything to report. Without it the progress bar sits still for
    minutes and the honest conclusion is that the app has hung.
    """
    if on_status:
        on_status("Uploading audio to Google...")
    uploaded = client.files.upload(file=path)
    deadline = time.time() + timeout_s
    waited = 0
    while time.time() < deadline:
        f = client.files.get(name=uploaded.name)
        state = str(f.state)
        if "ACTIVE" in state:
            return uploaded
        if "FAILED" in state:
            raise RuntimeError("Gemini file processing failed")
        if on_status:
            on_status(f"Google is processing the audio... ({waited}s)")
        time.sleep(3)
        waited += 3
    raise RuntimeError("Gemini file processing timeout")


def _delete_quiet(client, uploaded):
    try:
        client.files.delete(name=uploaded.name)
    except Exception:
        pass


# What the model is actually selecting for. This used to be one line — "START at
# a strong hook ... END at a natural stopping point" — and the clips it produced
# were defensible but samey: every one ran the minimum length, most started a
# sentence or two before the interesting part, and the scores clustered in the
# high eighties whatever the material was.
#
# Naming the four things that make a short travel, and being explicit about how
# to spend the length budget and the score range, is the whole difference between
# "a segment containing something good" and "a clip". It costs nothing per call —
# it is a fixed prefix on a prompt that already carries an entire transcript.
VIRAL_CRITERIA = """WHAT MAKES A CLIP TRAVEL (select and rank for these, in priority order):
1. A HOOK IN THE FIRST LINE. The opening sentence has to stop a scroll: a bold
   claim, a provocative question, a number nobody expects, a "most people don't
   know", a cliffhanger, or a raw emotional line. START on that line — not on the
   throat-clearing or the setup before it. Weak first three seconds, dead clip,
   no matter how good the middle is.
2. A COMPLETE ARC. It must stand entirely on its own: hook, build, then payoff —
   the answer, punchline, twist or lesson. Someone who never saw the source
   understands all of it. No dangling "as I mentioned earlier", no setup whose
   payoff is outside the clip.
3. HIGH EMOTION OR HIGH VALUE. The best clips do one of: spark emotion (funny,
   inspiring, shocking, infuriating, moving), reveal something counterintuitive,
   deliver a sharp opinion, tell a vivid story, or hand over something genuinely
   useful the viewer can act on.
4. SHAREABILITY. Would someone send this to a friend or argue with it in the
   comments? Does it open a curiosity gap that the payoff then closes?
   Contrarian takes, myth-busting and "wait, what?" moments travel furthest.

AVOID: rambling intros, housekeeping ("smash subscribe", "link in bio"), inside
references that need the rest of the video, and flat mid-thought stretches with
neither a hook nor a payoff."""


def _length_guidance(min_len: int, max_len: int) -> str:
    """How to spend the length budget, in tiers the model can aim at.

    Given only "15-60 seconds", models return 15-second clips almost every time —
    the shortest thing that satisfies the constraint. Real short-form does not
    look like that, so the tiers are described and a mix is asked for explicitly.
    Tiers outside the customer's own min/max are dropped rather than described,
    so this never contradicts the limits they set.
    """
    tiers = [
        (10, 30, "a single self-contained hit — one punchline, one striking claim, "
                 "one sharp exchange — with just enough lead-in to land"),
        (30, 60, "a complete thought — hook, build and payoff: a short story, an "
                 "argument, a back-and-forth"),
        (60, 120, "a multi-beat moment — a longer story or a bigger build-up, where "
                  "cutting it shorter would lose the payoff or the context"),
    ]
    usable = [
        (max(lo, min_len), min(hi, max_len), text)
        for lo, hi, text in tiers
        if hi > min_len and lo < max_len
    ]
    if len(usable) < 2:
        return ""
    lines = "\n".join(f"  - ~{lo}-{hi}s: {text}." for lo, hi, text in usable)
    return f"""
  Pick the length the moment actually needs:
{lines}
  Aim for a mix across these, not a pile of clips at the same length. Do not
  default to the shortest length allowed."""


SCORE_GUIDANCE = """- virality_score (0-100): score honestly and spread the range out — do not rate
  everything 85+. 85-100: exceptional — first-line hook, complete arc, real
  emotion or value, very shareable. 70-84: strong, clear hook and payoff.
  50-69: works, but the hook or the payoff is soft. Below 50: weak — include only
  if the video is thin on material."""


def _moment_rules(num_clips: int, min_len: int, max_len: int, duration: float, user_query: str = "") -> str:
    query_part = ""
    if user_query.strip():
        query_part = f"""
USER REQUEST (highest priority — find moments matching this):
\"{user_query.strip()}\"
Only return moments that match the user's request. If fewer than {num_clips} match, return fewer.
"""
    return f"""{query_part}
{VIRAL_CRITERIA}

Rules:
- Each clip must be {min_len}-{max_len} seconds long.{_length_guidance(min_len, max_len)}
- START on the hook line, let it run while it stays engaging, then END right
  after the payoff, before it drags. Snap the boundaries to transcript line
  edges so no word is cut mid-sentence.
- Clips must NOT overlap each other in time.
- Video is {int(duration)} seconds long; never exceed that.
- Quality over quantity: {num_clips} strong clips beat {num_clips} where half are
  filler. Return fewer if the material does not support more, and spread them
  across the whole video rather than clustering them at the start.
{SCORE_GUIDANCE}
- hook_title: short punchy title (max 8 words) usable as on-screen text, written
  the way a viral Short is titled — curiosity or stakes, not a dry summary
  ("He lost $2M in one trade", not "Discussion about a trade"). Same language as
  the content.
- reason: one sentence naming the specific hook and why it stops the scroll.
- caption: ready-to-post TikTok caption, same language as the content.
- hashtags: 4-6 relevant hashtags (with # prefix, no spaces).
- Return up to {num_clips} moments, best first."""


def detect_viral_moments(
    timestamped_transcript: str,
    video_duration: float,
    api_key: str,
    num_clips: int = 6,
    min_len: int = 15,
    max_len: int = 60,
    user_query: str = "",
) -> list[dict]:
    """Transcript-based detection. Returns validated, clamped moments sorted by score."""
    client = _client(api_key)
    prompt = f"""You are a world-class short-form video editor (TikTok / Reels / Shorts). You comb a
long-form transcript and pull out the handful of moments that would actually stop a
scroll and get shared. Below is a transcript with REAL timestamps in [MM:SS] format.
start/end MUST align with the [MM:SS] timestamps actually present in the transcript —
never invent times that are not near a transcript timestamp.
{_moment_rules(num_clips, min_len, max_len, video_duration, user_query)}

TRANSCRIPT:
{timestamped_transcript}"""

    moments = _generate_json(client, prompt, MOMENTS_SCHEMA)
    return _validate_moments(moments, video_duration, min_len, max_len)


def detect_viral_moments_visual(
    proxy_video_path: str,
    video_duration: float,
    api_key: str,
    num_clips: int = 6,
    min_len: int = 15,
    max_len: int = 60,
    user_query: str = "",
) -> list[dict]:
    """Visual+audio detection by sending the (low-res proxy) video to Gemini.

    Works for non-talking content: music videos, sports, gameplay, vlogs.
    """
    client = _client(api_key)
    uploaded = _upload_and_wait(client, proxy_video_path)
    try:
        prompt = f"""You are a short-form video expert (TikTok / Reels / Shorts). Watch this video
(visuals AND audio) and identify the most viral-worthy moments. The video may have little
or no dialogue — judge by visual action, music drops/beats, emotional expressions,
surprising events, beautiful shots, funny moments, and energy peaks.
Report times as they appear in THIS video.
{_moment_rules(num_clips, min_len, max_len, video_duration, user_query)}"""

        moments = _generate_json(client, [prompt, uploaded], MOMENTS_SCHEMA)
    finally:
        _delete_quiet(client, uploaded)
    return _validate_moments(moments, video_duration, min_len, max_len)


def plan_reel(
    api_key: str,
    video_duration: float,
    mode: str = "highlight",          # "highlight" | "teaser"
    theme: str = "",
    target_duration: int = 45,
    timestamped_transcript: str | None = None,
    proxy_video_path: str | None = None,
) -> dict:
    """Plan a multi-segment stitched clip (teaser or highlight reel).

    Uses transcript if given, else visual analysis on the proxy video.
    Returns {"segments": [{"start_sec", "end_sec"}...], "hook_title", "caption",
    "hashtags", "reason", "total": float}.
    """
    client = _client(api_key)

    if mode == "teaser":
        goal = f"""Create a TEASER of about {target_duration} seconds total: pick 3-6 short punchy
segments (3-10s each) that create curiosity and hype but DO NOT reveal the payoff/ending.
The goal is to make viewers want to watch the full video."""
    else:
        theme_part = f' on the theme: "{theme.strip()}"' if theme.strip() else ""
        goal = f"""Create a HIGHLIGHT REEL of about {target_duration} seconds total{theme_part}:
pick the best 3-8 segments (4-15s each) from different parts of the video that flow well
together as one continuous story."""

    rules = f"""
Rules:
- Video is {int(video_duration)} seconds long; never exceed that.
- Segments must be in chronological order and must NOT overlap.
- Total of all segments should be close to {target_duration} seconds (max {int(target_duration * 1.3)}).
- The FIRST segment must be the strongest hook.
- hook_title: short punchy title (max 8 words), same language as the content.
- caption: ready-to-post TikTok caption, same language as the content.
- hashtags: 4-6 relevant hashtags."""

    if timestamped_transcript:
        prompt = f"""You are a short-form video editor. Below is a transcript with REAL [MM:SS]
timestamps. {goal}
start/end MUST align with timestamps present in the transcript.{rules}

TRANSCRIPT:
{timestamped_transcript}"""
        plan = _generate_json(client, prompt, REEL_SCHEMA)
    elif proxy_video_path:
        uploaded = _upload_and_wait(client, proxy_video_path)
        try:
            prompt = f"""You are a short-form video editor. Watch this video (visuals AND audio).
{goal}
Report times as they appear in THIS video.{rules}"""
            plan = _generate_json(client, [prompt, uploaded], REEL_SCHEMA)
        finally:
            _delete_quiet(client, uploaded)
    else:
        raise ValueError("Either a transcript or a proxy video is required")

    # validate segments
    segments = []
    last_end = 0.0
    for seg in plan.get("segments", []):
        s, e = time_to_seconds(seg.get("start")), time_to_seconds(seg.get("end"))
        if s is None or e is None:
            continue
        s = max(s, last_end)  # enforce order, no overlap
        if video_duration:
            e = min(e, video_duration)
        if e - s < 1.5:
            continue
        segments.append({"start_sec": s, "end_sec": e})
        last_end = e
    if not segments:
        raise RuntimeError("AI returned no valid segments — please try again")

    return {
        "segments": segments,
        "hook_title": plan.get("hook_title", "reel"),
        "caption": plan.get("caption", ""),
        "hashtags": plan.get("hashtags", []),
        "reason": plan.get("reason", ""),
        "total": sum(s["end_sec"] - s["start_sec"] for s in segments),
    }


def edit_clip_plan(
    instruction: str,
    render: dict,
    video_duration: float,
    api_key: str,
    transcript_context: str | None = None,
) -> dict:
    """Turn a natural-language fix request into new render params for a clip.

    Returns {"segments": [{"start_sec", "end_sec"}], "reframe", "ratio",
    "captions", "explanation"} — non-changed values come back as current/"keep".
    """
    client = _client(api_key)
    cur_segs = ", ".join(
        f"{seconds_to_mmss(s['start_sec'])}-{seconds_to_mmss(s['end_sec'])}"
        for s in render["segments"]
    )
    cap_on = render.get("captions", {}).get("enabled", False)

    prompt = f"""You are a video clip editing assistant. The user wants to FIX an existing clip
that was cut from a longer video.

CURRENT CLIP SETTINGS:
- segments (in source video time): {cur_segs}
- aspect ratio: {render.get('ratio') or 'original'}
- reframe mode: {render.get('reframe', 'smart')}
  (smart = face-centered crop, fit = full video on blurred background (nothing cut),
   split = speaker top + 2nd person/scene bottom, center = plain center crop)
- captions: {'on' if cap_on else 'off'}
- full source video duration: {int(video_duration)} seconds

USER INSTRUCTION:
"{instruction.strip()}"

Rules:
- Change ONLY what the user asked for. Everything else: return current values (or "keep").
- Time requests ("start 5 seconds earlier", "make it shorter", "clip should end at the punchline")
  -> adjust segments (MM:SS), always within the source video duration.
- "speaker is cut off / not visible / out of frame" -> prefer reframe "fit" (safest) or "smart".
- "show both people" -> reframe "split".
- explanation: ONE short sentence in the same language as the user's instruction."""

    if transcript_context:
        prompt += f"""

TRANSCRIPT around the clip (real [MM:SS] timestamps in source video time):
{transcript_context}"""

    plan = _generate_json(client, prompt, EDIT_SCHEMA, temperature=0.2)

    segs = []
    for s in plan.get("segments", []):
        st, en = time_to_seconds(s.get("start")), time_to_seconds(s.get("end"))
        if st is None or en is None:
            continue
        if video_duration:
            en = min(en, video_duration)
        st = max(0.0, min(st, en))
        if en - st >= 1.0:
            segs.append({"start_sec": st, "end_sec": en})
    plan["segments"] = segs
    return plan


def _validate_moments(moments: list[dict], duration: float, min_len: int, max_len: int) -> list[dict]:
    valid = []
    for m in moments:
        start = time_to_seconds(m.get("start"))
        end = time_to_seconds(m.get("end"))
        if start is None or end is None:
            continue
        start = max(0.0, min(start, max(0.0, duration - min_len)))
        end = min(end, duration) if duration else end
        if end - start < min_len:
            end = min(start + min_len, duration) if duration else start + min_len
        if end - start > max_len:
            end = start + max_len
        if end <= start:
            continue
        m["start_sec"] = start
        m["end_sec"] = end
        m["virality_score"] = int(m.get("virality_score", 50))
        m.setdefault("hashtags", [])
        valid.append(m)
    valid.sort(key=lambda x: x["virality_score"], reverse=True)
    return valid


def transcribe_audio(audio_path: str, api_key: str, on_status=None) -> list[dict]:
    """Transcribe audio via Gemini Files API. Returns transcript segments
    [{"start": sec, "duration": sec, "text": ...}] or raises on failure.
    Works for Urdu, Pashto, Hindi, English, etc.

    `on_status(message)` is optional and reports what this is doing while it
    does it — upload, then Google's processing, then the transcription itself.
    Optional rather than required because two callers already exist and only one
    of them has a job to write progress into.
    """
    client = _client(api_key)
    uploaded = _upload_and_wait(client, audio_path, on_status=on_status)
    if on_status:
        on_status("Transcribing the audio...")
    try:
        prompt = """Transcribe this audio with accurate timestamps.
Split into short segments of at most 8-10 words each.
Keep the ORIGINAL language of the speech (do not translate).
Return start/end in seconds as numbers."""

        try:
            response = client.models.generate_content(
                model=_model(),
                contents=[prompt, uploaded],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=TRANSCRIPT_SCHEMA,
                    audio_timestamp=True,
                    temperature=0.0,
                ),
            )
        except Exception:
            # audio_timestamp is not supported on every endpoint — retry without it
            response = client.models.generate_content(
                model=_model(),
                contents=[prompt, uploaded],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=TRANSCRIPT_SCHEMA,
                    temperature=0.0,
                ),
            )
        segments = json.loads(response.text)
    finally:
        _delete_quiet(client, uploaded)

    out = []
    for seg in segments:
        try:
            start = float(seg["start"])
            end = float(seg["end"])
            text = str(seg["text"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if text and end > start:
            out.append({"start": start, "duration": end - start, "text": text})
    if not out:
        raise RuntimeError("Transcription returned no usable segments")
    return out
