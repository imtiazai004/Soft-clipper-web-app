"""TikTok-style caption generation (ASS subtitle format for ffmpeg burn-in).

Segments are split into small chunks (2-4 words) shown one at a time,
center-positioned, bold with outline — the standard short-form look.
Word timings inside a segment are approximated by character length.
"""
import os

CAPTION_STYLES = {
    "Bold White": {"colour": "&H00FFFFFF", "outline": "&H00000000", "fontsize": 16},
    "Yellow Pop": {"colour": "&H0000FFFF", "outline": "&H00000000", "fontsize": 16},
    "Green Highlight": {"colour": "&H0000FF00", "outline": "&H00000000", "fontsize": 16},
}

# ASS uses BGR hex. PlayRes below keeps font sizes consistent across resolutions.
ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 384
PlayResY: 288
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,{fontsize},{colour},&H000000FF,{outline},&H80000000,-1,0,0,0,100,100,0,0,1,2,1,2,10,10,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _chunk_words(words: list[str], max_words: int = 3) -> list[list[str]]:
    return [words[i:i + max_words] for i in range(0, len(words), max_words)]


def build_ass(
    segments: list[dict],
    out_path: str,
    style: str = "Bold White",
    words_per_line: int = 3,
    margin_v: int = 40,
) -> str | None:
    """Write an ASS file from clip-relative segments. Returns path or None if no text."""
    st = CAPTION_STYLES.get(style, CAPTION_STYLES["Bold White"])
    lines = []

    for seg in segments:
        text = seg["text"].strip()
        if not text:
            continue
        words = text.split()
        if not words:
            continue
        seg_start = seg["start"]
        seg_dur = max(0.5, seg.get("duration", 2.0))
        chunks = _chunk_words(words, words_per_line)
        total_chars = sum(len(w) for w in words) or 1

        t = seg_start
        for chunk in chunks:
            chunk_chars = sum(len(w) for w in chunk)
            chunk_dur = seg_dur * (chunk_chars / total_chars)
            chunk_text = " ".join(chunk).upper()
            # \an2 = bottom-center anchor
            lines.append(
                f"Dialogue: 0,{_ass_time(t)},{_ass_time(t + chunk_dur)},Default,,0,0,0,,"
                f"{{\\an2}}{chunk_text}"
            )
            t += chunk_dur

    if not lines:
        return None

    header = ASS_HEADER.format(
        fontsize=st["fontsize"], colour=st["colour"], outline=st["outline"], margin_v=margin_v
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(lines) + "\n")
    return out_path
