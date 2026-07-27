"""Transcribing on this PC instead of uploading audio to Gemini.

faster-whisper is not installed in this environment, and these tests do not
install it — they check the wiring around it, which is where the risk is: the
feature must be invisible when the library is missing, must not ask for a
Gemini key when it is on, and must produce segments in the exact shape the rest
of the pipeline already consumes.

    .venv\\Scripts\\python.exe -m pytest tests/test_whisper_local.py -q
"""
from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import whisper_local  # noqa: E402


class FakeSegment:
	def __init__(self, start, end, text):
		self.start, self.end, self.text = start, end, text


class FakeModel:
	"""Stands in for faster_whisper.WhisperModel."""

	def __init__(self, *args, **kwargs):
		self.kwargs = kwargs
		FakeModel.last = self

	def transcribe(self, path, **kwargs):
		self.transcribe_kwargs = kwargs
		return (
			[
				FakeSegment(0.0, 2.0, " hello there "),
				FakeSegment(2.0, 2.1, "   "),          # blank, must be dropped
				FakeSegment(2.2, 5.0, "this is the hook"),
			],
			types.SimpleNamespace(language="en"),
		)


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
	monkeypatch.setenv("APPDATA", str(tmp_path))
	whisper_local._model_cache.clear()
	yield
	whisper_local._model_cache.clear()


@pytest.fixture
def installed(monkeypatch):
	"""Pretend faster-whisper is installed."""
	module = types.ModuleType("faster_whisper")
	module.WhisperModel = FakeModel
	monkeypatch.setitem(sys.modules, "faster_whisper", module)
	return module


# ── when the library is missing ──────────────────────────────────────────────


def test_it_reports_itself_unavailable_when_not_installed(monkeypatch):
	monkeypatch.setitem(sys.modules, "faster_whisper", None)
	assert whisper_local.available() is False


def test_transcribing_without_the_library_explains_the_alternative(monkeypatch, tmp_path):
	monkeypatch.setitem(sys.modules, "faster_whisper", None)
	audio = tmp_path / "a.mp3"
	audio.write_bytes(b"x")
	with pytest.raises(whisper_local.WhisperUnavailable) as exc:
		whisper_local.transcribe(str(audio))
	assert "Gemini" in str(exc.value)


# ── the model cache ──────────────────────────────────────────────────────────


def test_models_are_cached_outside_the_app_folder():
	"""Re-extracting the app must not mean downloading a model again."""
	path = whisper_local.cache_dir()
	assert path.endswith(os.path.join("SoftClipper", "models"))
	assert os.path.isdir(path)


def test_a_model_is_reported_missing_before_it_is_downloaded():
	assert whisper_local.is_downloaded("base") is False


def test_a_downloaded_model_is_detected():
	os.makedirs(
		os.path.join(whisper_local.cache_dir(), "models--Systran--faster-whisper-base"),
		exist_ok=True,
	)
	assert whisper_local.is_downloaded("base") is True
	assert whisper_local.is_downloaded("small") is False


def test_every_offered_model_is_described_with_a_size():
	"""A one-line label without a size would have people accidentally starting a
	1.5 GB download on a phone tether."""
	for name, meta in whisper_local.MODELS.items():
		assert meta["label"] and meta["size"]
	assert whisper_local.DEFAULT_MODEL in whisper_local.MODELS


# ── transcribing ─────────────────────────────────────────────────────────────


def test_segments_come_back_in_the_shape_the_pipeline_expects(installed, tmp_path):
	audio = tmp_path / "a.mp3"
	audio.write_bytes(b"x")
	segs = whisper_local.transcribe(str(audio))

	assert [s["text"] for s in segs] == ["hello there", "this is the hook"]
	for seg in segs:
		assert set(seg) == {"start", "duration", "text"}
		assert seg["duration"] > 0


def test_blank_segments_are_dropped(installed, tmp_path):
	audio = tmp_path / "a.mp3"
	audio.write_bytes(b"x")
	assert len(whisper_local.transcribe(str(audio))) == 2


def test_it_runs_on_the_cpu_with_int8(installed, tmp_path):
	"""The difference between usable and unusable on a laptop without a GPU."""
	audio = tmp_path / "a.mp3"
	audio.write_bytes(b"x")
	whisper_local.transcribe(str(audio))
	assert FakeModel.last.kwargs["device"] == "cpu"
	assert FakeModel.last.kwargs["compute_type"] == "int8"
	assert FakeModel.last.kwargs["download_root"] == whisper_local.cache_dir()


def test_the_model_is_loaded_once_and_reused(installed, tmp_path):
	audio = tmp_path / "a.mp3"
	audio.write_bytes(b"x")
	whisper_local.transcribe(str(audio))
	first = FakeModel.last
	whisper_local.transcribe(str(audio))
	assert FakeModel.last is first, "the model was reloaded on the second clip"


def test_the_job_is_told_a_download_is_starting(installed, tmp_path):
	"""Five silent minutes on first use reads as a hang."""
	audio = tmp_path / "a.mp3"
	audio.write_bytes(b"x")
	job = {}
	whisper_local.transcribe(str(audio), job=job)
	assert "message" in job


def test_a_missing_audio_file_is_a_clear_error(tmp_path):
	with pytest.raises(whisper_local.WhisperUnavailable):
		whisper_local.transcribe(str(tmp_path / "nope.mp3"))


def test_a_video_with_no_speech_points_at_the_other_modes(installed, tmp_path, monkeypatch):
	class Silent(FakeModel):
		def transcribe(self, path, **kwargs):
			return ([], types.SimpleNamespace(language="en"))

	installed.WhisperModel = Silent
	audio = tmp_path / "a.mp3"
	audio.write_bytes(b"x")
	with pytest.raises(whisper_local.WhisperUnavailable) as exc:
		whisper_local.transcribe(str(audio))
	message = str(exc.value)
	assert "Visual" in message and "fixed-length" in message
