"""Transcription on this computer, with no API key and nothing uploaded.

This is the alternative to sending audio to Gemini. It uses faster-whisper,
which is Whisper compiled through CTranslate2 and runs at a usable speed on an
ordinary CPU.

Two deliberate choices:

**The model is not bundled.** Putting even the small model in the executable
would take the download from about 160 MB to well over a gigabyte for everyone,
including the majority who never turn this on. Instead the model is fetched the
first time it is used and cached in %APPDATA%, so the cost falls on the people
who asked for it.

**The import is optional.** If faster-whisper is missing the feature reports
itself unavailable and the Gemini path carries on untouched. A packaging
problem must never take the whole app down with it.
"""
from __future__ import annotations

import os

# Bigger is more accurate and slower. `base` is the useful middle for
# short-form work: it gets names wrong that `small` gets right, but it runs at
# roughly real time on a laptop CPU, and captions are read in chunks of three
# words where a wrong name is survivable.
MODELS = {
	"tiny": {"label": "Tiny — fastest, roughest", "size": "~75 MB"},
	"base": {"label": "Base — recommended", "size": "~145 MB"},
	"small": {"label": "Small — slower, more accurate", "size": "~490 MB"},
	"medium": {"label": "Medium — much slower", "size": "~1.5 GB"},
}
DEFAULT_MODEL = "base"

_model_cache: dict[str, object] = {}


class WhisperUnavailable(Exception):
	"""Raised with a message meant for the person using the app."""


def available() -> bool:
	"""Is local transcription possible in this build?"""
	try:
		import faster_whisper  # noqa: F401
	except Exception:
		return False
	return True


def cache_dir() -> str:
	"""Models live beside the licence, not in the app folder — re-extracting the
	app should not mean downloading a model again."""
	base = os.environ.get("APPDATA") or os.path.expanduser("~")
	path = os.path.join(base, "SoftClipper", "models")
	os.makedirs(path, exist_ok=True)
	return path


def is_downloaded(name: str = DEFAULT_MODEL) -> bool:
	"""Whether the model is already on disk, so the UI can warn before a
	first run silently spends five minutes downloading."""
	root = cache_dir()
	needle = f"models--Systran--faster-whisper-{name}"
	try:
		return any(needle in entry for entry in os.listdir(root))
	except OSError:
		return False


def _load(name: str, job: dict | None = None):
	if name in _model_cache:
		return _model_cache[name]

	try:
		from faster_whisper import WhisperModel
	except Exception as exc:  # noqa: BLE001
		raise WhisperUnavailable(
			"Local transcription is not available in this build. Use Gemini "
			"transcription instead, or reinstall Soft Clipper."
		) from exc

	if job is not None and not is_downloaded(name):
		job["message"] = f"Downloading the {name} speech model, once ({MODELS[name]['size']})..."

	try:
		# int8 on CPU is the difference between usable and unusable here, and
		# the accuracy cost is not audible in captions.
		model = WhisperModel(name, device="cpu", compute_type="int8", download_root=cache_dir())
	except Exception as exc:  # noqa: BLE001
		raise WhisperUnavailable(
			f"Could not load the {name} speech model: {exc}. Check your internet "
			"connection — the model is downloaded once, then kept."
		) from exc

	_model_cache[name] = model
	return model


def transcribe(audio_path: str, model_name: str = DEFAULT_MODEL, job: dict | None = None) -> list[dict]:
	"""Return caption segments in the same shape the rest of the app expects:
	`[{"start", "duration", "text"}]`.

	Nothing about this leaves the machine.
	"""
	if not os.path.exists(audio_path):
		raise WhisperUnavailable("The audio file to transcribe is missing.")

	model = _load(model_name, job)

	if job is not None:
		job["message"] = "Transcribing on this PC (no upload)..."

	segments, _info = model.transcribe(audio_path, vad_filter=True, beam_size=1)

	out: list[dict] = []
	for seg in segments:
		text = (seg.text or "").strip()
		if not text:
			continue
		out.append(
			{
				"start": float(seg.start),
				"duration": max(0.3, float(seg.end) - float(seg.start)),
				"text": text,
			}
		)
		# Streaming progress: transcription is the longest wait in the whole
		# app when it runs locally, and silence for four minutes reads as a hang.
		if job is not None and out and len(out) % 10 == 0:
			job["message"] = f"Transcribing on this PC — {len(out)} lines so far..."

	if not out:
		raise WhisperUnavailable(
			"No speech was found in this video. If it has no talking, use Visual "
			"mode or fixed-length splitting instead."
		)
	return out
