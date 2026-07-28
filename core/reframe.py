"""Face-aware reframing: find where speakers are so crops keep them in frame.

Detection is YuNet, a small DNN that ships with the app as a 230 KB model, with
OpenCV's Haar cascade as the fallback if that file is ever missing. Haar was
the original choice and it was the wrong one: on a dimly lit podcast it drew a
box around the *painting* behind the speaker, and because the crop follows the
largest face, clips came back framing the artwork instead of the person.

Frames are sampled across the clip window; detections are clustered by
horizontal position so we get stable "person regions" instead of jittery
per-frame boxes.

All returned coordinates are normalized (0..1) relative to source frame size.
"""
import glob
import os
import tempfile

import cv2
import numpy as np

from . import proc

_CASCADE = None
_YUNET = None
_YUNET_SIZE = None

# Ships with the app: about 230 KB, and the difference between "found a face"
# and "found the right face".
_YUNET_MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models",
                            "face_detection_yunet_2023mar.onnx")

# YuNet returns a confidence per detection. Below this, discard — the value was
# picked by watching what it puts a box around in a dimly lit podcast: the
# painting on the wall behind the speaker scores around 0.7, the speaker 0.9+.
MIN_CONFIDENCE = 0.80

# A face smaller than this share of the frame height is background: someone in
# a photograph, a face in a painting, a person at the far end of a room. The
# subject of a talking-head shot is never this small.
MIN_FACE_HEIGHT = 0.10


def _cascade():
    global _CASCADE
    if _CASCADE is None:
        _CASCADE = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
    return _CASCADE


def _yunet(width: int, height: int):
    """The DNN face detector, sized to the frame it is about to look at.

    Haar was the original choice because it ships with OpenCV and needs no
    model file. Watching it work on real footage is what settled the argument:
    on a dim podcast set it drew a box around the *painting* on the wall, and
    that box was larger than the speaker's face — so the crop, which follows
    the largest face, framed the artwork. It also missed the speaker entirely
    on shots where YuNet was confident.

    Returns None when the model file is missing, and the caller falls back to
    Haar rather than failing.
    """
    global _YUNET, _YUNET_SIZE
    if not os.path.exists(_YUNET_MODEL):
        return None
    try:
        if _YUNET is None:
            _YUNET = cv2.FaceDetectorYN.create(
                _YUNET_MODEL, "", (width, height), MIN_CONFIDENCE, 0.3, 5000
            )
            _YUNET_SIZE = (width, height)
        elif _YUNET_SIZE != (width, height):
            _YUNET.setInputSize((width, height))
            _YUNET_SIZE = (width, height)
    except Exception:
        return None
    return _YUNET


def _detect(frame) -> list[dict]:
    """Faces in one frame as normalized {cx, cy, w, h, score}, best first.

    "Best" is size first, then confidence: in a talking-head shot the person
    being filmed is the nearest and therefore the biggest face, and everything
    smaller is furniture, artwork or someone in the background.
    """
    sh, sw = frame.shape[:2]
    found: list[dict] = []

    detector = _yunet(sw, sh)
    if detector is not None:
        try:
            _, faces = detector.detect(frame)
        except Exception:
            faces = None
        for f in faces if faces is not None else []:
            x, y, w, h, score = float(f[0]), float(f[1]), float(f[2]), float(f[3]), float(f[-1])
            if score < MIN_CONFIDENCE or h / sh < MIN_FACE_HEIGHT:
                continue
            found.append({
                "cx": (x + w / 2) / sw, "cy": (y + h / 2) / sh,
                "w": w / sw, "h": h / sh, "score": score,
            })
    else:
        gray = cv2.equalizeHist(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        for (x, y, w, h) in _cascade().detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=6, minSize=(28, 28)
        ):
            if h / sh < MIN_FACE_HEIGHT:
                continue
            found.append({
                "cx": (x + w / 2) / sw, "cy": (y + h / 2) / sh,
                "w": w / sw, "h": h / sh, "score": 0.5,
            })

    found.sort(key=lambda d: (d["h"], d["score"]), reverse=True)
    return found


def sample_faces(video_path: str, start: float, end: float, max_samples: int = 32) -> list[dict]:
    """Detect faces on sampled frames. Returns [{cx, cy, w, h}] normalized.

    Frames are extracted in a single ffmpeg pass (much faster than seeking
    through the video with OpenCV)."""
    duration = max(0.5, end - start)
    fps = max_samples / duration
    detections = []
    with tempfile.TemporaryDirectory() as td:
        pattern = os.path.join(td, "f_%03d.jpg")
        cmd = [
            "ffmpeg", "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
            "-i", video_path,
            "-vf", f"fps={fps:.4f},scale=480:-2",
            "-frames:v", str(max_samples), "-q:v", "5", pattern,
        ]
        proc.run(cmd)
        for fp in sorted(glob.glob(os.path.join(td, "f_*.jpg"))):
            frame = cv2.imread(fp)
            if frame is None:
                continue
            detections.extend(_detect(frame))
    return detections


def cluster_faces(detections: list[dict], min_share: float = 0.15) -> list[dict]:
    """Cluster detections by horizontal position (1D). Returns clusters sorted by
    prominence (frequency x face size), each {cx, cy, w, h, weight, score}.
    Clusters seen in less than min_share of detections are dropped (noise).

    Merge threshold is 16% of frame width so the same person moving a little
    does not get split into two 'people'."""
    if not detections:
        return []
    dets = sorted(detections, key=lambda d: d["cx"])
    clusters = []
    current = [dets[0]]
    for d in dets[1:]:
        # compare against the cluster's running median, not the last point,
        # so a slowly moving person stays one cluster
        med = float(np.median([x["cx"] for x in current]))
        if abs(d["cx"] - med) <= 0.16:
            current.append(d)
        else:
            clusters.append(current)
            current = [d]
    clusters.append(current)

    total = len(dets)
    out = []
    for c in clusters:
        weight = len(c) / total
        if weight < min_share:
            continue
        size = float(np.median([d["h"] for d in c]))
        out.append({
            "cx": float(np.median([d["cx"] for d in c])),
            "cy": float(np.median([d["cy"] for d in c])),
            "w": float(np.median([d["w"] for d in c])),
            "h": size,
            "weight": weight,
            # prominence: how often seen x how big (big face = close to camera = speaker)
            "score": weight * size,
        })
    out.sort(key=lambda c: c["score"], reverse=True)
    return out


def analyze(video_path: str, start: float, end: float) -> list[dict]:
    """Sample + cluster. Returns face regions sorted by prominence (may be empty)."""
    return cluster_faces(sample_faces(video_path, start, end))


def sample_faces_timed(video_path: str, start: float, end: float, max_samples: int = 32) -> list[dict]:
    """Like sample_faces but keeps WHEN each frame was sampled.

    Returns [{"t": clip_relative_seconds, "faces": [{cx, cy, w, h}, ...]}, ...]
    (one entry per sampled frame, possibly with an empty faces list)."""
    duration = max(0.5, end - start)
    fps = max_samples / duration
    frames = []
    with tempfile.TemporaryDirectory() as td:
        pattern = os.path.join(td, "f_%03d.jpg")
        cmd = [
            "ffmpeg", "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
            "-i", video_path,
            "-vf", f"fps={fps:.4f},scale=480:-2",
            "-frames:v", str(max_samples), "-q:v", "5", pattern,
        ]
        proc.run(cmd)
        for idx, fp in enumerate(sorted(glob.glob(os.path.join(td, "f_*.jpg")))):
            frame = cv2.imread(fp)
            if frame is None:
                continue
            t = (idx + 0.5) / fps  # clip-relative time of this sample
            frames.append({"t": t, "faces": _detect(frame)})
    return frames


def crop_timeline(video_path: str, start: float, end: float,
                  jump: float = 0.15, min_window: float = 1.5,
                  frames: list[dict] | None = None) -> list[dict]:
    """Build a piecewise crop plan so the crop FOLLOWS the speaker across
    scene changes / position jumps.

    Returns [{"start": clip_rel_s, "end": clip_rel_s, "face": {cx, cy} | None}]
    covering [0, end-start] contiguously. A single window means a static crop
    is fine for the whole clip."""
    duration = max(0.1, end - start)
    if frames is None:
        frames = sample_faces_timed(video_path, start, end)
    if not frames:
        return [{"start": 0.0, "end": duration, "face": None}]

    # dominant face per sample = biggest face in that frame
    points = []
    for fr in frames:
        if fr["faces"]:
            f = max(fr["faces"], key=lambda d: d["h"])
            points.append({"t": fr["t"], "cx": f["cx"], "cy": f["cy"]})
        else:
            points.append({"t": fr["t"], "cx": None, "cy": None})

    # fill gaps (no face detected) with the last known position
    last = None
    for p in points:
        if p["cx"] is None:
            if last is not None:
                p["cx"], p["cy"] = last
        else:
            last = (p["cx"], p["cy"])
    # leading gap: fill from the first known position
    first = next(((p["cx"], p["cy"]) for p in points if p["cx"] is not None), None)
    if first is None:
        return [{"start": 0.0, "end": duration, "face": None}]
    for p in points:
        if p["cx"] is None:
            p["cx"], p["cy"] = first

    # segment: cut where cx jumps away from the current window's median
    # and STAYS there (2 consecutive samples) — avoids one-frame noise
    windows = []
    seg = [points[0]]
    for i in range(1, len(points)):
        med = float(np.median([q["cx"] for q in seg]))
        p = points[i]
        moved = abs(p["cx"] - med) > jump
        next_moved = i + 1 < len(points) and abs(points[i + 1]["cx"] - med) > jump
        if moved and (next_moved or i == len(points) - 1):
            windows.append(seg)
            seg = [p]
        else:
            seg.append(p)
    windows.append(seg)

    # to time ranges; boundary = midpoint between neighboring samples. The
    # samples travel with the window because merging needs them: a merge that
    # keeps the old position is how the crop ends up on an empty chair.
    out = []
    for i, seg in enumerate(windows):
        t0 = 0.0 if i == 0 else (windows[i - 1][-1]["t"] + seg[0]["t"]) / 2
        t1 = duration if i == len(windows) - 1 else (seg[-1]["t"] + windows[i + 1][0]["t"]) / 2
        out.append({
            "start": t0, "end": t1, "pts": seg,
            "face": {
                "cx": float(np.median([p["cx"] for p in seg])),
                "cy": float(np.median([p["cy"] for p in seg])),
            },
        })

    def _recentre(window: dict) -> dict:
        window["face"] = {
            "cx": float(np.median([p["cx"] for p in window["pts"]])),
            "cy": float(np.median([p["cy"] for p in window["pts"]])),
        }
        return window

    # Merge windows too short to be worth a move. Absorbing a short window used
    # to extend the previous window's *time* while keeping its *position* — so
    # when the camera cut to the other person for a second, the crop stayed
    # pointing where the first person had been, which by then was a painting on
    # the wall. Merging the samples and taking the median of the combination
    # puts the crop where most of the footage actually is.
    merged = [out[0]]
    for w in out[1:]:
        previous = merged[-1]
        short = w["end"] - w["start"] < min_window or previous["end"] - previous["start"] < min_window
        # Distance is what separates a camera cut from a person shifting in
        # their seat. Merging on length alone deleted real cuts: on a
        # two-camera interview the crop stayed pointing where the last speaker
        # had been, which after the cut was a painting on the wall. A short
        # window far from its neighbour is a cut and has to survive.
        moved = abs(w["face"]["cx"] - previous["face"]["cx"]) > jump
        if short and not moved:
            previous["end"] = w["end"]
            previous["pts"] = previous["pts"] + w["pts"]
            _recentre(previous)
        else:
            merged.append(w)
    return merged


# Below this share of sampled frames containing a usable face, smart cropping
# is guesswork. It was producing clips framed on a lamp and on the back of a
# listener's head — a whole-frame fallback is worse than a good crop and far
# better than a wrong one.
MIN_COVERAGE = 0.25


def face_coverage(video_path: str, start: float, end: float) -> float:
    """Share of sampled frames that contain a face worth cropping to.

    The point of measuring this is to let the caller decline. An automatic
    reframe that cannot see anyone should say so rather than pick a corner of
    the room and commit to it.
    """
    frames = sample_faces_timed(video_path, start, end)
    if not frames:
        return 0.0
    return sum(1 for f in frames if f["faces"]) / len(frames)


def plan_error(frames: list[dict], windows: list[dict], tolerance: float = 0.15) -> float:
    """Share of the clip where the planned crop is not where the face is.

    This measures the defect itself instead of a proxy for it. Cut rate was the
    first attempt and it was the wrong question: footage that averages a cut
    every four seconds sounds calm, and one two-second cut-away inside a long
    window is still two seconds of a clip framed on an empty chair — which is
    exactly what came back from the test render.

    Comparing each sampled face against the crop the plan puts there at that
    moment catches that directly, however the cuts happen to be spaced.
    """
    if not frames or not windows:
        return 0.0

    def planned_cx(t: float):
        for w in windows:
            if w["start"] <= t <= w["end"]:
                return w["face"]["cx"] if w.get("face") else None
        return windows[-1]["face"]["cx"] if windows[-1].get("face") else None

    checked = wrong = 0
    for f in frames:
        if not f["faces"]:
            continue
        actual = max(f["faces"], key=lambda d: d["h"])["cx"]
        target = planned_cx(f["t"])
        if target is None:
            continue
        checked += 1
        if abs(actual - target) > tolerance:
            wrong += 1

    return wrong / checked if checked else 0.0


def worst_miss_seconds(frames: list[dict], windows: list[dict], tolerance: float = 0.15) -> float:
    """The longest unbroken stretch where the crop is not on the speaker.

    The share of a clip that is mis-framed turned out to be the wrong number to
    judge by: one cut-away inside a long window came to six per cent of an
    eighteen-second clip, which sounds negligible and is a full second of
    watching an empty chair. A second is visible. Scattered single frames are
    not.
    """
    if not frames or not windows:
        return 0.0

    def planned_cx(t: float):
        for w in windows:
            if w["start"] <= t <= w["end"]:
                return w["face"]["cx"] if w.get("face") else None
        return windows[-1]["face"]["cx"] if windows[-1].get("face") else None

    times = [f["t"] for f in frames]
    gap = (times[-1] - times[0]) / max(1, len(times) - 1) if len(times) > 1 else 0.0

    worst = run = 0
    for f in frames:
        target = planned_cx(f["t"])
        actual = max(f["faces"], key=lambda d: d["h"])["cx"] if f["faces"] else None
        if actual is not None and target is not None and abs(actual - target) > tolerance:
            run += 1
            worst = max(worst, run)
        else:
            run = 0

    return worst * gap


# A mis-framed stretch longer than this is something a viewer sees.
MAX_MISS_SECONDS = 0.9


# Above this share of the clip mis-framed, following the speaker is doing more
# harm than good and a layout that holds everyone is the better answer.
MAX_PLAN_ERROR = 0.15


MIN_COVERAGE = 0.25


def face_coverage(video_path: str, start: float, end: float) -> float:
    """Share of sampled frames that contain a face worth cropping to.

    The point of measuring this is to let the caller decline. An automatic
    reframe that cannot see anyone should say so rather than pick a corner of
    the room and commit to it.
    """
    frames = sample_faces_timed(video_path, start, end)
    if not frames:
        return 0.0
    return sum(1 for f in frames if f["faces"]) / len(frames)


def is_intercut(frames: list[dict], duration: float, jump: float = 0.15) -> bool:
    """Is this footage cutting between people faster than a crop can follow?

    Measured on the samples rather than on the finished crop plan. The plan is
    the wrong place to look: its whole job is to merge away short windows, so
    footage cutting every 1.5 seconds comes out of it as two or three long
    windows and looks perfectly calm. The samples still show the speaker
    jumping from one side of the frame to the other and back.

    The honest answer for such footage is not a better chase — it is a layout
    that holds both people, which is what split mode is for.
    """
    if duration <= 0 or len(frames) < 4:
        return False

    positions = [
        max(f["faces"], key=lambda d: d["h"])["cx"] if f["faces"] else None for f in frames
    ]
    known = [p for p in positions if p is not None]
    if len(known) < 4:
        return False

    cuts = sum(
        1
        for a, b in zip(known, known[1:])
        if abs(a - b) > jump
    )
    return cuts >= 2 and (duration / cuts) < MIN_SHOT_SECONDS
