"""Post-render look effects: mirror, colour adjust, preset looks, speed.

These are cheap ffmpeg filters, but the point of the feature is the *live*
preview in the browser (CSS `filter` / `transform` / `playbackRate`), which costs
the server nothing. This module only bakes the final result on export, so the
filter strings here must stay in step with the CSS approximations in the editor.

The editor's look list and these keys must match — see LOOKS below and the
matching table in the frontend EditModal.
"""

# Each look is a ready-made ffmpeg video-filter chain. "none" adds nothing.
# Kept deliberately mild: a look should flatter most clips, not wreck the odd one.
LOOKS = {
    "none": "",
    "warm": "colorbalance=rs=.12:gs=.02:bs=-.10,eq=saturation=1.08",
    "cold": "colorbalance=rs=-.10:gs=0:bs=.12,eq=saturation=1.05",
    "vintage": "curves=preset=vintage,eq=saturation=.85",
    "bw": "hue=s=0",
    "cinematic": "colorbalance=rs=-.04:bs=.06,eq=contrast=1.10:saturation=.90",
    "vivid": "eq=saturation=1.40:contrast=1.06",
}

# clamp ranges — mirror the slider bounds in the editor
BRIGHTNESS = (-0.5, 0.5)      # ffmpeg eq brightness is additive; 0 = unchanged
CONTRAST = (0.5, 2.0)         # 1 = unchanged
SATURATION = (0.0, 3.0)       # 1 = unchanged
SPEED = (0.5, 2.0)            # 1 = unchanged; atempo takes 0.5..2 in one pass


def _clamp(v, lo, hi):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return lo if lo > 0 else 0.0
    return max(lo, min(v, hi))


def speed_factor(effects: dict | None) -> float:
    return _clamp((effects or {}).get("speed", 1.0), *SPEED)


def active(effects: dict | None) -> bool:
    """True if anything here would change a frame — lets callers skip work."""
    return bool(video_filters(effects) or audio_filter(effects))


def video_filters(effects: dict | None) -> list[str]:
    """Ordered ffmpeg video filters for these effects (may be empty).

    Order matters: flip first, then the preset look, then the user's own colour
    tweak on top of it, then the speed retime last so captions burned afterwards
    line up with the sped-up frames.
    """
    e = effects or {}
    out: list[str] = []

    if e.get("mirror"):
        out.append("hflip")

    look = LOOKS.get(e.get("look", "none"), "")
    if look:
        out.append(look)

    b = _clamp(e.get("brightness", 0.0), *BRIGHTNESS)
    c = _clamp(e.get("contrast", 1.0), *CONTRAST)
    s = _clamp(e.get("saturation", 1.0), *SATURATION)
    if abs(b) > 1e-3 or abs(c - 1) > 1e-3 or abs(s - 1) > 1e-3:
        out.append(f"eq=brightness={b:.3f}:contrast={c:.3f}:saturation={s:.3f}")

    speed = speed_factor(e)
    if abs(speed - 1) > 1e-3:
        out.append(f"setpts={1 / speed:.5f}*PTS")

    return out


def audio_filter(effects: dict | None) -> str | None:
    """Audio tempo change to match a speed effect, or None."""
    speed = speed_factor(effects)
    if abs(speed - 1) <= 1e-3:
        return None
    return f"atempo={speed:.5f}"


def scale_time(seconds: float, effects: dict | None) -> float:
    """Rebase a clip-relative time onto the sped-up output timeline.

    Captions are cut from the transcript in real seconds; once the video is
    retimed by `speed`, a caption at real second t lands at t/speed in the
    output, so the burned times have to be divided to stay in sync.
    """
    return seconds / speed_factor(effects)


def scale_captions(caption_segs: list[dict], effects: dict | None) -> list[dict]:
    """Copy caption segments with their times rebased for the speed effect."""
    speed = speed_factor(effects)
    if abs(speed - 1) <= 1e-3:
        return caption_segs
    return [
        {**s, "start": s["start"] / speed, "duration": s.get("duration", 2.0) / speed}
        for s in caption_segs
    ]
