"""Choosing where to point the crop.

Every case here came from watching one bad clip: a crop framed on the back of a
listener's head, then on a lamp, then on a painting while the speaker talked
off to the side.

    .venv/Scripts/python.exe -m pytest tests/test_reframe_detection.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import reframe  # noqa: E402


def frames_at(positions, gap=0.5):
	"""Sampled frames from a list of cx values; None means no face found."""
	return [
		{"t": i * gap, "faces": [] if cx is None else [{"cx": cx, "cy": 0.4, "w": 0.1, "h": 0.25}]}
		for i, cx in enumerate(positions)
	]


# ── the detector ─────────────────────────────────────────────────────────────


def test_the_bundled_model_is_present():
	"""Without it the app silently drops back to Haar, which is what put a box
	around a painting in the first place."""
	assert os.path.exists(reframe._YUNET_MODEL)
	assert os.path.getsize(reframe._YUNET_MODEL) > 100_000


def test_tiny_faces_are_ignored():
	"""A face in a painting on the wall is a real face and the wrong subject."""
	assert reframe.MIN_FACE_HEIGHT >= 0.05
	assert reframe.MIN_CONFIDENCE >= 0.5


# ── following the speaker across cuts ────────────────────────────────────────


def test_a_camera_cut_gets_its_own_window(tmp_path):
	"""The bug that produced the bad clip: a short window after a genuine cut was
	absorbed into the previous one, so the crop stayed pointing where the last
	speaker had been — by then an empty chair."""
	frames = frames_at([0.36] * 8 + [0.62] * 3 + [0.36] * 8)
	windows = reframe.crop_timeline("unused", 0.0, 9.5, frames=frames)

	centres = [round(w["face"]["cx"], 2) for w in windows]
	assert 0.62 in centres, f"the cut was merged away: {centres}"


def test_a_person_shifting_in_their_seat_does_not_split_the_window():
	"""The merge exists for a reason — small movement must not become a cut."""
	frames = frames_at([0.40, 0.42, 0.44, 0.43, 0.41, 0.40, 0.42, 0.41])
	windows = reframe.crop_timeline("unused", 0.0, 4.0, frames=frames)
	assert len(windows) == 1


def test_the_plan_matches_where_the_faces_actually_are():
	frames = frames_at([0.36] * 8 + [0.62] * 4 + [0.36] * 8)
	windows = reframe.crop_timeline("unused", 0.0, 10.0, frames=frames)
	assert reframe.plan_error(frames, windows) == 0.0
	assert reframe.worst_miss_seconds(frames, windows) == 0.0


def test_a_wrong_plan_is_measured_as_wrong():
	"""The measurement has to be able to fail, or it is not a measurement."""
	frames = frames_at([0.36] * 6 + [0.62] * 6)
	wrong = [{"start": 0.0, "end": 6.0, "face": {"cx": 0.36, "cy": 0.4}}]
	assert reframe.plan_error(frames, wrong) > 0.4
	assert reframe.worst_miss_seconds(frames, wrong) > 1.0


def test_frames_with_no_face_do_not_count_as_mis_framed():
	frames = frames_at([0.4, None, None, 0.4])
	windows = reframe.crop_timeline("unused", 0.0, 2.0, frames=frames)
	assert reframe.plan_error(frames, windows) == 0.0


# ── declining to guess ───────────────────────────────────────────────────────


def test_coverage_is_the_share_of_frames_with_a_face(monkeypatch):
	monkeypatch.setattr(
		reframe, "sample_faces_timed", lambda *a, **k: frames_at([0.4, None, 0.4, None])
	)
	assert reframe.face_coverage("unused", 0, 4) == 0.5


def test_no_faces_at_all_is_zero_coverage(monkeypatch):
	monkeypatch.setattr(reframe, "sample_faces_timed", lambda *a, **k: frames_at([None] * 6))
	assert reframe.face_coverage("unused", 0, 3) == 0.0


def test_an_unreadable_video_is_zero_coverage(monkeypatch):
	monkeypatch.setattr(reframe, "sample_faces_timed", lambda *a, **k: [])
	assert reframe.face_coverage("unused", 0, 3) == 0.0


def test_the_bar_for_cropping_at_all_is_set():
	"""Below this, the app keeps the whole frame instead of guessing."""
	assert 0.0 < reframe.MIN_COVERAGE <= 0.5
