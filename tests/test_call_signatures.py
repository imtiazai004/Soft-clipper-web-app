"""Does the backend call core/ the way core/ is written?

This file exists because of one bug that reached a user's screen:
`backend/main.py` called `ai.transcribe_audio(..., on_status=...)` while
`core/ai.py` still had `transcribe_audio(audio_path, api_key)`. Nothing caught
it. Python resolves keyword arguments at call time, so both files import, both
pass a linter, and every test that does not actually run a transcription passes.
The failure only appeared on a real video with no YouTube captions — the slow,
expensive path nobody exercises by accident.

It got there because the call site was added in one commit and the parameter was
dropped in a later one that synced `core/ai.py` across from the desktop build.
Neither commit looked wrong on its own, and this repo is the one where that kind
of sync happens — see the note about porting rather than copying.

So this checks the seam directly, by inspecting signatures rather than running
anything. Fast, no key, no network, and it fails the moment either side moves.
"""
from __future__ import annotations

import inspect

from core import ai, broll, projects, transcript, video


def accepts(func, name: str) -> bool:
	"""Whether `func` would accept `name=` as a keyword argument."""
	sig = inspect.signature(func)
	if name in sig.parameters:
		return True
	return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())


# ── the one that broke ────────────────────────────────────────────────────────
def test_transcribe_audio_takes_the_progress_callback():
	"""backend/main.py passes on_status=. It has to exist here."""
	assert accepts(ai.transcribe_audio, "on_status")


def test_the_progress_callback_is_optional():
	default = inspect.signature(ai.transcribe_audio).parameters["on_status"].default
	assert default is None


def test_on_status_is_actually_called(monkeypatch):
	"""A parameter that is accepted and then ignored would pass the check above
	while still leaving the progress bar frozen for minutes."""
	said = []

	class FakeFiles:
		def upload(self, file=None):
			return type("F", (), {"name": "files/x"})()

		def get(self, name=None):
			return type("F", (), {"state": "ACTIVE"})()

		def delete(self, name=None):
			pass

	class FakeClient:
		files = FakeFiles()
		models = type("M", (), {
			"generate_content": staticmethod(
				lambda **kw: type("R", (), {"text": '[{"start":0,"end":1,"text":"hi"}]'})()
			)
		})()

	monkeypatch.setattr(ai, "_client", lambda key: FakeClient())
	out = ai.transcribe_audio("audio.mp3", "key", on_status=said.append)

	assert out == [{"start": 0.0, "duration": 1.0, "text": "hi"}]
	assert said, "on_status was accepted but never called"


# ── this repo's own divergences, which are exactly where the next one will be ─
def test_youtube_captions_take_the_proxy():
	"""Server-only: a datacenter IP gets caption requests blocked too, so this
	parameter is the difference between a fast transcript and a slow one."""
	assert accepts(transcript.fetch_youtube_transcript, "proxy")


def test_project_helpers_take_the_user_root_first():
	"""The port's whole point: no project call may reach for a global folder."""
	for func in (projects.create, projects.load, projects.save, projects.listing,
	             projects.delete, projects.exists, projects.clips_dir, projects.path_for):
		first = next(iter(inspect.signature(func).parameters))
		assert first == "user_root", f"{func.__name__} starts with {first!r}, not user_root"


def test_broll_takes_the_settings_and_folder_rather_than_a_global():
	assert "cfg" in inspect.signature(broll.search).parameters
	assert "cfg" in inspect.signature(broll.key_for).parameters
	for arg in ("dest_dir", "name_hint", "cfg"):
		assert accepts(broll.fetch, arg), f"broll.fetch is missing {arg}"


# ── the rest of the seam ──────────────────────────────────────────────────────
def test_detection_entry_points_take_what_the_backend_sends():
	for func in (ai.detect_viral_moments, ai.detect_viral_moments_visual):
		for arg in ("num_clips", "min_len", "max_len", "user_query"):
			assert accepts(func, arg), f"{func.__name__} is missing {arg}"


def test_reel_planner_takes_what_the_backend_sends():
	for arg in ("mode", "theme", "target_duration", "timestamped_transcript",
	            "proxy_video_path"):
		assert accepts(ai.plan_reel, arg), f"plan_reel is missing {arg}"


def test_render_clip_takes_every_option_the_api_exposes():
	for arg in ("ratio", "ass_file", "reframe", "crop", "effects", "facecam"):
		assert accepts(video.render_clip, arg), f"render_clip is missing {arg}"


def test_caption_builder_takes_the_new_options():
	from core import captions

	for arg in ("style", "words_per_line", "highlight", "headline", "clip_duration",
	            "overlays", "ratio", "position", "overrides"):
		assert accepts(captions.build_ass, arg), arg


def test_transcript_export_helpers_exist_with_the_expected_shape():
	assert accepts(transcript.to_txt, "timestamps")
	assert callable(transcript.to_srt)
