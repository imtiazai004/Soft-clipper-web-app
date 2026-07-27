"""FFmpeg video processing.

Single-pass pipeline: accurate cut + reframe + caption burn-in in one re-encode.

Reframe modes:
  center — plain center crop (old behaviour)
  smart  — face-aware crop: crop window centers on the dominant speaker
  fit    — no crop: full video centered on a blurred background
  split  — stacked layout: speaker panel on top, second face / scene below
  manual — user-placed crop window (cx, cy, zoom) from the frame editor

Every ffmpeg call goes through proc.run so a cancelled job kills it mid-encode.
"""
import json
import os
import subprocess

from . import effects as fx
from . import proc
from . import reframe as face_reframe

CLIPS_DIR = "clips"
os.makedirs(CLIPS_DIR, exist_ok=True)

# target output sizes per ratio
TARGETS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080)}

# center-crop then scale — NEVER stretch
RATIO_FILTERS = {
    "9:16": "crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',scale=1080:1920",
    "1:1": "crop='min(iw,ih)':'min(iw,ih)',scale=1080:1080",
    "16:9": "crop='min(iw,ih*16/9)':'min(ih,iw*9/16)',scale=1920:1080",
}

ENCODE = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
    "-c:a", "aac", "-b:a", "128k",
    "-movflags", "+faststart",
]


def get_video_info(path: str) -> dict:
    """Return {'duration': float, 'width': int, 'height': int}."""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    info = json.loads(result.stdout)
    video_stream = next(
        (s for s in info.get("streams", []) if s.get("codec_type") == "video"), {}
    )
    return {
        "duration": float(info.get("format", {}).get("duration", 0)),
        "width": int(video_stream.get("width", 0)),
        "height": int(video_stream.get("height", 0)),
    }


def make_proxy(input_file: str, output_file: str, height: int = 360) -> tuple[bool, str | None]:
    """Low-res proxy for cheap/fast upload to Gemini (visual analysis)."""
    cmd = [
        "ffmpeg", "-y", "-i", input_file,
        "-vf", f"scale=-2:{height}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "32",
        "-c:a", "aac", "-b:a", "64k", "-ac", "1",
        "-movflags", "+faststart",
        output_file,
    ]
    result = proc.run(cmd)
    if result.returncode != 0:
        return False, (result.stderr or "")[-300:]
    return True, None


def _clamp(v, lo, hi):
    return max(lo, min(v, hi))


def _even(v: int) -> int:
    """h264 needs even dimensions."""
    return max(2, int(v) // 2 * 2)


def _ass_escape(ass_file: str) -> str:
    return ass_file.replace("\\", "/").replace(":", "\\:")


def _smart_crop_filter(iw: int, ih: int, ratio: str, faces: list[dict]) -> str | None:
    """Static face-centered crop filter. None -> caller falls back to center.

    The face is placed at the exact horizontal center and at 42% height
    (slight headroom) whenever the crop window allows it."""
    tw, th = TARGETS[ratio]
    target_ar = tw / th
    cw = min(iw, int(ih * target_ar))
    ch = min(ih, int(cw / target_ar))
    if cw >= iw and ch >= ih:
        return f"scale={tw}:{th}:force_original_aspect_ratio=decrease,pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2"
    cx = faces[0]["cx"] if faces else 0.5
    cy = faces[0]["cy"] if faces else 0.42
    x = _clamp(int(cx * iw - cw / 2), 0, iw - cw)
    y = _clamp(int(cy * ih - 0.42 * ch), 0, ih - ch)
    return f"crop={cw}:{ch}:{x}:{y},scale={tw}:{th}"


def _manual_crop_filter(iw: int, ih: int, ratio: str, crop: dict | None) -> str:
    """User-placed crop window from the frame editor.

    crop = {"cx", "cy", "zoom"} — cx/cy are the window's center as 0..1 of the
    source frame, zoom 1.0 is the widest window this ratio allows (>1 = tighter).
    """
    tw, th = TARGETS[ratio]
    target_ar = tw / th
    crop = crop or {}

    def num(key: str, default: float) -> float:
        # `or default` would be wrong here: 0.0 is a valid edge position
        value = crop.get(key)
        return default if value is None else float(value)

    zoom = _clamp(num("zoom", 1.0), 1.0, 4.0)

    cw = min(iw, ih * target_ar) / zoom
    ch = cw / target_ar
    if ch > ih:                      # never ask for more height than we have
        ch = ih
        cw = ch * target_ar
    cw, ch = _even(cw), _even(ch)

    cx = _clamp(num("cx", 0.5), 0.0, 1.0)
    cy = _clamp(num("cy", 0.5), 0.0, 1.0)
    x = _clamp(int(cx * iw - cw / 2), 0, max(0, iw - cw))
    y = _clamp(int(cy * ih - ch / 2), 0, max(0, ih - ch))
    return f"crop={cw}:{ch}:{x}:{y},scale={tw}:{th}"


def _tail(chain: str, vfx: list[str], ass_file: str | None) -> str:
    """Append effect filters, then caption/headline burn-in, to a chain ending [out].

    Effects come before the ass burn so a mirror flips the video but not the
    (mirrored-would-be-unreadable) captions, and a speed retime shifts the frames
    the captions are then drawn over.
    """
    steps = list(vfx)
    if ass_file:
        steps.append(f"ass='{_ass_escape(ass_file)}'")
    if not steps:
        return chain
    return chain.replace("[out]", "[fx]") + ";[fx]" + ",".join(steps) + "[out]"


def _fit_filter_complex(ratio: str, ass_file: str | None, vfx: list[str]) -> str:
    """Full video over blurred background, no cropping."""
    tw, th = TARGETS[ratio]
    chain = (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={tw}:{th}:force_original_aspect_ratio=increase,"
        f"crop={tw}:{th},gblur=sigma=24,eq=brightness=-0.08[bgb];"
        f"[fg]scale={tw}:{th}:force_original_aspect_ratio=decrease[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2[out]"
    )
    return _tail(chain, vfx, ass_file)


def _split_filter_complex(iw: int, ih: int, faces: list[dict], ass_file: str | None,
                          vfx: list[str]) -> str | None:
    """Stacked 9:16 layout — NEVER shows the same person twice.

    - 2+ clearly separate faces  -> speaker panel top, 2nd person panel bottom
    - 1 face, clearly off-center -> speaker top, opposite side of the scene bottom
    - otherwise                  -> None (caller falls back to smart crop)

    Each panel is 1080x960 (aspect 9:8)."""
    panel_ar = 1080 / 960  # 1.125
    pw = min(iw, int(ih * panel_ar))
    ph = min(ih, int(pw / panel_ar))

    def panel_xy(cx, cy):
        x = _clamp(int(cx * iw - pw / 2), 0, iw - pw)
        y = _clamp(int(cy * ih - ph / 2), 0, ih - ph)
        return x, y

    two_people = (
        len(faces) >= 2
        and abs(faces[0]["cx"] - faces[1]["cx"]) >= 0.20   # genuinely different positions
        and faces[1]["weight"] >= 0.20                      # 2nd person seen often enough
    )

    if two_people:
        x1, y1 = panel_xy(faces[0]["cx"], faces[0]["cy"])
        x2, y2 = panel_xy(faces[1]["cx"], faces[1]["cy"])
    elif faces and abs(faces[0]["cx"] - 0.5) >= 0.18:
        # single speaker on one side: top = speaker, bottom = the OTHER side of the scene
        x1, y1 = panel_xy(faces[0]["cx"], faces[0]["cy"])
        scene_cx = 0.78 if faces[0]["cx"] < 0.5 else 0.22
        x2, y2 = panel_xy(scene_cx, 0.5)
    else:
        return None  # talking-head in the middle -> split adds nothing; use smart crop

    chain = (
        f"[0:v]split=2[a][b];"
        f"[a]crop={pw}:{ph}:{x1}:{y1},scale=1080:960[top];"
        f"[b]crop={pw}:{ph}:{x2}:{y2},scale=1080:960[bot];"
        f"[top][bot]vstack[out]"
    )
    return _tail(chain, vfx, ass_file)


# Where a streamer's camera box usually sits. The corner is a choice in the UI
# because it is the one thing that varies between setups; the size barely does.
FACECAM_CORNERS = {
    "bottom-left": {"cx": 0.135, "cy": 0.79},
    "bottom-right": {"cx": 0.865, "cy": 0.79},
    "top-left": {"cx": 0.135, "cy": 0.21},
    "top-right": {"cx": 0.865, "cy": 0.21},
}
DEFAULT_FACECAM = "bottom-left"
FACECAM_W, FACECAM_H = 0.25, 0.30      # share of the source frame


def _gamecam_filter_complex(iw: int, ih: int, facecam: dict | None, ass_file: str | None,
                            vfx: list[str]) -> str | None:
    """Gameplay below, the streamer's camera above — the layout every gaming
    clip uses.

    The gameplay panel is *fitted*, never cropped: cropping a game to 9:16
    throws away the half of the screen where everything happens. The camera
    panel is cropped out of the corner the user points at, since a facecam is a
    small box in a fixed place rather than something to detect.
    """
    if iw <= 0 or ih <= 0:
        return None

    corner = FACECAM_CORNERS.get((facecam or {}).get("corner"), FACECAM_CORNERS[DEFAULT_FACECAM])
    cx = _clamp(float((facecam or {}).get("cx", corner["cx"])), 0.0, 1.0)
    cy = _clamp(float((facecam or {}).get("cy", corner["cy"])), 0.0, 1.0)

    cw = _even(min(iw, iw * FACECAM_W))
    ch = _even(min(ih, ih * FACECAM_H))
    x = _clamp(int(cx * iw - cw / 2), 0, max(0, iw - cw))
    y = _clamp(int(cy * ih - ch / 2), 0, max(0, ih - ch))

    # 600 + 1320 = 1920. The camera gets under a third: it is context, and the
    # game is what people came to watch.
    cam_h, game_h = 600, 1320

    chain = (
        f"[0:v]split=2[cam][game];"
        f"[cam]crop={cw}:{ch}:{x}:{y},scale=1080:{cam_h}[camout];"
        f"[game]scale=1080:{game_h}:force_original_aspect_ratio=decrease,"
        f"pad=1080:{game_h}:(ow-iw)/2:(oh-ih)/2:black[gameout];"
        f"[camout][gameout]vstack[out]"
    )
    return _tail(chain, vfx, ass_file)


def render_clip(
    input_file: str,
    output_file: str,
    start: float,
    end: float,
    ratio: str | None = None,
    ass_file: str | None = None,
    reframe: str = "center",
    crop: dict | None = None,
    effects: dict | None = None,
    facecam: dict | None = None,
) -> tuple[bool, str | None]:
    """Cut [start, end], reframe + look effects + burn captions. One ffmpeg pass.

    Returns (ok, error_message).
    """
    duration = end - start
    if duration <= 0:
        return False, "End time is before start time"

    if ass_file and not os.path.exists(ass_file):
        ass_file = None

    vfx = fx.video_filters(effects)          # mirror / look / colour / speed
    afx = fx.audio_filter(effects)           # atempo, when speed != 1

    # -t goes BEFORE -i so it trims the SOURCE window, not the output. With a
    # speed effect the two differ: as an output limit ffmpeg would keep pulling
    # source until the (retimed) output hit `duration`, dragging in footage from
    # past the intended end. Trimming the input first cuts [start, start+duration]
    # of the source, then setpts retimes that to duration/speed.
    base = ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", input_file]
    filter_complex = None
    vf_filters = []

    if ratio and ratio in TARGETS:
        if reframe == "fit":
            filter_complex = _fit_filter_complex(ratio, ass_file, vfx)
        elif reframe == "manual":
            info = get_video_info(input_file)
            iw, ih = info["width"], info["height"]
            if iw <= 0 or ih <= 0:
                vf_filters.append(RATIO_FILTERS[ratio])
            else:
                vf_filters.append(_manual_crop_filter(iw, ih, ratio, crop))
        elif reframe == "gamecam":
            info = get_video_info(input_file)
            iw, ih = info["width"], info["height"]
            filter_complex = (
                _gamecam_filter_complex(iw, ih, facecam, ass_file, vfx)
                if ratio == "9:16" else None
            )
            if filter_complex is None:
                # Only 9:16 has room for two stacked panels; anything else falls
                # back rather than producing a squashed frame.
                vf_filters.append(RATIO_FILTERS[ratio])
        elif reframe in ("smart", "split"):
            info = get_video_info(input_file)
            iw, ih = info["width"], info["height"]
            if iw <= 0 or ih <= 0:
                vf_filters.append(RATIO_FILTERS[ratio])
            else:
                faces = face_reframe.analyze(input_file, start, end)
                if reframe == "split" and ratio == "9:16":
                    filter_complex = _split_filter_complex(iw, ih, faces, ass_file, vfx)
                    if filter_complex is None:
                        # single centered speaker (or no faces): split would duplicate
                        # the person, so use face-centered smart crop instead
                        vf_filters.append(_smart_crop_filter(iw, ih, ratio, faces))
                else:  # smart (or split on non-9:16 ratios)
                    # the multi-window "follow the speaker" path can't also carry
                    # effects cleanly, so only take it when there are no effects
                    windows = face_reframe.crop_timeline(input_file, start, end)
                    if len(windows) > 1 and not vfx and not afx:
                        return _render_dynamic_smart(
                            input_file, output_file, start, windows,
                            iw, ih, ratio, ass_file,
                        )
                    wf = windows[0]["face"]
                    vf_filters.append(_smart_crop_filter(iw, ih, ratio, [wf] if wf else faces))
        else:  # center
            vf_filters.append(RATIO_FILTERS[ratio])

    if filter_complex:
        # fit/split already folded vfx + ass into the chain via _tail
        audio_map = ["-map", "0:a?"]
        if afx:
            filter_complex += f";[0:a]{afx}[aout]"
            audio_map = ["-map", "[aout]?"]
        cmd = base + [
            "-filter_complex", filter_complex, "-map", "[out]",
        ] + audio_map + ENCODE + [output_file]
    else:
        # crop filter (if any) -> effects -> captions, all on the plain -vf chain
        vf_filters.extend(vfx)
        if ass_file:
            vf_filters.append(f"ass='{_ass_escape(ass_file)}'")
        cmd = base + (["-vf", ",".join(vf_filters)] if vf_filters else [])
        cmd += (["-af", afx] if afx else []) + ENCODE + [output_file]

    result = proc.run(cmd)
    if result.returncode != 0:
        return False, (result.stderr or "")[-300:]
    return True, None


def _render_dynamic_smart(
    input_file: str,
    output_file: str,
    clip_start: float,
    windows: list[dict],
    iw: int,
    ih: int,
    ratio: str,
    ass_file: str | None,
) -> tuple[bool, str | None]:
    """Render a clip whose smart crop FOLLOWS the speaker: each window is cut
    with its own face-centered crop, parts are concatenated, captions burned
    over the full stitched timeline."""
    work_dir = os.path.dirname(output_file) or "."
    part_files = []
    try:
        for i, w in enumerate(windows):
            part = os.path.join(work_dir, f"_dyn_{i}.mp4")
            vf = _smart_crop_filter(iw, ih, ratio, [w["face"]] if w["face"] else [])
            abs_start = clip_start + w["start"]
            dur = w["end"] - w["start"]
            cmd = [
                "ffmpeg", "-y", "-ss", f"{abs_start:.3f}", "-i", input_file,
                "-t", f"{dur:.3f}", "-vf", vf,
            ] + ENCODE + [part]
            result = proc.run(cmd)
            if result.returncode != 0:
                return False, (result.stderr or "")[-300:]
            part_files.append(part)

        concat_list = os.path.join(work_dir, "_dyn_concat.txt")
        with open(concat_list, "w", encoding="utf-8") as f:
            for p in part_files:
                f.write(f"file '{os.path.abspath(p)}'\n")
        part_files.append(concat_list)

        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list]
        if ass_file and os.path.exists(ass_file):
            cmd += [
                "-vf", f"ass='{_ass_escape(ass_file)}'",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "128k",
            ]
        else:
            cmd += ["-c", "copy"]
        cmd += ["-movflags", "+faststart", output_file]
        result = proc.run(cmd)
        if result.returncode != 0:
            return False, (result.stderr or "")[-300:]
        return True, None
    finally:
        for p in part_files:
            if os.path.exists(p):
                os.remove(p)


def render_stitched_clip(
    input_file: str,
    output_file: str,
    segments: list[dict],
    ratio: str | None = None,
    ass_file: str | None = None,
    work_dir: str | None = None,
    reframe: str = "center",
    crop: dict | None = None,
    effects: dict | None = None,
    facecam: dict | None = None,
) -> tuple[bool, str | None]:
    """Stitch multiple [start_sec, end_sec] segments into ONE clip (teaser/highlight reel).

    Each segment is rendered with identical encoding params (each with its own
    face analysis), then joined with the concat demuxer. Captions are burned in
    a final pass over the stitched timeline.
    """
    if not segments:
        return False, "No segments to stitch"

    work_dir = work_dir or os.path.dirname(output_file) or "."
    part_files = []
    try:
        for i, seg in enumerate(segments):
            part = os.path.join(work_dir, f"_part_{i}.mp4")
            ok, err = render_clip(
                input_file, part, seg["start_sec"], seg["end_sec"],
                ratio=ratio, reframe=reframe, crop=crop, effects=effects, facecam=facecam,
            )
            if not ok:
                return False, f"Segment {i+1}: {err}"
            part_files.append(part)

        concat_list = os.path.join(work_dir, "_concat.txt")
        with open(concat_list, "w", encoding="utf-8") as f:
            for p in part_files:
                f.write(f"file '{os.path.abspath(p)}'\n")
        part_files.append(concat_list)

        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list]
        if ass_file and os.path.exists(ass_file):
            cmd += [
                "-vf", f"ass='{_ass_escape(ass_file)}'",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "128k",
            ]
        else:
            cmd += ["-c", "copy"]
        cmd += ["-movflags", "+faststart", output_file]

        result = proc.run(cmd)
        if result.returncode != 0:
            return False, (result.stderr or "")[-300:]
        return True, None
    finally:
        for p in part_files:
            if os.path.exists(p):
                os.remove(p)
