"""Fixed-length splitting: the path that needs no AI and no API key.

The planner is shared with the desktop app; the endpoint here is multi-user and
auth-gated, so only the planning logic is pinned in this repo.

The planning is pure arithmetic, so it is tested directly. The one part that
shells out — ffmpeg's silencedetect — is tested by feeding its real log format
through the parser rather than by rendering a video.

    .venv\\Scripts\\python.exe -m pytest tests/test_silence_split.py -q
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import silence  # noqa: E402


class FakeResult:
	def __init__(self, stderr: str):
		self.stderr = stderr
		self.stdout = ""


FFMPEG_LOG = """
[silencedetect @ 000001] silence_start: 12.345
[silencedetect @ 000001] silence_end: 13.001 | silence_duration: 0.656
[silencedetect @ 000001] silence_start: 58.2
[silencedetect @ 000001] silence_end: 59.05 | silence_duration: 0.85
"""


# ── reading ffmpeg ───────────────────────────────────────────────────────────


def test_pauses_are_read_out_of_the_ffmpeg_log(monkeypatch):
	monkeypatch.setattr(silence.proc, "run", lambda *a, **k: FakeResult(FFMPEG_LOG))
	assert silence.detect_silences("video.mp4") == [(12.345, 13.001), (58.2, 59.05)]


def test_a_trailing_silence_with_no_end_is_ignored(monkeypatch):
	"""Silence running to the end of the file has no silence_end line."""
	log = FFMPEG_LOG + "[silencedetect @ 000001] silence_start: 300.0\n"
	monkeypatch.setattr(silence.proc, "run", lambda *a, **k: FakeResult(log))
	assert len(silence.detect_silences("video.mp4")) == 2


def test_ffmpeg_failing_is_not_an_error(monkeypatch):
	"""Without pauses the clips are still cut, just on exact boundaries. A
	missing ffmpeg must degrade the result, never fail the job."""
	def boom(*a, **k):
		raise OSError("ffmpeg not found")

	monkeypatch.setattr(silence.proc, "run", boom)
	assert silence.detect_silences("video.mp4") == []


# ── planning the cuts ────────────────────────────────────────────────────────


def test_a_video_shorter_than_the_target_is_one_clip():
	clips = silence.plan_fixed_clips(40, 60)
	assert len(clips) == 1
	assert clips[0]["start"] == 0.0 and clips[0]["end"] == 40


def test_clips_are_contiguous_and_cover_the_whole_video():
	clips = silence.plan_fixed_clips(300, 60)
	assert clips[0]["start"] == 0.0
	assert clips[-1]["end"] == 300
	for a, b in zip(clips, clips[1:]):
		assert a["end"] == b["start"], "a gap or an overlap between clips"


def test_cuts_move_to_the_middle_of_a_nearby_pause():
	"""The point of the feature: 60 s exactly would land mid-sentence."""
	silences = [(58.0, 59.0)]
	clips = silence.plan_fixed_clips(200, 60, silences)
	assert clips[0]["end"] == pytest.approx(58.5, abs=0.01)


def test_a_pause_too_far_away_is_ignored():
	silences = [(20.0, 21.0)]     # 40 seconds from the 60 s target
	clips = silence.plan_fixed_clips(200, 60, silences)
	assert clips[0]["end"] == 60


def test_the_nearest_pause_wins():
	silences = [(52.0, 52.6), (59.0, 59.6), (64.0, 64.4)]
	clips = silence.plan_fixed_clips(200, 60, silences)
	assert clips[0]["end"] == pytest.approx(59.3, abs=0.01)


def test_the_final_cut_is_the_end_of_the_video_not_a_pause():
	"""Snapping the last cut would leave the tail of the video uncut."""
	silences = [(115.0, 116.0)]
	clips = silence.plan_fixed_clips(120, 60, silences)
	assert clips[-1]["end"] == 120


def test_a_tiny_leftover_is_folded_into_the_previous_clip():
	"""125 s at 60 s would otherwise end with a 5-second clip nobody will post."""
	clips = silence.plan_fixed_clips(125, 60)
	assert clips[-1]["end"] == 125
	assert all(c["end"] - c["start"] >= 10 for c in clips)


def test_no_clip_is_absurdly_short_even_with_pauses_everywhere():
	silences = [(t, t + 0.5) for t in range(1, 200)]
	clips = silence.plan_fixed_clips(200, 60, silences)
	assert all(c["end"] - c["start"] >= 5 for c in clips)


def test_every_offered_length_produces_a_sane_plan():
	for length in silence.LENGTHS:
		clips = silence.plan_fixed_clips(600, length)
		assert clips and clips[-1]["end"] == 600
		assert all(c["end"] > c["start"] for c in clips)


def test_planning_shapes_moments_the_way_the_ui_expects():
	"""These go into the same list the AI's suggestions do, so they need the
	same fields or the results panel renders blanks."""
	clip = silence.plan_fixed_clips(120, 60)[0]
	assert set(clip) >= {"start", "end", "hook_title", "reason"}


def test_a_zero_length_video_plans_nothing():
	assert silence.plan_fixed_clips(0, 60) == []
	assert silence.plan_fixed_clips(120, 0) == []
