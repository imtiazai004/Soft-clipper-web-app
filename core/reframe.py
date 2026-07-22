"""Face-aware reframing: find where speakers are so crops keep them in frame.

Uses OpenCV Haar cascade (ships with opencv, no model download needed).
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


def _cascade():
    global _CASCADE
    if _CASCADE is None:
        _CASCADE = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
    return _CASCADE


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
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.equalizeHist(gray)
            faces = _cascade().detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(28, 28))
            sh, sw = frame.shape[:2]
            for (x, y, w, h) in faces:
                detections.append({
                    "cx": (x + w / 2) / sw,
                    "cy": (y + h / 2) / sh,
                    "w": w / sw,
                    "h": h / sh,
                })
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
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.equalizeHist(gray)
            faces = _cascade().detectMultiScale(gray, scaleFactor=1.1, minNeighbors=6, minSize=(28, 28))
            sh, sw = frame.shape[:2]
            frames.append({
                "t": t,
                "faces": [
                    {"cx": (x + w / 2) / sw, "cy": (y + h / 2) / sh, "w": w / sw, "h": h / sh}
                    for (x, y, w, h) in faces
                ],
            })
    return frames


def crop_timeline(video_path: str, start: float, end: float,
                  jump: float = 0.15, min_window: float = 1.5) -> list[dict]:
    """Build a piecewise crop plan so the crop FOLLOWS the speaker across
    scene changes / position jumps.

    Returns [{"start": clip_rel_s, "end": clip_rel_s, "face": {cx, cy} | None}]
    covering [0, end-start] contiguously. A single window means a static crop
    is fine for the whole clip."""
    duration = max(0.1, end - start)
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

    # to time ranges; boundary = midpoint between neighboring samples
    out = []
    for i, seg in enumerate(windows):
        t0 = 0.0 if i == 0 else (windows[i - 1][-1]["t"] + seg[0]["t"]) / 2
        t1 = duration if i == len(windows) - 1 else (seg[-1]["t"] + windows[i + 1][0]["t"]) / 2
        out.append({
            "start": t0, "end": t1,
            "face": {
                "cx": float(np.median([p["cx"] for p in seg])),
                "cy": float(np.median([p["cy"] for p in seg])),
            },
        })

    # merge windows that are too short into the previous one
    merged = [out[0]]
    for w in out[1:]:
        if w["end"] - w["start"] < min_window:
            merged[-1]["end"] = w["end"]
        elif merged[-1]["end"] - merged[-1]["start"] < min_window:
            w["start"] = merged[-1]["start"]
            merged[-1] = w
        else:
            merged.append(w)
    return merged
