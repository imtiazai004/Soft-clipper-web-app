"""TikTok-style caption + headline generation (ASS subtitles for ffmpeg burn-in).

Captions: segments are split into small chunks (2-4 words) shown one at a time,
center-positioned, bold with outline — the standard short-form look.
Word timings inside a segment are approximated by character length.

Headline: one line of text pinned for the whole clip (top or bottom), either
plain outlined text or sitting on a solid box — the "title bar" look.

Both live in a single .ass file with two styles, so ffmpeg burns them in one pass.
"""
import re

# `highlight` is the colour the currently-spoken word turns when word-by-word
# highlighting is on. It is ignored otherwise, so adding it changed nothing for
# the four styles that already shipped.
CAPTION_STYLES = {
    "TikTok Bold": {"colour": "&H00FFFFFF", "outline": "&H00000000", "fontsize": 17, "highlight": "&H0000FFFF"},
    "Clean White": {"colour": "&H00FFFFFF", "outline": "&H00000000", "fontsize": 14, "highlight": "&H0000FF00"},
    "Yellow Pop": {"colour": "&H0000FFFF", "outline": "&H00000000", "fontsize": 17, "highlight": "&H00FFFFFF"},
    "Neon": {"colour": "&H00FFFF00", "outline": "&H00800080", "fontsize": 16, "highlight": "&H00FF00FF"},
    # Added alongside word-by-word highlighting. Bounce pops the spoken word;
    # Boxed is the karaoke look that reads on a busy background.
    "Bounce": {"colour": "&H00FFFFFF", "outline": "&H00000000", "fontsize": 18, "highlight": "&H0000D7FF", "pop": True},
    "Boxed": {"colour": "&H00FFFFFF", "outline": "&H00000000", "fontsize": 16, "highlight": "&H0000FFFF", "box": True},
}

# earlier builds shipped these names; keep saved clip records rendering the same
CAPTION_ALIASES = {
    "Bold White": "TikTok Bold",
    "Green Highlight": "Neon",
}

DEFAULT_CAPTION_STYLE = "TikTok Bold"

# BorderStyle 1 = outlined text, 3 = opaque box drawn in OutlineColour.
HEADLINE_STYLES = {
    "plain": {"border_style": 1, "outline": 2.5, "shadow": 1, "box": "&H00000000"},
    "box": {"border_style": 3, "outline": 8, "shadow": 0, "box": "&H1A000000"},
}

DEFAULT_HEADLINE_STYLE = "box"

# Named colours for text overlays, as ASS &HBBGGRR& (BGR, opaque). A "#rrggbb"
# from the editor is accepted too and converted on the fly.
OVERLAY_COLORS = {
    "white": "&H00FFFFFF",
    "black": "&H00000000",
    "yellow": "&H0000FFFF",
    "red": "&H000000FF",
    "green": "&H0000FF00",
    "blue": "&H00FF3C00",
    "pink": "&H00B469FF",
    "cyan": "&H00FFFF00",
}
DEFAULT_OVERLAY_COLOR = "white"

# ASS uses BGR hex with a leading alpha byte (00 = opaque, FF = transparent).
# PlayRes below keeps font sizes consistent across output resolutions.
ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {play_x}
PlayResY: 288
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,{fontsize},{colour},&H000000FF,{outline},&H80000000,-1,0,0,0,100,100,0,0,{c_border},{c_outline},1,2,10,10,{margin_v},1
Style: Headline,Arial,{h_fontsize},{h_colour},&H000000FF,{h_box},&H80000000,-1,0,0,0,100,100,0,0,{h_border},{h_outline},{h_shadow},{h_align},14,14,{h_margin},1
Style: Overlay,Arial,24,&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,2,1,5,10,10,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


# Average glyph advance as a fraction of the font size. Captions are rendered
# upper-cased, and capitals in bold Arial are appreciably wider than the mixed-
# case average — 0.55 was the first guess and still let "CAN SEE THROUGH" wrap.
_GLYPH_WIDTH = 0.62

# Left and right margins in the Default style.
_SIDE_MARGINS = 20


def line_budget(fontsize: int, play_x: int) -> int:
    """How many characters actually fit on one line.

    Guessing a fixed number was the first attempt and it was wrong: at
    fontsize 17 on a 9:16 canvas only about fifteen characters fit, so a
    twenty-character chunk still wrapped and still ended up two lines tall in
    the middle of the picture. The budget has to come from the font size and
    the canvas width, and then it adjusts itself for every style and ratio.
    """
    usable = max(40, play_x - _SIDE_MARGINS)
    return max(8, int(usable / (_GLYPH_WIDTH * max(8, fontsize))))


def _chunk_words(words: list[str], max_words: int = 3, max_chars: int = 15) -> list[list[str]]:
    """Split into chunks of at most `max_words`, and never wider than a line.

    A chunk that the renderer has to wrap is a chunk that ends up two lines
    tall in the middle of the picture, so the width limit wins over the word
    count. A single word longer than the budget still gets its own chunk —
    there is nothing else to do with it.
    """
    chunks: list[list[str]] = []
    current: list[str] = []
    width = 0

    for word in words:
        added = len(word) + (1 if current else 0)
        too_many = len(current) >= max_words
        too_wide = current and width + added > max_chars
        if too_many or too_wide:
            chunks.append(current)
            current, width = [], 0
            added = len(word)
        current.append(word)
        width += added

    if current:
        chunks.append(current)
    return chunks


# YouTube's auto-captions carry markup meant for a caption track, not for
# burning into a picture: ">>" for a speaker change, bracketed sound events,
# and "NAME:" labels. Left in, they get rendered onto the video as-is.
_CAPTION_NOISE = re.compile(
    r"""
      ^\s*>>+\s*            # >> speaker change, at the start of a line
    | \[[^\]]{0,40}\]       # [Music], [Applause], [inaudible]
    | \([^)]{0,40}\)        # (laughs), (crosstalk)
    | ^\s*[A-Z][A-Z .'-]{1,20}:\s   # SPEAKER NAME:
    """,
    re.VERBOSE,
)


def _clean_caption_text(text: str) -> str:
    return re.sub(r"\s+", " ", _CAPTION_NOISE.sub(" ", str(text or ""))).strip()


def tidy_segments(segments: list[dict]) -> list[dict]:
    """Put caption segments in a state fit to burn into a picture.

    Three things go wrong with real transcripts, and all three were visible in
    a rendered clip:

    * **They overlap.** YouTube's captions roll — the next line starts before
      the previous one ends — so two captions were on screen at once, stacked,
      climbing into the middle of the frame. Each segment is cut off where the
      next one begins.
    * **They repeat.** Rolling captions restate the tail of the previous line.
      A repeated opening is dropped.
    * **They carry markup.** ">>" and "[Music]" were being burned into video.
    """
    cleaned = []
    for seg in segments or []:
        text = _clean_caption_text(seg.get("text", ""))
        if not text:
            continue
        start = float(seg.get("start", 0.0))
        cleaned.append(
            {"start": start, "duration": float(seg.get("duration", 2.0)), "text": text}
        )

    cleaned.sort(key=lambda s: s["start"])

    out: list[dict] = []
    for i, seg in enumerate(cleaned):
        # Drop a repeated opening left over from a rolling caption. Rolling
        # captions restate the whole previous line before adding new words, so
        # the repeat can be any length — take the longest prefix that already
        # appeared, not a fixed window.
        if out:
            previous = out[-1]["text"].lower()
            words = seg["text"].split()
            for take in range(min(len(words), 12), 1, -1):
                if " ".join(words[:take]).lower() in previous:
                    words = words[take:]
                    break
            seg["text"] = " ".join(words)
            if not seg["text"]:
                continue

        end = seg["start"] + max(0.2, seg["duration"])
        if i + 1 < len(cleaned):
            end = min(end, cleaned[i + 1]["start"])
        duration = end - seg["start"]
        # A sliver left after clamping is not readable; give it a floor and let
        # the next caption replace it rather than showing a flicker.
        if duration < 0.25:
            continue
        out.append({"start": seg["start"], "duration": duration, "text": seg["text"]})

    return out


def _ass_text(text: str) -> str:
    """Escape a user string so ASS treats it as plain text, not markup."""
    return (
        str(text).strip()
        .replace("\\", "\\\\")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\r", " ")
        .replace("\n", "\\N")
    )


def _wrap_headline(text: str, max_chars: int = 26) -> str:
    """Break a long headline into balanced lines — one long line looks terrible
    on a 1080-wide vertical video."""
    words = text.split()
    if not words:
        return ""
    lines, current = [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > max_chars and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return "\\N".join(lines[:3])


def caption_style(name: str) -> dict:
    name = CAPTION_ALIASES.get(name, name)
    return CAPTION_STYLES.get(name, CAPTION_STYLES[DEFAULT_CAPTION_STYLE])


def _overlay_color(color: str) -> str:
    """Named colour or '#rrggbb' -> ASS &H00BBGGRR&."""
    color = (color or "").strip()
    if color.startswith("#") and len(color) == 7:
        try:
            r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
            return f"&H00{b:02X}{g:02X}{r:02X}"
        except ValueError:
            pass
    return OVERLAY_COLORS.get(color.lower(), OVERLAY_COLORS[DEFAULT_OVERLAY_COLOR])


def _overlay_lines(overlays: list[dict] | None, clip_duration: float) -> list[str]:
    """One full-clip Dialogue per placed text overlay.

    Position, size and colour vary per overlay, so they ride as inline overrides
    on a single shared Overlay style. \\an5 anchors on the text centre, matching
    the draggable chip's centre in the editor. Layer 2 keeps overlays above both
    captions and the headline.
    """
    if not overlays or clip_duration <= 0:
        return []
    out = []
    for ov in overlays:
        text = _ass_text(ov.get("text", ""))
        if not text:
            continue
        px = int(_clamp01(ov.get("x", 0.5)) * 384)
        py = int(_clamp01(ov.get("y", 0.5)) * 288)
        size = max(8, min(64, int(ov.get("size", 22))))
        colour = _overlay_color(ov.get("color", DEFAULT_OVERLAY_COLOR))
        # inline \c override needs the trailing & to close the colour token
        tags = f"\\an5\\pos({px},{py})\\fs{size}\\c{colour}&\\3c&H000000&\\bord2\\shad1"
        out.append(
            f"Dialogue: 2,{_ass_time(0)},{_ass_time(clip_duration)},Overlay,,0,0,0,,"
            f"{{{tags}}}{text}"
        )
    return out


def _clamp01(v) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.5


def _karaoke_lines(chunk: list[str], start: float, duration: float, st: dict) -> list[str]:
    """One line per word, with the spoken word coloured.

    ASS has a \\k karaoke tag, but how it renders depends on the player and
    libass ignores it for burn-in in the way we want. Repeating the chunk once
    per word with an inline colour override is uglier in the file and exactly
    right on screen — the whole phrase stays readable while the current word
    lights up, which is what makes these captions hold attention.

    Word timings are split by character count. Without word-level timestamps
    from the transcript that is an approximation, but a long word does take
    longer to say, so it tracks speech better than an even split would.
    """
    chars = [max(1, len(w)) for w in chunk]
    total = sum(chars)
    active = st.get("highlight", "&H0000FFFF")
    base = st["colour"]
    pop = st.get("pop")

    lines = []
    t = start
    for i, word in enumerate(chunk):
        word_dur = duration * (chars[i] / total)
        parts = []
        for j, other in enumerate(chunk):
            text = _ass_text(other.upper())
            if j == i:
                scale = "\\fscx112\\fscy112" if pop else ""
                parts.append(f"{{\\c{active}{scale}}}{text}{{\\c{base}\\fscx100\\fscy100}}")
            else:
                parts.append(text)
        lines.append(
            f"Dialogue: 0,{_ass_time(t)},{_ass_time(t + word_dur)},Default,,0,0,0,,"
            f"{{\\an2}}{' '.join(parts)}"
        )
        t += word_dur
    return lines


def build_ass(
    segments: list[dict],
    out_path: str,
    style: str = DEFAULT_CAPTION_STYLE,
    words_per_line: int = 3,
    margin_v: int = 40,
    headline: dict | None = None,
    clip_duration: float = 0.0,
    overlays: list[dict] | None = None,
    highlight: bool = False,
    ratio: str | None = "9:16",
) -> str | None:
    """Write an ASS file from clip-relative caption segments, a headline, overlays.

    headline: {"text", "style": plain|box, "position": top|bottom, "size": int}
    overlays: [{"text", "x", "y", "size", "color"}] — placed text, x/y are 0..1.
    clip_duration: how long the headline / overlays stay on screen (whole clip).
    Returns the path, or None when there is nothing to burn in.
    """
    st = caption_style(style)
    lines = []

    # The caption canvas has to be the same shape as the video. A 4:3 PlayRes
    # over a 9:16 frame stretches every glyph sideways, which is why the text
    # came out fat and only about seventeen characters fitted on a line.
    play_x = {"9:16": 162, "1:1": 288, "16:9": 512}.get(ratio or "9:16", 162)

    # Overlapping, repeated and marked-up segments are not this function's
    # problem to reason about further down — they are fixed once, here.
    segments = tidy_segments(segments)

    # How much text fits on one line at this style's size, on this canvas.
    budget = line_budget(st["fontsize"], play_x)

    for seg in segments or []:
        text = seg["text"].strip()
        if not text:
            continue
        words = text.split()
        if not words:
            continue
        seg_start = seg["start"]
        seg_dur = max(0.5, seg.get("duration", 2.0))
        chunks = _chunk_words(words, words_per_line, budget)
        total_chars = sum(len(w) for w in words) or 1

        t = seg_start
        for chunk in chunks:
            chunk_chars = sum(len(w) for w in chunk)
            chunk_dur = seg_dur * (chunk_chars / total_chars)

            if highlight:
                lines.extend(_karaoke_lines(chunk, t, chunk_dur, st))
            else:
                # \an2 = bottom-center anchor
                chunk_text = _ass_text(" ".join(chunk).upper())
                lines.append(
                    f"Dialogue: 0,{_ass_time(t)},{_ass_time(t + chunk_dur)},Default,,0,0,0,,"
                    f"{{\\an2}}{chunk_text}"
                )
            t += chunk_dur

    head_text = _wrap_headline(_ass_text((headline or {}).get("text", "")))
    if head_text and clip_duration > 0:
        # layer 1 so the headline always wins if a caption chunk overlaps it
        lines.append(
            f"Dialogue: 1,{_ass_time(0)},{_ass_time(clip_duration)},Headline,,0,0,0,,{head_text}"
        )

    lines.extend(_overlay_lines(overlays, clip_duration))

    if not lines:
        return None

    hs = HEADLINE_STYLES.get(
        (headline or {}).get("style"), HEADLINE_STYLES[DEFAULT_HEADLINE_STYLE]
    )
    top = (headline or {}).get("position", "top") != "bottom"
    header = ASS_HEADER.format(
        play_x=play_x,
        fontsize=st["fontsize"], colour=st["colour"], outline=st["outline"],
        # BorderStyle 3 draws an opaque box behind the words instead of an
        # outline around them — the only way captions stay readable over a
        # bright, busy frame.
        c_border=3 if st.get("box") else 1,
        c_outline=6 if st.get("box") else 2,
        margin_v=margin_v,
        h_fontsize=max(8, min(40, int((headline or {}).get("size", 20)))),
        h_colour="&H00FFFFFF",
        h_box=hs["box"],
        h_border=hs["border_style"],
        h_outline=hs["outline"],
        h_shadow=hs["shadow"],
        h_align=8 if top else 2,          # \an8 top-center, \an2 bottom-center
        h_margin=30 if top else margin_v + 55,   # bottom headline sits above captions
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(lines) + "\n")
    return out_path
